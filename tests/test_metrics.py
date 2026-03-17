"""Tests for metrics collection module (SQLite-backed event store)."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from supercharge.metrics import _db_path, _emit, _init_db, _open_readonly, _query_events

# ── _db_path ─────────────────────────────────────────────────────────────────


@pytest.mark.no_isolate_metrics
class TestDbPath:
    """Test _db_path returns the correct path."""

    def test_returns_metrics_db_under_project(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/fake/project")
        result = _db_path()
        assert result == Path("/fake/project/.claude/SuperchargeAI/metrics.db")

    def test_uses_project_dir(self, monkeypatch):
        """Verify it delegates to _project_dir()."""
        with patch("supercharge.metrics._project_dir", return_value="/mock/root"):
            result = _db_path()
        assert result == Path("/mock/root/.claude/SuperchargeAI/metrics.db")


# ── _init_db ─────────────────────────────────────────────────────────────────


class TestInitDb:
    """Test _init_db creates the events table with correct schema."""

    def test_creates_events_table(self, tmp_path: Path):
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        _init_db(conn)

        # Verify table exists
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='events'"
        )
        assert cursor.fetchone() is not None
        conn.close()

    def test_schema_columns(self, tmp_path: Path):
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        _init_db(conn)

        cursor = conn.execute("PRAGMA table_info(events)")
        columns = {row[1] for row in cursor.fetchall()}
        expected = {
            "id",
            "timestamp",
            "event_type",
            "session_id",
            "agent_id",
            "agent_type",
            "task_uuid",
            "worker_id",
            "parent_id",
            "tool_name",
            "detail",
        }
        assert columns == expected
        conn.close()

    def test_idempotent(self, tmp_path: Path):
        """Calling _init_db twice does not raise."""
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        _init_db(conn)
        _init_db(conn)
        conn.close()

    def test_indexes_created(self, tmp_path: Path):
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        _init_db(conn)

        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
        indexes = {row[0] for row in cursor.fetchall()}
        assert "idx_events_type" in indexes
        assert "idx_events_session" in indexes
        assert "idx_events_task" in indexes
        conn.close()


# ── _emit ────────────────────────────────────────────────────────────────────


class TestEmit:
    """Test _emit writes events correctly."""

    def _patch_db_path(self, tmp_path: Path):
        return patch("supercharge.metrics._db_path", return_value=tmp_path / "metrics.db")

    def test_writes_event(self, tmp_path: Path):
        with self._patch_db_path(tmp_path):
            _emit("task_init", session_id="s1", task_uuid="t1")

        conn = sqlite3.connect(str(tmp_path / "metrics.db"))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM events").fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row["event_type"] == "task_init"
        assert row["session_id"] == "s1"
        assert row["task_uuid"] == "t1"
        assert row["timestamp"]  # non-empty ISO timestamp
        conn.close()

    def test_auto_creates_db(self, tmp_path: Path):
        db_path = tmp_path / "subdir" / "metrics.db"
        with patch("supercharge.metrics._db_path", return_value=db_path):
            _emit("session_start")

        assert db_path.exists()
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT count(*) FROM events").fetchone()
        assert rows[0] == 1
        conn.close()

    def test_never_raises_broken_path(self):
        """_emit with an impossible path must not raise."""
        with patch(
            "supercharge.metrics._db_path",
            return_value=Path("/nonexistent/path/metrics.db"),
        ):
            _emit("test_event")  # should not raise

    def test_never_raises_on_error(self, tmp_path: Path):
        """_emit must never raise, even with unexpected errors."""
        with patch("supercharge.metrics._db_path", side_effect=RuntimeError("boom")):
            _emit("test_event")  # should not raise

    def test_default_empty_strings(self, tmp_path: Path):
        """Columns not passed default to empty string."""
        with self._patch_db_path(tmp_path):
            _emit("minimal_event")

        conn = sqlite3.connect(str(tmp_path / "metrics.db"))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM events").fetchone()
        assert row["session_id"] == ""
        assert row["agent_id"] == ""
        assert row["agent_type"] == ""
        assert row["task_uuid"] == ""
        assert row["worker_id"] == ""
        assert row["parent_id"] == ""
        assert row["tool_name"] == ""
        assert row["detail"] == ""
        conn.close()

    def test_all_kwargs(self, tmp_path: Path):
        """All supported kwargs are stored."""
        with self._patch_db_path(tmp_path):
            _emit(
                "tool_use",
                session_id="s1",
                agent_id="a1",
                agent_type="code",
                task_uuid="t1",
                worker_id="w1",
                parent_id="p1",
                tool_name="Bash",
                detail='{"cmd":"ls"}',
            )

        conn = sqlite3.connect(str(tmp_path / "metrics.db"))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM events").fetchone()
        assert row["session_id"] == "s1"
        assert row["agent_id"] == "a1"
        assert row["agent_type"] == "code"
        assert row["task_uuid"] == "t1"
        assert row["worker_id"] == "w1"
        assert row["parent_id"] == "p1"
        assert row["tool_name"] == "Bash"
        assert row["detail"] == '{"cmd":"ls"}'
        conn.close()

    def test_concurrent_writes(self, tmp_path: Path):
        """50+ concurrent _emit calls must all succeed (WAL mode)."""
        n = 60
        errors: list[Exception] = []

        def emit_one(i: int):
            try:
                with patch(
                    "supercharge.metrics._db_path",
                    return_value=tmp_path / "metrics.db",
                ):
                    _emit("concurrent", session_id=f"s{i}")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=emit_one, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Concurrent writes raised: {errors}"

        conn = sqlite3.connect(str(tmp_path / "metrics.db"))
        count = conn.execute("SELECT count(*) FROM events").fetchone()[0]
        assert count == n
        conn.close()

    def test_wal_mode_set(self, tmp_path: Path):
        """_emit sets WAL journal mode on the connection."""
        with self._patch_db_path(tmp_path):
            _emit("wal_test")

        conn = sqlite3.connect(str(tmp_path / "metrics.db"))
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
        conn.close()

    def test_timestamp_is_iso_utc(self, tmp_path: Path):
        """Timestamp should be a valid ISO 8601 string with UTC timezone."""
        with self._patch_db_path(tmp_path):
            _emit("ts_test")

        conn = sqlite3.connect(str(tmp_path / "metrics.db"))
        ts = conn.execute("SELECT timestamp FROM events").fetchone()[0]
        assert "T" in ts  # ISO format has T separator
        assert "+" in ts or "Z" in ts or ts.endswith("+00:00")  # UTC indicator
        conn.close()


# ── _query_events ────────────────────────────────────────────────────────────


class TestQueryEvents:
    """Test _query_events filtering and error handling."""

    def _patch_db_path(self, tmp_path: Path):
        return patch("supercharge.metrics._db_path", return_value=tmp_path / "metrics.db")

    def _seed_events(self, tmp_path: Path):
        """Insert a few test events."""
        with self._patch_db_path(tmp_path):
            _emit("task_init", session_id="s1", task_uuid="t1")
            _emit("tool_use", session_id="s1", task_uuid="t1", tool_name="Bash")
            _emit("task_init", session_id="s2", task_uuid="t2")
            _emit("session_start", session_id="s1")
            _emit("tool_use", session_id="s2", task_uuid="t2", tool_name="Read")

    def test_query_all(self, tmp_path: Path):
        self._seed_events(tmp_path)
        with self._patch_db_path(tmp_path):
            results = _query_events()
        assert len(results) == 5

    def test_filter_by_event_type(self, tmp_path: Path):
        self._seed_events(tmp_path)
        with self._patch_db_path(tmp_path):
            results = _query_events(event_type="task_init")
        assert len(results) == 2
        assert all(r["event_type"] == "task_init" for r in results)

    def test_filter_by_session_id(self, tmp_path: Path):
        self._seed_events(tmp_path)
        with self._patch_db_path(tmp_path):
            results = _query_events(session_id="s1")
        assert len(results) == 3
        assert all(r["session_id"] == "s1" for r in results)

    def test_filter_by_task_uuid(self, tmp_path: Path):
        self._seed_events(tmp_path)
        with self._patch_db_path(tmp_path):
            results = _query_events(task_uuid="t2")
        assert len(results) == 2
        assert all(r["task_uuid"] == "t2" for r in results)

    def test_combined_filters(self, tmp_path: Path):
        self._seed_events(tmp_path)
        with self._patch_db_path(tmp_path):
            results = _query_events(event_type="tool_use", session_id="s1")
        assert len(results) == 1
        assert results[0]["tool_name"] == "Bash"

    def test_limit(self, tmp_path: Path):
        self._seed_events(tmp_path)
        with self._patch_db_path(tmp_path):
            results = _query_events(limit=2)
        assert len(results) == 2

    def test_returns_dicts(self, tmp_path: Path):
        self._seed_events(tmp_path)
        with self._patch_db_path(tmp_path):
            results = _query_events(limit=1)
        assert isinstance(results[0], dict)
        assert "event_type" in results[0]
        assert "timestamp" in results[0]

    def test_returns_empty_on_error(self):
        """_query_events returns empty list on error (never raises)."""
        with patch(
            "supercharge.metrics._db_path",
            return_value=Path("/nonexistent/path/metrics.db"),
        ):
            results = _query_events()
        assert results == []

    def test_returns_empty_on_exception(self):
        """_query_events returns empty list on unexpected error."""
        with patch("supercharge.metrics._db_path", side_effect=RuntimeError("boom")):
            results = _query_events()
        assert results == []

    def test_empty_db(self, tmp_path: Path):
        """Querying an empty (but valid) DB returns empty list."""
        # Create db with schema but no events
        db = tmp_path / "metrics.db"
        conn = sqlite3.connect(str(db))
        _init_db(conn)
        conn.close()

        with self._patch_db_path(tmp_path):
            results = _query_events()
        assert results == []


# ── _open_readonly ────────────────────────────────────────────────────────


class TestOpenReadonly:
    """Test _open_readonly opens the database in read-only mode."""

    def _patch_db_path(self, tmp_path: Path):
        return patch("supercharge.metrics._db_path", return_value=tmp_path / "metrics.db")

    def test_readonly_rejects_writes(self, tmp_path: Path):
        """A connection from _open_readonly should reject INSERT statements."""
        db = tmp_path / "metrics.db"
        conn = sqlite3.connect(str(db))
        _init_db(conn)
        conn.close()

        with self._patch_db_path(tmp_path):
            ro_conn = _open_readonly()
            try:
                with pytest.raises(sqlite3.OperationalError):
                    ro_conn.execute(
                        "INSERT INTO events (timestamp, event_type) VALUES ('t', 'e')"
                    )
            finally:
                ro_conn.close()

    def test_readonly_nonexistent_db_raises(self, tmp_path: Path):
        """Opening a non-existent DB in read-only mode should raise OperationalError."""
        with self._patch_db_path(tmp_path):
            # DB file does not exist — should raise
            with pytest.raises(sqlite3.OperationalError):
                _open_readonly()
