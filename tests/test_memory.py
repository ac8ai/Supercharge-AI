"""Tests for background memory harvesting: scanning, stamping, and spawning."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

from supercharge.memory import (
    _STAMP_TYPE,
    _format_transcript_task,
    _newest_mtime,
    _scan_stale_task_folders,
    _scan_unreviewed_transcripts,
    _spawn_background_memory,
    _stamp_status,
    _stamp_transcript,
)

# ── _scan_unreviewed_transcripts ──────────────────────────────────────────


class TestScanUnreviewedTranscripts:
    """Test transcript scanning logic."""

    def test_empty_dir_returns_empty(self, tmp_path: Path):
        transcript = tmp_path / "current.jsonl"
        transcript.touch()
        result = _scan_unreviewed_transcripts(str(transcript), min_age_hours=0)
        assert result == []

    def test_skips_current_session(self, tmp_path: Path):
        current = tmp_path / "current.jsonl"
        current.write_text('{"type": "user", "text": "hello"}\n')
        # Set old mtime so it would qualify by age
        os.utime(current, (0, 0))

        result = _scan_unreviewed_transcripts(str(current), min_age_hours=0)
        assert result == []

    def test_skips_fully_reviewed_transcript(self, tmp_path: Path):
        current = tmp_path / "current.jsonl"
        current.touch()

        stamped = tmp_path / "old_session.jsonl"
        stamp = {"type": _STAMP_TYPE, "timestamp": "2025-01-01T00:00:00Z", "version": "0.1.0"}
        stamped.write_text('{"type": "user", "text": "hello"}\n' + json.dumps(stamp) + "\n")
        os.utime(stamped, (0, 0))

        result = _scan_unreviewed_transcripts(str(current), min_age_hours=0)
        assert result == []

    def test_includes_stamped_transcript_with_new_content(self, tmp_path: Path):
        current = tmp_path / "current.jsonl"
        current.touch()

        partially = tmp_path / "old_session.jsonl"
        stamp = {"type": _STAMP_TYPE, "timestamp": "2025-01-01T00:00:00Z", "version": "0.1.0"}
        partially.write_text(
            '{"type": "user", "text": "hello"}\n'
            + json.dumps(stamp)
            + "\n"
            + '{"type": "user", "text": "new content"}\n'
        )
        os.utime(partially, (0, 0))

        result = _scan_unreviewed_transcripts(str(current), min_age_hours=0)
        assert len(result) == 1
        path, offset = result[0]
        assert path == partially
        assert offset == 3  # stamp is on line 2, so start reading from line 3

    def test_returns_none_offset_for_unstamped(self, tmp_path: Path):
        current = tmp_path / "current.jsonl"
        current.touch()

        unstamped = tmp_path / "old_session.jsonl"
        unstamped.write_text('{"type": "user", "text": "hello"}\n')
        os.utime(unstamped, (0, 0))

        result = _scan_unreviewed_transcripts(str(current), min_age_hours=0)
        assert len(result) == 1
        path, offset = result[0]
        assert path == unstamped
        assert offset is None

    def test_skips_too_recent_transcript(self, tmp_path: Path):
        current = tmp_path / "current.jsonl"
        current.touch()

        recent = tmp_path / "recent.jsonl"
        recent.write_text('{"type": "user", "text": "hello"}\n')
        # mtime is now (very recent)

        result = _scan_unreviewed_transcripts(str(current), min_age_hours=1)
        assert result == []

    def test_returns_eligible_transcript(self, tmp_path: Path):
        current = tmp_path / "current.jsonl"
        current.touch()

        eligible = tmp_path / "old_session.jsonl"
        eligible.write_text('{"type": "user", "text": "hello"}\n')
        # Set old mtime
        os.utime(eligible, (0, 0))

        result = _scan_unreviewed_transcripts(str(current), min_age_hours=0)
        assert len(result) == 1
        path, offset = result[0]
        assert path == eligible
        assert offset is None

    def test_returns_multiple_eligible(self, tmp_path: Path):
        current = tmp_path / "current.jsonl"
        current.touch()

        for i in range(3):
            f = tmp_path / f"session_{i}.jsonl"
            f.write_text(f'{{"type": "user", "text": "msg {i}"}}\n')
            os.utime(f, (0, 0))

        result = _scan_unreviewed_transcripts(str(current), min_age_hours=0)
        assert len(result) == 3
        # All should be (path, None) since none are stamped
        for path, offset in result:
            assert offset is None

    def test_missing_parent_dir_returns_empty(self, tmp_path: Path):
        nonexistent = tmp_path / "nodir" / "session.jsonl"
        result = _scan_unreviewed_transcripts(str(nonexistent), min_age_hours=0)
        assert result == []

    def test_skips_non_jsonl_files(self, tmp_path: Path):
        current = tmp_path / "current.jsonl"
        current.touch()

        txt_file = tmp_path / "notes.txt"
        txt_file.write_text("some notes")
        os.utime(txt_file, (0, 0))

        result = _scan_unreviewed_transcripts(str(current), min_age_hours=0)
        assert result == []

    def test_uses_env_var_for_age(self, tmp_path: Path):
        current = tmp_path / "current.jsonl"
        current.touch()

        eligible = tmp_path / "old.jsonl"
        eligible.write_text('{"type": "user"}\n')
        # Set mtime to 30 minutes ago
        os.utime(eligible, (time.time() - 1800, time.time() - 1800))

        # With env var set to 0.25 hours (15 min), the file is old enough
        with patch.dict(os.environ, {"SUPERCHARGE_MEMORY_SESSION_AGE_HOURS": "0.25"}):
            result = _scan_unreviewed_transcripts(str(current))
            assert len(result) == 1

        # With env var set to 2 hours, the file is too recent
        with patch.dict(os.environ, {"SUPERCHARGE_MEMORY_SESSION_AGE_HOURS": "2"}):
            result = _scan_unreviewed_transcripts(str(current))
            assert len(result) == 0


# ── _stamp_status ─────────────────────────────────────────────────────────


class TestStampStatus:
    """Test stamp status detection in transcript files."""

    def test_no_stamp(self, tmp_path: Path):
        f = tmp_path / "session.jsonl"
        f.write_text(
            '{"type": "user", "text": "hello"}\n'
            '{"type": "assistant", "text": "hi"}\n'
            '{"type": "user", "text": "bye"}\n'
        )
        assert _stamp_status(f) == (False, None)

    def test_stamp_as_last_entry(self, tmp_path: Path):
        f = tmp_path / "session.jsonl"
        stamp = {"type": _STAMP_TYPE, "timestamp": "2025-01-01T00:00:00Z", "version": "0.1.0"}
        f.write_text(
            '{"type": "user", "text": "hello"}\n'
            '{"type": "assistant", "text": "hi"}\n'
            '{"type": "user", "text": "bye"}\n' + json.dumps(stamp) + "\n"
        )
        assert _stamp_status(f) == (True, 4)

    def test_stamp_followed_by_new_content(self, tmp_path: Path):
        f = tmp_path / "session.jsonl"
        stamp = {"type": _STAMP_TYPE, "timestamp": "2025-01-01T00:00:00Z", "version": "0.1.0"}
        f.write_text(
            '{"type": "user", "text": "hello"}\n'
            '{"type": "assistant", "text": "hi"}\n'
            '{"type": "user", "text": "bye"}\n'
            + json.dumps(stamp)
            + "\n"
            + '{"type": "user", "text": "new message"}\n'
            + '{"type": "assistant", "text": "new reply"}\n'
        )
        assert _stamp_status(f) == (False, 4)

    def test_multiple_stamps_last_is_final(self, tmp_path: Path):
        f = tmp_path / "session.jsonl"
        stamp1 = {"type": _STAMP_TYPE, "timestamp": "2025-01-01T00:00:00Z", "version": "0.1.0"}
        stamp2 = {"type": _STAMP_TYPE, "timestamp": "2025-01-02T00:00:00Z", "version": "0.1.0"}
        f.write_text(
            '{"type": "user", "text": "hello"}\n'
            + json.dumps(stamp1)
            + "\n"
            + '{"type": "user", "text": "more"}\n'
            + json.dumps(stamp2)
            + "\n"
        )
        assert _stamp_status(f) == (True, 4)

    def test_multiple_stamps_with_content_after_last(self, tmp_path: Path):
        f = tmp_path / "session.jsonl"
        stamp1 = {"type": _STAMP_TYPE, "timestamp": "2025-01-01T00:00:00Z", "version": "0.1.0"}
        stamp2 = {"type": _STAMP_TYPE, "timestamp": "2025-01-02T00:00:00Z", "version": "0.1.0"}
        f.write_text(
            '{"type": "user", "text": "hello"}\n'
            + json.dumps(stamp1)
            + "\n"
            + '{"type": "user", "text": "more"}\n'
            + json.dumps(stamp2)
            + "\n"
            + '{"type": "user", "text": "even more"}\n'
        )
        assert _stamp_status(f) == (False, 4)

    def test_empty_file(self, tmp_path: Path):
        f = tmp_path / "session.jsonl"
        f.touch()
        assert _stamp_status(f) == (False, None)

    def test_nonexistent_file(self, tmp_path: Path):
        f = tmp_path / "nonexistent.jsonl"
        assert _stamp_status(f) == (False, None)

    def test_stamp_followed_by_blank_lines(self, tmp_path: Path):
        f = tmp_path / "session.jsonl"
        stamp = {"type": _STAMP_TYPE, "timestamp": "2025-01-01T00:00:00Z", "version": "0.1.0"}
        f.write_text(
            '{"type": "user", "text": "hello"}\n' + json.dumps(stamp) + "\n" + "\n" + "\n" + "\n"
        )
        assert _stamp_status(f) == (True, 2)

    def test_stamp_as_only_content(self, tmp_path: Path):
        f = tmp_path / "session.jsonl"
        stamp = {"type": _STAMP_TYPE, "timestamp": "2025-01-01T00:00:00Z", "version": "0.1.0"}
        f.write_text(json.dumps(stamp) + "\n")
        assert _stamp_status(f) == (True, 1)


# ── _format_transcript_task ───────────────────────────────────────────────


class TestFormatTranscriptTask:
    """Test transcript task formatting with offsets."""

    def test_format_with_no_offsets(self):
        transcripts = [
            (Path("/tmp/a.jsonl"), None),
            (Path("/tmp/b.jsonl"), None),
        ]
        with patch("supercharge.memory._load_template") as mock_tpl:
            mock_tpl.return_value = "Files:\n{transcript_list}\n\nMemory: {memory_dir}"
            result = _format_transcript_task(transcripts, "/mem")
        assert "- `/tmp/a.jsonl`" in result
        assert "- `/tmp/b.jsonl`" in result
        assert "start reading from line" not in result

    def test_format_with_offsets(self):
        transcripts = [
            (Path("/tmp/a.jsonl"), 42),
            (Path("/tmp/b.jsonl"), 10),
        ]
        with patch("supercharge.memory._load_template") as mock_tpl:
            mock_tpl.return_value = "Files:\n{transcript_list}\n\nMemory: {memory_dir}"
            result = _format_transcript_task(transcripts, "/mem")
        assert (
            "- `/tmp/a.jsonl` (start reading from line 42"
            " -- skip previously reviewed content)" in result
        )
        assert (
            "- `/tmp/b.jsonl` (start reading from line 10"
            " -- skip previously reviewed content)" in result
        )

    def test_format_mixed(self):
        transcripts = [
            (Path("/tmp/a.jsonl"), None),
            (Path("/tmp/b.jsonl"), 5),
        ]
        with patch("supercharge.memory._load_template") as mock_tpl:
            mock_tpl.return_value = "Files:\n{transcript_list}\n\nMemory: {memory_dir}"
            result = _format_transcript_task(transcripts, "/mem")
        assert "- `/tmp/a.jsonl`\n" in result
        assert (
            "- `/tmp/b.jsonl` (start reading from line 5"
            " -- skip previously reviewed content)" in result
        )


# ── _stamp_transcript ─────────────────────────────────────────────────────


class TestStampTranscript:
    """Test transcript stamping."""

    def test_appends_valid_jsonl(self, tmp_path: Path):
        f = tmp_path / "session.jsonl"
        original = '{"type": "user", "text": "hello"}\n'
        f.write_text(original)

        _stamp_transcript(f)

        content = f.read_text()
        lines = content.strip().splitlines()
        assert len(lines) == 2
        # Original preserved
        assert lines[0] == '{"type": "user", "text": "hello"}'

    def test_stamp_has_correct_fields(self, tmp_path: Path):
        f = tmp_path / "session.jsonl"
        f.write_text('{"type": "user"}\n')

        _stamp_transcript(f)

        lines = f.read_text().strip().splitlines()
        stamp = json.loads(lines[-1])
        assert stamp["type"] == _STAMP_TYPE
        assert "timestamp" in stamp
        assert "version" in stamp
        # Timestamp should be ISO format
        assert "T" in stamp["timestamp"]

    def test_preserves_existing_content(self, tmp_path: Path):
        f = tmp_path / "session.jsonl"
        lines = [
            '{"type": "user", "text": "hello"}',
            '{"type": "assistant", "text": "hi"}',
            '{"type": "user", "text": "bye"}',
        ]
        f.write_text("\n".join(lines) + "\n")

        _stamp_transcript(f)

        content = f.read_text()
        result_lines = content.strip().splitlines()
        # Original 3 lines + 1 stamp
        assert len(result_lines) == 4
        for i, line in enumerate(lines):
            assert result_lines[i] == line

    def test_stamped_file_detected_by_stamp_status(self, tmp_path: Path):
        f = tmp_path / "session.jsonl"
        f.write_text('{"type": "user"}\n')

        is_reviewed, line = _stamp_status(f)
        assert is_reviewed is False
        assert line is None

        _stamp_transcript(f)

        is_reviewed, line = _stamp_status(f)
        assert is_reviewed is True
        assert line == 2


# ── _scan_stale_task_folders ──────────────────────────────────────────────


class TestScanStaleTaskFolders:
    """Test stale task folder scanning."""

    def test_empty_root_returns_empty(self, tmp_path: Path):
        result = _scan_stale_task_folders(tmp_path, max_age_days=0)
        assert result == []

    def test_missing_root_returns_empty(self, tmp_path: Path):
        nonexistent = tmp_path / "nonexistent"
        result = _scan_stale_task_folders(nonexistent, max_age_days=0)
        assert result == []

    def test_includes_memory_task_folders(self, tmp_path: Path):
        """Memory agent UUID task folders are scanned (self-cleaning loop)."""
        memory_task = tmp_path / "memory" / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        memory_task.mkdir(parents=True)
        (memory_task / "task.md").write_text("# Task")
        os.utime(memory_task / "task.md", (0, 0))

        result = _scan_stale_task_folders(tmp_path, max_age_days=0)
        assert result == [memory_task]

    def test_excludes_non_uuid_dirs(self, tmp_path: Path):
        non_uuid = tmp_path / "code" / "not-a-uuid"
        non_uuid.mkdir(parents=True)
        (non_uuid / "task.md").write_text("# Task")
        os.utime(non_uuid / "task.md", (0, 0))

        result = _scan_stale_task_folders(tmp_path, max_age_days=0)
        assert result == []

    def test_returns_stale_folders(self, tmp_path: Path):
        task_dir = tmp_path / "code" / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        task_dir.mkdir(parents=True)
        task_md = task_dir / "task.md"
        task_md.write_text("# Task")
        os.utime(task_md, (0, 0))

        result = _scan_stale_task_folders(tmp_path, max_age_days=0)
        assert len(result) == 1
        assert result[0] == task_dir

    def test_skips_recent_folders(self, tmp_path: Path):
        task_dir = tmp_path / "code" / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        task_dir.mkdir(parents=True)
        (task_dir / "task.md").write_text("# Task")
        # mtime is now (very recent)

        result = _scan_stale_task_folders(tmp_path, max_age_days=1)
        assert result == []

    def test_uses_newest_file_mtime(self, tmp_path: Path):
        task_dir = tmp_path / "plan" / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        task_dir.mkdir(parents=True)

        # Old file
        old_file = task_dir / "task.md"
        old_file.write_text("# Task")
        os.utime(old_file, (0, 0))

        # Recent file in subfolder
        workers = task_dir / "workers"
        workers.mkdir()
        recent = workers / "worker.md"
        recent.write_text("# Worker")
        # mtime is now

        result = _scan_stale_task_folders(tmp_path, max_age_days=1)
        # Should not be stale because workers/worker.md is recent
        assert result == []

    def test_empty_task_folder_not_returned(self, tmp_path: Path):
        """Empty task folders have no newest file, so _newest_mtime returns None."""
        task_dir = tmp_path / "code" / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        task_dir.mkdir(parents=True)

        result = _scan_stale_task_folders(tmp_path, max_age_days=0)
        assert result == []

    def test_multiple_agent_types(self, tmp_path: Path):
        """Scans across multiple agent type directories."""
        for agent in ("code", "plan", "review"):
            task_dir = tmp_path / agent / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            task_dir.mkdir(parents=True)
            f = task_dir / "task.md"
            f.write_text("# Task")
            os.utime(f, (0, 0))

        result = _scan_stale_task_folders(tmp_path, max_age_days=0)
        assert len(result) == 3

    def test_uses_env_var_for_age(self, tmp_path: Path):
        task_dir = tmp_path / "code" / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        task_dir.mkdir(parents=True)
        f = task_dir / "task.md"
        f.write_text("# Task")
        # Set mtime to 1 day ago
        one_day_ago = time.time() - 86400
        os.utime(f, (one_day_ago, one_day_ago))

        # With env set to 0.5 days, the folder is stale
        with patch.dict(os.environ, {"SUPERCHARGE_MEMORY_STALE_DAYS": "0.5"}):
            result = _scan_stale_task_folders(tmp_path)
            assert len(result) == 1

        # With env set to 3 days, the folder is not stale
        with patch.dict(os.environ, {"SUPERCHARGE_MEMORY_STALE_DAYS": "3"}):
            result = _scan_stale_task_folders(tmp_path)
            assert len(result) == 0


# ── _newest_mtime ─────────────────────────────────────────────────────────


class TestNewestMtime:
    """Test recursive mtime detection."""

    def test_empty_folder(self, tmp_path: Path):
        assert _newest_mtime(tmp_path) is None

    def test_single_file(self, tmp_path: Path):
        f = tmp_path / "file.txt"
        f.write_text("hello")
        result = _newest_mtime(tmp_path)
        assert result is not None
        assert abs(result - f.stat().st_mtime) < 1

    def test_nested_files(self, tmp_path: Path):
        old = tmp_path / "old.txt"
        old.write_text("old")
        os.utime(old, (0, 0))

        sub = tmp_path / "sub"
        sub.mkdir()
        new = sub / "new.txt"
        new.write_text("new")

        result = _newest_mtime(tmp_path)
        assert result is not None
        assert result == new.stat().st_mtime


# ── _spawn_background_memory ─────────────────────────────────────────────


class TestSpawnBackgroundMemory:
    """Test background memory agent spawning."""

    def test_returns_uuid_on_success(self, tmp_path: Path):
        task_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        task_dir = tmp_path / ".claude" / "SuperchargeAI" / "tasks" / "memory" / task_uuid
        task_dir.mkdir(parents=True)

        with (
            patch("supercharge.memory.subprocess.run") as mock_run,
            patch("supercharge.memory.subprocess.Popen") as mock_popen,
        ):
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = task_uuid + "\n"
            mock_run.return_value.stderr = ""

            result = _spawn_background_memory("# Task content", str(tmp_path))

        assert result == task_uuid
        mock_popen.assert_called_once()
        # Verify the Popen call args
        popen_args = mock_popen.call_args
        assert popen_args[0][0] == ["supercharge", "memory", "run", task_uuid]
        assert popen_args[1]["start_new_session"] is True

    def test_returns_none_on_init_failure(self, tmp_path: Path):
        with patch("supercharge.memory.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "error"

            result = _spawn_background_memory("# Task content", str(tmp_path))

        assert result is None

    def test_returns_none_on_empty_uuid(self, tmp_path: Path):
        with patch("supercharge.memory.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "\n"
            mock_run.return_value.stderr = ""

            result = _spawn_background_memory("# Task content", str(tmp_path))

        assert result is None

    def test_returns_none_on_exception(self, tmp_path: Path):
        with patch("supercharge.memory.subprocess.run", side_effect=OSError("fail")):
            result = _spawn_background_memory("# Task content", str(tmp_path))

        assert result is None

    def test_writes_task_md(self, tmp_path: Path):
        task_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        task_dir = tmp_path / ".claude" / "SuperchargeAI" / "tasks" / "memory" / task_uuid
        task_dir.mkdir(parents=True)

        with (
            patch("supercharge.memory.subprocess.run") as mock_run,
            patch("supercharge.memory.subprocess.Popen"),
        ):
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = task_uuid + "\n"
            mock_run.return_value.stderr = ""

            _spawn_background_memory("# Custom task content", str(tmp_path))

        task_md = task_dir / "task.md"
        assert task_md.read_text() == "# Custom task content"
