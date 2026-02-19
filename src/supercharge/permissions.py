"""Agent permissions and user settings management for SuperchargeAI."""

from __future__ import annotations

import json
import os
from pathlib import Path

from supercharge.paths import _task_root

_DEFAULT_MAX_RECURSION_DEPTH = 5
_ENV_MAX_DEPTH = "SUPERCHARGE_MAX_RECURSION_DEPTH"
_ENV_REMAINING = "SUPERCHARGE_RECURSION_REMAINING"
_ENV_TASK_UUID = "SUPERCHARGE_TASK_UUID"

_DEFAULT_FAST_MODELS: set[str] = {"haiku"}
_ENV_FAST_MODELS = "SUPERCHARGE_FAST_MODELS"

_SUPERCHARGE_PERMISSIONS = [
    "Bash(supercharge *)",
    "Write(.claude/SuperchargeAI/**)",
    "Edit(.claude/SuperchargeAI/**)",
    "WebSearch",
    "WebFetch",
]

# ── Per-agent tool permissions ──────────────────────────────────────────────
# deep_tools: allowed_tools for deep workers (opus/sonnet)
# fast_tools: allowed_tools for fast workers (haiku) — always read-only
# write_scope: where deep workers may Write/Edit
#   "project" = anywhere in project root
#   "memory"  = memory dir + context file
#   "context" = context file only

_AGENT_PERMISSIONS: dict[str, dict] = {
    "code": {
        "deep_tools": ["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        "fast_tools": ["Read", "Glob", "Grep"],
        "write_scope": "project",
    },
    "plan": {
        "deep_tools": ["Read", "Write", "Glob", "Grep"],
        "fast_tools": ["Read", "Glob", "Grep"],
        "write_scope": "context",
    },
    "review": {
        "deep_tools": ["Read", "Write", "Bash", "Glob", "Grep"],
        "fast_tools": ["Read", "Glob", "Grep"],
        "write_scope": "context",
    },
    "document": {
        "deep_tools": ["Read", "Write", "Edit", "Glob", "Grep"],
        "fast_tools": ["Read", "Glob", "Grep"],
        "write_scope": "project",
    },
    "research": {
        "deep_tools": ["Read", "Write", "Glob", "Grep", "WebSearch", "WebFetch"],
        "fast_tools": ["Read", "Glob", "Grep", "WebSearch", "WebFetch"],
        "write_scope": "context",
    },
    "consistency": {
        "deep_tools": ["Read", "Write", "Glob", "Grep"],
        "fast_tools": ["Read", "Glob", "Grep"],
        "write_scope": "context",
    },
    "memory": {
        "deep_tools": ["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        "fast_tools": ["Read", "Glob", "Grep"],
        "write_scope": "memory",
    },
}

_DEFAULT_PERMS = _AGENT_PERMISSIONS["code"]


def _make_can_use_tool(
    agent_type: str,
    task_dir: Path,
    worker_id: str,
    project_root: str,
):
    """Create a can_use_tool callback scoped to agent type and worker."""
    from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

    perms = _AGENT_PERMISSIONS.get(agent_type, _DEFAULT_PERMS)
    write_scope = perms["write_scope"]

    worker_file = str(task_dir / "workers" / f"{worker_id}.md")
    worker_subdir = str(task_dir / "workers" / worker_id) + "/"
    memory_dir = str(Path(project_root) / ".claude" / "SuperchargeAI" / "memory") + "/"

    async def _can_use_tool(tool_name, input_data, context):
        # Block task creation (only orchestrator creates tasks)
        if tool_name == "Bash":
            cmd = input_data.get("command", "")
            if "supercharge task init" in cmd:
                return PermissionResultDeny(
                    message="Only the orchestrator creates task workspaces.",
                )

        # Scope Write/Edit by agent type
        if tool_name in ("Write", "Edit"):
            file_path = input_data.get("file_path", "")

            if write_scope == "project":
                if project_root and not file_path.startswith(project_root):
                    return PermissionResultDeny(
                        message=f"Write restricted to project: {project_root}",
                    )
            elif write_scope == "memory":
                if not (
                    file_path.startswith(memory_dir)
                    or file_path == worker_file
                    or file_path.startswith(worker_subdir)
                ):
                    return PermissionResultDeny(
                        message="Write restricted to memory dir and context file.",
                    )
            else:  # "context"
                if not (file_path == worker_file or file_path.startswith(worker_subdir)):
                    return PermissionResultDeny(
                        message="Write restricted to context file.",
                    )

        return PermissionResultAllow()

    return _can_use_tool


def _get_fast_models() -> set[str]:
    """Return the set of model names that use fast (fire-and-forget) mode.

    If SUPERCHARGE_FAST_MODELS is set, it replaces the default entirely.
    Comma-separated, case-insensitive. Empty string = no fast models.
    """
    env_val = os.environ.get(_ENV_FAST_MODELS)
    if env_val is not None:
        return {m.strip().lower() for m in env_val.split(",") if m.strip()}
    return _DEFAULT_FAST_MODELS


def _get_remaining_depth() -> int:
    """Return remaining recursion budget for worker spawning.

    Heuristic:
    1. _ENV_REMAINING is set -> we're inside a worker, use it.
    2. Not set -> orchestrator level:
       a) _ENV_MAX_DEPTH is set (via settings.json env) -> use it.
       b) Fall back to _DEFAULT_MAX_RECURSION_DEPTH.
    """
    remaining = os.environ.get(_ENV_REMAINING)
    if remaining is not None:
        return int(remaining)
    max_depth = os.environ.get(_ENV_MAX_DEPTH)
    if max_depth is not None:
        return int(max_depth)
    return _DEFAULT_MAX_RECURSION_DEPTH


def _is_fast_mode(model: str | None) -> bool:
    """Check if the model should use fast (fire-and-forget) mode."""
    if model is None:
        return False
    fast_models = _get_fast_models()
    return any(fast in model.lower() for fast in fast_models)


def _find_worker_file(worker_id: str) -> Path | None:
    """Search for a worker file across all task directories."""
    root = _task_root()
    if not root.exists():
        return None
    for agent_dir in root.iterdir():
        if not agent_dir.is_dir():
            continue
        for task_dir in agent_dir.iterdir():
            if not task_dir.is_dir():
                continue
            candidate = task_dir / "workers" / f"{worker_id}.md"
            if candidate.exists():
                return candidate
    return None


def _user_settings_path() -> Path:
    """Return path to ~/.claude/settings.json."""
    return Path.home() / ".claude" / "settings.json"


def _add_user_permissions(settings_path: Path) -> list[str]:
    """Add SuperchargeAI permissions to settings.json. Returns list of newly added entries."""
    if settings_path.exists():
        settings = json.loads(settings_path.read_text())
    else:
        settings = {}

    perms = settings.setdefault("permissions", {})
    allow = perms.setdefault("allow", [])

    added = []
    for entry in _SUPERCHARGE_PERMISSIONS:
        if entry not in allow:
            allow.append(entry)
            added.append(entry)

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    return added


def _remove_user_permissions(settings_path: Path) -> int:
    """Remove SuperchargeAI permissions from settings.json. Returns count removed."""
    if not settings_path.exists():
        return 0

    settings = json.loads(settings_path.read_text())
    perms = settings.get("permissions", {})
    allow = perms.get("allow", [])

    original_len = len(allow)
    allow = [entry for entry in allow if entry not in _SUPERCHARGE_PERMISSIONS]
    removed = original_len - len(allow)

    if removed > 0:
        perms["allow"] = allow
        settings["permissions"] = perms
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")

    return removed
