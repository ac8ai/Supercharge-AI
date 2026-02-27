"""Tests for worker spawning, focusing on the memory agent entry point."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import claude_agent_sdk
import pytest

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
