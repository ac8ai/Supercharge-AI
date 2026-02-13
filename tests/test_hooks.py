"""Tests for PreToolUse permission helpers and user permission management."""

from __future__ import annotations

import json
from pathlib import Path

from supercharge.cli import (
    _add_user_permissions,
    _evaluate_pre_tool_use,
    _remove_user_permissions,
)

# ── _evaluate_pre_tool_use ──────────────────────────────────────────────────


class TestEvaluatePreToolUse:
    """Test the PreToolUse decision logic directly."""

    def test_bash_supercharge_command_allowed(self):
        result = _evaluate_pre_tool_use("Bash", {"command": "supercharge task init code"})
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_bash_non_supercharge_passthrough(self):
        result = _evaluate_pre_tool_use("Bash", {"command": "rm -rf /"})
        assert result is None

    def test_write_workspace_file_allowed(self):
        result = _evaluate_pre_tool_use(
            "Write",
            {"file_path": "/home/user/project/.claude/SuperchargeAI/code/abc/task.md"},
        )
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_write_non_workspace_passthrough(self):
        result = _evaluate_pre_tool_use("Write", {"file_path": "src/main.py"})
        assert result is None

    def test_edit_workspace_file_allowed(self):
        result = _evaluate_pre_tool_use(
            "Edit",
            {"file_path": "/project/.claude/SuperchargeAI/memory/project/patterns.md"},
        )
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_edit_non_workspace_passthrough(self):
        result = _evaluate_pre_tool_use(
            "Edit",
            {"file_path": "/project/src/app.py"},
        )
        assert result is None

    def test_task_supercharge_agent_with_workspace_allowed(self):
        result = _evaluate_pre_tool_use(
            "Task",
            {
                "subagent_type": "supercharge-ai:code",
                "prompt": "Work in /home/user/project/.claude/SuperchargeAI/code/abc/",
            },
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
        )
        assert result is None

    def test_unknown_tool_passthrough(self):
        result = _evaluate_pre_tool_use("Read", {"file_path": "/etc/passwd"})
        assert result is None


# ── _add_user_permissions / _remove_user_permissions ────────────────────────


class TestUserPermissions:
    """Test permission management in settings.json."""

    def test_creates_settings_if_missing(self, tmp_path: Path):
        settings_path = tmp_path / ".claude" / "settings.json"
        added = _add_user_permissions(settings_path)

        assert len(added) == 3
        assert settings_path.exists()

        settings = json.loads(settings_path.read_text())
        assert settings["permissions"]["allow"] == [
            "Bash(supercharge *)",
            "Write(.claude/SuperchargeAI/**)",
            "Edit(.claude/SuperchargeAI/**)",
        ]

    def test_merges_without_destroying_existing(self, tmp_path: Path):
        settings_path = tmp_path / "settings.json"
        existing = {
            "permissions": {"allow": ["Bash(git *)"]},
            "other_key": True,
        }
        settings_path.write_text(json.dumps(existing))

        added = _add_user_permissions(settings_path)
        assert len(added) == 3

        settings = json.loads(settings_path.read_text())
        assert "Bash(git *)" in settings["permissions"]["allow"]
        assert "Bash(supercharge *)" in settings["permissions"]["allow"]
        assert settings["other_key"] is True

    def test_idempotent_on_second_run(self, tmp_path: Path):
        settings_path = tmp_path / "settings.json"

        first_added = _add_user_permissions(settings_path)
        assert len(first_added) == 3

        second_added = _add_user_permissions(settings_path)
        assert len(second_added) == 0

        settings = json.loads(settings_path.read_text())
        # No duplicates
        assert len(settings["permissions"]["allow"]) == 3

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
