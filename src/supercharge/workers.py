"""Worker spawning (deep and fast) for SuperchargeAI."""

from __future__ import annotations

import json
import os
from contextlib import aclosing
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import click

from supercharge.metrics import _emit, _emit_worker_result
from supercharge.paths import (
    _ENV_PROJECT_DIR,
    _cli_data_dir,
    _copy_template,
    _find_task_dir,
    _project_dir,
    _read_frontmatter,
    _read_prompt,
)
from supercharge.permissions import (
    _AGENT_PERMISSIONS,
    _DEFAULT_PERMS,
    _ENV_REMAINING,
    _ENV_TASK_UUID,
    _ENV_WORKER_ID,
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
    worker_id: str = "",
) -> str:
    """Compose the initial prompt sent to a deep worker."""
    budget = remaining_depth - 1
    if budget > 0:
        depth_note = (
            f"Recursion budget: {budget} levels remaining. "
            f"To spawn sub-workers: "
            f'`supercharge subtask init <agent_type> "<prompt>"'
            f' --model <model> --author "worker:{worker_id}"` '
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
    author: str | None = None,
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

    # Prepend YAML frontmatter
    task_uuid = os.environ.get(_ENV_TASK_UUID, "")
    frontmatter_fields = [
        f"worker_id: {worker_id}",
        f"agent_type: {task_dir.parent.name}",
        f"spawned_at: {datetime.now(timezone.utc).isoformat()}",
        "model: deep",
    ]
    if author:
        frontmatter_fields.append(f"created_by: {author}")
    elif task_uuid:
        frontmatter_fields.append(f"created_by: task:{task_uuid}")
    frontmatter = "---\n" + "\n".join(frontmatter_fields) + "\n---\n\n"
    content = frontmatter + content

    worker_file.write_text(content)
    return worker_file


def _make_worker_tool_hook(
    worker_id: str,
    task_uuid: str,
    agent_type: str,
):
    """Create an async PreToolUse hook that emits tool_use metrics.

    Values are captured via closure so the hook can attribute events correctly
    (env vars are set on the child process, but the hook runs in the parent).
    """

    async def _hook(hook_input, match, context):  # noqa: ARG001
        tool_input = hook_input.get("tool_input", {})
        # Truncate string values to avoid bloating the metrics DB
        truncated = {
            k: (v[:200] if isinstance(v, str) else v)
            for k, v in tool_input.items()
        }
        _emit(
            "tool_use",
            session_id=hook_input.get("session_id", ""),
            worker_id=worker_id,
            task_uuid=task_uuid,
            agent_type=agent_type,
            tool_name=hook_input.get("tool_name", ""),
            detail=json.dumps(truncated, default=str),
        )
        return {}  # Passthrough — no permission decision

    return _hook


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
    All workers get PreToolUse hooks for tool tracking metrics.
    """
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

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

    # Resolve task_uuid for hook attribution
    resolved_task_uuid = _read_frontmatter(task_dir / "task.md").get(
        "task_uuid", task_dir.name
    )

    # Register PreToolUse hook for tool tracking on all workers
    tool_hook = _make_worker_tool_hook(
        worker_id=worker_id or "",
        task_uuid=resolved_task_uuid,
        agent_type=agent_type,
    )
    hooks = {
        "PreToolUse": [
            HookMatcher(matcher=None, hooks=[tool_hook]),
        ],
    }

    return ClaudeAgentOptions(
        system_prompt=_build_worker_system_prompt(),
        cwd=str(task_dir),
        add_dirs=[project_root] if project_root else [],
        allowed_tools=tools,
        can_use_tool=can_use_tool_cb,
        permission_mode="acceptEdits",
        max_turns=max_turns,
        model=model,
        hooks=hooks,
        env={
            _ENV_REMAINING: str(remaining_depth - 1),
            _ENV_TASK_UUID: resolved_task_uuid,
            _ENV_WORKER_ID: worker_id or "",
            _ENV_PROJECT_DIR: project_root,
            "CLAUDECODE": "",  # Allow nested Claude Code spawn via Agent SDK
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
    _emit(
        "worker_start",
        worker_id=worker_id,
        task_uuid=task_dir.name,
        agent_type=agent_type,
        detail="deep",
    )
    try:
        await client.connect()
        await client.query(
            _build_deep_worker_prompt(
                task_dir,
                agent_type,
                worker_file,
                prompt,
                remaining_depth,
                worker_id=worker_id,
            ),
            session_id=worker_id,
        )
        async for message in client.receive_response():
            if isinstance(message, ResultMessage):
                result_msg = message
    finally:
        detail = "error" if (result_msg and result_msg.is_error) else "success"
        if not result_msg:
            detail = "error: no result"
        _emit(
            "worker_end",
            worker_id=worker_id,
            task_uuid=task_dir.name,
            agent_type=agent_type,
            detail=detail,
        )
        if result_msg:
            _emit_worker_result(worker_id, result_msg, agent_type, task_dir.name)
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
    async with aclosing(query(prompt=prompt, options=options)) as stream:
        async for message in stream:
            if isinstance(message, ResultMessage):
                result_msg = message

    if result_msg:
        _emit_worker_result(worker_id, result_msg, agent_type, task_dir.name)

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
    _emit(
        "worker_start",
        worker_id=worker_id,
        task_uuid=task_dir.name,
        agent_type=agent_type,
        detail="fast",
    )
    try:
        async with aclosing(
            query(
                prompt=_build_fast_worker_prompt(task_dir, agent_type, prompt),
                options=options,
            )
        ) as stream:
            async for message in stream:
                if isinstance(message, ResultMessage):
                    result_msg = message
    finally:
        detail = "error" if (result_msg and result_msg.is_error) else "success"
        if not result_msg:
            detail = "error: no result"
        _emit(
            "worker_end",
            worker_id=worker_id,
            task_uuid=task_dir.name,
            agent_type=agent_type,
            detail=detail,
        )
        if result_msg:
            _emit_worker_result(worker_id, result_msg, agent_type, task_dir.name)

    if not result_msg:
        raise click.ClickException("No result returned from worker")

    if result_msg.is_error:
        return {"worker_id": worker_id, "error": result_msg.result}

    return {"worker_id": worker_id, "result": result_msg.result}


# ── Memory agent (background, one-shot) ───────────────────────────────────


async def _memory_agent_run(task_uuid: str) -> None:
    """Run the memory agent on a task workspace (background entry point).

    Resolves the task directory, reads task.md for the prompt, builds
    Agent SDK options with bypassPermissions, and runs one-shot via
    query(). Logs to stderr since this runs as a detached process.
    """
    import sys

    from claude_agent_sdk import ResultMessage, query

    task_dir = _find_task_dir(task_uuid)
    if not task_dir:
        print(f"[SuperchargeAI] memory run: task {task_uuid} not found", file=sys.stderr)
        return

    task_md = task_dir / "task.md"
    if not task_md.exists():
        print(f"[SuperchargeAI] memory run: task.md missing in {task_dir}", file=sys.stderr)
        return

    options = _build_options(
        task_dir,
        remaining_depth=1,
        max_turns=50,
        model=None,
        agent_type="memory",
        worker_id=task_uuid,
    )
    options.permission_mode = "bypassPermissions"
    # can_use_tool is incompatible with string prompts in the Agent SDK (requires
    # AsyncIterable/streaming mode). Since we use bypassPermissions, write-scope
    # enforcement via can_use_tool is unnecessary — clear it to avoid the crash.
    options.can_use_tool = None

    prompt = (
        f"You are a memory agent. Your task is at "
        f".claude/SuperchargeAI/tasks/memory/{task_uuid}/task.md\n\n"
        f"Read task.md and execute all requirements."
    )

    try:
        async with aclosing(query(prompt=prompt, options=options)) as stream:
            async for message in stream:
                if isinstance(message, ResultMessage):
                    _emit_worker_result(task_uuid, message, "memory", task_uuid)
                    if message.is_error:
                        print(
                            f"[SuperchargeAI] memory agent error: {message.result}",
                            file=sys.stderr,
                        )
                    else:
                        print(
                            f"[SuperchargeAI] memory agent completed: {task_uuid}",
                            file=sys.stderr,
                        )
    except Exception as exc:
        print(f"[SuperchargeAI] memory agent failed: {exc}", file=sys.stderr)
