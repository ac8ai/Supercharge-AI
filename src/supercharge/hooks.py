"""Hook evaluation logic and hook CLI commands for SuperchargeAI."""

from __future__ import annotations

import fnmatch
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import click

from supercharge.metrics import _emit
from supercharge.paths import _SUPERCHARGE_WORKSPACE_MARKER, _hook_data_dir, _read_prompt

# ── Settings allowlist caching and matching ─────────────────────────────────

# WORKAROUND: Claude Code subagents don't inherit settings.json permissions.
# The functions below (_load_settings_allowlist, _tool_matches_pattern,
# _check_settings_allowlist) re-implement allowlist matching so our PreToolUse
# hook can auto-approve tools for agents that the user already approved.
# Remove when upstream bugs #18950, #22665, #28584 are fixed.
_cached_allowlist: list[str] | None = None

# Regex to parse allowlist entries like "Bash(git:*)" or "Write(.claude/**)"
_ALLOWLIST_ENTRY_RE = re.compile(r"^(\w+)(?:\((.+)\))?$")


def _load_settings_allowlist() -> list[str]:
    """Load and cache merged permission allowlists from all settings files.

    Reads from (in order):
    - ~/.claude/settings.json
    - ~/.claude/settings.local.json
    - .claude/settings.json (project)
    - .claude/settings.local.json (project)

    Returns the merged deduplicated list of allow patterns.
    """
    global _cached_allowlist
    if _cached_allowlist is not None:
        return _cached_allowlist

    from supercharge.paths import _user_config_dir

    patterns: list[str] = []
    seen: set[str] = set()

    # Global settings
    user_dir = _user_config_dir()
    # Project settings
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")

    candidates = [
        user_dir / "settings.json",
        user_dir / "settings.local.json",
    ]
    if project_dir:
        candidates.append(Path(project_dir) / ".claude" / "settings.json")
        candidates.append(Path(project_dir) / ".claude" / "settings.local.json")

    for path in candidates:
        try:
            if not path.is_file():
                continue
            data = json.loads(path.read_text())
            allow = data.get("permissions", {}).get("allow", [])
            for entry in allow:
                if isinstance(entry, str) and entry not in seen:
                    seen.add(entry)
                    patterns.append(entry)
        except (json.JSONDecodeError, OSError):
            continue

    _cached_allowlist = patterns
    return _cached_allowlist


def _reset_allowlist_cache() -> None:
    """Reset the cached allowlist (for testing)."""
    global _cached_allowlist
    _cached_allowlist = None


def _tool_matches_pattern(tool_name: str, tool_input: dict, pattern: str) -> bool:
    """Check if a tool call matches a single allowlist pattern.

    Pattern formats:
    - "ToolName" — matches all calls to that tool
    - "Bash(cmd:*)" or "Bash(cmd *)" — matches Bash where command starts with prefix
    - "Bash(exact command)" — matches exact command
    - "Write(glob)" / "Edit(glob)" — matches file_path against glob
    - "WebFetch(domain:example.com)" — matches URL domain
    - "WebSearch" — matches all WebSearch calls
    """
    m = _ALLOWLIST_ENTRY_RE.match(pattern)
    if not m:
        return False

    pattern_tool = m.group(1)
    pattern_param = m.group(2)  # None if no parentheses

    if pattern_tool != tool_name:
        return False

    # No parameter constraint — matches all calls to this tool
    if pattern_param is None:
        return True

    if tool_name == "Bash":
        command = tool_input.get("command", "")
        # "Bash(prefix:*)" — command starts with prefix
        if pattern_param.endswith(":*"):
            prefix = pattern_param[:-2]
            return command.startswith(prefix)
        # "Bash(prefix *)" — command starts with "prefix "
        if pattern_param.endswith(" *"):
            prefix = pattern_param[:-1]  # keep trailing space
            return command.startswith(prefix)
        # Exact match (no wildcard)
        return command == pattern_param

    if tool_name in ("Write", "Edit"):
        file_path = tool_input.get("file_path", "")
        # Glob matching against the file path
        return fnmatch.fnmatch(file_path, pattern_param)

    if tool_name == "WebFetch":
        url = tool_input.get("url", "")
        if pattern_param.startswith("domain:"):
            domain = pattern_param[7:]
            try:
                parsed = urlparse(url)
                return parsed.hostname == domain
            except Exception:
                return False
        return False

    # For any other tool with a parameter, we can't match specifics
    return False


def _check_settings_allowlist(tool_name: str, tool_input: dict) -> bool:
    """Check if a tool call matches any pattern in the settings allowlists."""
    allowlist = _load_settings_allowlist()
    return any(_tool_matches_pattern(tool_name, tool_input, p) for p in allowlist)


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


def _has_project_write_permissions() -> bool:
    """Check if the settings allowlist covers Write and Edit for project files.

    Returns True if the allowlist contains patterns that would match Write/Edit
    calls to arbitrary project paths (not just .claude/SuperchargeAI/).
    This is needed because Claude Code subagents don't inherit settings.json
    permissions (upstream bugs #18950, #22665, #28584), so our PreToolUse hook
    must auto-approve writes. Without broad Write/Edit in the allowlist, the
    hook returns None (pass-through), which silently fails for subagents.
    """
    allowlist = _load_settings_allowlist()
    has_write = False
    has_edit = False
    for pattern in allowlist:
        m = _ALLOWLIST_ENTRY_RE.match(pattern)
        if not m:
            continue
        tool = m.group(1)
        param = m.group(2)
        if tool == "Write":
            # Bare "Write" covers all paths; any glob covering project files also works
            # (but .claude/SuperchargeAI/** is NOT sufficient for project writes)
            if param is None or (param and not param.startswith(".claude/SuperchargeAI")):
                has_write = True
        elif tool == "Edit":
            if param is None or (param and not param.startswith(".claude/SuperchargeAI")):
                has_edit = True
    return has_write and has_edit


def _evaluate_task_call(tool_input: dict, permission_mode: str) -> dict | None:
    """Evaluate a Task tool call for SuperchargeAI workspace enforcement.

    Returns allow if the subagent is ours and the prompt references the workspace.
    Returns deny if the subagent is ours but the workspace path is missing.
    Returns deny if a project-writing agent (code/document) is launched without
    sufficient permissions -- either in background (can't prompt) or in foreground
    when the settings allowlist doesn't cover Write/Edit (upstream bug: subagents
    don't inherit settings.json permissions, so our hook is the only path).
    Returns None (pass-through) for non-SuperchargeAI subagents.
    """
    subagent_type = tool_input.get("subagent_type", "")
    if not subagent_type.startswith("supercharge-ai:"):
        return None

    agent_type = subagent_type.removeprefix("supercharge-ai:")
    run_in_background = tool_input.get("run_in_background", False)

    # Reject project-writing agents when they won't be able to Write/Edit.
    #
    # Background: always fails (can't prompt the user at all).
    # Foreground: also fails unless the settings allowlist covers Write/Edit,
    # because subagents don't inherit settings.json permissions (upstream
    # bugs #18950, #22665, #28584). Our PreToolUse hook mirrors the allowlist,
    # but only if the entries exist. Without them, the hook returns None
    # (pass-through) and the subagent's permission prompt silently fails.
    #
    # bypassPermissions = --dangerously-skip-permissions flag
    # dontAsk           = auto-approve mode (no user prompts)
    # acceptEdits       = auto-approve Write/Edit (Bash still needs prompts)
    _PROJECT_WRITERS = {"code", "document"}
    _AUTONOMOUS_MODES = {"bypassPermissions", "dontAsk"}
    if agent_type in _PROJECT_WRITERS and permission_mode not in _AUTONOMOUS_MODES:
        # Background: always deny (can't prompt for Bash, even acceptEdits
        # only auto-approves Write/Edit but not Bash)
        if run_in_background:
            return _deny(
                f"Task: {agent_type} agent writes project files and cannot run in "
                f"the background under permission mode '{permission_mode}'. "
                f"Run it in the foreground so the user can approve file writes, "
                f"or add 'Write' and 'Edit' to settings.json permissions.allow."
            )
        # Foreground: acceptEdits auto-approves Write/Edit at the Claude Code
        # level (even for subagents), so the agent can work. But in default
        # mode, subagents can't inherit settings.json permissions — check if
        # our hook workaround (allowlist mirroring) can cover the gap.
        if permission_mode != "acceptEdits" and not _has_project_write_permissions():
            return _deny(
                f"Task: {agent_type} agent needs Write/Edit for project files, but "
                f"the settings.json allowlist doesn't cover them. Subagents can't "
                f"inherit permission prompts (Claude Code bugs #18950, #22665, #28584). "
                f"Either add 'Write' and 'Edit' to ~/.claude/settings.json "
                f"permissions.allow, or handle file writes directly in the orchestrator."
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
    if tool_name == "Read":
        return _allow("Read: always allowed")

    if tool_name == "Bash":
        command = tool_input.get("command", "")
        if command.startswith("supercharge "):
            return _allow("Bash: supercharge CLI command")
        # Don't auto-deny dangerous patterns at the hook level.
        # The hook fires for BOTH orchestrator and subagents, and we can't
        # distinguish them. Passthrough lets the user approve/deny.
        # Workers are still hard-blocked by the can_use_tool callback.
        if _check_settings_allowlist(tool_name, tool_input):
            return _allow("Bash: matches settings.json allowlist")
        return None

    if tool_name in ("Write", "Edit"):
        file_path = tool_input.get("file_path", "")
        # Match both absolute (/.claude/SuperchargeAI/) and relative (.claude/SuperchargeAI/)
        if _SUPERCHARGE_WORKSPACE_MARKER in file_path or file_path.startswith(".claude/SuperchargeAI/"):
            return _allow(f"{tool_name}: SuperchargeAI workspace file")
        if _check_settings_allowlist(tool_name, tool_input):
            return _allow(f"{tool_name}: matches settings.json allowlist")
        return None

    if tool_name == "Task":
        return _evaluate_task_call(tool_input, permission_mode)

    # For any other tool (WebSearch, WebFetch, etc.), check allowlist
    if _check_settings_allowlist(tool_name, tool_input):
        return _allow(f"{tool_name}: matches settings.json allowlist")

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


def _trigger_background_memory(input_data: dict) -> None:
    """Scan for unreviewed transcripts and stale task folders, spawn memory agents.

    Runs after hook output is emitted. Errors are caught and logged to stderr
    to never crash the hook (which would block session start).
    """
    from supercharge.memory import (
        _format_stale_folders_task,
        _format_transcript_task,
        _migrate_methodology_memory,
        _scan_stale_task_folders,
        _scan_unreviewed_transcripts,
        _spawn_background_memory,
    )
    from supercharge.paths import _project_dir, _user_methodology_dir

    try:
        transcript_path = input_data.get("transcript_path", "")
        cwd = input_data.get("cwd", "")
        project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or cwd or _project_dir()

        if not project_dir:
            return

        memory_dir = str(Path(project_dir) / ".claude" / "SuperchargeAI" / "memory")
        methodology_dir = str(_user_methodology_dir())

        # Feature 0: Migrate methodology memory from project to user scope
        _migrate_methodology_memory(project_dir)

        # Feature 1: Transcript harvesting
        if transcript_path:
            transcripts = _scan_unreviewed_transcripts(transcript_path)
            if transcripts:
                task_content = _format_transcript_task(transcripts, memory_dir, methodology_dir)
                uuid = _spawn_background_memory(task_content, project_dir)
                if uuid:
                    click.echo(
                        f"[SuperchargeAI] Background memory: transcript harvesting ({uuid})",
                        err=True,
                    )

        # Feature 2: Stale task folder cleanup
        task_root = Path(project_dir) / ".claude" / "SuperchargeAI" / "tasks"
        if task_root.is_dir():
            stale = _scan_stale_task_folders(task_root)
            if stale:
                task_content = _format_stale_folders_task(stale, memory_dir, methodology_dir)
                uuid = _spawn_background_memory(task_content, project_dir)
                if uuid:
                    click.echo(
                        f"[SuperchargeAI] Background memory: stale folder cleanup ({uuid})",
                        err=True,
                    )
    except Exception as exc:
        click.echo(f"[SuperchargeAI] Background memory scan failed: {exc}", err=True)


def _ensure_project_dir(input_data: dict) -> None:
    """Pre-create the Claude Code extension's session directory.

    The VS Code extension reads this directory on startup to list sessions.
    In devcontainers, the directory may not exist yet, causing a permanent
    ENOENT failure in the extension. Creating it here ensures subsequent
    extension retries succeed.
    """
    cwd = input_data.get("cwd", "")
    if not cwd:
        return
    from supercharge.paths import _user_config_dir

    project_slug = cwd.replace("/", "-")
    project_dir = _user_config_dir() / "projects" / project_slug
    try:
        project_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


@click.command("hook-session-start", hidden=True)
def hook_session_start():
    """SessionStart hook: inject shared protocol + orchestrator prompt."""
    input_data = json.load(sys.stdin)

    _ensure_project_dir(input_data)

    warning = _check_version_sync()
    if warning:
        click.echo(warning, err=True)

    hook_dir = _hook_data_dir()
    parts = [_read_prompt("protocol.md", hook_dir), _read_prompt("orchestrator.md", hook_dir)]

    session_id = input_data.get("session_id", "")
    if session_id:
        parts.append(f'\n<session-identity session_id="{session_id}" />')

    # Contribution nudge (non-blocking)
    try:
        from supercharge.nudge import get_contribution_nudge
        nudge = get_contribution_nudge(session_id)
        if nudge:
            parts.append(nudge)
    except Exception:
        pass

    content = "\n".join(p for p in parts if p)

    if content:
        _emit_hook("SessionStart", content, hook_dir)

    _emit(
        "session_start",
        session_id=input_data.get("session_id", ""),
        detail=input_data.get("source", ""),
    )

    # Background memory harvesting (non-blocking, after hook output)
    _trigger_background_memory(input_data)


@click.command("hook-subagent-start", hidden=True)
def hook_subagent_start():
    """SubagentStart hook: inject shared protocol + agent prompt into agents."""
    input_data = json.load(sys.stdin)
    session_id = input_data.get("session_id", "")
    agent_id = input_data.get("agent_id", "")
    agent_type = input_data.get("agent_type", "")

    hook_dir = _hook_data_dir()
    parts = [_read_prompt("protocol.md", hook_dir), _read_prompt("agent.md", hook_dir)]

    if session_id or agent_id:
        parts.append(
            f'\n<agent-identity session_id="{session_id}" '
            f'agent_id="{agent_id}" agent_type="{agent_type}" />'
        )

    content = "\n".join(p for p in parts if p)
    if content:
        _emit_hook("SubagentStart", content, hook_dir)

    # Normalize agent_type: strip "supercharge-ai:" prefix for consistency
    # with CLI-emitted events that use the short form (e.g., "code" not "supercharge-ai:code")
    norm_type = agent_type
    if norm_type.startswith("supercharge-ai:"):
        norm_type = norm_type[len("supercharge-ai:"):]

    _emit(
        "subagent_start",
        session_id=session_id,
        agent_id=agent_id,
        agent_type=norm_type,
        parent_id=f"orchestrator:{session_id}" if session_id else "",
    )


@click.command("hook-subagent-stop", hidden=True)
def hook_subagent_stop():
    """SubagentStop hook: record agent completion with duration."""
    input_data = json.load(sys.stdin)
    session_id = input_data.get("session_id", "")
    agent_id = input_data.get("agent_id", "")
    agent_type = input_data.get("agent_type", "")

    norm_type = agent_type
    if norm_type.startswith("supercharge-ai:"):
        norm_type = norm_type[len("supercharge-ai:"):]

    transcript_path = input_data.get("agent_transcript_path", "")

    _emit(
        "subagent_stop",
        session_id=session_id,
        agent_id=agent_id,
        agent_type=norm_type,
        parent_id=f"orchestrator:{session_id}" if session_id else "",
        detail=transcript_path,
    )


@click.command("hook-pre-tool-use", hidden=True)
def hook_pre_tool_use():
    """PreToolUse hook: auto-approve SuperchargeAI tool calls."""
    input_data = json.load(sys.stdin)

    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {})
    permission_mode = input_data.get("permission_mode", "default")

    result = _evaluate_pre_tool_use(tool_name, tool_input, permission_mode)

    _emit(
        "tool_use",
        session_id=input_data.get("session_id", ""),
        tool_name=tool_name,
        detail=json.dumps(
            {k: v[:200] if isinstance(v, str) else v for k, v in tool_input.items()},
            default=str,
        ),
    )

    if result is not None:
        json.dump(result, sys.stdout)
