"""Tests for metrics collection module (SQLite-backed event store)."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from supercharge.metrics import _db_path, _emit, _import_legacy_dbs, _init_db, _open_readonly, _query_events

# ── _db_path ─────────────────────────────────────────────────────────────────


@pytest.mark.no_isolate_metrics
class TestDbPath:
    """Test _db_path returns the correct path."""

    def test_returns_user_level_path(self):
        result = _db_path()
        assert result == Path.home() / '.claude' / 'SuperchargeAI' / 'metrics.db'


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
            "project",
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
                    _emit("concurrent", session_id=f"s{i}", project=str(tmp_path))
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


# ── Migration 4 ───────────────────────────────────────────────────────────────


class TestMigration4:
    """Test migration 4: project columns, projects table, and indexes."""

    def test_fresh_db_has_project_column(self, tmp_path: Path):
        """Fresh DB should have project column in events after migration."""
        db = tmp_path / 'test.db'
        conn = sqlite3.connect(str(db))
        _init_db(conn)
        cols = {r[1] for r in conn.execute('PRAGMA table_info(events)').fetchall()}
        assert 'project' in cols
        conn.close()

    def test_fresh_db_has_session_stats_project_columns(self, tmp_path: Path):
        """Fresh DB should have project and project_name in session_stats."""
        db = tmp_path / 'test.db'
        conn = sqlite3.connect(str(db))
        _init_db(conn)
        cols = {r[1] for r in conn.execute('PRAGMA table_info(session_stats)').fetchall()}
        assert 'project' in cols
        assert 'project_name' in cols
        conn.close()

    def test_fresh_db_has_projects_table(self, tmp_path: Path):
        """Fresh DB should have projects table with correct schema."""
        db = tmp_path / 'test.db'
        conn = sqlite3.connect(str(db))
        _init_db(conn)
        cursor = conn.execute('PRAGMA table_info(projects)')
        cols = {r[1] for r in cursor.fetchall()}
        assert cols == {'project_path', 'project_slug', 'display_name', 'user_edited', 'last_updated'}
        conn.close()

    def test_fresh_db_has_project_indexes(self, tmp_path: Path):
        """Fresh DB should have indexes on events(project) and session_stats(project)."""
        db = tmp_path / 'test.db'
        conn = sqlite3.connect(str(db))
        _init_db(conn)
        indexes = {r[0] for r in conn.execute('SELECT name FROM sqlite_master WHERE type="index"').fetchall()}
        assert 'idx_events_project' in indexes
        assert 'idx_session_stats_project' in indexes
        conn.close()

    def test_migration_on_v3_db(self, tmp_path: Path):
        """Running migration 4 on an existing v3 DB should add project columns."""
        db = tmp_path / 'test.db'
        conn = sqlite3.connect(str(db))
        # Simulate v3 schema manually
        conn.executescript('''
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                event_type TEXT NOT NULL,
                session_id TEXT NOT NULL DEFAULT '',
                agent_id TEXT NOT NULL DEFAULT '',
                agent_type TEXT NOT NULL DEFAULT '',
                task_uuid TEXT NOT NULL DEFAULT '',
                worker_id TEXT NOT NULL DEFAULT '',
                parent_id TEXT NOT NULL DEFAULT '',
                tool_name TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
            INSERT INTO schema_version (version) VALUES (1);
            INSERT INTO schema_version (version) VALUES (2);
            INSERT INTO schema_version (version) VALUES (3);
            CREATE TABLE session_stats (
                session_id TEXT PRIMARY KEY,
                custom_name TEXT DEFAULT '',
                total_input_tokens INTEGER DEFAULT 0,
                total_output_tokens INTEGER DEFAULT 0,
                total_cache_creation_tokens INTEGER DEFAULT 0,
                total_cache_read_tokens INTEGER DEFAULT 0,
                message_count INTEGER DEFAULT 0,
                last_parsed_line INTEGER DEFAULT 0
            );
            CREATE TABLE agent_token_stats (
                agent_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL DEFAULT '',
                agent_type TEXT NOT NULL DEFAULT '',
                transcript_path TEXT NOT NULL DEFAULT '',
                total_input_tokens INTEGER DEFAULT 0,
                total_output_tokens INTEGER DEFAULT 0,
                total_cache_creation_tokens INTEGER DEFAULT 0,
                total_cache_read_tokens INTEGER DEFAULT 0,
                message_count INTEGER DEFAULT 0,
                last_parsed_line INTEGER DEFAULT 0
            );
        ''')
        # Insert a pre-existing event
        conn.execute("INSERT INTO events (timestamp, event_type, session_id) VALUES ('2024-01-01', 'test', 's1')")
        conn.commit()

        # Run init_db which should trigger migration 4
        _init_db(conn)

        # Verify project column was added
        cols = {r[1] for r in conn.execute('PRAGMA table_info(events)').fetchall()}
        assert 'project' in cols

        # Verify projects table
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert 'projects' in tables

        # Verify all migrations ran
        version = conn.execute('SELECT MAX(version) FROM schema_version').fetchone()[0]
        assert version == 7

        # Verify existing event still has empty project (default)
        row = conn.execute('SELECT project FROM events WHERE session_id = "s1"').fetchone()
        assert row[0] == ''
        conn.close()

    def test_schema_version_is_7(self, tmp_path: Path):
        """After full init, schema version should be 7."""
        db = tmp_path / 'test.db'
        conn = sqlite3.connect(str(db))
        _init_db(conn)
        version = conn.execute('SELECT MAX(version) FROM schema_version').fetchone()[0]
        assert version == 7
        conn.close()


# ── _emit project column ─────────────────────────────────────────────────────


class TestEmitProject:
    """Test _emit populates project column."""

    def _patch_db_path(self, tmp_path: Path):
        return patch('supercharge.metrics._db_path', return_value=tmp_path / 'metrics.db')

    def test_project_auto_populated(self, tmp_path: Path):
        """_emit should auto-populate project from _project_dir()."""
        with self._patch_db_path(tmp_path), \
             patch('supercharge.metrics._project_dir', return_value='/fake/project'):
            _emit('test_event', session_id='s1')

        conn = sqlite3.connect(str(tmp_path / 'metrics.db'))
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT project FROM events').fetchone()
        assert row['project'] == '/fake/project'
        conn.close()

    def test_project_explicit_override(self, tmp_path: Path):
        """Explicit project kwarg should override auto-detection."""
        with self._patch_db_path(tmp_path):
            _emit('test_event', project='/explicit/path')

        conn = sqlite3.connect(str(tmp_path / 'metrics.db'))
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT project FROM events').fetchone()
        assert row['project'] == '/explicit/path'
        conn.close()

    def test_project_empty_on_detection_failure(self, tmp_path: Path):
        """project should be empty if _project_dir() fails."""
        with self._patch_db_path(tmp_path), \
             patch('supercharge.metrics._project_dir', side_effect=RuntimeError('no project')):
            _emit('test_event')

        conn = sqlite3.connect(str(tmp_path / 'metrics.db'))
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT project FROM events').fetchone()
        assert row['project'] == ''
        conn.close()


# ── _import_legacy_dbs ────────────────────────────────────────────────────────


class TestLegacyImport:
    """Test _import_legacy_dbs correctly copies data and marks old DB."""

    def test_imports_events_from_legacy_db(self, tmp_path: Path):
        """Legacy events should be copied to global DB with project column set."""
        import tempfile
        # Use a custom tmpdir without hyphens — slug reverse-mapping (replace - with /)
        # is lossy and breaks on paths containing hyphens (like pytest's default tmp_path)
        hyphen_free = Path(tempfile.mkdtemp(prefix='supercharge_test_', dir='/tmp'))
        fake_home = hyphen_free / 'home'
        fake_home.mkdir()

        # Create projects slug dir with a .jsonl file
        project_path = str(fake_home / 'workspace' / 'myproject')
        slug = project_path.replace('/', '-')
        slug_dir = fake_home / '.claude' / 'projects' / slug
        slug_dir.mkdir(parents=True)
        (slug_dir / 'session.jsonl').write_text('')

        # Create legacy metrics.db at the project path
        legacy_dir = Path(project_path) / '.claude' / 'SuperchargeAI'
        legacy_dir.mkdir(parents=True)
        legacy_db = legacy_dir / 'metrics.db'
        conn = sqlite3.connect(str(legacy_db))
        conn.executescript('''
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                event_type TEXT NOT NULL,
                session_id TEXT NOT NULL DEFAULT '',
                agent_id TEXT NOT NULL DEFAULT '',
                agent_type TEXT NOT NULL DEFAULT '',
                task_uuid TEXT NOT NULL DEFAULT '',
                worker_id TEXT NOT NULL DEFAULT '',
                parent_id TEXT NOT NULL DEFAULT '',
                tool_name TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
            INSERT INTO schema_version (version) VALUES (1);
            INSERT INTO schema_version (version) VALUES (2);
            INSERT INTO schema_version (version) VALUES (3);
            CREATE TABLE session_stats (
                session_id TEXT PRIMARY KEY,
                custom_name TEXT DEFAULT '',
                total_input_tokens INTEGER DEFAULT 0,
                total_output_tokens INTEGER DEFAULT 0,
                total_cache_creation_tokens INTEGER DEFAULT 0,
                total_cache_read_tokens INTEGER DEFAULT 0,
                message_count INTEGER DEFAULT 0,
                last_parsed_line INTEGER DEFAULT 0
            );
        ''')
        conn.execute("INSERT INTO events (timestamp, event_type, session_id) VALUES ('2024-01-01T00:00:00+00:00', 'task_init', 's1')")
        conn.execute("INSERT INTO session_stats (session_id, custom_name) VALUES ('s1', 'My Session')")
        conn.commit()
        conn.close()

        # Create the global DB path and patch _db_path
        global_db = fake_home / '.claude' / 'SuperchargeAI' / 'metrics.db'
        global_db.parent.mkdir(parents=True, exist_ok=True)

        with patch('supercharge.metrics._db_path', return_value=global_db), \
             patch('pathlib.Path.home', return_value=fake_home):
            # Initialize global DB
            gconn = sqlite3.connect(str(global_db))
            gconn.execute('PRAGMA journal_mode=WAL')
            _init_db(gconn)
            gconn.close()

            _import_legacy_dbs()

        # Verify events were imported with project column
        gconn = sqlite3.connect(str(global_db))
        gconn.row_factory = sqlite3.Row
        rows = gconn.execute('SELECT * FROM events WHERE session_id = "s1"').fetchall()
        assert len(rows) == 1
        assert rows[0]['project'] == project_path

        # Verify session_stats were imported
        stats = gconn.execute('SELECT * FROM session_stats WHERE session_id = "s1"').fetchall()
        assert len(stats) == 1
        assert stats[0]['custom_name'] == 'My Session'
        gconn.close()

        # Verify legacy DB was renamed
        assert not legacy_db.exists()
        assert (legacy_dir / 'metrics.db.migrated').exists()

    def test_skips_already_migrated(self, tmp_path: Path, monkeypatch):
        """Should skip DBs that already have .migrated marker."""
        fake_home = tmp_path / 'home'
        fake_home.mkdir()
        monkeypatch.setattr(Path, 'home', staticmethod(lambda: fake_home))

        project_path = str(tmp_path / 'projects' / 'myproject')
        slug = project_path.replace('/', '-')
        slug_dir = fake_home / '.claude' / 'projects' / slug
        slug_dir.mkdir(parents=True)
        (slug_dir / 'session.jsonl').write_text('')

        # Create BOTH metrics.db and metrics.db.migrated
        legacy_dir = Path(project_path) / '.claude' / 'SuperchargeAI'
        legacy_dir.mkdir(parents=True)
        (legacy_dir / 'metrics.db').write_text('fake')
        (legacy_dir / 'metrics.db.migrated').write_text('marker')

        global_db = fake_home / '.claude' / 'SuperchargeAI' / 'metrics.db'
        global_db.parent.mkdir(parents=True, exist_ok=True)

        with patch('supercharge.metrics._db_path', return_value=global_db):
            gconn = sqlite3.connect(str(global_db))
            gconn.execute('PRAGMA journal_mode=WAL')
            _init_db(gconn)
            gconn.close()

            _import_legacy_dbs()

        # Legacy DB should NOT have been touched
        assert (legacy_dir / 'metrics.db').exists()

    def test_no_projects_dir(self, tmp_path: Path, monkeypatch):
        """Should handle missing ~/.claude/projects/ gracefully."""
        fake_home = tmp_path / 'home'
        fake_home.mkdir()
        monkeypatch.setattr(Path, 'home', staticmethod(lambda: fake_home))

        _import_legacy_dbs()  # Should not raise
