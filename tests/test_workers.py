"""Tests for worker spawning, focusing on the memory agent entry point."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import claude_agent_sdk
import pytest

from supercharge.permissions import _make_can_use_tool
from supercharge.workers import _build_options, _memory_agent_run


# ── _memory_agent_run: streaming mode bug ──────────────────────────────────


class TestMemoryAgentRun:
    """Regression tests for _memory_agent_run.

    The memory agent previously crashed with:
        ValueError: can_use_tool callback requires streaming mode.
        Please provide prompt as an AsyncIterable instead of a string.

    Root cause: _build_options() was called with worker_id=task_uuid, which
    set can_use_tool on the options. The Agent SDK's query() then rejected
    the string prompt because can_use_tool requires streaming mode (AsyncIterable).

    The memory agent runs with bypassPermissions and doesn't need the
    can_use_tool write-scope callback.
    """

    def test_build_options_sets_can_use_tool_for_memory_worker(self, tmp_path: Path):
        """Precondition: _build_options sets can_use_tool when worker_id is not None.

        This confirms the mechanism that causes the bug. The fix must clear
        can_use_tool before calling query() with a string prompt.
        """
        task_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        task_dir = tmp_path / "memory" / task_uuid
        task_dir.mkdir(parents=True)

        with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(tmp_path)}):
            options = _build_options(
                task_dir,
                remaining_depth=1,
                max_turns=50,
                model=None,
                agent_type="memory",
                worker_id=task_uuid,
            )

        assert options.can_use_tool is not None, (
            "Precondition: _build_options sets can_use_tool when worker_id is not None"
        )

    @pytest.mark.anyio
    async def test_memory_agent_run_clears_can_use_tool(self, tmp_path: Path):
        """_memory_agent_run must clear can_use_tool before calling query().

        The Agent SDK raises ValueError when can_use_tool is set and prompt
        is a string (not AsyncIterable). The memory agent uses bypassPermissions
        and doesn't need write-scope enforcement, so can_use_tool must be None.
        """
        task_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        task_dir = tmp_path / "memory" / task_uuid
        task_dir.mkdir(parents=True)
        (task_dir / "task.md").write_text("# Task\nHarvest memory.")

        captured_options = []

        async def mock_query(*, prompt, options):
            captured_options.append(options)
            from claude_agent_sdk import ResultMessage

            yield ResultMessage(
                subtype="result",
                duration_ms=100,
                duration_api_ms=80,
                is_error=False,
                num_turns=1,
                session_id="test",
                result="done",
            )

        # patch.object on the claude_agent_sdk package is needed because
        # _memory_agent_run imports query locally (from claude_agent_sdk import query),
        # and the query submodule has the same name causing namespace collision.
        with (
            patch("supercharge.workers._find_task_dir", return_value=task_dir),
            patch.object(claude_agent_sdk, "query", mock_query),
            patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(tmp_path)}),
        ):
            await _memory_agent_run(task_uuid)

        assert len(captured_options) == 1
        assert captured_options[0].can_use_tool is None, (
            "can_use_tool must be None when using a string prompt. "
            "The Agent SDK requires AsyncIterable prompts when can_use_tool is set."
        )

    @pytest.mark.anyio
    async def test_memory_agent_run_uses_bypass_permissions(self, tmp_path: Path):
        """_memory_agent_run sets permission_mode to bypassPermissions."""
        task_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        task_dir = tmp_path / "memory" / task_uuid
        task_dir.mkdir(parents=True)
        (task_dir / "task.md").write_text("# Task\nHarvest memory.")

        captured_options = []

        async def mock_query(*, prompt, options):  # noqa: ARG001
            captured_options.append(options)
            from claude_agent_sdk import ResultMessage

            yield ResultMessage(
                subtype="result",
                duration_ms=100,
                duration_api_ms=80,
                is_error=False,
                num_turns=1,
                session_id="test",
                result="done",
            )

        with (
            patch("supercharge.workers._find_task_dir", return_value=task_dir),
            patch.object(claude_agent_sdk, "query", mock_query),
            patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(tmp_path)}),
        ):
            await _memory_agent_run(task_uuid)

        assert captured_options[0].permission_mode == "bypassPermissions"

    @pytest.mark.anyio
    async def test_memory_agent_run_task_not_found(self, tmp_path: Path):
        """_memory_agent_run returns early when task dir is not found."""
        with patch("supercharge.workers._find_task_dir", return_value=None):
            await _memory_agent_run("nonexistent-uuid")

    @pytest.mark.anyio
    async def test_memory_agent_run_task_md_missing(self, tmp_path: Path):
        """_memory_agent_run returns early when task.md is missing."""
        task_dir = tmp_path / "memory" / "some-uuid"
        task_dir.mkdir(parents=True)

        with patch("supercharge.workers._find_task_dir", return_value=task_dir):
            await _memory_agent_run("some-uuid")


# ── _build_options: can_use_tool behavior ──────────────────────────────────


class TestBuildOptions:
    """Test _build_options tool permission callback behavior."""

    def test_deep_worker_gets_can_use_tool(self, tmp_path: Path):
        """Deep workers (worker_id set) get a can_use_tool callback."""
        with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(tmp_path)}):
            options = _build_options(
                tmp_path,
                remaining_depth=2,
                max_turns=None,
                model=None,
                agent_type="code",
                worker_id="some-worker-id",
            )
        assert options.can_use_tool is not None

    def test_fast_worker_no_can_use_tool(self, tmp_path: Path):
        """Fast workers (worker_id None) do not get a can_use_tool callback."""
        with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(tmp_path)}):
            options = _build_options(
                tmp_path,
                remaining_depth=1,
                max_turns=None,
                model=None,
                agent_type="code",
                worker_id=None,
            )
        assert options.can_use_tool is None


# ── Regression: context-scoped agents can write task files ─────────────────


class TestContextWriteScope:
    """Regression tests for write_scope='context' allowing task directory writes.

    Previously, 'context' restricted writes to only the worker context file
    (workers/{worker_id}.md), blocking result.md and notes.md in the task root.
    The fix redefines 'context' to allow writes anywhere in the task directory.
    """

    def _make_callback(self, tmp_path, agent_type="research"):
        task_dir = tmp_path / agent_type / "task-001"
        task_dir.mkdir(parents=True)
        (task_dir / "workers").mkdir()
        worker_id = "worker-abc"
        cb = _make_can_use_tool(
            agent_type=agent_type,
            task_dir=task_dir,
            worker_id=worker_id,
            project_root=str(tmp_path),
        )
        return cb, task_dir, worker_id

    @pytest.mark.anyio
    async def test_research_worker_can_write_result_md(self, tmp_path: Path):
        """Regression: research worker can write result.md in task dir."""
        from claude_agent_sdk.types import PermissionResultAllow

        cb, task_dir, _ = self._make_callback(tmp_path)
        result = await cb("Write", {"file_path": str(task_dir / "result.md")}, {})
        assert isinstance(result, PermissionResultAllow)

    @pytest.mark.anyio
    async def test_research_worker_can_write_notes_md(self, tmp_path: Path):
        """Regression: research worker can write notes.md in task dir."""
        from claude_agent_sdk.types import PermissionResultAllow

        cb, task_dir, _ = self._make_callback(tmp_path)
        result = await cb("Write", {"file_path": str(task_dir / "notes.md")}, {})
        assert isinstance(result, PermissionResultAllow)

    @pytest.mark.anyio
    async def test_research_worker_can_write_worker_context(self, tmp_path: Path):
        """Context-scoped agents can still write to worker context file."""
        from claude_agent_sdk.types import PermissionResultAllow

        cb, task_dir, worker_id = self._make_callback(tmp_path)
        worker_file = str(task_dir / "workers" / f"{worker_id}.md")
        result = await cb("Write", {"file_path": worker_file}, {})
        assert isinstance(result, PermissionResultAllow)

    @pytest.mark.anyio
    async def test_research_worker_denied_outside_task_dir(self, tmp_path: Path):
        """Context-scoped agents cannot write outside their task directory."""
        from claude_agent_sdk.types import PermissionResultDeny

        cb, _, _ = self._make_callback(tmp_path)
        result = await cb("Write", {"file_path": str(tmp_path / "src" / "main.py")}, {})
        assert isinstance(result, PermissionResultDeny)

    @pytest.mark.anyio
    async def test_plan_worker_can_write_result_md(self, tmp_path: Path):
        """Same fix applies to plan agent (also context-scoped)."""
        from claude_agent_sdk.types import PermissionResultAllow

        cb, task_dir, _ = self._make_callback(tmp_path, agent_type="plan")
        result = await cb("Write", {"file_path": str(task_dir / "result.md")}, {})
        assert isinstance(result, PermissionResultAllow)

    @pytest.mark.anyio
    async def test_consistency_worker_can_write_result_md(self, tmp_path: Path):
        """Same fix applies to consistency agent (also context-scoped)."""
        from claude_agent_sdk.types import PermissionResultAllow

        cb, task_dir, _ = self._make_callback(tmp_path, agent_type="consistency")
        result = await cb("Write", {"file_path": str(task_dir / "result.md")}, {})
        assert isinstance(result, PermissionResultAllow)

    @pytest.mark.anyio
    async def test_review_worker_can_write_result_md(self, tmp_path: Path):
        """Same fix applies to review agent (also context-scoped)."""
        from claude_agent_sdk.types import PermissionResultAllow

        cb, task_dir, _ = self._make_callback(tmp_path, agent_type="review")
        result = await cb("Write", {"file_path": str(task_dir / "result.md")}, {})
        assert isinstance(result, PermissionResultAllow)
