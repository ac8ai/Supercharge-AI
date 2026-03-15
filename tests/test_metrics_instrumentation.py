"""Tests for _emit() instrumentation across SuperchargeAI modules.

TDD: these tests are written first, then _emit() calls are added to each module.
Each test monkeypatches _emit at the module's import location and verifies the
correct event_type and fields are emitted.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import claude_agent_sdk
import pytest

from click.testing import CliRunner


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_emit_spy():
    """Return a spy callable that records all _emit() calls."""
    calls: list[tuple[str, dict]] = []

    def spy(event_type: str, **kwargs: str) -> None:
        calls.append((event_type, kwargs))

    return spy, calls


# ── hook_session_start ───────────────────────────────────────────────────────


class TestHookSessionStartEmit:
    """hook_session_start emits session_start with session_id."""

    def _run(self, input_data: dict, hook_dir: Path):
        from supercharge.hooks import hook_session_start

        spy, calls = _make_emit_spy()
        stdin_data = json.dumps(input_data)
        stdout_capture = io.StringIO()

        prompts_dir = hook_dir / "prompts"
        prompts_dir.mkdir(parents=True, exist_ok=True)
        (prompts_dir / "protocol.md").write_text("protocol")
        (prompts_dir / "orchestrator.md").write_text("orchestrator")

        with (
            patch("supercharge.hooks._hook_data_dir", return_value=hook_dir),
            patch("supercharge.hooks._check_version_sync", return_value=None),
            patch("supercharge.hooks._trigger_background_memory"),
            patch("supercharge.hooks._ensure_project_dir"),
            patch("supercharge.hooks._emit", spy),
            patch("sys.stdin", io.StringIO(stdin_data)),
            patch("sys.stdout", stdout_capture),
        ):
            hook_session_start.callback()

        return calls, stdout_capture.getvalue()

    def test_emits_session_start(self, tmp_path: Path):
        calls, output = self._run(
            {"session_id": "sess-123", "cwd": "/tmp"},
            tmp_path,
        )
        assert len(calls) == 1
        event_type, kwargs = calls[0]
        assert event_type == "session_start"
        assert kwargs["session_id"] == "sess-123"

    def test_session_start_detail_has_source(self, tmp_path: Path):
        """detail field should contain the source from input_data."""
        calls, _ = self._run(
            {"session_id": "sess-123", "source": "vscode", "cwd": "/tmp"},
            tmp_path,
        )
        assert len(calls) == 1
        _, kwargs = calls[0]
        assert "source" in kwargs.get("detail", "") or kwargs.get("detail", "") == "vscode"

    def test_still_emits_hook_output(self, tmp_path: Path):
        """_emit should not interfere with normal hook JSON output."""
        _, output = self._run(
            {"session_id": "sess-123", "cwd": "/tmp"},
            tmp_path,
        )
        data = json.loads(output)
        assert "hookSpecificOutput" in data


# ── hook_subagent_start ──────────────────────────────────────────────────────


class TestHookSubagentStartEmit:
    """hook_subagent_start emits subagent_start with identity fields."""

    def _run(self, input_data: dict, hook_dir: Path):
        from supercharge.hooks import hook_subagent_start

        spy, calls = _make_emit_spy()
        stdin_data = json.dumps(input_data)
        stdout_capture = io.StringIO()

        prompts_dir = hook_dir / "prompts"
        prompts_dir.mkdir(parents=True, exist_ok=True)
        (prompts_dir / "protocol.md").write_text("protocol")
        (prompts_dir / "agent.md").write_text("agent")

        with (
            patch("supercharge.hooks._hook_data_dir", return_value=hook_dir),
            patch("supercharge.hooks._emit", spy),
            patch("sys.stdin", io.StringIO(stdin_data)),
            patch("sys.stdout", stdout_capture),
        ):
            hook_subagent_start.callback()

        return calls

    def test_emits_subagent_start(self, tmp_path: Path):
        calls = self._run(
            {
                "session_id": "sess-xyz",
                "agent_id": "agent-001",
                "agent_type": "supercharge-ai:code",
            },
            tmp_path,
        )
        assert len(calls) == 1
        event_type, kwargs = calls[0]
        assert event_type == "subagent_start"
        assert kwargs["session_id"] == "sess-xyz"
        assert kwargs["agent_id"] == "agent-001"
        assert kwargs["agent_type"] == "supercharge-ai:code"


# ── hook_pre_tool_use ────────────────────────────────────────────────────────


class TestHookPreToolUseEmit:
    """hook_pre_tool_use emits tool_use with tool_name."""

    def _run(self, input_data: dict):
        from supercharge.hooks import hook_pre_tool_use

        spy, calls = _make_emit_spy()
        stdin_data = json.dumps(input_data)
        stdout_capture = io.StringIO()

        with (
            patch("supercharge.hooks._emit", spy),
            patch("sys.stdin", io.StringIO(stdin_data)),
            patch("sys.stdout", stdout_capture),
        ):
            hook_pre_tool_use.callback()

        return calls

    def test_emits_tool_use_for_allowed(self):
        """Emits tool_use when the tool call is allowed (Bash: supercharge command)."""
        calls = self._run(
            {
                "session_id": "sess-abc",
                "tool_name": "Bash",
                "tool_input": {"command": "supercharge task init code"},
                "permission_mode": "default",
            },
        )
        assert len(calls) == 1
        event_type, kwargs = calls[0]
        assert event_type == "tool_use"
        assert kwargs["session_id"] == "sess-abc"
        assert kwargs["tool_name"] == "Bash"

    def test_emits_tool_use_for_passthrough(self):
        """Emits tool_use even for pass-through tools (result is None)."""
        calls = self._run(
            {
                "session_id": "sess-abc",
                "tool_name": "Read",
                "tool_input": {"file_path": "/some/file"},
                "permission_mode": "default",
            },
        )
        assert len(calls) == 1
        event_type, kwargs = calls[0]
        assert event_type == "tool_use"
        assert kwargs["tool_name"] == "Read"

    def test_detail_has_tool_input_summary(self):
        """detail field should contain a JSON summary of tool_input."""
        calls = self._run(
            {
                "session_id": "sess-abc",
                "tool_name": "Write",
                "tool_input": {"file_path": "/project/src/main.py", "content": "x" * 1000},
                "permission_mode": "default",
            },
        )
        _, kwargs = calls[0]
        detail = kwargs.get("detail", "")
        # detail should be a JSON string with some tool_input info
        assert detail  # non-empty


# ── task_init CLI ────────────────────────────────────────────────────────────


class TestTaskInitEmit:
    """task_init emits task_init with task_uuid and agent_type."""

    @staticmethod
    def _mock_copy_template(name, dest):
        """Create a minimal file to satisfy task_init's read_text() call."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(f"# {name}\n")

    def test_emits_task_init(self, tmp_path: Path):
        from supercharge.cli import supercharge

        spy, calls = _make_emit_spy()
        runner = CliRunner()

        with (
            patch("supercharge.cli._task_root", return_value=tmp_path / "tasks"),
            patch("supercharge.cli._copy_template", side_effect=self._mock_copy_template),
            patch("supercharge.cli._emit", spy),
        ):
            (tmp_path / "tasks" / "code").mkdir(parents=True, exist_ok=True)
            result = runner.invoke(supercharge, ["task", "init", "code"])

        assert result.exit_code == 0, result.output
        assert len(calls) == 1
        event_type, kwargs = calls[0]
        assert event_type == "task_init"
        assert kwargs["agent_type"] == "code"
        assert kwargs["task_uuid"]  # non-empty UUID

    def test_emits_with_author(self, tmp_path: Path):
        from supercharge.cli import supercharge

        spy, calls = _make_emit_spy()
        runner = CliRunner()

        with (
            patch("supercharge.cli._task_root", return_value=tmp_path / "tasks"),
            patch("supercharge.cli._copy_template", side_effect=self._mock_copy_template),
            patch("supercharge.cli._validate_author", return_value="orchestrator:sess-1"),
            patch("supercharge.cli._emit", spy),
        ):
            (tmp_path / "tasks" / "plan").mkdir(parents=True, exist_ok=True)
            result = runner.invoke(
                supercharge,
                ["task", "init", "plan", "--author", "orchestrator:sess-1"],
            )

        assert result.exit_code == 0
        assert len(calls) == 1
        _, kwargs = calls[0]
        assert kwargs["parent_id"] == "orchestrator:sess-1"


# ── task_cleanup CLI ─────────────────────────────────────────────────────────


class TestTaskCleanupEmit:
    """task_cleanup emits task_cleanup on success, not on failure."""

    def test_emits_task_cleanup(self, tmp_path: Path):
        from supercharge.cli import supercharge

        spy, calls = _make_emit_spy()
        runner = CliRunner()

        task_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        task_dir = tmp_path / "tasks" / "code" / task_uuid
        task_dir.mkdir(parents=True)

        with (
            patch("supercharge.cli._find_task_dir", return_value=task_dir),
            patch("supercharge.cli._task_root", return_value=tmp_path / "tasks"),
            patch("supercharge.cli._emit", spy),
        ):
            result = runner.invoke(supercharge, ["task", "cleanup", task_uuid])

        assert result.exit_code == 0
        assert len(calls) == 1
        event_type, kwargs = calls[0]
        assert event_type == "task_cleanup"
        assert kwargs["task_uuid"] == task_uuid

    def test_no_emit_on_invalid_uuid(self, tmp_path: Path):
        from supercharge.cli import supercharge

        spy, calls = _make_emit_spy()
        runner = CliRunner()

        with patch("supercharge.cli._emit", spy):
            result = runner.invoke(supercharge, ["task", "cleanup", "not-a-uuid"])

        assert len(calls) == 0


# ── task_archive CLI ─────────────────────────────────────────────────────────


class TestTaskArchiveEmit:
    """task_archive emits task_archive with task_uuid and agent_type."""

    def test_emits_task_archive(self, tmp_path: Path):
        from supercharge.cli import supercharge

        spy, calls = _make_emit_spy()
        runner = CliRunner()

        task_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        task_dir = tmp_path / "tasks" / "research" / task_uuid
        task_dir.mkdir(parents=True)
        (task_dir / "task.md").write_text("# Task\nTest task")
        (task_dir / "result.md").write_text("## Report\nDone.")

        with (
            patch("supercharge.cli._find_task_dir", return_value=task_dir),
            patch("supercharge.cli._task_root", return_value=tmp_path / "tasks"),
            patch("supercharge.cli._archive_root", return_value=tmp_path / "archive"),
            patch("supercharge.cli._emit", spy),
        ):
            result = runner.invoke(supercharge, ["task", "archive", task_uuid])

        assert result.exit_code == 0
        assert len(calls) == 1
        event_type, kwargs = calls[0]
        assert event_type == "task_archive"
        assert kwargs["task_uuid"] == task_uuid
        assert kwargs["agent_type"] == "research"


# ── subtask_init CLI ─────────────────────────────────────────────────────────


class TestSubtaskInitEmit:
    """subtask_init emits subtask_init with worker_id and model info."""

    def test_emits_subtask_init_fast(self, tmp_path: Path):
        from supercharge.cli import supercharge

        spy, calls = _make_emit_spy()
        runner = CliRunner()

        task_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        task_dir = tmp_path / "tasks" / "code" / task_uuid
        task_dir.mkdir(parents=True)

        async def mock_fast_worker_init(*args, **kwargs):
            return {"worker_id": "wk-123", "result": "done"}

        with (
            patch("supercharge.cli._find_task_dir", return_value=task_dir),
            patch("supercharge.cli._is_fast_mode", return_value=True),
            patch("supercharge.cli._fast_worker_init", mock_fast_worker_init),
            patch("supercharge.cli._emit", spy),
            patch.dict(os.environ, {"SUPERCHARGE_TASK_UUID": task_uuid}, clear=False),
        ):
            result = runner.invoke(
                supercharge,
                ["subtask", "init", "code", "test prompt", "--task-uuid", task_uuid, "--model", "haiku"],
            )

        assert result.exit_code == 0
        assert len(calls) == 1
        event_type, kwargs = calls[0]
        assert event_type == "subtask_init"
        assert kwargs["agent_type"] == "code"
        assert kwargs["task_uuid"] == task_uuid
        assert "worker_id" in kwargs
        assert "haiku" in kwargs.get("detail", "") or "fast" in kwargs.get("detail", "")


# ── subtask_resume CLI ───────────────────────────────────────────────────────


class TestSubtaskResumeEmit:
    """subtask_resume emits subtask_resume with worker_id."""

    def test_emits_subtask_resume(self, tmp_path: Path):
        from supercharge.cli import supercharge

        spy, calls = _make_emit_spy()
        runner = CliRunner()

        worker_id = "wk-resume-123"
        task_dir = tmp_path / "tasks" / "code" / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        task_dir.mkdir(parents=True)
        workers_dir = task_dir / "workers"
        workers_dir.mkdir()
        worker_file = workers_dir / f"{worker_id}.md"
        worker_file.write_text("# Worker context")

        async def mock_resume(*args, **kwargs):
            return {"worker_id": worker_id, "result": "resumed"}

        with (
            patch("supercharge.cli._find_worker_file", return_value=worker_file),
            patch("supercharge.cli._deep_worker_resume", mock_resume),
            patch("supercharge.cli._emit", spy),
        ):
            result = runner.invoke(
                supercharge,
                ["subtask", "resume", worker_id, "continue please"],
            )

        assert result.exit_code == 0
        assert len(calls) == 1
        event_type, kwargs = calls[0]
        assert event_type == "subtask_resume"
        assert kwargs["worker_id"] == worker_id


# ── _deep_worker_init ────────────────────────────────────────────────────────


class TestDeepWorkerInitEmit:
    """_deep_worker_init emits worker_start and worker_end."""

    @pytest.mark.anyio
    async def test_emits_worker_start_and_end(self, tmp_path: Path):
        spy, calls = _make_emit_spy()

        task_dir = tmp_path / "code" / "task-001"
        task_dir.mkdir(parents=True)
        worker_file = task_dir / "workers" / "w1.md"
        worker_file.parent.mkdir()
        worker_file.write_text("context")

        mock_client = MagicMock()
        mock_client.connect = MagicMock(return_value=_async_noop())
        mock_client.disconnect = MagicMock(return_value=_async_noop())
        mock_client.query = MagicMock(return_value=_async_noop())
        mock_client.receive_response = MagicMock(
            return_value=_async_iter([
                claude_agent_sdk.ResultMessage(
                    subtype="result",
                    duration_ms=100,
                    duration_api_ms=80,
                    is_error=False,
                    num_turns=1,
                    session_id="w1",
                    result="done",
                ),
            ])
        )

        with (
            patch.object(claude_agent_sdk, "ClaudeSDKClient", return_value=mock_client),
            patch("supercharge.workers._emit", spy),
            patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(tmp_path)}),
        ):
            from supercharge.workers import _deep_worker_init

            result = await _deep_worker_init(
                task_dir, "code", "do stuff", "w1", worker_file, 2, None, None,
            )

        # Should have worker_start and worker_end
        event_types = [c[0] for c in calls]
        assert "worker_start" in event_types
        assert "worker_end" in event_types

        # worker_start fields
        start_call = next(c for c in calls if c[0] == "worker_start")
        assert start_call[1]["worker_id"] == "w1"
        assert start_call[1]["agent_type"] == "code"

        # worker_end fields
        end_call = next(c for c in calls if c[0] == "worker_end")
        assert end_call[1]["worker_id"] == "w1"
        assert "success" in end_call[1].get("detail", "").lower() or "done" in end_call[1].get("detail", "").lower()

    @pytest.mark.anyio
    async def test_emits_worker_end_on_error(self, tmp_path: Path):
        spy, calls = _make_emit_spy()

        task_dir = tmp_path / "code" / "task-001"
        task_dir.mkdir(parents=True)
        worker_file = task_dir / "workers" / "w1.md"
        worker_file.parent.mkdir()
        worker_file.write_text("context")

        mock_client = MagicMock()
        mock_client.connect = MagicMock(return_value=_async_noop())
        mock_client.disconnect = MagicMock(return_value=_async_noop())
        mock_client.query = MagicMock(return_value=_async_noop())
        mock_client.receive_response = MagicMock(
            return_value=_async_iter([
                claude_agent_sdk.ResultMessage(
                    subtype="result",
                    duration_ms=100,
                    duration_api_ms=80,
                    is_error=True,
                    num_turns=1,
                    session_id="w1",
                    result="something went wrong",
                ),
            ])
        )

        with (
            patch.object(claude_agent_sdk, "ClaudeSDKClient", return_value=mock_client),
            patch("supercharge.workers._emit", spy),
            patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(tmp_path)}),
        ):
            from supercharge.workers import _deep_worker_init

            result = await _deep_worker_init(
                task_dir, "code", "do stuff", "w1", worker_file, 2, None, None,
            )

        end_call = next(c for c in calls if c[0] == "worker_end")
        assert "error" in end_call[1].get("detail", "").lower()


# ── _fast_worker_init ────────────────────────────────────────────────────────


class TestFastWorkerInitEmit:
    """_fast_worker_init emits worker_start and worker_end."""

    @pytest.mark.anyio
    async def test_emits_worker_start_and_end(self, tmp_path: Path):
        spy, calls = _make_emit_spy()

        task_dir = tmp_path / "code" / "task-001"
        task_dir.mkdir(parents=True)

        async def mock_query(*, prompt, options):
            yield claude_agent_sdk.ResultMessage(
                subtype="result",
                duration_ms=50,
                duration_api_ms=40,
                is_error=False,
                num_turns=1,
                session_id="test",
                result="fast result",
            )

        with (
            patch.object(claude_agent_sdk, "query", mock_query),
            patch("supercharge.workers._emit", spy),
            patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(tmp_path)}),
        ):
            from supercharge.workers import _fast_worker_init

            result = await _fast_worker_init(
                task_dir, "code", "quick task", "fw-1", None, None,
            )

        event_types = [c[0] for c in calls]
        assert "worker_start" in event_types
        assert "worker_end" in event_types

        start_call = next(c for c in calls if c[0] == "worker_start")
        assert start_call[1]["worker_id"] == "fw-1"
        assert "fast" in start_call[1].get("detail", "").lower()


# ── _spawn_background_memory ─────────────────────────────────────────────────


class TestSpawnBackgroundMemoryEmit:
    """_spawn_background_memory emits memory_spawn on success."""

    def test_emits_memory_spawn(self, tmp_path: Path):
        spy, calls = _make_emit_spy()

        task_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        task_dir = tmp_path / ".claude" / "SuperchargeAI" / "tasks" / "memory" / task_uuid
        task_dir.mkdir(parents=True)

        mock_init_result = MagicMock()
        mock_init_result.returncode = 0
        mock_init_result.stdout = task_uuid + "\n"
        mock_init_result.stderr = ""

        mock_popen = MagicMock()

        with (
            patch("supercharge.memory.subprocess.run", return_value=mock_init_result),
            patch("supercharge.memory.subprocess.Popen", return_value=mock_popen),
            patch("supercharge.memory._emit", spy),
        ):
            from supercharge.memory import _spawn_background_memory

            result = _spawn_background_memory("# Task\nHarvest", str(tmp_path))

        assert result == task_uuid
        assert len(calls) == 1
        event_type, kwargs = calls[0]
        assert event_type == "memory_spawn"
        assert kwargs["task_uuid"] == task_uuid

    def test_no_emit_on_failure(self, tmp_path: Path):
        spy, calls = _make_emit_spy()

        mock_init_result = MagicMock()
        mock_init_result.returncode = 1
        mock_init_result.stdout = ""
        mock_init_result.stderr = "error"

        with (
            patch("supercharge.memory.subprocess.run", return_value=mock_init_result),
            patch("supercharge.memory._emit", spy),
        ):
            from supercharge.memory import _spawn_background_memory

            result = _spawn_background_memory("# Task\nHarvest", str(tmp_path))

        assert result is None
        assert len(calls) == 0


# ── Negative: no emit on failure paths ───────────────────────────────────────


class TestNoEmitOnFailure:
    """_emit should NOT be called when operations fail."""

    def test_task_cleanup_no_emit_on_not_found(self, tmp_path: Path):
        from supercharge.cli import supercharge

        spy, calls = _make_emit_spy()
        runner = CliRunner()

        task_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

        with (
            patch("supercharge.cli._find_task_dir", return_value=None),
            patch("supercharge.cli._emit", spy),
        ):
            result = runner.invoke(supercharge, ["task", "cleanup", task_uuid])

        assert len(calls) == 0


# ── Async helpers ────────────────────────────────────────────────────────────


async def _async_noop():
    """Async no-op for mocking awaitable methods."""
    pass


class _async_iter:
    """Wrap a list into an async iterator for mocking receive_response."""

    def __init__(self, items):
        self._items = items
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._index]
        self._index += 1
        return item