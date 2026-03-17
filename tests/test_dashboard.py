"""Tests for the dashboard server module."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from supercharge.metrics import _init_db

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_db(tmp_path: Path, rows: list[tuple] | None = None) -> Path:
    """Create a metrics DB, optionally seeded with event rows."""
    db = tmp_path / "metrics.db"
    conn = sqlite3.connect(str(db))
    _init_db(conn)
    if rows:
        for row in rows:
            conn.execute(
                "INSERT INTO events (timestamp, event_type, session_id, agent_id, "
                "agent_type, task_uuid, worker_id, parent_id, tool_name, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
    conn.commit()
    conn.close()
    return db


def _patch_db(tmp_path: Path):
    return patch("supercharge.metrics._db_path", return_value=tmp_path / "metrics.db")


def _sample_rows():
    return [
        ("2026-01-10T10:00:00+00:00", "session_start", "s1", "a1", "orchestrator", "", "", "", "", ""),
        ("2026-01-10T10:00:01+00:00", "task_init", "s1", "a2", "code", "t1", "", "orchestrator:s1", "", ""),
        ("2026-01-10T10:00:02+00:00", "tool_use", "s1", "a2", "code", "t1", "", "", "Bash", "ls"),
        ("2026-01-10T10:00:03+00:00", "tool_use", "s1", "a2", "code", "t1", "", "", "Read", "file.py"),
    ]


# ── PID file tests ───────────────────────────────────────────────────────────


class TestPidFile:
    def test_write_and_read(self, tmp_path):
        from supercharge.dashboard import _read_pidfile, _write_pidfile

        pidfile = tmp_path / "dashboard.pid"
        with patch("supercharge.dashboard._pidfile_path", return_value=pidfile):
            _write_pidfile(1234, 9333)
            result = _read_pidfile()
            assert result == (1234, 9333)

    def test_read_missing(self, tmp_path):
        from supercharge.dashboard import _read_pidfile

        pidfile = tmp_path / "dashboard.pid"
        with patch("supercharge.dashboard._pidfile_path", return_value=pidfile):
            assert _read_pidfile() is None

    def test_cleanup(self, tmp_path):
        from supercharge.dashboard import _cleanup_pidfile, _write_pidfile

        pidfile = tmp_path / "dashboard.pid"
        with patch("supercharge.dashboard._pidfile_path", return_value=pidfile):
            _write_pidfile(1234, 9333)
            assert pidfile.exists()
            _cleanup_pidfile()
            assert not pidfile.exists()

    def test_cleanup_missing_file_no_error(self, tmp_path):
        from supercharge.dashboard import _cleanup_pidfile

        pidfile = tmp_path / "dashboard.pid"
        with patch("supercharge.dashboard._pidfile_path", return_value=pidfile):
            _cleanup_pidfile()  # Should not raise

    def test_stale_pid_detected(self, tmp_path):
        """A PID file with a dead process should be treated as stale."""
        from supercharge.dashboard import _read_pidfile, _write_pidfile

        pidfile = tmp_path / "dashboard.pid"
        with patch("supercharge.dashboard._pidfile_path", return_value=pidfile):
            # Write a PID that almost certainly doesn't exist
            _write_pidfile(999999999, 9333)
            # _read_pidfile returns the tuple regardless; stale detection
            # happens in _run_server. Verify we can at least read it.
            result = _read_pidfile()
            assert result == (999999999, 9333)

    def test_corrupt_pidfile_returns_none(self, tmp_path):
        from supercharge.dashboard import _read_pidfile

        pidfile = tmp_path / "dashboard.pid"
        pidfile.write_text("garbage\ndata\n")
        with patch("supercharge.dashboard._pidfile_path", return_value=pidfile):
            assert _read_pidfile() is None


# ── Port finding tests ───────────────────────────────────────────────────────


class TestFindFreePort:
    def test_default_port_available(self):

        from supercharge.dashboard import _find_free_port

        with patch("socket.socket") as mock_sock_cls:
            mock_sock = MagicMock()
            mock_sock_cls.return_value.__enter__ = MagicMock(return_value=mock_sock)
            mock_sock_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_sock.bind.return_value = None  # bind succeeds

            port = _find_free_port(default=9333)
            assert port == 9333

    def test_falls_back_when_port_busy(self):

        from supercharge.dashboard import _find_free_port

        call_count = 0

        def bind_side_effect(addr):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise OSError("Address already in use")

        with patch("socket.socket") as mock_sock_cls:
            mock_sock = MagicMock()
            mock_sock_cls.return_value.__enter__ = MagicMock(return_value=mock_sock)
            mock_sock_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_sock.bind.side_effect = bind_side_effect

            port = _find_free_port(default=9333)
            assert port == 9335  # First two fail, third (9335) succeeds

    def test_raises_when_all_ports_busy(self):
        from supercharge.dashboard import _find_free_port

        with patch("socket.socket") as mock_sock_cls:
            mock_sock = MagicMock()
            mock_sock_cls.return_value.__enter__ = MagicMock(return_value=mock_sock)
            mock_sock_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_sock.bind.side_effect = OSError("Address already in use")

            with pytest.raises(RuntimeError, match="No free port found"):
                _find_free_port(default=9333, max_attempts=3)


# ── API endpoint tests ───────────────────────────────────────────────────────


class TestApiEndpoints:
    @pytest.fixture(autouse=True)
    def setup_db(self, tmp_path):
        self.tmp_path = tmp_path
        _make_db(tmp_path, _sample_rows())
        self.db_patch = _patch_db(tmp_path)
        self.db_patch.start()
        yield
        self.db_patch.stop()

    @pytest.fixture()
    def client(self):
        from supercharge.dashboard import _create_app

        app = _create_app()
        return TestClient(app)

    def test_root_returns_html(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_sessions(self, client):
        resp = client.get("/api/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        assert data[0]["session_id"] == "s1"

    def test_session_tree(self, client):
        resp = client.get("/api/sessions/s1/tree")
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "session"
        assert data["id"] == "s1"

    def test_stats(self, client):
        resp = client.get("/api/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "totals" in data

    def test_events_default(self, client):
        resp = client.get("/api/events")
        assert resp.status_code == 200
        data = resp.json()
        assert "events" in data
        assert "total" in data
        assert len(data["events"]) == 4

    def test_events_filter_by_type(self, client):
        resp = client.get("/api/events?event_type=tool_use")
        assert resp.status_code == 200
        data = resp.json()
        assert all(e["event_type"] == "tool_use" for e in data["events"])
        assert len(data["events"]) == 2

    def test_events_filter_by_session(self, client):
        resp = client.get("/api/events?session_id=s1")
        assert resp.status_code == 200
        assert len(resp.json()["events"]) == 4

    def test_events_pagination(self, client):
        resp = client.get("/api/events?limit=2&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["events"]) == 2

    def test_events_order_desc(self, client):
        resp = client.get("/api/events?order=desc")
        assert resp.status_code == 200
        data = resp.json()
        ids = [e["id"] for e in data["events"]]
        assert ids == sorted(ids, reverse=True)

    def test_events_since_until(self, client):
        resp = client.get(
            "/api/events?since=2026-01-10T10:00:02%2B00:00&until=2026-01-10T10:00:03%2B00:00"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["events"]) == 2

    def test_events_invalid_limit_returns_400(self, client):
        resp = client.get("/api/events?limit=abc")
        assert resp.status_code == 400
        assert "error" in resp.json()

    def test_events_invalid_offset_returns_400(self, client):
        resp = client.get("/api/events?offset=xyz")
        assert resp.status_code == 400
        assert "error" in resp.json()

    def test_events_invalid_order_returns_400(self, client):
        resp = client.get("/api/events?order=sideways")
        assert resp.status_code == 400
        assert "error" in resp.json()

    def test_events_limit_capped_at_1000(self, client):
        resp = client.get("/api/events?limit=9999")
        assert resp.status_code == 200
        data = resp.json()
        assert data["limit"] == 1000

    def test_session_tools(self, client):
        resp = client.get("/api/sessions/s1/tools")
        assert resp.status_code == 200
        data = resp.json()
        assert "agents" in data
        assert "totals" in data
        assert data["totals"] == {"Bash": 1, "Read": 1}
        assert len(data["agents"]) == 1
        agent = data["agents"][0]
        assert agent["agent_id"] == "a2"
        assert agent["agent_type"] == "code"
        tool_names = {t["tool_name"] for t in agent["tools"]}
        assert tool_names == {"Bash", "Read"}

    def test_session_tools_empty(self, client):
        resp = client.get("/api/sessions/nonexistent/tools")
        assert resp.status_code == 200
        data = resp.json()
        assert data == {"agents": [], "totals": {}}

    def test_task_content_valid(self, client, tmp_path):
        task_dir = tmp_path / "task_abc123"
        task_dir.mkdir()
        (task_dir / "task.md").write_text("# My Task\nDo stuff")
        (task_dir / "result.md").write_text("# Result\nDone")
        (task_dir / "notes.md").write_text("# Notes\nSome notes")

        with patch("supercharge.dashboard._find_task_dir", return_value=task_dir):
            resp = client.get("/api/tasks/abc123/content")
        assert resp.status_code == 200
        data = resp.json()
        assert data["task_uuid"] == "abc123"
        assert data["task_md"] == "# My Task\nDo stuff"
        assert data["result_md"] == "# Result\nDone"
        assert data["notes_md"] == "# Notes\nSome notes"

    def test_task_content_missing_dir(self, client):
        with patch("supercharge.dashboard._find_task_dir", return_value=None):
            resp = client.get("/api/tasks/nonexistent/content")
        assert resp.status_code == 404
        assert "error" in resp.json()

    def test_task_content_partial_files(self, client, tmp_path):
        task_dir = tmp_path / "task_partial"
        task_dir.mkdir()
        (task_dir / "task.md").write_text("# Task Only")

        with patch("supercharge.dashboard._find_task_dir", return_value=task_dir):
            resp = client.get("/api/tasks/partial/content")
        assert resp.status_code == 200
        data = resp.json()
        assert data["task_md"] == "# Task Only"
        assert data["result_md"] == ""
        assert data["notes_md"] == ""

    def test_browse(self, client):
        # browse depends on filesystem; just verify the endpoint returns JSON
        with patch("supercharge.dashboard.browse._build_browse_response") as mock_browse:
            mock_browse.return_value = {"root": "/tmp", "tasks": {}, "archive": [], "tree": None}
            resp = client.get("/api/browse")
            assert resp.status_code == 200
            data = resp.json()
            assert "root" in data


# ── Tools query function tests ───────────────────────────────────────────────


class TestQuerySessionTools:
    def test_returns_per_agent_breakdown(self, tmp_path):
        rows = [
            ("2026-01-10T10:00:00+00:00", "session_start", "s1", "a1", "orchestrator", "", "", "", "", ""),
            ("2026-01-10T10:00:01+00:00", "tool_use", "s1", "a2", "code", "t1", "", "", "Bash", "ls"),
            ("2026-01-10T10:00:02+00:00", "tool_use", "s1", "a2", "code", "t1", "", "", "Bash", "pwd"),
            ("2026-01-10T10:00:03+00:00", "tool_use", "s1", "a2", "code", "t1", "", "", "Read", "f.py"),
            ("2026-01-10T10:00:04+00:00", "tool_use", "s1", "a3", "code", "t2", "w1", "", "Edit", "g.py"),
        ]
        _make_db(tmp_path, rows)

        from supercharge.metrics import _query_session_tools

        with _patch_db(tmp_path):
            result = _query_session_tools("s1")

        assert result["totals"] == {"Bash": 2, "Read": 1, "Edit": 1}
        assert len(result["agents"]) == 2

        # Find agent a2
        a2 = next(a for a in result["agents"] if a["agent_id"] == "a2")
        assert a2["agent_type"] == "code"
        tool_map = {t["tool_name"]: t["count"] for t in a2["tools"]}
        assert tool_map == {"Bash": 2, "Read": 1}

    def test_empty_session(self, tmp_path):
        _make_db(tmp_path, [])

        from supercharge.metrics import _query_session_tools

        with _patch_db(tmp_path):
            result = _query_session_tools("nonexistent")

        assert result == {"agents": [], "totals": {}}


# ── Span tool attribution tests ───────────────────────────────────────────────


class TestSpanToolAttribution:
    """Tests for timestamp-based tool attribution in _query_session_spans (Fix 2)."""

    def test_tools_attributed_to_narrowest_span(self, tmp_path):
        """Tool events with empty agent_id should be attributed to the narrowest
        containing span, not double-counted across overlapping spans."""
        rows = [
            # Orchestrator span: 10:00:00 - 10:05:00
            ("2026-01-10T10:00:00+00:00", "subagent_start", "s1", "orch1", "orchestrator", "", "", "", "", ""),
            ("2026-01-10T10:05:00+00:00", "subagent_stop", "s1", "orch1", "orchestrator", "", "", "", "", ""),
            # Code agent span (child): 10:01:00 - 10:03:00
            ("2026-01-10T10:01:00+00:00", "subagent_start", "s1", "code1", "code", "", "", "orchestrator:s1", "", ""),
            ("2026-01-10T10:03:00+00:00", "subagent_stop", "s1", "code1", "code", "", "", "", "", ""),
            # Tool events with empty agent_id (real session behavior)
            # These fall within the code agent's time window
            ("2026-01-10T10:01:30+00:00", "tool_use", "s1", "", "", "", "", "", "Bash", "ls"),
            ("2026-01-10T10:02:00+00:00", "tool_use", "s1", "", "", "", "", "", "Read", "file.py"),
            ("2026-01-10T10:02:30+00:00", "tool_use", "s1", "", "", "", "", "", "Edit", "file.py"),
            # Tool event outside code agent's window but inside orchestrator's
            ("2026-01-10T10:04:00+00:00", "tool_use", "s1", "", "", "", "", "", "Bash", "pwd"),
        ]
        _make_db(tmp_path, rows)

        from supercharge.metrics import _query_session_spans

        with _patch_db(tmp_path):
            spans = _query_session_spans("s1")

        # Find the code agent span — should have 3 tools (narrowest match)
        code_span = next(s for s in spans if s["id"] == "code1")
        assert code_span["tool_calls"] == 3

        # Orchestrator span should have only 1 tool (the one outside code agent's window)
        orch_span = next(s for s in spans if s["id"] == "orch1")
        assert orch_span["tool_calls"] == 1

    def test_tools_attributed_with_agent_id_set(self, tmp_path):
        """Tool events with agent_id set should be attributed by agent_id lookup,
        not timestamp-based fallback."""
        rows = [
            ("2026-01-10T10:00:00+00:00", "subagent_start", "s1", "a1", "code", "", "", "", "", ""),
            ("2026-01-10T10:05:00+00:00", "subagent_stop", "s1", "a1", "code", "", "", "", "", ""),
            # Tool with matching agent_id
            ("2026-01-10T10:01:00+00:00", "tool_use", "s1", "a1", "code", "", "", "", "Bash", "ls"),
            ("2026-01-10T10:02:00+00:00", "tool_use", "s1", "a1", "code", "", "", "", "Read", "f"),
        ]
        _make_db(tmp_path, rows)

        from supercharge.metrics import _query_session_spans

        with _patch_db(tmp_path):
            spans = _query_session_spans("s1")

        agent_span = next(s for s in spans if s["id"] == "a1")
        assert agent_span["tool_calls"] == 2


# ── Session filtering tests ──────────────────────────────────────────────────


class TestSessionFiltering:
    """Tests for session list filtering (Fix 4)."""

    def test_sessions_with_no_agents_and_no_tools_excluded(self, tmp_path):
        """Sessions with >1 event but 0 agents, 0 workers, and 0 tools
        should be excluded from the session list."""
        rows = [
            # Good session: has agents and tools
            ("2026-01-10T10:00:00+00:00", "session_start", "s_good", "a1", "orchestrator", "", "", "", "", ""),
            ("2026-01-10T10:00:01+00:00", "tool_use", "s_good", "a1", "orchestrator", "", "", "", "Bash", "ls"),
            # Bad session: has multiple events but no agents, no workers, no tools
            ("2026-01-10T10:00:00+00:00", "session_start", "s_empty", "", "", "", "", "", "", ""),
            ("2026-01-10T10:00:01+00:00", "session_start", "s_empty", "", "", "", "", "", "", ""),
        ]
        _make_db(tmp_path, rows)

        from supercharge.metrics import _query_sessions

        with _patch_db(tmp_path):
            sessions = _query_sessions()

        session_ids = [s["session_id"] for s in sessions]
        assert "s_good" in session_ids
        assert "s_empty" not in session_ids

    def test_sessions_with_tools_but_no_agents_included(self, tmp_path):
        """Sessions with tool_use events should be included even without agents."""
        rows = [
            ("2026-01-10T10:00:00+00:00", "session_start", "s_tools", "", "", "", "", "", "", ""),
            ("2026-01-10T10:00:01+00:00", "tool_use", "s_tools", "", "", "", "", "", "Bash", "ls"),
        ]
        _make_db(tmp_path, rows)

        from supercharge.metrics import _query_sessions

        with _patch_db(tmp_path):
            sessions = _query_sessions()

        session_ids = [s["session_id"] for s in sessions]
        assert "s_tools" in session_ids


# ── SSE streaming tests ──────────────────────────────────────────────────────


class TestSSE:
    def test_sse_generator_yields_events(self, tmp_path):
        """Test the SSE generator logic directly (without HTTP)."""
        _make_db(tmp_path, _sample_rows())

        import supercharge.metrics as metrics

        with _patch_db(tmp_path):
            # Query events that would be streamed
            events = metrics._query_events(after_id=0, limit=100, order="asc")
            assert len(events) > 0

            # Verify Last-Event-ID filtering works
            events_after_2 = metrics._query_events(after_id=2, limit=100, order="asc")
            assert all(e["id"] > 2 for e in events_after_2)


# ── Singleton check tests ────────────────────────────────────────────────────


class TestSingleton:
    def test_already_running_prints_url(self, tmp_path, capsys):
        from supercharge.dashboard import _run_server, _write_pidfile

        pidfile = tmp_path / "dashboard.pid"

        with (
            patch("supercharge.dashboard._pidfile_path", return_value=pidfile),
            patch("os.kill") as mock_kill,  # Make process appear alive
        ):
            mock_kill.return_value = None  # No exception = process alive
            _write_pidfile(os.getpid(), 9333)

            _run_server(host="127.0.0.1", port=9333)

            captured = capsys.readouterr()
            assert "127.0.0.1:9333" in captured.out

    def test_stale_pid_allows_startup(self, tmp_path):
        from supercharge.dashboard import _write_pidfile

        pidfile = tmp_path / "dashboard.pid"

        with (
            patch("supercharge.dashboard._pidfile_path", return_value=pidfile),
            patch("os.kill", side_effect=ProcessLookupError),  # Process is dead
            patch("supercharge.dashboard._find_free_port", return_value=9333),
            patch("uvicorn.run") as mock_uvicorn,
        ):
            _write_pidfile(999999999, 9333)

            from supercharge.dashboard import _run_server

            _run_server(host="127.0.0.1", port=9333)

            mock_uvicorn.assert_called_once()
