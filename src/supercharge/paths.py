"""Path resolution and file helpers for SuperchargeAI."""

from __future__ import annotations

import os
from pathlib import Path

_ENV_PROJECT_DIR = "CLAUDE_PROJECT_DIR"
_SUPERCHARGE_WORKSPACE_MARKER = "/.claude/SuperchargeAI/"


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

    plugins_cache = Path.home() / ".claude" / "plugins" / "cache"
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
    """Runtime task data lives in <project>/.claude/SuperchargeAI/."""
    return Path(_project_dir()) / ".claude" / "SuperchargeAI"


def _copy_template(name: str, dest: Path) -> None:
    """Copy a template file from templates/ to dest."""
    src = _cli_data_dir() / "templates" / name
    if src.exists():
        dest.write_text(src.read_text())
    else:
        dest.touch()


def _find_task_dir(task_uuid: str) -> Path | None:
    """Search for a task UUID across all agent types."""
    root = _task_root()
    if not root.exists():
        return None
    for agent_dir in root.iterdir():
        if not agent_dir.is_dir():
            continue
        candidate = agent_dir / task_uuid
        if candidate.is_dir():
            return candidate
    return None


def _read_prompt(name: str, data_dir: Path) -> str:
    """Read a prompt file, return empty string if missing."""
    path = data_dir / "prompts" / name
    return path.read_text() if path.exists() else ""
