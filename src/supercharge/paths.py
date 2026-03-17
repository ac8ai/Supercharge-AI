"""Path resolution and file helpers for SuperchargeAI."""

from __future__ import annotations

import os
import re
from pathlib import Path

_FULL_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_HEX8_RE = re.compile(r"^[0-9a-f]{8,}$")


class AmbiguousPrefixError(Exception):
    """Raised when a short prefix matches multiple task/worker folders."""

    def __init__(self, prefix: str, matches: list[str]) -> None:
        self.prefix = prefix
        self.matches = matches
        super().__init__(
            f"Prefix {prefix!r} is ambiguous — matches {len(matches)} entries: "
            + ", ".join(matches[:5])
        )

_ENV_PROJECT_DIR = "CLAUDE_PROJECT_DIR"
_SUPERCHARGE_WORKSPACE_MARKER = "/.claude/SuperchargeAI/"


def _user_config_dir() -> Path:
    """Return user-level Claude config dir, respecting CLAUDE_CONFIG_DIR.

    CLAUDE_CONFIG_DIR is a user-set env var that controls where Claude Code
    stores user-level config and state files (credentials, projects, settings).
    It does NOT affect the project-level .claude/ directory.

    If not set or empty, defaults to ~/.claude (standard location).
    """
    val = os.environ.get("CLAUDE_CONFIG_DIR", "")
    if val:
        return Path(val)
    return Path.home() / ".claude"


def _hook_data_dir() -> Path:
    """Return data directory for hook execution (prompts/).

    Hooks run with CLAUDE_PLUGIN_ROOT set by Claude Code, so the plugin
    directory is the primary source. Falls back to SUPERCHARGE_ROOT, then
    the plugin cache.
    """
    for var in ("CLAUDE_PLUGIN_ROOT", "SUPERCHARGE_ROOT"):
        val = os.environ.get(var)
        if val:
            return Path(val)

    plugins_cache = _user_config_dir() / "plugins" / "cache"
    if plugins_cache.is_dir():
        for marketplace_dir in plugins_cache.iterdir():
            sa_dir = marketplace_dir / "supercharge-ai"
            if sa_dir.is_dir():
                for version_dir in sorted(sa_dir.iterdir(), reverse=True):
                    if (version_dir / "prompts").is_dir():
                        return version_dir

    # Last resort: fall through to CLI resolution
    return _cli_data_dir()


def _cli_data_dir() -> Path:
    """Return data directory for CLI commands (prompts/ and templates/).

    CLI commands (task init, subtask init) run in Bash where
    CLAUDE_PLUGIN_ROOT is NOT reliably available. The installed package
    data is the primary source.
    """
    val = os.environ.get("SUPERCHARGE_ROOT")
    if val:
        return Path(val)

    pkg_data = Path(__file__).resolve().parent / "data"
    if (pkg_data / "prompts").is_dir():
        return pkg_data

    dev_root = Path(__file__).resolve().parents[2]
    if (dev_root / "prompts").is_dir():
        return dev_root

    return pkg_data


def _user_methodology_dir() -> Path:
    """Return user-level methodology memory dir: <config>/SuperchargeAI/memory/methodology/."""
    return _user_config_dir() / "SuperchargeAI" / "memory" / "methodology"


def _project_memory_dir(project_dir: str) -> Path:
    """Return project-level memory dir: <project>/.claude/SuperchargeAI/memory/."""
    return Path(project_dir) / ".claude" / "SuperchargeAI" / "memory"


def _project_dir() -> str:
    """Resolve the project root directory.

    Priority: CLAUDE_PROJECT_DIR env -> git toplevel -> cwd.
    """
    project = os.environ.get(_ENV_PROJECT_DIR)
    if project:
        return project
    import subprocess

    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return os.getcwd()


def _task_root() -> Path:
    """Runtime task data lives in <project>/.claude/SuperchargeAI/tasks/."""
    return Path(_project_dir()) / ".claude" / "SuperchargeAI" / "tasks"


def _archive_root() -> Path:
    """Archive directory: <project>/.claude/SuperchargeAI/archive/."""
    return Path(_project_dir()) / ".claude" / "SuperchargeAI" / "archive"


def _copy_template(name: str, dest: Path) -> None:
    """Copy a template file from templates/ to dest."""
    src = _cli_data_dir() / "templates" / name
    if src.exists():
        dest.write_text(src.read_text())
    else:
        dest.touch()


def _resolve_prefix(prefix: str) -> tuple[str, str] | None:
    """Resolve a task prefix to ``(full_uuid, folder_name)``.

    Accepts:
    - A full 36-char UUID (fast exact lookup).
    - An exact folder name like ``5b6d9c66-implement-auth`` (exact match).
    - An 8+ hex-char prefix (scans folders whose name starts with *prefix*).

    Returns ``None`` when no match is found.

    Raises:
        ValueError: if *prefix* is a hex string shorter than 8 characters.
        AmbiguousPrefixError: if *prefix* matches more than one folder.
    """
    root = _task_root()
    if not root.exists():
        return None

    # ── Fast path: full UUID ──────────────────────────────────────────
    if _FULL_UUID_RE.match(prefix):
        for agent_dir in root.iterdir():
            if not agent_dir.is_dir():
                continue
            candidate = agent_dir / prefix
            if candidate.is_dir():
                return (prefix, prefix)
        # Full UUID didn't match a folder name directly.  The task may live
        # in a short-named folder (e.g. "5b6d9c66-implement-auth") whose
        # frontmatter stores this full UUID.  Extract the 8-char prefix and
        # fall through to the prefix search instead of returning None.
        prefix = prefix[:8]

    # ── Exact folder name match (e.g. "5b6d9c66-implement-auth") ─────
    for agent_dir in root.iterdir():
        if not agent_dir.is_dir():
            continue
        candidate = agent_dir / prefix
        if candidate.is_dir():
            fm = _read_frontmatter(candidate / "task.md")
            full_uuid = fm.get("task_uuid", prefix)
            return (full_uuid, prefix)

    # ── Prefix search (8+ hex chars) ─────────────────────────────────
    if not _HEX8_RE.match(prefix):
        # Not pure hex or too short
        if re.fullmatch(r"[0-9a-f]+", prefix) and len(prefix) < 8:
            raise ValueError(
                f"Prefix must be at least 8 hex characters, got {len(prefix)}: {prefix!r}"
            )
        # Non-hex string that didn't match as exact folder name
        return None

    matches: list[tuple[str, str]] = []  # (full_uuid, folder_name)
    for agent_dir in root.iterdir():
        if not agent_dir.is_dir():
            continue
        for folder in agent_dir.iterdir():
            if not folder.is_dir():
                continue
            if folder.name.startswith(prefix):
                fm = _read_frontmatter(folder / "task.md")
                full_uuid = fm.get("task_uuid", folder.name)
                matches.append((full_uuid, folder.name))

    if len(matches) == 0:
        return None
    if len(matches) == 1:
        return matches[0]
    raise AmbiguousPrefixError(prefix, [m[1] for m in matches])


def _find_task_dir(task_uuid: str) -> Path | None:
    """Search for a task by UUID, prefix, or folder name across all agent types.

    Returns the task directory ``Path`` on success, ``None`` if not found.

    Raises:
        AmbiguousPrefixError: if a short prefix matches multiple folders.
    """
    root = _task_root()
    if not root.exists():
        return None

    result = _resolve_prefix(task_uuid)
    if result is None:
        return None

    _full_uuid, folder_name = result
    # Find the folder in agent dirs
    for agent_dir in root.iterdir():
        if not agent_dir.is_dir():
            continue
        candidate = agent_dir / folder_name
        if candidate.is_dir():
            return candidate
    return None


def _read_prompt(name: str, data_dir: Path) -> str:
    """Read a prompt file, return empty string if missing."""
    path = data_dir / "prompts" / name
    return path.read_text() if path.exists() else ""


def _read_frontmatter(path: Path) -> dict[str, str]:
    """Read YAML frontmatter from a markdown file, stopping at the closing ---.

    Returns a dict of key-value pairs. Reads line-by-line until the closing
    ``---`` marker (or EOF). Returns empty dict if no frontmatter found.
    """
    try:
        with path.open() as f:
            if f.readline().strip() != "---":
                return {}
            result = {}
            for line in f:
                if line.strip() == "---":
                    break
                if ":" in line:
                    key, _, val = line.partition(":")
                    result[key.strip()] = val.strip()
        return result
    except OSError:
        return {}
