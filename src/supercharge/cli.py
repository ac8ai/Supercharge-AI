#!/usr/bin/env python3
"""SuperchargeAI CLI — thin entry point delegating to submodules."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path

import click

from supercharge.hooks import hook_pre_tool_use, hook_session_start, hook_subagent_start
from supercharge.paths import _cli_data_dir, _copy_template, _find_task_dir, _task_root
from supercharge.permissions import (
    _ENV_TASK_UUID,
    _add_user_permissions,
    _find_worker_file,
    _get_remaining_depth,
    _is_fast_mode,
    _remove_user_permissions,
    _user_settings_path,
)
from supercharge.workers import (
    _deep_worker_init,
    _deep_worker_resume,
    _fast_worker_init,
    _memory_agent_run,
    _prepare_worker_file,
)

# ── Main group ───────────────────────────────────────────────────────────────


@click.group()
def supercharge():
    """SuperchargeAI - multi-agent framework for Claude Code."""


@supercharge.command("version")
def version_cmd():
    """Print installed version."""
    from supercharge import __version__

    click.echo(__version__)


# ── Hook commands (defined in hooks.py, registered here) ─────────────────────

supercharge.add_command(hook_session_start)
supercharge.add_command(hook_subagent_start)
supercharge.add_command(hook_pre_tool_use)

# ── init / deinit ────────────────────────────────────────────────────────────

_SUPERCHARGE_MARKER = "supercharge-ai"


def _find_claude_md(project_dir: str | None = None) -> Path:
    """Locate CLAUDE.md in the project's .claude/ directory."""
    root = Path(project_dir) if project_dir else Path.cwd()
    return root / ".claude" / "CLAUDE.md"


@supercharge.command()
@click.option("--project-dir", default=None, help="Project root (default: cwd)")
@click.option(
    "--add-permissions",
    is_flag=True,
    default=False,
    help="Add tool permissions to ~/.claude/settings.json (needed for VS Code).",
)
def init(project_dir: str | None, add_permissions: bool):
    """Add SuperchargeAI include line to the project's CLAUDE.md."""
    claude_md = _find_claude_md(project_dir)

    if claude_md.exists() and _SUPERCHARGE_MARKER in claude_md.read_text():
        click.echo("Already configured — CLAUDE.md contains supercharge-ai reference.")
    else:
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

    if add_permissions:
        settings_path = _user_settings_path()
        added = _add_user_permissions(settings_path)
        if added:
            click.echo(f"\nAdded to {settings_path}:")
            for entry in added:
                click.echo(f"  {entry}")
            click.echo(
                "\nThese permissions are needed for VS Code where plugin hooks "
                "don't fire yet (https://github.com/anthropics/claude-code/issues/18547)."
            )
            click.echo("Remove with: supercharge permissions remove")
        else:
            click.echo("\nPermissions already present in settings.json.")


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


# ── permissions ──────────────────────────────────────────────────────────────


@supercharge.group()
def permissions():
    """Manage SuperchargeAI tool permissions in ~/.claude/settings.json."""


@permissions.command("remove")
def permissions_remove():
    """Remove SuperchargeAI permission entries from ~/.claude/settings.json."""
    settings_path = _user_settings_path()
    removed = _remove_user_permissions(settings_path)
    if removed:
        click.echo(f"Removed {removed} SuperchargeAI permission(s) from {settings_path}.")
    else:
        click.echo("No SuperchargeAI permissions found in settings.json.")


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


@task.command("cleanup")
@click.argument("task_uuid")
def task_cleanup(task_uuid: str):
    """Safely delete a task folder after memory harvesting.

    Validates the UUID format and confirms the path is inside
    .claude/SuperchargeAI/tasks/ before removing.
    """
    import re
    import shutil

    uuid_re = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    )
    if not uuid_re.match(task_uuid):
        raise click.ClickException(f"Invalid UUID format: {task_uuid}")

    task_dir = _find_task_dir(task_uuid)
    if not task_dir:
        raise click.ClickException(f"Task {task_uuid} not found")

    # Verify the resolved path is actually inside the task root
    task_root = _task_root()
    try:
        task_dir.resolve().relative_to(task_root.resolve())
    except ValueError:
        raise click.ClickException(
            f"Task directory {task_dir} is outside task root {task_root}"
        )

    shutil.rmtree(task_dir)
    click.echo(f"Removed {task_dir}")


# ── subtask ──────────────────────────────────────────────────────────────────


@supercharge.group()
def subtask():
    """Manage Agent SDK workers within a task."""


@subtask.command("init")
@click.argument("agent_type")
@click.argument("prompt")
@click.option(
    "--task-uuid",
    envvar=_ENV_TASK_UUID,
    default=None,
    help="Parent task UUID (agents pass this; workers get it from env).",
)
@click.option(
    "--model",
    default=None,
    help="Model override (sonnet, opus, haiku)",
)
def subtask_init(
    agent_type: str,
    prompt: str,
    task_uuid: str | None,
    model: str | None,
):
    """Spawn a new Agent SDK worker on a task. Prints JSON {worker_id, result}."""
    # Resolve task UUID: --task-uuid flag > SUPERCHARGE_TASK_UUID env var
    # Click's envvar= already handles the fallback, so task_uuid may come
    # from either source. Validate consistency if both are present.
    env_uuid = os.environ.get(_ENV_TASK_UUID)
    if task_uuid and env_uuid and task_uuid != env_uuid:
        raise click.ClickException(
            f"--task-uuid ({task_uuid}) conflicts with {_ENV_TASK_UUID} env var ({env_uuid})."
        )
    if not task_uuid:
        raise click.ClickException(f"Task UUID required. Pass --task-uuid or set {_ENV_TASK_UUID}.")

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
                task_dir,
                agent_type,
                prompt,
                worker_id,
                max_turns,
                model,
            )
        )
    else:
        worker_file = _prepare_worker_file(task_dir, worker_id, prompt)
        result = asyncio.run(
            _deep_worker_init(
                task_dir,
                agent_type,
                prompt,
                worker_id,
                worker_file,
                remaining,
                max_turns,
                model,
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
            f"Worker {worker_id} not found. Only deep workers (opus/sonnet) can be resumed."
        )

    # Derive task_dir and agent_type from worker file path:
    # <task_root>/<agent_type>/<uuid>/workers/<worker_id>.md
    task_dir = worker_file.parent.parent
    agent_type = task_dir.parent.name

    result = asyncio.run(_deep_worker_resume(worker_id, prompt, task_dir, agent_type))
    click.echo(json.dumps(result))


# ── memory ──────────────────────────────────────────────────────────────────


@supercharge.group()
def memory():
    """Background memory harvesting commands."""


@memory.command("run")
@click.argument("task_uuid")
def memory_run(task_uuid: str):
    """Run the memory agent on a task workspace (background process)."""
    asyncio.run(_memory_agent_run(task_uuid))


@memory.command("stamp")
@click.argument("transcript_path", type=click.Path(exists=True))
def memory_stamp(transcript_path: str):
    """Mark a transcript as reviewed by appending a stamp entry."""
    from supercharge.memory import _stamp_transcript

    _stamp_transcript(Path(transcript_path))
    click.echo(f"Stamped {transcript_path}")
