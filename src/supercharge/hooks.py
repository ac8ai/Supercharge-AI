"""Hook evaluation logic and hook CLI commands for SuperchargeAI."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import click

from supercharge.paths import _SUPERCHARGE_WORKSPACE_MARKER, _hook_data_dir, _read_prompt


def _allow(reason: str) -> dict:
    """Build a PreToolUse allow decision."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": reason,
        }
    }


def _deny(reason: str) -> dict:
    """Build a PreToolUse deny decision."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _evaluate_task_call(tool_input: dict, permission_mode: str) -> dict | None:
    """Evaluate a Task tool call for SuperchargeAI workspace enforcement.

    Returns allow if the subagent is ours and the prompt references the workspace.
    Returns deny if the subagent is ours but the workspace path is missing.
    Returns deny if a project-writing agent (code/document) is launched in background
    without sufficient permissions -- the orchestrator should run it in the foreground.
    Returns None (pass-through) for non-SuperchargeAI subagents.
    """
    subagent_type = tool_input.get("subagent_type", "")
    if not subagent_type.startswith("supercharge-ai:"):
        return None

    agent_type = subagent_type.removeprefix("supercharge-ai:")
    run_in_background = tool_input.get("run_in_background", False)

    # Reject background agents that write project files when permissions
    # require user approval.  These agents would silently fail on every
    # Write/Edit outside .claude/SuperchargeAI/ because the user cannot
    # approve prompts for background tasks.  Direct the orchestrator to
    # run the agent in the foreground instead.
    #
    # bypassPermissions = --dangerously-skip-permissions flag
    # dontAsk           = auto-approve mode (no user prompts)
    _PROJECT_WRITERS = {"code", "document"}
    _AUTONOMOUS_MODES = {"bypassPermissions", "dontAsk"}
    if (
        agent_type in _PROJECT_WRITERS
        and run_in_background
        and permission_mode not in _AUTONOMOUS_MODES
    ):
        return _deny(
            f"Task: {agent_type} agent writes project files and cannot run in "
            f"the background under permission mode '{permission_mode}'. "
            f"Run it in the foreground so the user can approve file writes."
        )

    prompt = tool_input.get("prompt", "")
    if _SUPERCHARGE_WORKSPACE_MARKER in prompt:
        return _allow("Task: SuperchargeAI agent with workspace path")

    return _deny("Task: SuperchargeAI agent missing workspace path in prompt.")


def _evaluate_pre_tool_use(tool_name: str, tool_input: dict, permission_mode: str) -> dict | None:
    """Evaluate a PreToolUse hook call. Returns allow/deny dict or None for pass-through.

    Scope: fires for orchestrator and Task-tool subagents (Claude Code sessions).
    Does NOT fire for Agent SDK workers (supercharge subtask init) -- those use
    the _make_can_use_tool() callback with separate write-scope enforcement.

    Assumptions and known limitations:
    - Bash: startswith("supercharge ") will match any binary named "supercharge".
      No other such binary is known to exist. Will not match commands that merely
      contain "supercharge" in the middle (e.g., "echo supercharge ...").
    - Write/Edit: substring match on "/.claude/SuperchargeAI/" (with slashes).
      False positive requires a project with that exact path segment outside of
      SuperchargeAI's workspace -- extremely unlikely in practice.
    - Task: substring match on prompt text. Relies on orchestrator prompt rules
      requiring the workspace path in every delegation prompt. A malformed prompt
      without the path will be denied (fail-safe).
    - None (pass-through) means we make no decision -- Claude Code continues with
      its normal permission flow (typically prompting the user).
    """
    if tool_name == "Bash":
        command = tool_input.get("command", "")
        if command.startswith("supercharge "):
            return _allow("Bash: supercharge CLI command")
        return None

    if tool_name in ("Write", "Edit"):
        file_path = tool_input.get("file_path", "")
        if _SUPERCHARGE_WORKSPACE_MARKER in file_path:
            return _allow(f"{tool_name}: SuperchargeAI workspace file")
        return None

    if tool_name == "Task":
        return _evaluate_task_call(tool_input, permission_mode)

    return None


def _emit_hook(hook_event: str, content: str, data_dir: Path) -> None:
    """Emit hook JSON with additionalContext, prepending directive."""
    directive = _read_prompt("directive.md", data_dir)
    body = f"{directive}\n{content}" if directive else content
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": hook_event,
                "additionalContext": (f"<supercharge-ai>\n{body}\n</supercharge-ai>"),
            }
        },
        sys.stdout,
    )


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


@click.command("hook-session-start", hidden=True)
def hook_session_start():
    """SessionStart hook: inject shared protocol + orchestrator prompt."""
    json.load(sys.stdin)  # consume stdin (required by hook protocol)

    warning = _check_version_sync()
    if warning:
        click.echo(warning, err=True)

    hook_dir = _hook_data_dir()
    parts = [_read_prompt("protocol.md", hook_dir), _read_prompt("orchestrator.md", hook_dir)]
    content = "\n".join(p for p in parts if p)

    if content:
        _emit_hook("SessionStart", content, hook_dir)


@click.command("hook-subagent-start", hidden=True)
def hook_subagent_start():
    """SubagentStart hook: inject shared protocol + agent prompt into agents."""
    json.load(sys.stdin)

    hook_dir = _hook_data_dir()
    parts = [_read_prompt("protocol.md", hook_dir), _read_prompt("agent.md", hook_dir)]
    content = "\n".join(p for p in parts if p)
    if content:
        _emit_hook("SubagentStart", content, hook_dir)


@click.command("hook-pre-tool-use", hidden=True)
def hook_pre_tool_use():
    """PreToolUse hook: auto-approve SuperchargeAI tool calls."""
    input_data = json.load(sys.stdin)

    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {})
    permission_mode = input_data.get("permission_mode", "default")

    result = _evaluate_pre_tool_use(tool_name, tool_input, permission_mode)
    if result is not None:
        json.dump(result, sys.stdout)
