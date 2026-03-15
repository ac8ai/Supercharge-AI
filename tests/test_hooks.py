"""Tests for PreToolUse permission helpers, user permission management, and hook identity."""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch

from supercharge.hooks import _evaluate_pre_tool_use, hook_session_start, hook_subagent_start
from supercharge.permissions import _add_user_permissions, _remove_user_permissions

# ── _evaluate_pre_tool_use ──────────────────────────────────────────────────


class TestEvaluatePreToolUse:
    """Test the PreToolUse decision logic directly."""

    def test_bash_supercharge_command_allowed(self):
        result = _evaluate_pre_tool_use(
            "Bash", {"command": "supercharge task init code"}, "default"
        )
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_bash_non_supercharge_passthrough(self):
        """Safe non-supercharge Bash commands return None (passthrough)."""
        result = _evaluate_pre_tool_use("Bash", {"command": "ls -la /tmp"}, "default")
        assert result is None

    def test_bash_dangerous_command_denied(self):
        """Dangerous Bash commands return deny."""
        dangerous = [
            "git push origin main",
            "rm -rf /",
            "echo hello > file.txt",
            "git commit -m 'test'",
        ]
        for cmd in dangerous:
            result = _evaluate_pre_tool_use("Bash", {"command": cmd}, "default")
            assert result is not None, f"Should deny: {cmd}"
            assert result["hookSpecificOutput"]["permissionDecision"] == "deny", (
                f"Should deny: {cmd}"
            )

    def test_bash_safe_non_supercharge_passthrough(self):
        """Safe non-supercharge Bash commands still return None."""
        safe = ["cat file.txt", "grep pattern src/", "git status", "git diff"]
        for cmd in safe:
            result = _evaluate_pre_tool_use("Bash", {"command": cmd}, "default")
            assert result is None, f"Should passthrough: {cmd}"

    def test_write_workspace_file_allowed(self):
        result = _evaluate_pre_tool_use(
            "Write",
            {"file_path": "/home/user/project/.claude/SuperchargeAI/tasks/code/abc/task.md"},
            "default",
        )
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_write_non_workspace_passthrough(self):
        result = _evaluate_pre_tool_use("Write", {"file_path": "src/main.py"}, "default")
        assert result is None

    def test_edit_workspace_file_allowed(self):
        result = _evaluate_pre_tool_use(
            "Edit",
            {"file_path": "/project/.claude/SuperchargeAI/memory/project/patterns.md"},
            "default",
        )
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_edit_non_workspace_passthrough(self):
        result = _evaluate_pre_tool_use(
            "Edit",
            {"file_path": "/project/src/app.py"},
            "default",
        )
        assert result is None

    def test_task_supercharge_agent_with_workspace_allowed(self):
        result = _evaluate_pre_tool_use(
            "Task",
            {
                "subagent_type": "supercharge-ai:code",
                "prompt": "Work in /home/user/project/.claude/SuperchargeAI/tasks/code/abc/",
            },
            "default",
        )
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_task_supercharge_agent_without_workspace_denied(self):
        result = _evaluate_pre_tool_use(
            "Task",
            {
                "subagent_type": "supercharge-ai:code",
                "prompt": "just do it",
            },
            "default",
        )
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_task_non_supercharge_agent_passthrough(self):
        result = _evaluate_pre_tool_use(
            "Task",
            {
                "subagent_type": "other-plugin:worker",
                "prompt": "something with /.claude/SuperchargeAI/ path",
            },
            "default",
        )
        assert result is None

    def test_unknown_tool_passthrough(self):
        result = _evaluate_pre_tool_use("Read", {"file_path": "/etc/passwd"}, "default")
        assert result is None


# ── background agent rejection ──────────────────────────────────────────────


class TestBackgroundAgentRejection:
    """Project-writing agents (code/document) are rejected when run in background
    without sufficient permissions (permission_mode not bypassPermissions/dontAsk)."""

    _WORKSPACE_PROMPT = "Work in /project/.claude/SuperchargeAI/tasks/code/abc/"

    def test_code_background_default_denied(self):
        result = _evaluate_pre_tool_use(
            "Task",
            {
                "subagent_type": "supercharge-ai:code",
                "prompt": self._WORKSPACE_PROMPT,
                "run_in_background": True,
            },
            "default",
        )
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "foreground" in result["hookSpecificOutput"]["permissionDecisionReason"]

    def test_document_background_default_denied(self):
        result = _evaluate_pre_tool_use(
            "Task",
            {
                "subagent_type": "supercharge-ai:document",
                "prompt": self._WORKSPACE_PROMPT,
                "run_in_background": True,
            },
            "default",
        )
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_code_background_bypass_allowed(self):
        result = _evaluate_pre_tool_use(
            "Task",
            {
                "subagent_type": "supercharge-ai:code",
                "prompt": self._WORKSPACE_PROMPT,
                "run_in_background": True,
            },
            "bypassPermissions",
        )
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_code_background_dontask_allowed(self):
        result = _evaluate_pre_tool_use(
            "Task",
            {
                "subagent_type": "supercharge-ai:code",
                "prompt": self._WORKSPACE_PROMPT,
                "run_in_background": True,
            },
            "dontAsk",
        )
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_code_foreground_default_allowed(self):
        """Foreground agents are not rejected regardless of permission mode."""
        result = _evaluate_pre_tool_use(
            "Task",
            {
                "subagent_type": "supercharge-ai:code",
                "prompt": self._WORKSPACE_PROMPT,
                "run_in_background": False,
            },
            "default",
        )
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_research_background_default_not_rejected(self):
        """Non-project-writing agents pass the background check."""
        result = _evaluate_pre_tool_use(
            "Task",
            {
                "subagent_type": "supercharge-ai:research",
                "prompt": self._WORKSPACE_PROMPT,
                "run_in_background": True,
            },
            "default",
        )
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_plan_background_default_not_rejected(self):
        result = _evaluate_pre_tool_use(
            "Task",
            {
                "subagent_type": "supercharge-ai:plan",
                "prompt": self._WORKSPACE_PROMPT,
                "run_in_background": True,
            },
            "default",
        )
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_code_background_accept_edits_denied(self):
        """acceptEdits still requires prompts for non-edit operations (Bash)."""
        result = _evaluate_pre_tool_use(
            "Task",
            {
                "subagent_type": "supercharge-ai:code",
                "prompt": self._WORKSPACE_PROMPT,
                "run_in_background": True,
            },
            "acceptEdits",
        )
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


# ── _add_user_permissions / _remove_user_permissions ────────────────────────


class TestUserPermissions:
    """Test permission management in settings.json."""

    def test_creates_settings_if_missing(self, tmp_path: Path):
        settings_path = tmp_path / ".claude" / "settings.json"
        added = _add_user_permissions(settings_path)

        assert len(added) == 20
        assert settings_path.exists()

        settings = json.loads(settings_path.read_text())
        assert settings["permissions"]["allow"] == [
            "Bash(supercharge *)",
            "Bash(cat)",
            "Bash(cat *)",
            "Bash(ls)",
            "Bash(ls *)",
            "Bash(find *)",
            "Bash(head *)",
            "Bash(tail *)",
            "Bash(echo *)",
            "Bash(wc *)",
            "Bash(diff *)",
            "Bash(stat *)",
            "Bash(pwd)",
            "Bash(which *)",
            "Bash(env)",
            "Bash(env *)",
            "Write(.claude/SuperchargeAI/**)",
            "Edit(.claude/SuperchargeAI/**)",
            "WebSearch",
            "WebFetch",
        ]

    def test_merges_without_destroying_existing(self, tmp_path: Path):
        settings_path = tmp_path / "settings.json"
        existing = {
            "permissions": {"allow": ["Bash(git *)"]},
            "other_key": True,
        }
        settings_path.write_text(json.dumps(existing))

        added = _add_user_permissions(settings_path)
        assert len(added) == 20

        settings = json.loads(settings_path.read_text())
        assert "Bash(git *)" in settings["permissions"]["allow"]
        assert "Bash(supercharge *)" in settings["permissions"]["allow"]
        assert settings["other_key"] is True

    def test_idempotent_on_second_run(self, tmp_path: Path):
        settings_path = tmp_path / "settings.json"

        first_added = _add_user_permissions(settings_path)
        assert len(first_added) == 20

        second_added = _add_user_permissions(settings_path)
        assert len(second_added) == 0

        settings = json.loads(settings_path.read_text())
        # No duplicates
        assert len(settings["permissions"]["allow"]) == 20

    def test_remove_only_removes_ours(self, tmp_path: Path):
        settings_path = tmp_path / "settings.json"
        settings = {
            "permissions": {
                "allow": [
                    "Bash(git *)",
                    "Bash(supercharge *)",
                    "Write(.claude/SuperchargeAI/**)",
                    "Edit(.claude/SuperchargeAI/**)",
                    "Read(src/**)",
                ]
            }
        }
        settings_path.write_text(json.dumps(settings))

        removed = _remove_user_permissions(settings_path)
        assert removed == 3

        result = json.loads(settings_path.read_text())
        assert result["permissions"]["allow"] == ["Bash(git *)", "Read(src/**)"]

    def test_remove_from_missing_file(self, tmp_path: Path):
        settings_path = tmp_path / "nonexistent.json"
        removed = _remove_user_permissions(settings_path)
        assert removed == 0

    def test_remove_when_none_present(self, tmp_path: Path):
        settings_path = tmp_path / "settings.json"
        settings = {"permissions": {"allow": ["Bash(git *)"]}}
        settings_path.write_text(json.dumps(settings))

        removed = _remove_user_permissions(settings_path)
        assert removed == 0


# ── B1: session_id and agent identity in hooks ──────────────────────────────


class TestHookSessionIdentity:
    """Test that hook_session_start captures session_id in output."""

    def _run_hook_session_start(self, input_data: dict, hook_dir: Path) -> str:
        """Run hook_session_start with mocked stdin/stdout and return stdout content."""
        stdin_data = json.dumps(input_data)
        stdout_capture = io.StringIO()

        # Create minimal prompt files so the hook emits something
        prompts_dir = hook_dir / "prompts"
        prompts_dir.mkdir(parents=True, exist_ok=True)
        (prompts_dir / "protocol.md").write_text("protocol content")
        (prompts_dir / "orchestrator.md").write_text("orchestrator content")

        with (
            patch("supercharge.hooks._hook_data_dir", return_value=hook_dir),
            patch("supercharge.hooks._check_version_sync", return_value=None),
            patch("supercharge.hooks._trigger_background_memory"),
            patch("supercharge.hooks._ensure_project_dir"),
            patch("sys.stdin", io.StringIO(stdin_data)),
            patch("sys.stdout", stdout_capture),
        ):
            # Call the underlying function directly (not via CliRunner)
            hook_session_start.callback()

        return stdout_capture.getvalue()

    def test_session_start_with_session_id(self, tmp_path: Path):
        """hook_session_start with session_id emits <session-identity> tag."""
        output = self._run_hook_session_start(
            {"session_id": "sess-abc-123", "cwd": "/tmp"},
            tmp_path,
        )
        data = json.loads(output)
        ctx = data["hookSpecificOutput"]["additionalContext"]
        assert 'session_id="sess-abc-123"' in ctx
        assert "<session-identity" in ctx

    def test_session_start_without_session_id(self, tmp_path: Path):
        """hook_session_start without session_id does NOT emit <session-identity> tag."""
        output = self._run_hook_session_start({"cwd": "/tmp"}, tmp_path)
        data = json.loads(output)
        ctx = data["hookSpecificOutput"]["additionalContext"]
        assert "<session-identity" not in ctx


class TestHookSubagentIdentity:
    """Test that hook_subagent_start captures agent identity in output."""

    def _run_hook_subagent_start(self, input_data: dict, hook_dir: Path) -> str:
        """Run hook_subagent_start with mocked stdin/stdout and return stdout content."""
        stdin_data = json.dumps(input_data)
        stdout_capture = io.StringIO()

        prompts_dir = hook_dir / "prompts"
        prompts_dir.mkdir(parents=True, exist_ok=True)
        (prompts_dir / "protocol.md").write_text("protocol content")
        (prompts_dir / "agent.md").write_text("agent content")

        with (
            patch("supercharge.hooks._hook_data_dir", return_value=hook_dir),
            patch("sys.stdin", io.StringIO(stdin_data)),
            patch("sys.stdout", stdout_capture),
        ):
            hook_subagent_start.callback()

        return stdout_capture.getvalue()

    def test_subagent_start_with_identity(self, tmp_path: Path):
        """hook_subagent_start with session_id/agent_id emits <agent-identity> tag."""
        output = self._run_hook_subagent_start(
            {
                "session_id": "sess-xyz",
                "agent_id": "agent-001",
                "agent_type": "supercharge-ai:code",
            },
            tmp_path,
        )
        data = json.loads(output)
        ctx = data["hookSpecificOutput"]["additionalContext"]
        assert "<agent-identity" in ctx
        assert 'session_id="sess-xyz"' in ctx
        assert 'agent_id="agent-001"' in ctx
        assert 'agent_type="supercharge-ai:code"' in ctx

    def test_subagent_start_without_identity(self, tmp_path: Path):
        """hook_subagent_start with empty ids does NOT emit <agent-identity> tag."""
        output = self._run_hook_subagent_start({}, tmp_path)
        data = json.loads(output)
        ctx = data["hookSpecificOutput"]["additionalContext"]
        assert "<agent-identity" not in ctx
