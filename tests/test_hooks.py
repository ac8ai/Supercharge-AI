"""Tests for PreToolUse permission helpers, user permission management, and hook identity."""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from supercharge.hooks import (
    _evaluate_pre_tool_use,
    _has_project_write_permissions,
    _load_settings_allowlist,
    _reset_allowlist_cache,
    _tool_matches_pattern,
    hook_session_start,
    hook_subagent_start,
)
from supercharge.permissions import _add_user_permissions, _remove_user_permissions

# ── _evaluate_pre_tool_use ──────────────────────────────────────────────────


class TestEvaluatePreToolUse:
    """Test the PreToolUse decision logic directly.

    Uses a temporary user config dir to avoid loading real ~/.claude/settings.json
    permissions (which may include SuperchargeAI entries added by ``supercharge init``).
    """

    @pytest.fixture(autouse=True)
    def _isolate_allowlist(self, tmp_path):
        _reset_allowlist_cache()
        with patch("supercharge.paths._user_config_dir", return_value=tmp_path / "no_user"):
            with patch.dict("os.environ", {"CLAUDE_PROJECT_DIR": ""}, clear=False):
                yield
        _reset_allowlist_cache()

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

    def test_bash_dangerous_command_passthrough(self):
        """Dangerous Bash commands pass through (user decides, not auto-denied).

        The hook can't distinguish orchestrator from subagent, so dangerous
        patterns are passed through to user approval. Workers are still
        hard-blocked by the can_use_tool callback.
        """
        dangerous = [
            "git push origin main",
            "rm -rf /",
            "echo hello > file.txt",
            "git commit -m 'test'",
        ]
        for cmd in dangerous:
            result = _evaluate_pre_tool_use("Bash", {"command": cmd}, "default")
            assert result is None, f"Should passthrough: {cmd}"

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

    def test_write_workspace_relative_path_allowed(self):
        """Relative paths like .claude/SuperchargeAI/... must also be auto-approved."""
        result = _evaluate_pre_tool_use(
            "Write",
            {"file_path": ".claude/SuperchargeAI/tasks/code/abc123/notes.md"},
            "default",
        )
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_edit_workspace_relative_path_allowed(self):
        """Relative paths like .claude/SuperchargeAI/... must also be auto-approved."""
        result = _evaluate_pre_tool_use(
            "Edit",
            {"file_path": ".claude/SuperchargeAI/tasks/plan/def456/task.md"},
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
        """Non-writing agent (plan) with workspace path is allowed."""
        result = _evaluate_pre_tool_use(
            "Task",
            {
                "subagent_type": "supercharge-ai:plan",
                "prompt": "Work in /home/user/project/.claude/SuperchargeAI/tasks/plan/abc/",
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

    def test_read_always_allowed(self):
        result = _evaluate_pre_tool_use("Read", {"file_path": "/etc/passwd"}, "default")
        assert result is not None
        decision = result["hookSpecificOutput"]["permissionDecision"]
        assert decision == "allow"

    def test_unknown_tool_passthrough(self):
        result = _evaluate_pre_tool_use("Grep", {"pattern": "foo"}, "default")
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

    def test_code_foreground_default_no_perms_denied(self):
        """Foreground code agents denied in default mode without Write/Edit permissions.

        This is the core fix for BUG_REPORT-permissions.md — subagents can't inherit
        settings.json permissions, so without allowlist coverage they fail silently.
        """
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
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "settings.json" in result["hookSpecificOutput"]["permissionDecisionReason"]

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
        """acceptEdits still requires prompts for non-edit operations (Bash) in background."""
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

    def test_code_foreground_accept_edits_allowed(self):
        """acceptEdits auto-approves Write/Edit, so foreground code agents work."""
        result = _evaluate_pre_tool_use(
            "Task",
            {
                "subagent_type": "supercharge-ai:code",
                "prompt": self._WORKSPACE_PROMPT,
                "run_in_background": False,
            },
            "acceptEdits",
        )
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"


# ── foreground agent permission gap (bug report) ────────────────────────────


class TestForegroundPermissionGap:
    """Foreground project-writing agents are denied when settings allowlist
    doesn't cover Write/Edit for project files — the core bug from the
    BUG_REPORT-permissions.md report. Subagents can't inherit settings.json
    permissions, so without allowlist entries our hook can't approve writes."""

    _WORKSPACE_PROMPT = "Work in /project/.claude/SuperchargeAI/tasks/code/abc/"

    def setup_method(self):
        _reset_allowlist_cache()

    def teardown_method(self):
        _reset_allowlist_cache()

    def _with_allowlist(self, patterns: list[str]):
        return patch("supercharge.hooks._load_settings_allowlist", return_value=patterns)

    def test_code_foreground_no_write_perms_denied(self):
        """Code agent denied when allowlist has no Write/Edit for project files."""
        with self._with_allowlist(["Write(.claude/SuperchargeAI/**)", "Edit(.claude/SuperchargeAI/**)"]):
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
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "settings.json" in result["hookSpecificOutput"]["permissionDecisionReason"]

    def test_document_foreground_no_write_perms_denied(self):
        """Document agent denied when allowlist has no Write/Edit for project files."""
        with self._with_allowlist([]):
            result = _evaluate_pre_tool_use(
                "Task",
                {
                    "subagent_type": "supercharge-ai:document",
                    "prompt": self._WORKSPACE_PROMPT,
                    "run_in_background": False,
                },
                "default",
            )
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_code_foreground_with_bare_write_edit_allowed(self):
        """Code agent allowed when allowlist has bare Write and Edit."""
        with self._with_allowlist(["Write", "Edit"]):
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

    def test_code_foreground_with_glob_write_edit_allowed(self):
        """Code agent allowed when allowlist has project-covering globs."""
        with self._with_allowlist(["Write(src/**)", "Edit(src/**)"]):
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

    def test_code_foreground_only_write_no_edit_denied(self):
        """Denied when allowlist has Write but not Edit."""
        with self._with_allowlist(["Write"]):
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
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_research_foreground_no_write_perms_allowed(self):
        """Research agent doesn't need Write/Edit for project files — not blocked."""
        with self._with_allowlist([]):
            result = _evaluate_pre_tool_use(
                "Task",
                {
                    "subagent_type": "supercharge-ai:research",
                    "prompt": self._WORKSPACE_PROMPT,
                    "run_in_background": False,
                },
                "default",
            )
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_code_foreground_bypass_permissions_allowed(self):
        """bypassPermissions skips the allowlist check entirely."""
        with self._with_allowlist([]):
            result = _evaluate_pre_tool_use(
                "Task",
                {
                    "subagent_type": "supercharge-ai:code",
                    "prompt": self._WORKSPACE_PROMPT,
                    "run_in_background": False,
                },
                "bypassPermissions",
            )
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_code_foreground_dontask_allowed(self):
        """dontAsk mode skips the allowlist check."""
        with self._with_allowlist([]):
            result = _evaluate_pre_tool_use(
                "Task",
                {
                    "subagent_type": "supercharge-ai:code",
                    "prompt": self._WORKSPACE_PROMPT,
                    "run_in_background": False,
                },
                "dontAsk",
            )
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"


# ── _has_project_write_permissions ──────────────────────────────────────────


class TestHasProjectWritePermissions:
    """Test the allowlist check for project-level Write/Edit coverage."""

    def setup_method(self):
        _reset_allowlist_cache()

    def teardown_method(self):
        _reset_allowlist_cache()

    def _with_allowlist(self, patterns: list[str]):
        return patch("supercharge.hooks._load_settings_allowlist", return_value=patterns)

    def test_bare_write_edit(self):
        with self._with_allowlist(["Write", "Edit"]):
            assert _has_project_write_permissions() is True

    def test_only_workspace_globs(self):
        with self._with_allowlist(["Write(.claude/SuperchargeAI/**)", "Edit(.claude/SuperchargeAI/**)"]):
            assert _has_project_write_permissions() is False

    def test_project_globs(self):
        with self._with_allowlist(["Write(src/**)", "Edit(src/**)"]):
            assert _has_project_write_permissions() is True

    def test_empty_allowlist(self):
        with self._with_allowlist([]):
            assert _has_project_write_permissions() is False

    def test_write_only(self):
        with self._with_allowlist(["Write"]):
            assert _has_project_write_permissions() is False

    def test_edit_only(self):
        with self._with_allowlist(["Edit"]):
            assert _has_project_write_permissions() is False

    def test_mixed_workspace_and_project(self):
        """Write covers project, Edit only covers workspace → False."""
        with self._with_allowlist(["Write", "Edit(.claude/SuperchargeAI/**)"]):
            assert _has_project_write_permissions() is False


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


# ── _tool_matches_pattern ───────────────────────────────────────────────────


class TestToolMatchesPattern:
    """Test individual pattern matching against tool calls."""

    # -- Bash patterns --

    def test_bash_colon_wildcard_match(self):
        assert _tool_matches_pattern("Bash", {"command": "git status"}, "Bash(git:*)")

    def test_bash_colon_wildcard_no_match(self):
        assert not _tool_matches_pattern("Bash", {"command": "ls -la"}, "Bash(git:*)")

    def test_bash_space_wildcard_match(self):
        assert _tool_matches_pattern("Bash", {"command": "python3 script.py"}, "Bash(python3 *)")

    def test_bash_space_wildcard_no_match(self):
        assert not _tool_matches_pattern("Bash", {"command": "python2 script.py"}, "Bash(python3 *)")

    def test_bash_exact_match(self):
        assert _tool_matches_pattern("Bash", {"command": "pwd"}, "Bash(pwd)")

    def test_bash_exact_no_match(self):
        assert not _tool_matches_pattern("Bash", {"command": "pwd -L"}, "Bash(pwd)")

    def test_bash_bare_tool_matches_all(self):
        assert _tool_matches_pattern("Bash", {"command": "anything"}, "Bash")

    # -- Write/Edit patterns --

    def test_write_glob_match(self):
        assert _tool_matches_pattern(
            "Write",
            {"file_path": ".claude/SuperchargeAI/tasks/code/abc/task.md"},
            "Write(.claude/SuperchargeAI/**)",
        )

    def test_write_glob_no_match(self):
        assert not _tool_matches_pattern(
            "Write",
            {"file_path": "src/main.py"},
            "Write(.claude/SuperchargeAI/**)",
        )

    def test_edit_bare_matches_all(self):
        assert _tool_matches_pattern("Edit", {"file_path": "src/main.py"}, "Edit")

    def test_write_bare_matches_all(self):
        assert _tool_matches_pattern("Write", {"file_path": "src/main.py"}, "Write")

    # -- WebFetch patterns --

    def test_webfetch_domain_match(self):
        assert _tool_matches_pattern(
            "WebFetch",
            {"url": "https://docs.claude.com/api/v1"},
            "WebFetch(domain:docs.claude.com)",
        )

    def test_webfetch_domain_no_match(self):
        assert not _tool_matches_pattern(
            "WebFetch",
            {"url": "https://evil.com/steal"},
            "WebFetch(domain:docs.claude.com)",
        )

    def test_webfetch_bare_matches_all(self):
        assert _tool_matches_pattern(
            "WebFetch",
            {"url": "https://anything.com"},
            "WebFetch",
        )

    # -- WebSearch patterns --

    def test_websearch_bare_match(self):
        assert _tool_matches_pattern("WebSearch", {}, "WebSearch")

    # -- Cross-tool mismatch --

    def test_wrong_tool_name(self):
        assert not _tool_matches_pattern("Write", {"file_path": "x"}, "Bash(git:*)")

    def test_invalid_pattern(self):
        assert not _tool_matches_pattern("Bash", {"command": "ls"}, "")


# ── _load_settings_allowlist ────────────────────────────────────────────────


class TestLoadSettingsAllowlist:
    """Test settings allowlist loading and caching."""

    def setup_method(self):
        _reset_allowlist_cache()

    def teardown_method(self):
        _reset_allowlist_cache()

    def test_loads_from_project_settings(self, tmp_path: Path):
        """Loads allowlist from project .claude/settings.json."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        settings = {"permissions": {"allow": ["Bash(git:*)", "WebSearch"]}}
        (claude_dir / "settings.json").write_text(json.dumps(settings))

        with patch.dict("os.environ", {"CLAUDE_PROJECT_DIR": str(tmp_path)}):
            with patch("supercharge.paths._user_config_dir", return_value=tmp_path / "no_user"):
                result = _load_settings_allowlist()

        assert "Bash(git:*)" in result
        assert "WebSearch" in result

    def test_loads_from_user_settings(self, tmp_path: Path):
        """Loads allowlist from user-level settings.json."""
        settings = {"permissions": {"allow": ["Write", "Edit"]}}
        (tmp_path / "settings.json").write_text(json.dumps(settings))

        with patch.dict("os.environ", {"CLAUDE_PROJECT_DIR": ""}, clear=False):
            with patch("supercharge.paths._user_config_dir", return_value=tmp_path):
                result = _load_settings_allowlist()

        assert "Write" in result
        assert "Edit" in result

    def test_merges_all_sources(self, tmp_path: Path):
        """Merges allowlists from user and project settings."""
        user_dir = tmp_path / "user"
        user_dir.mkdir()
        (user_dir / "settings.json").write_text(
            json.dumps({"permissions": {"allow": ["WebSearch"]}})
        )

        project_dir = tmp_path / "project"
        claude_dir = project_dir / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "settings.json").write_text(
            json.dumps({"permissions": {"allow": ["Bash(git:*)"]}})
        )

        with patch.dict("os.environ", {"CLAUDE_PROJECT_DIR": str(project_dir)}):
            with patch("supercharge.paths._user_config_dir", return_value=user_dir):
                result = _load_settings_allowlist()

        assert "WebSearch" in result
        assert "Bash(git:*)" in result

    def test_deduplicates(self, tmp_path: Path):
        """Does not add duplicate entries."""
        user_dir = tmp_path / "user"
        user_dir.mkdir()
        (user_dir / "settings.json").write_text(
            json.dumps({"permissions": {"allow": ["WebSearch"]}})
        )
        (user_dir / "settings.local.json").write_text(
            json.dumps({"permissions": {"allow": ["WebSearch"]}})
        )

        with patch.dict("os.environ", {"CLAUDE_PROJECT_DIR": ""}, clear=False):
            with patch("supercharge.paths._user_config_dir", return_value=user_dir):
                result = _load_settings_allowlist()

        assert result.count("WebSearch") == 1

    def test_caches_result(self, tmp_path: Path):
        """Second call returns cached result without re-reading files."""
        (tmp_path / "settings.json").write_text(
            json.dumps({"permissions": {"allow": ["WebSearch"]}})
        )

        with patch.dict("os.environ", {"CLAUDE_PROJECT_DIR": ""}, clear=False):
            with patch("supercharge.paths._user_config_dir", return_value=tmp_path):
                first = _load_settings_allowlist()

        # Modify the file — should still get cached result
        (tmp_path / "settings.json").write_text(
            json.dumps({"permissions": {"allow": ["Bash(rm:*)"]}})
        )
        second = _load_settings_allowlist()
        assert first is second
        assert "WebSearch" in second

    def test_handles_missing_files(self, tmp_path: Path):
        """Returns empty list when no settings files exist."""
        with patch.dict("os.environ", {"CLAUDE_PROJECT_DIR": str(tmp_path)}):
            with patch("supercharge.paths._user_config_dir", return_value=tmp_path / "nope"):
                result = _load_settings_allowlist()

        assert result == []

    def test_handles_malformed_json(self, tmp_path: Path):
        """Skips files with invalid JSON."""
        (tmp_path / "settings.json").write_text("not json {{{")

        with patch.dict("os.environ", {"CLAUDE_PROJECT_DIR": ""}, clear=False):
            with patch("supercharge.paths._user_config_dir", return_value=tmp_path):
                result = _load_settings_allowlist()

        assert result == []


# ── Settings allowlist integration with _evaluate_pre_tool_use ──────────────


class TestAllowlistIntegration:
    """Test that _evaluate_pre_tool_use checks settings allowlists."""

    def setup_method(self):
        _reset_allowlist_cache()

    def teardown_method(self):
        _reset_allowlist_cache()

    def _with_allowlist(self, patterns: list[str]):
        """Patch _load_settings_allowlist to return given patterns."""
        return patch("supercharge.hooks._load_settings_allowlist", return_value=patterns)

    def test_bash_allowed_by_allowlist(self):
        """Bash command matching allowlist is auto-approved."""
        with self._with_allowlist(["Bash(git:*)"]):
            result = _evaluate_pre_tool_use("Bash", {"command": "git status"}, "default")
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert "allowlist" in result["hookSpecificOutput"]["permissionDecisionReason"]

    def test_bash_not_in_allowlist_passthrough(self):
        """Bash command not in allowlist still passes through."""
        with self._with_allowlist(["Bash(git:*)"]):
            result = _evaluate_pre_tool_use("Bash", {"command": "rm -rf /"}, "default")
        assert result is None

    def test_write_allowed_by_allowlist(self):
        """Write to file matching allowlist glob is auto-approved."""
        with self._with_allowlist(["Write"]):
            result = _evaluate_pre_tool_use("Write", {"file_path": "src/main.py"}, "default")
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_edit_allowed_by_allowlist(self):
        """Edit matching allowlist is auto-approved."""
        with self._with_allowlist(["Edit"]):
            result = _evaluate_pre_tool_use("Edit", {"file_path": "src/app.py"}, "default")
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_workspace_write_still_handled_first(self):
        """SuperchargeAI workspace files are still handled by workspace check, not allowlist."""
        with self._with_allowlist([]):
            result = _evaluate_pre_tool_use(
                "Write",
                {"file_path": "/project/.claude/SuperchargeAI/tasks/code/abc/task.md"},
                "default",
            )
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert "workspace" in result["hookSpecificOutput"]["permissionDecisionReason"]

    def test_websearch_allowed_by_allowlist(self):
        """WebSearch matching allowlist is auto-approved."""
        with self._with_allowlist(["WebSearch"]):
            result = _evaluate_pre_tool_use("WebSearch", {}, "default")
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_webfetch_domain_allowed_by_allowlist(self):
        """WebFetch with matching domain is auto-approved."""
        with self._with_allowlist(["WebFetch(domain:docs.claude.com)"]):
            result = _evaluate_pre_tool_use(
                "WebFetch", {"url": "https://docs.claude.com/api"}, "default"
            )
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_webfetch_wrong_domain_passthrough(self):
        """WebFetch with non-matching domain passes through."""
        with self._with_allowlist(["WebFetch(domain:docs.claude.com)"]):
            result = _evaluate_pre_tool_use(
                "WebFetch", {"url": "https://evil.com/steal"}, "default"
            )
        assert result is None

    def test_empty_allowlist_passthrough(self):
        """With empty allowlist, non-workspace tools pass through."""
        with self._with_allowlist([]):
            result = _evaluate_pre_tool_use("Write", {"file_path": "src/main.py"}, "default")
        assert result is None

    def test_supercharge_bash_still_handled_first(self):
        """Supercharge CLI commands are still handled by the explicit check, not allowlist."""
        with self._with_allowlist([]):
            result = _evaluate_pre_tool_use(
                "Bash", {"command": "supercharge task init code"}, "default"
            )
        assert result is not None
        assert "supercharge CLI" in result["hookSpecificOutput"]["permissionDecisionReason"]
