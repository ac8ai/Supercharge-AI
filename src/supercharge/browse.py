"""Filesystem tree walker for .claude/SuperchargeAI/.

Provides functions to walk the SuperchargeAI directory tree, read task and
worker summaries from frontmatter, and build a complete browse response.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from supercharge.paths import _read_frontmatter


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
