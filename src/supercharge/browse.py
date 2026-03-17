"""Filesystem tree walker for .claude/SuperchargeAI/.

Provides functions to walk the SuperchargeAI directory tree, read task and
worker summaries from frontmatter, and build a complete browse response.
Also provides memory browsing and structured task browsing.
"""

from __future__ import annotations

import itertools
from datetime import datetime
from pathlib import Path

from supercharge.paths import _read_frontmatter, _user_config_dir


def _walk_tree(root: Path) -> dict:
    """Recursively walk *root* and return a tree structure.

    Dirs: ``{name, type: "dir", children, mtime}``
    Files: ``{name, type: "file", size, mtime}``

    Hidden entries (starting with ``"."``) are skipped.
    Children are sorted: directories first, then files, each alphabetically.
    """
    stat = root.stat()
    mtime = datetime.fromtimestamp(stat.st_mtime).isoformat()

    dirs: list[dict] = []
    files: list[dict] = []

    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if entry.name.startswith("."):
            continue
        if entry.is_dir() and not entry.is_symlink():
            dirs.append(_walk_tree(entry))
        elif entry.is_file() and not entry.is_symlink():
            st = entry.stat()
            files.append({
                "name": entry.name,
                "type": "file",
                "size": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(),
            })

    return {
        "name": root.name,
        "type": "dir",
        "children": dirs + files,
        "mtime": mtime,
    }


def _read_task_summary(task_dir: Path) -> dict | None:
    """Read task.md frontmatter and return a summary dict.

    Returns ``{task_uuid, agent_type, created_at, created_by, task_name?}``
    or ``None`` if task.md is missing, unreadable, or lacks a ``task_uuid``.
    """
    task_md = task_dir / "task.md"
    fm = _read_frontmatter(task_md)
    if not fm or "task_uuid" not in fm:
        return None

    result: dict = {
        "task_uuid": fm["task_uuid"],
        "agent_type": fm.get("agent_type", ""),
        "created_at": fm.get("created_at", ""),
        "created_by": fm.get("created_by", ""),
    }
    if "task_name" in fm:
        result["task_name"] = fm["task_name"]
    return result


def _read_worker_summary(worker_file: Path) -> dict | None:
    """Read a worker .md frontmatter and return a summary dict.

    Returns ``{worker_id, agent_type, spawned_at, created_by, model?}``
    or ``None`` if the file is missing, unreadable, or lacks ``worker_id``.
    """
    fm = _read_frontmatter(worker_file)
    if not fm or "worker_id" not in fm:
        return None

    result: dict = {
        "worker_id": fm["worker_id"],
        "agent_type": fm.get("agent_type", ""),
        "spawned_at": fm.get("spawned_at", ""),
        "created_by": fm.get("created_by", ""),
    }
    if "model" in fm:
        result["model"] = fm["model"]
    return result


def _supercharge_root_for(project_dir: Path) -> Path:
    """Return the SuperchargeAI root for a given project directory."""
    return project_dir / ".claude" / "SuperchargeAI"


def _build_browse_response(project_dir: Path | None = None) -> dict:
    """Build the full browse response for the SuperchargeAI workspace.

    Response format::

        {
            "root": "/path/to/.claude/SuperchargeAI",
            "tasks": {
                "plan": [{"uuid": "...", "folder": "...", ...}],
                "code": [...],
            },
            "archive": [...],
            "tree": { ... }
        }

    If *project_dir* is ``None``, uses ``paths._supercharge_root()`` to find it.
    """
    if project_dir is None:
        # Import here to avoid circular imports and to use the function
        # that the parallel task is adding to paths.py.
        try:
            from supercharge.paths import _supercharge_root
            sa_root = _supercharge_root()
        except ImportError:
            from supercharge.paths import _project_dir
            sa_root = Path(_project_dir()) / ".claude" / "SuperchargeAI"
    else:
        sa_root = _supercharge_root_for(project_dir)

    result: dict = {
        "root": str(sa_root),
        "tasks": {},
        "archive": [],
        "tree": None,
    }

    if not sa_root.is_dir():
        return result

    # Build tree
    result["tree"] = _walk_tree(sa_root)

    # Scan tasks/<agent_type>/<uuid>/
    tasks_dir = sa_root / "tasks"
    if tasks_dir.is_dir():
        for agent_dir in sorted(tasks_dir.iterdir()):
            if not agent_dir.is_dir() or agent_dir.name.startswith("."):
                continue
            agent_type = agent_dir.name
            entries: list[dict] = []
            for task_folder in sorted(agent_dir.iterdir()):
                if not task_folder.is_dir() or task_folder.name.startswith("."):
                    continue
                summary = _read_task_summary(task_folder)
                if summary:
                    entries.append({
                        "uuid": summary["task_uuid"],
                        "folder": task_folder.name,
                        "agent_type": summary["agent_type"],
                        "created_at": summary.get("created_at", ""),
                        "created_by": summary.get("created_by", ""),
                    })
            if entries:
                result["tasks"][agent_type] = entries

    # Scan archive/
    archive_dir = sa_root / "archive"
    if archive_dir.is_dir():
        for task_folder in sorted(archive_dir.iterdir()):
            if not task_folder.is_dir() or task_folder.name.startswith("."):
                continue
            summary = _read_task_summary(task_folder)
            if summary:
                result["archive"].append({
                    "uuid": summary["task_uuid"],
                    "folder": task_folder.name,
                    "agent_type": summary.get("agent_type", ""),
                    "created_at": summary.get("created_at", ""),
                })

    return result


def _resolve_sa_root(project_dir: Path | None) -> Path:
    """Resolve the SuperchargeAI root directory.

    Shared helper for memory/task browsing functions.
    """
    if project_dir is None:
        try:
            from supercharge.paths import _supercharge_root
            return _supercharge_root()
        except ImportError:
            from supercharge.paths import _project_dir
            return Path(_project_dir()) / ".claude" / "SuperchargeAI"
    return _supercharge_root_for(project_dir)


def _parse_keywords(raw: str) -> list[str]:
    """Parse a keywords string like ``[kw1, kw2, kw3]`` into a list.

    Returns empty list if the format doesn't match.
    """
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1]
        return [k.strip() for k in inner.split(",") if k.strip()]
    return [raw] if raw else []


def _extract_preview(path: Path, max_lines: int = 3) -> str:
    """Extract first *max_lines* non-empty content lines after frontmatter."""
    try:
        with path.open() as f:
            first_line = f.readline()
            if first_line.strip() == "---":
                # Skip frontmatter
                for line in f:
                    if line.strip() == "---":
                        break
                remaining_lines = f
            else:
                # No frontmatter — chain the first line back
                remaining_lines = itertools.chain([first_line], f)
            lines: list[str] = []
            for line in remaining_lines:
                stripped = line.strip()
                if not stripped:
                    continue
                # Skip markdown headings for preview
                if stripped.startswith("#"):
                    continue
                lines.append(stripped)
                if len(lines) >= max_lines:
                    break
            return " ".join(lines)
    except OSError:
        return ""


def _scan_memory_dir(memory_root: Path) -> dict[str, list[dict]]:
    """Scan a memory directory and return entries grouped by category."""
    categories: dict[str, list[dict]] = {}
    if not memory_root.is_dir():
        return categories
    for md_file in sorted(memory_root.rglob("*.md")):
        if not md_file.is_file():
            continue
        rel = md_file.relative_to(memory_root)
        parts = rel.parts
        category = parts[0] if len(parts) > 1 else "root"
        fm = _read_frontmatter(md_file)
        entry: dict = {
            "path": str(rel),
            "title": fm.get("title", md_file.stem),
            "keywords": _parse_keywords(fm.get("keywords", "")),
            "created": fm.get("created", ""),
            "updated": fm.get("updated", ""),
            "preview": _extract_preview(md_file),
        }
        categories.setdefault(category, []).append(entry)
    return categories


def _build_memories_response(project_dir: Path | None = None) -> dict:
    """Build methodology memory listing from user-level config.

    Reads from ``~/.claude/SuperchargeAI/memory/`` (the global methodology
    memory directory). This is used by the Framework tab.

    Returns::

        {"categories": {"behavior": [...], "flows": [...]}}
    """
    memory_root = _user_config_dir() / "SuperchargeAI" / "memory"
    return {"categories": _scan_memory_dir(memory_root)}


def _build_project_memories_response(project_dir: Path | None = None) -> dict:
    """Build project-level memory listing.

    Reads from ``<project>/.claude/SuperchargeAI/memory/`` (specifically
    the ``project/`` subdirectory). Used by the Projects tab.

    Returns::

        {"categories": {"project": [...]}}
    """
    sa_root = _resolve_sa_root(project_dir)
    memory_root = sa_root / "memory"
    categories = _scan_memory_dir(memory_root)
    # Only include project-specific categories (exclude stamps, etc.)
    project_cats = {k: v for k, v in categories.items() if k == "project"}
    return {"categories": project_cats}


def _read_memory_content(rel_path: str, project_dir: Path | None = None, *, source: str = "framework") -> dict | None:
    """Read the full content of a memory file by relative path.

    *source* selects the memory root:
    - ``"framework"``: user-level ``~/.claude/SuperchargeAI/memory/``
    - ``"project"``: project-level ``<project>/.claude/SuperchargeAI/memory/``

    Validates that the path stays within the memory directory to prevent
    directory traversal. Returns ``None`` if the file doesn't exist or
    the path escapes the memory root.

    Returns::

        {"path": "...", "title": "...", "keywords": [...], "content": "..."}
    """
    if source == "framework":
        memory_root = _user_config_dir() / "SuperchargeAI" / "memory"
    else:
        sa_root = _resolve_sa_root(project_dir)
        memory_root = sa_root / "memory"

    # Resolve and validate path stays within memory_root
    target = (memory_root / rel_path).resolve()
    try:
        target.relative_to(memory_root.resolve())
    except ValueError:
        return None

    if not target.is_file():
        return None

    fm = _read_frontmatter(target)

    # Read content after frontmatter
    content_lines: list[str] = []
    try:
        with target.open() as f:
            first_line = f.readline()
            if first_line.strip() == "---":
                # Skip frontmatter block
                for line in f:
                    if line.strip() == "---":
                        break
                content_lines = f.readlines()
            else:
                # No frontmatter — include the first line
                content_lines = [first_line] + f.readlines()
    except OSError:
        return None

    return {
        "path": rel_path,
        "title": fm.get("title", Path(rel_path).stem),
        "keywords": _parse_keywords(fm.get("keywords", "")),
        "content": "".join(content_lines).strip(),
    }


def _extract_task_title(task_md: Path) -> str:
    """Extract task title from the first line after ``# Task`` heading."""
    try:
        with task_md.open() as f:
            found_heading = False
            for line in f:
                stripped = line.strip()
                if stripped == "# Task":
                    found_heading = True
                    continue
                if found_heading:
                    if not stripped:
                        continue
                    return stripped
        return ""
    except OSError:
        return ""


def _build_tasks_response(project_dir: Path | None = None) -> dict:
    """Build a structured task listing from active tasks and archive.

    Returns::

        {
            "active": {"plan": [...], "code": [...]},
            "archived": [...]
        }

    Each entry has ``uuid``, ``agent_type``, ``created_at``, ``title``,
    ``status`` (``"completed"`` or ``"pending"``), and ``has_result``.
    """
    sa_root = _resolve_sa_root(project_dir)
    active: dict[str, list[dict]] = {}
    archived: list[dict] = []

    # Scan active tasks
    tasks_dir = sa_root / "tasks"
    if tasks_dir.is_dir():
        for agent_dir in sorted(tasks_dir.iterdir()):
            if not agent_dir.is_dir() or agent_dir.name.startswith("."):
                continue
            agent_type = agent_dir.name
            entries: list[dict] = []
            for task_folder in sorted(agent_dir.iterdir()):
                if not task_folder.is_dir() or task_folder.name.startswith("."):
                    continue
                summary = _read_task_summary(task_folder)
                if not summary:
                    continue
                has_result = (task_folder / "result.md").is_file()
                entries.append({
                    "uuid": summary["task_uuid"],
                    "agent_type": summary.get("agent_type", agent_type),
                    "created_at": summary.get("created_at", ""),
                    "title": _extract_task_title(task_folder / "task.md"),
                    "status": "completed" if has_result else "pending",
                    "has_result": has_result,
                })
            if entries:
                active[agent_type] = entries

    # Scan archive
    archive_dir = sa_root / "archive"
    if archive_dir.is_dir():
        for task_folder in sorted(archive_dir.iterdir()):
            if not task_folder.is_dir() or task_folder.name.startswith("."):
                continue
            summary = _read_task_summary(task_folder)
            if not summary:
                continue
            has_result = (task_folder / "result.md").is_file()
            archived.append({
                "uuid": summary["task_uuid"],
                "agent_type": summary.get("agent_type", ""),
                "created_at": summary.get("created_at", ""),
                "title": _extract_task_title(task_folder / "task.md"),
                "status": "completed" if has_result else "pending",
                "has_result": has_result,
            })

    return {"active": active, "archived": archived}
