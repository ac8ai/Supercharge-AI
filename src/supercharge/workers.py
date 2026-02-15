"""Worker spawning (deep and fast) for SuperchargeAI."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import click

from supercharge.paths import (
    _ENV_PROJECT_DIR,
    _cli_data_dir,
    _copy_template,
    _project_dir,
    _read_prompt,
)
from supercharge.permissions import (
    _AGENT_PERMISSIONS,
    _DEFAULT_PERMS,
    _ENV_REMAINING,
    _ENV_TASK_UUID,
    _get_remaining_depth,
    _make_can_use_tool,
)

if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeAgentOptions


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
    budget = remaining_depth - 1
    if budget > 0:
        depth_note = (
            f"Recursion budget: {budget} levels remaining. "
            f"To spawn sub-workers: "
            f'`supercharge subtask init <agent_type> "<prompt>" --model <model>` '
            f"(SUPERCHARGE_TASK_UUID is auto-set in your env)"
        )
    else:
        depth_note = "Recursion budget: 0. You cannot spawn sub-workers."
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
    task_dir: Path,
    worker_id: str,
    prompt: str,
) -> Path:
    """Create the worker context file from template and fill in assignment."""
    workers_dir = task_dir / "workers"
    workers_dir.mkdir(exist_ok=True)
    worker_file = workers_dir / f"{worker_id}.md"
    _copy_template("worker.md", worker_file)
    content = worker_file.read_text()
    content = content.replace(
        "## Assignment\n",
        f"## Assignment\n\n{prompt}\n",
        1,
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
            agent_type,
            task_dir,
            worker_id,
            project_root,
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
        task_dir,
        remaining_depth,
        max_turns,
        model,
        agent_type=agent_type,
        worker_id=worker_id,
    )
    client = ClaudeSDKClient(options=options)

    result_msg = None
    try:
        await client.connect()
        await client.query(
            _build_deep_worker_prompt(
                task_dir,
                agent_type,
                worker_file,
                prompt,
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
        task_dir,
        remaining,
        max_turns=None,
        model=None,
        agent_type=agent_type,
        worker_id=worker_id,
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
        task_dir,
        remaining_depth=1,
        max_turns=max_turns,
        model=model,
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
