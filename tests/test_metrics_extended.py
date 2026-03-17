"""Tests for extended metrics query functions."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

from supercharge.metrics import (
    _event_count,
    _find_session_jsonl,
    _init_db,
    _query_events,
    _query_session_events,
    _query_sessions,
    _query_stats,
    _vote_session_project,
)


def _patch_db(tmp_path: Path):
    return patch("supercharge.metrics._db_path", return_value=tmp_path / "metrics.db")


def _seed_db(tmp_path: Path) -> None:
    """Create a realistic dataset spanning two sessions."""
    db = tmp_path / "metrics.db"
    conn = sqlite3.connect(str(db))
    _init_db(conn)

    rows = [
        ("2026-01-10T10:00:00+00:00", "session_start", "sess-1", "orch-1", "orchestrator", "", "", "", "", ""),
        ("2026-01-10T10:00:01+00:00", "task_init", "sess-1", "agent-c1", "code", "task-1", "", "orchestrator:sess-1", "", ""),
        ("2026-01-10T10:00:02+00:00", "tool_use", "sess-1", "agent-c1", "code", "task-1", "", "", "Bash", "ls"),
        ("2026-01-10T10:00:03+00:00", "tool_use", "sess-1", "agent-c1", "code", "task-1", "", "", "Read", "/foo"),
        ("2026-01-10T10:00:04+00:00", "subtask_init", "sess-1", "agent-c1", "code", "task-1", "w1", "task:task-1", "", ""),
        ("2026-01-10T10:00:05+00:00", "worker_start", "sess-1", "agent-w1", "code", "", "w1", "", "", ""),
        ("2026-01-10T10:00:06+00:00", "tool_use", "sess-1", "agent-w1", "code", "", "w1", "", "Write", "file.py"),
        ("2026-01-10T10:00:07+00:00", "worker_end", "sess-1", "agent-w1", "code", "", "w1", "", "", ""),
        ("2026-01-10T10:00:08+00:00", "task_cleanup", "sess-1", "agent-c1", "code", "task-1", "", "", "", ""),
        ("2026-01-10T11:00:00+00:00", "session_start", "sess-2", "orch-2", "orchestrator", "", "", "", "", ""),
        ("2026-01-10T11:00:01+00:00", "task_init", "sess-2", "agent-r1", "research", "task-2", "", "orchestrator:sess-2", "", ""),
        ("2026-01-10T11:00:02+00:00", "tool_use", "sess-2", "agent-r1", "research", "task-2", "", "", "Bash", "curl"),
        ("2026-01-10T11:00:10+00:00", "task_cleanup", "sess-2", "agent-r1", "research", "task-2", "", "", "", ""),
    ]

    for row in rows:
        conn.execute(
            "INSERT INTO events (timestamp, event_type, session_id, agent_id, "
            "agent_type, task_uuid, worker_id, parent_id, tool_name, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            row,
        )
    conn.commit()
    conn.close()


class TestQueryEventsExtended:
    """Test new optional parameters on _query_events."""

    def test_offset(self, tmp_path: Path):
        _seed_db(tmp_path)
        with _patch_db(tmp_path):
            all_events = _query_events(limit=100)
            offset_events = _query_events(limit=100, offset=5)
        assert len(offset_events) == len(all_events) - 5
        assert offset_events[0]["id"] == all_events[5]["id"]

    def test_order_desc(self, tmp_path: Path):
        _seed_db(tmp_path)
        with _patch_db(tmp_path):
            desc = _query_events(limit=100, order="desc")
        assert desc[0]["id"] > desc[-1]["id"]

    def test_order_asc_default(self, tmp_path: Path):
        _seed_db(tmp_path)
        with _patch_db(tmp_path):
            asc = _query_events(limit=100)
        assert asc[0]["id"] < asc[-1]["id"]

    def test_since(self, tmp_path: Path):
        _seed_db(tmp_path)
        with _patch_db(tmp_path):
            results = _query_events(limit=100, since="2026-01-10T11:00:00+00:00")
        assert all(r["timestamp"] >= "2026-01-10T11:00:00+00:00" for r in results)
        assert len(results) == 4

    def test_until(self, tmp_path: Path):
        _seed_db(tmp_path)
        with _patch_db(tmp_path):
            results = _query_events(limit=100, until="2026-01-10T10:00:05+00:00")
        assert len(results) == 6

    def test_since_and_until(self, tmp_path: Path):
        _seed_db(tmp_path)
        with _patch_db(tmp_path):
            results = _query_events(
                limit=100,
                since="2026-01-10T10:00:03+00:00",
                until="2026-01-10T10:00:06+00:00",
            )
        assert len(results) == 4

    def test_after_id(self, tmp_path: Path):
        _seed_db(tmp_path)
        with _patch_db(tmp_path):
            all_events = _query_events(limit=100)
            mid_id = all_events[6]["id"]
            after = _query_events(limit=100, after_id=mid_id)
        assert all(r["id"] > mid_id for r in after)

    def test_combined_filters_with_new_params(self, tmp_path: Path):
        _seed_db(tmp_path)
        with _patch_db(tmp_path):
            results = _query_events(
                event_type="tool_use",
                session_id="sess-1",
                limit=10,
                offset=1,
                order="desc",
            )
        assert len(results) == 2

    def test_backward_compatible(self, tmp_path: Path):
        """Original call signature still works."""
        _seed_db(tmp_path)
        with _patch_db(tmp_path):
            results = _query_events(event_type="session_start", limit=10)
        assert len(results) == 2


class TestEventCount:
    """Test _event_count returns correct counts."""

    def test_total_count(self, tmp_path: Path):
        _seed_db(tmp_path)
        with _patch_db(tmp_path):
            count = _event_count()
        assert count == 13

    def test_count_with_event_type(self, tmp_path: Path):
        _seed_db(tmp_path)
        with _patch_db(tmp_path):
            count = _event_count(event_type="tool_use")
        assert count == 4

    def test_count_with_session_id(self, tmp_path: Path):
        _seed_db(tmp_path)
        with _patch_db(tmp_path):
            count = _event_count(session_id="sess-1")
        assert count == 9

    def test_count_with_since(self, tmp_path: Path):
        _seed_db(tmp_path)
        with _patch_db(tmp_path):
            count = _event_count(since="2026-01-10T11:00:00+00:00")
        assert count == 4

    def test_count_error_returns_zero(self):
        with patch("supercharge.metrics._db_path", side_effect=RuntimeError):
            count = _event_count()
        assert count == 0


class TestQuerySessions:
    """Test _query_sessions returns session summaries."""

    def test_returns_all_sessions(self, tmp_path: Path):
        _seed_db(tmp_path)
        with _patch_db(tmp_path):
            sessions = _query_sessions()
        assert len(sessions) == 2

    def test_session_fields(self, tmp_path: Path):
        _seed_db(tmp_path)
        with _patch_db(tmp_path):
            sessions = _query_sessions()
        s1 = next(s for s in sessions if s["session_id"] == "sess-1")
        assert s1["first_timestamp"] == "2026-01-10T10:00:00+00:00"
        assert s1["last_timestamp"] == "2026-01-10T10:00:08+00:00"
        assert s1["duration_seconds"] == 8.0
        assert s1["event_count"] == 9
        assert s1["agent_count"] == 3
        assert s1["worker_count"] == 1
        assert s1["tool_call_count"] == 3
        assert "code" in s1["agent_types"]
        assert "orchestrator" in s1["agent_types"]

    def test_session_2_fields(self, tmp_path: Path):
        _seed_db(tmp_path)
        with _patch_db(tmp_path):
            sessions = _query_sessions()
        s2 = next(s for s in sessions if s["session_id"] == "sess-2")
        assert s2["event_count"] == 4
        assert s2["duration_seconds"] == 10.0
        assert s2["tool_call_count"] == 1
        assert "research" in s2["agent_types"]

    def test_empty_db(self, tmp_path: Path):
        db = tmp_path / "metrics.db"
        conn = sqlite3.connect(str(db))
        _init_db(conn)
        conn.close()
        with _patch_db(tmp_path):
            sessions = _query_sessions()
        assert sessions == []

    def test_error_returns_empty(self):
        with patch("supercharge.metrics._db_path", side_effect=RuntimeError):
            sessions = _query_sessions()
        assert sessions == []


class TestQuerySessionEvents:
    """Test _query_session_events returns ordered events for a session."""

    def test_returns_all_events_for_session(self, tmp_path: Path):
        _seed_db(tmp_path)
        with _patch_db(tmp_path):
            events = _query_session_events("sess-1")
        assert len(events) == 9
        assert all(e["session_id"] == "sess-1" for e in events)

    def test_ordered_by_timestamp_asc(self, tmp_path: Path):
        _seed_db(tmp_path)
        with _patch_db(tmp_path):
            events = _query_session_events("sess-1")
        timestamps = [e["timestamp"] for e in events]
        assert timestamps == sorted(timestamps)

    def test_nonexistent_session(self, tmp_path: Path):
        _seed_db(tmp_path)
        with _patch_db(tmp_path):
            events = _query_session_events("nonexistent")
        assert events == []

    def test_error_returns_empty(self):
        with patch("supercharge.metrics._db_path", side_effect=RuntimeError):
            events = _query_session_events("sess-1")
        assert events == []


class TestQueryStats:
    """Test _query_stats returns global aggregates."""

    def test_totals(self, tmp_path: Path):
        _seed_db(tmp_path)
        with _patch_db(tmp_path):
            stats = _query_stats()
        totals = stats["totals"]
        assert totals["sessions"] == 2
        assert totals["events"] == 13
        assert totals["agents"] == 5
        assert totals["workers"] == 1
        assert totals["tool_calls"] == 4

    def test_by_agent_type(self, tmp_path: Path):
        _seed_db(tmp_path)
        with _patch_db(tmp_path):
            stats = _query_stats()
        by_type = stats["by_agent_type"]
        assert "code" in by_type
        assert "orchestrator" in by_type
        assert "research" in by_type

    def test_by_event_type(self, tmp_path: Path):
        _seed_db(tmp_path)
        with _patch_db(tmp_path):
            stats = _query_stats()
        by_event = stats["by_event_type"]
        assert by_event["tool_use"] == 4
        assert by_event["session_start"] == 2
        assert by_event["task_init"] == 2

    def test_path_frequencies(self, tmp_path: Path):
        _seed_db(tmp_path)
        with _patch_db(tmp_path):
            stats = _query_stats()
        paths = stats["path_frequencies"]
        assert isinstance(paths, list)
        assert all("path" in p and "count" in p for p in paths)

    def test_averages(self, tmp_path: Path):
        _seed_db(tmp_path)
        with _patch_db(tmp_path):
            stats = _query_stats()
        avgs = stats["averages"]
        assert "session_duration" in avgs
        assert "agents_per_session" in avgs
        assert avgs["session_duration"] == 9.0

    def test_timeline(self, tmp_path: Path):
        _seed_db(tmp_path)
        with _patch_db(tmp_path):
            stats = _query_stats()
        timeline = stats["timeline"]
        assert isinstance(timeline, list)
        assert len(timeline) == 2
        assert all("hour" in t and "count" in t for t in timeline)

    def test_error_returns_empty_dict(self):
        with patch("supercharge.metrics._db_path", side_effect=RuntimeError):
            stats = _query_stats()
        assert stats == {}

    def test_empty_db(self, tmp_path: Path):
        db = tmp_path / "metrics.db"
        conn = sqlite3.connect(str(db))
        _init_db(conn)
        conn.close()
        with _patch_db(tmp_path):
            stats = _query_stats()
        assert stats["totals"]["sessions"] == 0
        assert stats["totals"]["events"] == 0


class TestCwdVoting:
    """Test _vote_session_project CWD voting logic."""

    def _write_jsonl(self, path: Path, messages: list) -> None:
        path.write_text("\n".join(json.dumps(m) for m in messages) + "\n")

    def test_vote_uniform_cwds_settles_at_5(self, tmp_path: Path):
        """5 consecutive identical CWDs with no other CWD ever seen → Rule 1 settles early."""
        jsonl = tmp_path / "session.jsonl"
        messages = [{"type": "user", "cwd": "/projects/myapp", "sessionId": "test-session"}] * 5
        self._write_jsonl(jsonl, messages)

        with patch("supercharge.metrics._find_session_jsonl", return_value=jsonl):
            result = _vote_session_project("test-session")

        assert result == "/projects/myapp"

    def test_vote_dominant_cwd_settles_at_20(self, tmp_path: Path):
        """17 of 20 CWD votes for one path (85% > 80%) → Rule 2 settles at 20 votes."""
        jsonl = tmp_path / "session.jsonl"
        # Scatter 3 'other' messages so Rule 1 never triggers (len(cwd_counts) > 1 from msg 4)
        # Layout: [main*3, other, main*13, other, main, other] = 17 main + 3 other = 20
        messages = []
        for _ in range(3):
            messages.append({"type": "user", "cwd": "/projects/main", "sessionId": "s"})
        messages.append({"type": "user", "cwd": "/projects/other", "sessionId": "s"})
        for _ in range(13):
            messages.append({"type": "user", "cwd": "/projects/main", "sessionId": "s"})
        messages.append({"type": "user", "cwd": "/projects/other", "sessionId": "s"})
        messages.append({"type": "user", "cwd": "/projects/main", "sessionId": "s"})
        messages.append({"type": "user", "cwd": "/projects/other", "sessionId": "s"})
        assert len(messages) == 20
        self._write_jsonl(jsonl, messages)

        with patch("supercharge.metrics._find_session_jsonl", return_value=jsonl):
            result = _vote_session_project("test-session")

        assert result == "/projects/main"

    def test_vote_mixed_cwds_uses_most_frequent_after_50(self, tmp_path: Path):
        """30 alpha + 20 beta interleaved → neither Rule 1 nor 2 fires; Rule 3 returns most frequent."""
        jsonl = tmp_path / "session.jsonl"
        # Alternate alpha/beta for 40 messages (20/20), then 10 more alpha
        # At every 20-vote checkpoint share is 50%, so Rule 2 never fires before 50
        messages = []
        for _ in range(20):
            messages.append({"type": "user", "cwd": "/projects/alpha", "sessionId": "s"})
            messages.append({"type": "user", "cwd": "/projects/beta", "sessionId": "s"})
        for _ in range(10):
            messages.append({"type": "user", "cwd": "/projects/alpha", "sessionId": "s"})
        assert len(messages) == 50
        self._write_jsonl(jsonl, messages)

        with patch("supercharge.metrics._find_session_jsonl", return_value=jsonl):
            result = _vote_session_project("test-session")

        assert result == "/projects/alpha"

    def test_vote_no_cwd_returns_none(self, tmp_path: Path):
        """Messages with no cwd field → returns None (no votes cast)."""
        jsonl = tmp_path / "session.jsonl"
        messages = [{"type": "user", "sessionId": "s"} for _ in range(10)]
        self._write_jsonl(jsonl, messages)

        with patch("supercharge.metrics._find_session_jsonl", return_value=jsonl):
            result = _vote_session_project("test-session")

        assert result is None

    def test_vote_skips_non_cwd_messages(self, tmp_path: Path):
        """Non-cwd messages are skipped; 3 cwd messages all for same path return that path."""
        jsonl = tmp_path / "session.jsonl"
        messages = []
        for _ in range(3):
            messages.append({"type": "user", "cwd": "/projects/x", "sessionId": "s"})
        for _ in range(5):
            messages.append({"type": "assistant", "sessionId": "s"})
        self._write_jsonl(jsonl, messages)

        with patch("supercharge.metrics._find_session_jsonl", return_value=jsonl):
            result = _vote_session_project("test-session")

        assert result == "/projects/x"


class TestFindSessionJsonlCrossProject:
    """Test _find_session_jsonl scans across all project slug dirs."""

    def test_find_session_across_projects(self, tmp_path: Path):
        """Session JSONL file placed in one of several slug dirs is found correctly."""
        # Mimic ~/.claude/projects/ with two slug dirs
        slug1 = tmp_path / "projects" / "slug-aaa"
        slug2 = tmp_path / "projects" / "slug-bbb"
        slug1.mkdir(parents=True)
        slug2.mkdir(parents=True)

        # Place the session file only in the second slug dir
        session_file = slug2 / "test-session-123.jsonl"
        session_file.write_text('{"type": "user", "sessionId": "test-session-123"}\n')

        with patch("supercharge.metrics._user_config_dir", return_value=tmp_path):
            result = _find_session_jsonl("test-session-123")

        assert result == session_file

    def test_find_session_returns_none_when_missing(self, tmp_path: Path):
        """Returns None when the session file doesn't exist in any slug dir."""
        slug1 = tmp_path / "projects" / "slug-aaa"
        slug2 = tmp_path / "projects" / "slug-bbb"
        slug1.mkdir(parents=True)
        slug2.mkdir(parents=True)

        with patch("supercharge.metrics._user_config_dir", return_value=tmp_path):
            result = _find_session_jsonl("test-session-123")

        assert result is None
