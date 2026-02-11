#!/usr/bin/env python3
"""SuperchargeAI CLI."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeAgentOptions


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

    Priority: CLAUDE_PROJECT_DIR env → git toplevel → cwd.
    """
    project = os.environ.get(_ENV_PROJECT_DIR)
    if project:
        return project
    import subprocess

    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            text=True, stderr=subprocess.DEVNULL,
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


_DEFAULT_MAX_RECURSION_DEPTH = 5
_ENV_MAX_DEPTH = "SUPERCHARGE_MAX_RECURSION_DEPTH"
_ENV_REMAINING = "SUPERCHARGE_RECURSION_REMAINING"
_ENV_TASK_UUID = "SUPERCHARGE_TASK_UUID"
_ENV_PROJECT_DIR = "CLAUDE_PROJECT_DIR"

_DEFAULT_FAST_MODELS = {"haiku"}
_ENV_FAST_MODELS = "SUPERCHARGE_FAST_MODELS"

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
        "deep_tools": ["Read", "Write", "Glob", "Grep"],
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
                if not (
                    file_path == worker_file
                    or file_path.startswith(worker_subdir)
                ):
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
    1. _ENV_REMAINING is set → we're inside a worker, use it.
    2. Not set → orchestrator level:
       a) _ENV_MAX_DEPTH is set (via settings.json env) → use it.
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


# ── Main group ───────────────────────────────────────────────────────────────


@click.group()
def supercharge():
    """SuperchargeAI - multi-agent framework for Claude Code."""


@supercharge.command("version")
def version_cmd():
    """Print installed version."""
    from supercharge import __version__

    click.echo(__version__)


# ── init / deinit ────────────────────────────────────────────────────────────

_SUPERCHARGE_MARKER = "supercharge-ai"


def _find_claude_md(project_dir: str | None = None) -> Path:
    """Locate CLAUDE.md in the project's .claude/ directory."""
    root = Path(project_dir) if project_dir else Path.cwd()
    return root / ".claude" / "CLAUDE.md"


@supercharge.command()
@click.option("--project-dir", default=None, help="Project root (default: cwd)")
def init(project_dir: str | None):
    """Add SuperchargeAI include line to the project's CLAUDE.md."""
    claude_md = _find_claude_md(project_dir)

    if claude_md.exists() and _SUPERCHARGE_MARKER in claude_md.read_text():
        click.echo("Already configured — CLAUDE.md contains supercharge-ai reference.")
        return

    # Resolve the absolute path to claude-md.md template
    data_dir = _cli_data_dir()
    template_path = data_dir / "prompts" / "claude-md.md"
    if not template_path.exists():
        raise click.ClickException(f"Template not found: {template_path}")

    include_line = f"\nSupercharge-AI: @{template_path}\n"

    claude_md.parent.mkdir(parents=True, exist_ok=True)
    with claude_md.open("a") as f:
        f.write(include_line)

    click.echo(f"Added to {claude_md}:")
    click.echo(f"  Supercharge-AI: @{template_path}")


@supercharge.command()
@click.option("--project-dir", default=None, help="Project root (default: cwd)")
def deinit(project_dir: str | None):
    """Remove SuperchargeAI include line from the project's CLAUDE.md."""
    claude_md = _find_claude_md(project_dir)

    if not claude_md.exists():
        click.echo("No CLAUDE.md found.")
        return

    lines = claude_md.read_text().splitlines(keepends=True)
    filtered = [line for line in lines if _SUPERCHARGE_MARKER not in line]

    if len(filtered) == len(lines):
        click.echo("No supercharge-ai reference found in CLAUDE.md.")
        return

    claude_md.write_text("".join(filtered))
    click.echo(f"Removed supercharge-ai reference from {claude_md}.")


# ── task ─────────────────────────────────────────────────────────────────────


@supercharge.group()
def task():
    """Manage task workspaces for native subagents."""


@task.command("init")
@click.argument("agent_type")
def task_init(agent_type: str):
    """Create a new task workspace. Prints the UUID."""
    task_id = str(uuid.uuid4())
    task_dir = _task_root() / agent_type / task_id

    task_dir.mkdir(parents=True, exist_ok=True)

    _copy_template("task.md", task_dir / "task.md")
    _copy_template("result.md", task_dir / "result.md")
    _copy_template("notes.md", task_dir / "notes.md")

    click.echo(task_id)


# ── subtask ──────────────────────────────────────────────────────────────────


def _build_worker_system_prompt() -> str:
    """Compose the system prompt for Agent SDK workers."""
    cli_dir = _cli_data_dir()
    protocol = _read_prompt("protocol.md", cli_dir)
    worker_role = _read_prompt("worker.md", cli_dir)
    parts = [p for p in (protocol, worker_role) if p]
    return f"<supercharge-ai>\n{''.join(parts)}\n</supercharge-ai>"


def _build_deep_worker_prompt(
    task_dir: Path,
    agent_type: str,
    worker_file: Path,
    prompt: str,
    remaining_depth: int,
) -> str:
    """Compose the initial prompt sent to a deep worker."""
    task_uuid = task_dir.name
    budget = remaining_depth - 1
    if budget > 0:
        depth_note = (
            f"Recursion budget: {budget} levels remaining. "
            f"To spawn sub-workers: "
            f'`supercharge subtask init <agent_type> "<prompt>" --model <model>` '
            f"(SUPERCHARGE_TASK_UUID is auto-set in your env)"
        )
    else:
        depth_note = (
            "Recursion budget: 0. You cannot spawn sub-workers."
        )
    return (
        f"You are a **deep** worker assisting a `{agent_type}` agent.\n"
        f"Task workspace: {task_dir}/\n"
        f"Your context file: {worker_file}\n"
        f"{depth_note}\n"
        f"Read task.md for full requirements.\n\n"
        f"Your assignment: {prompt}"
    )


def _build_fast_worker_prompt(
    task_dir: Path,
    agent_type: str,
    prompt: str,
) -> str:
    """Compose the initial prompt sent to a fast worker."""
    return (
        f"You are a **fast** worker assisting a `{agent_type}` agent.\n"
        f"Task workspace: {task_dir}/\n"
        f"Recursion budget: 0. You cannot spawn sub-workers.\n"
        f"No context file — return the result directly.\n"
        f"Read task.md for full requirements.\n\n"
        f"Your assignment: {prompt}"
    )


def _prepare_worker_file(
    task_dir: Path, worker_id: str, prompt: str,
) -> Path:
    """Create the worker context file from template and fill in assignment."""
    workers_dir = task_dir / "workers"
    workers_dir.mkdir(exist_ok=True)
    worker_file = workers_dir / f"{worker_id}.md"
    _copy_template("worker.md", worker_file)
    content = worker_file.read_text()
    content = content.replace(
        "## Assignment\n", f"## Assignment\n\n{prompt}\n", 1,
    )
    worker_file.write_text(content)
    return worker_file


def _build_options(
    task_dir: Path,
    remaining_depth: int,
    max_turns: int | None,
    model: str | None,
    agent_type: str,
    worker_id: str | None = None,
) -> "ClaudeAgentOptions":
    """Build ClaudeAgentOptions for workers.

    Deep workers (worker_id set): get can_use_tool callback for path scoping.
    Fast workers (worker_id None): get allowed_tools only (no callback).
    """
    from claude_agent_sdk import ClaudeAgentOptions

    project_root = os.environ.get(_ENV_PROJECT_DIR) or _project_dir()
    perms = _AGENT_PERMISSIONS.get(agent_type, _DEFAULT_PERMS)

    if worker_id is not None:
        tools = perms["deep_tools"]
        can_use_tool_cb = _make_can_use_tool(
            agent_type, task_dir, worker_id, project_root,
        )
    else:
        tools = perms["fast_tools"]
        can_use_tool_cb = None

    return ClaudeAgentOptions(
        system_prompt=_build_worker_system_prompt(),
        cwd=str(task_dir),
        add_dirs=[project_root] if project_root else [],
        allowed_tools=tools,
        can_use_tool=can_use_tool_cb,
        permission_mode="acceptEdits",
        max_turns=max_turns,
        model=model,
        env={
            _ENV_REMAINING: str(remaining_depth - 1),
            _ENV_TASK_UUID: task_dir.name,
            _ENV_PROJECT_DIR: project_root,
        },
    )


# ── Deep worker (opus/sonnet): ClaudeSDKClient with session_id=worker_id ──


async def _deep_worker_init(
    task_dir: Path,
    agent_type: str,
    prompt: str,
    worker_id: str,
    worker_file: Path,
    remaining_depth: int,
    max_turns: int | None,
    model: str | None,
) -> dict:
    """Spawn a deep worker using ClaudeSDKClient. worker_id = session_id."""
    from claude_agent_sdk import ClaudeSDKClient, ResultMessage

    options = _build_options(
        task_dir, remaining_depth, max_turns, model,
        agent_type=agent_type, worker_id=worker_id,
    )
    client = ClaudeSDKClient(options=options)

    result_msg = None
    try:
        await client.connect()
        await client.query(
            _build_deep_worker_prompt(
                task_dir, agent_type, worker_file, prompt,
                remaining_depth,
            ),
            session_id=worker_id,
        )
        async for message in client.receive_response():
            if isinstance(message, ResultMessage):
                result_msg = message
    finally:
        await client.disconnect()

    if not result_msg:
        raise click.ClickException("No result returned from worker")

    if result_msg.is_error:
        return {"worker_id": worker_id, "error": result_msg.result}

    return {"worker_id": worker_id, "result": result_msg.result}


async def _deep_worker_resume(
    worker_id: str,
    prompt: str,
    task_dir: Path,
    agent_type: str,
) -> dict:
    """Resume a deep worker with full options restored."""
    from claude_agent_sdk import ResultMessage, query

    remaining = _get_remaining_depth()
    options = _build_options(
        task_dir, remaining, max_turns=None, model=None,
        agent_type=agent_type, worker_id=worker_id,
    )
    options.resume = worker_id

    result_msg = None
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage):
            result_msg = message

    if not result_msg:
        raise click.ClickException("No result returned from worker")

    if result_msg.is_error:
        return {"worker_id": worker_id, "error": result_msg.result}

    return {"worker_id": worker_id, "result": result_msg.result}


# ── Fast worker (haiku): fire-and-forget via module-level query() ──────────


async def _fast_worker_init(
    task_dir: Path,
    agent_type: str,
    prompt: str,
    worker_id: str,
    max_turns: int | None,
    model: str | None,
) -> dict:
    """Spawn a fast worker. No context file, no resume, no recursion."""
    from claude_agent_sdk import ResultMessage, query

    options = _build_options(
        task_dir, remaining_depth=1, max_turns=max_turns, model=model,
        agent_type=agent_type,
    )

    result_msg = None
    async for message in query(
        prompt=_build_fast_worker_prompt(task_dir, agent_type, prompt),
        options=options,
    ):
        if isinstance(message, ResultMessage):
            result_msg = message

    if not result_msg:
        raise click.ClickException("No result returned from worker")

    if result_msg.is_error:
        return {"worker_id": worker_id, "error": result_msg.result}

    return {"worker_id": worker_id, "result": result_msg.result}


# ── CLI commands ─────────────────────────────────────────────────────────────


@supercharge.group()
def subtask():
    """Manage Agent SDK workers within a task."""


@subtask.command("init")
@click.argument("agent_type")
@click.argument("prompt")
@click.option(
    "--task-uuid", envvar=_ENV_TASK_UUID, default=None,
    help="Parent task UUID (agents pass this; workers get it from env).",
)
# @click.option("--max-turns", ...)  # now read from env
@click.option(
    "--model", default=None, help="Model override (sonnet, opus, haiku)",
)
def subtask_init(
    agent_type: str,
    prompt: str,
    task_uuid: str | None,
    # max_turns: read from SUPERCHARGE_MAX_TURNS env var
    model: str | None,
):
    """Spawn a new Agent SDK worker on a task. Prints JSON {worker_id, result}."""
    # Resolve task UUID: --task-uuid flag > SUPERCHARGE_TASK_UUID env var
    # Click's envvar= already handles the fallback, so task_uuid may come
    # from either source. Validate consistency if both are present.
    env_uuid = os.environ.get(_ENV_TASK_UUID)
    if task_uuid and env_uuid and task_uuid != env_uuid:
        raise click.ClickException(
            f"--task-uuid ({task_uuid}) conflicts with "
            f"{_ENV_TASK_UUID} env var ({env_uuid})."
        )
    if not task_uuid:
        raise click.ClickException(
            f"Task UUID required. Pass --task-uuid or set {_ENV_TASK_UUID}."
        )

    # Resolve max_turns from env (optional)
    max_turns_str = os.environ.get("SUPERCHARGE_MAX_TURNS")
    max_turns = int(max_turns_str) if max_turns_str else None

    fast = _is_fast_mode(model)

    if not fast:
        remaining = _get_remaining_depth()
        if remaining <= 0:
            raise click.ClickException(
                "Max recursion depth reached (0 remaining). "
                "Set SUPERCHARGE_MAX_RECURSION_DEPTH in "
                "settings.json env to increase the limit."
            )
    else:
        remaining = 1  # fast workers always get budget=0 (1-1)

    task_dir = _find_task_dir(task_uuid)
    if not task_dir:
        raise click.ClickException(f"Task {task_uuid} not found")

    worker_id = str(uuid.uuid4())

    if fast:
        result = asyncio.run(
            _fast_worker_init(
                task_dir, agent_type, prompt, worker_id,
                max_turns, model,
            )
        )
    else:
        worker_file = _prepare_worker_file(task_dir, worker_id, prompt)
        result = asyncio.run(
            _deep_worker_init(
                task_dir, agent_type, prompt, worker_id,
                worker_file, remaining, max_turns, model,
            )
        )
    click.echo(json.dumps(result))


@subtask.command("resume")
@click.argument("worker_id")
@click.argument("prompt")
def subtask_resume(worker_id: str, prompt: str):
    """Resume a deep worker by worker_id. worker_id is the session_id."""
    # Verify the worker exists (has a context file)
    worker_file = _find_worker_file(worker_id)
    if not worker_file:
        raise click.ClickException(
            f"Worker {worker_id} not found. "
            "Only deep workers (opus/sonnet) can be resumed."
        )

    # Derive task_dir and agent_type from worker file path:
    # <task_root>/<agent_type>/<uuid>/workers/<worker_id>.md
    task_dir = worker_file.parent.parent
    agent_type = task_dir.parent.name

    result = asyncio.run(
        _deep_worker_resume(worker_id, prompt, task_dir, agent_type)
    )
    click.echo(json.dumps(result))


# ── hooks (internal) ────────────────────────────────────────────────────────


def _emit_hook(hook_event: str, content: str, data_dir: Path) -> None:
    """Emit hook JSON with additionalContext, prepending directive."""
    directive = _read_prompt("directive.md", data_dir)
    body = f"{directive}\n{content}" if directive else content
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": hook_event,
                "additionalContext": (
                    f"<supercharge-ai>\n{body}\n</supercharge-ai>"
                ),
            }
        },
        sys.stdout,
    )


def _read_prompt(name: str, data_dir: Path) -> str:
    """Read a prompt file, return empty string if missing."""
    path = data_dir / "prompts" / name
    return path.read_text() if path.exists() else ""


def _check_version_sync() -> str | None:
    """Compare installed CLI version against plugin.json. Return warning or None."""
    from supercharge import __version__ as cli_version

    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if not plugin_root:
        return None
    plugin_json = Path(plugin_root) / ".claude-plugin" / "plugin.json"
    if not plugin_json.exists():
        return None
    try:
        plugin_version = json.loads(plugin_json.read_text()).get("version", "")
    except (json.JSONDecodeError, OSError):
        return None
    if plugin_version and plugin_version != cli_version:
        return (
            f"[SuperchargeAI] Version mismatch: CLI={cli_version}, plugin={plugin_version}. "
            f"Run: uv tool upgrade supercharge-ai"
        )
    return None


@supercharge.command("hook-session-start", hidden=True)
def hook_session_start():
    """SessionStart hook: inject shared protocol + orchestrator prompt."""
    input_data = json.load(sys.stdin)

    warning = _check_version_sync()
    if warning:
        click.echo(warning, err=True)

    hook_dir = _hook_data_dir()
    parts = [_read_prompt("protocol.md", hook_dir), _read_prompt("orchestrator.md", hook_dir)]
    content = "\n".join(p for p in parts if p)

    if content:
        _emit_hook("SessionStart", content, hook_dir)


@supercharge.command("hook-subagent-start", hidden=True)
def hook_subagent_start():
    """SubagentStart hook: inject shared protocol into agents."""
    json.load(sys.stdin)

    hook_dir = _hook_data_dir()
    content = _read_prompt("protocol.md", hook_dir)
    if content:
        _emit_hook("SubagentStart", content, hook_dir)
