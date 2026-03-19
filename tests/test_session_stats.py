"""Tests for DB migration, session stats, JSONL parser, session/tool APIs."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

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


def _make_jsonl(path: Path, entries: list[dict]) -> None:
    """Write JSONL entries to a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def _sample_jsonl_entries() -> list[dict]:
    """Sample JSONL entries simulating a Claude Code session transcript."""
    return [
        # Line 0: user message (no usage)
        {"type": "user", "message": {"role": "user", "content": "Hello"}},
        # Line 1: assistant message with usage
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Hi there!"}],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 20,
                    "cache_creation_input_tokens": 100,
                    "cache_read_input_tokens": 200,
                },
            },
        },
        # Line 2: custom title
        {
            "type": "custom-title",
            "sessionId": "test-session-1",
            "customTitle": "My Test Session",
        },
        # Line 3: another assistant message
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "How can I help?"}],
                "usage": {
                    "input_tokens": 15,
                    "output_tokens": 30,
                    "cache_creation_input_tokens": 150,
                    "cache_read_input_tokens": 250,
                },
            },
        },
        # Line 4: updated custom title
        {
            "type": "custom-title",
            "sessionId": "test-session-1",
            "customTitle": "Renamed Session",
        },
    ]


# ── Migration tests ─────────────────────────────────────────────────────────


class TestMigrations:
    """Test schema migrations run correctly."""

    def test_schema_version_table_created(self, tmp_path: Path):
        """_init_db creates the schema_version table."""
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        _init_db(conn)

        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        )
        assert cursor.fetchone() is not None
        conn.close()

    def test_session_stats_table_created(self, tmp_path: Path):
        """Migration 2 creates the session_stats table."""
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        _init_db(conn)

        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='session_stats'"
        )
        assert cursor.fetchone() is not None
        conn.close()

    def test_session_stats_columns(self, tmp_path: Path):
        """session_stats table has the expected columns."""
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        _init_db(conn)

        cursor = conn.execute("PRAGMA table_info(session_stats)")
        columns = {row[1] for row in cursor.fetchall()}
        expected = {
            "session_id",
            "custom_name",
            "total_input_tokens",
            "total_output_tokens",
            "total_cache_creation_tokens",
            "total_cache_read_tokens",
            "message_count",
            "last_parsed_line",
            "project",
            "project_name",
            "skill_usage",
            "segments",
        }
        assert columns == expected
        conn.close()

    def test_migration_normalizes_agent_type(self, tmp_path: Path):
        """Migration 1 strips 'supercharge-ai:' prefix from agent_type values."""
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        # Create events table WITHOUT migrations first
        conn.executescript(
            """\
            CREATE TABLE IF NOT EXISTS events (
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
            """
        )
        # Insert rows with old prefixed agent_type
        conn.execute(
            "INSERT INTO events (timestamp, event_type, session_id, agent_type) "
            "VALUES ('2026-01-01T00:00:00+00:00', 'test', 's1', 'supercharge-ai:code')"
        )
        conn.execute(
            "INSERT INTO events (timestamp, event_type, session_id, agent_type) "
            "VALUES ('2026-01-01T00:00:00+00:00', 'test', 's1', 'code')"
        )
        conn.commit()

        # Now run _init_db which includes migrations
        _init_db(conn)

        rows = conn.execute("SELECT agent_type FROM events ORDER BY id").fetchall()
        assert rows[0][0] == "code"
        assert rows[1][0] == "code"
        conn.close()

    def test_migration_idempotent(self, tmp_path: Path):
        """Running _init_db twice does not fail or re-apply migrations."""
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        _init_db(conn)
        _init_db(conn)  # should not raise

        # Verify schema_version has correct version
        version = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0]
        assert version >= 2
        conn.close()

    def test_schema_version_tracks_migrations(self, tmp_path: Path):
        """schema_version table records each applied migration."""
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        _init_db(conn)

        versions = conn.execute(
            "SELECT version FROM schema_version ORDER BY version"
        ).fetchall()
        version_list = [v[0] for v in versions]
        assert 1 in version_list
        assert 2 in version_list
        conn.close()


# ── JSONL parser tests ───────────────────────────────────────────────────────


class TestParseSessionJsonl:
    """Test _parse_session_jsonl with sample JSONL data."""

    def test_parse_full_file(self, tmp_path: Path):
        """Parse all lines of a sample JSONL file."""
        from supercharge.metrics import _parse_session_jsonl

        jsonl_dir = tmp_path / "projects" / "-test-project"
        jsonl_path = jsonl_dir / "test-session-1.jsonl"
        _make_jsonl(jsonl_path, _sample_jsonl_entries())

        with patch("supercharge.metrics._find_session_jsonl", return_value=jsonl_path):
            result = _parse_session_jsonl("test-session-1")

        assert result["custom_name"] == "Renamed Session"
        assert result["total_input_tokens"] == 25  # 10 + 15
        assert result["total_output_tokens"] == 50  # 20 + 30
        assert result["total_cache_creation_tokens"] == 250  # 100 + 150
        assert result["total_cache_read_tokens"] == 450  # 200 + 250
        assert result["message_count"] == 2
        assert result["last_parsed_line"] == 5  # 5 lines total

    def test_parse_incremental(self, tmp_path: Path):
        """Incremental parse from start_line skips already-parsed lines."""
        from supercharge.metrics import _parse_session_jsonl

        jsonl_dir = tmp_path / "projects" / "-test-project"
        jsonl_path = jsonl_dir / "test-session-1.jsonl"
        _make_jsonl(jsonl_path, _sample_jsonl_entries())

        with patch("supercharge.metrics._find_session_jsonl", return_value=jsonl_path):
            result = _parse_session_jsonl("test-session-1", start_line=3)

        # Only lines 3-4: one assistant message + one custom title
        assert result["custom_name"] == "Renamed Session"
        assert result["total_input_tokens"] == 15
        assert result["total_output_tokens"] == 30
        assert result["total_cache_creation_tokens"] == 150
        assert result["total_cache_read_tokens"] == 250
        assert result["message_count"] == 1
        assert result["last_parsed_line"] == 5

    def test_parse_missing_file(self, tmp_path: Path):
        """Parsing a nonexistent session returns empty result."""
        from supercharge.metrics import _parse_session_jsonl

        with patch("supercharge.metrics._find_session_jsonl", return_value=None):
            result = _parse_session_jsonl("nonexistent")

        assert result["custom_name"] == ""
        assert result["total_input_tokens"] == 0
        assert result["message_count"] == 0
        assert result["last_parsed_line"] == 0

    def test_parse_empty_file(self, tmp_path: Path):
        """Parsing an empty JSONL returns empty result."""
        from supercharge.metrics import _parse_session_jsonl

        jsonl_path = tmp_path / "empty.jsonl"
        jsonl_path.write_text("")

        with patch("supercharge.metrics._find_session_jsonl", return_value=jsonl_path):
            result = _parse_session_jsonl("test")

        assert result["message_count"] == 0
        assert result["last_parsed_line"] == 0

    def test_parse_malformed_lines_skipped(self, tmp_path: Path):
        """Malformed JSON lines are skipped without error."""
        from supercharge.metrics import _parse_session_jsonl

        jsonl_path = tmp_path / "bad.jsonl"
        jsonl_path.write_text(
            "not json\n"
            '{"type": "assistant", "message": {"usage": {"input_tokens": 5, "output_tokens": 10, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}}\n'
        )

        with patch("supercharge.metrics._find_session_jsonl", return_value=jsonl_path):
            result = _parse_session_jsonl("test")

        assert result["total_input_tokens"] == 5
        assert result["message_count"] == 1
        assert result["last_parsed_line"] == 2


class TestFindSessionJsonl:
    """Test _find_session_jsonl locates the correct JSONL file."""

    def test_finds_existing_file(self, tmp_path: Path):
        from supercharge.metrics import _find_session_jsonl

        config_dir = tmp_path / "claude_config"
        jsonl_dir = config_dir / "projects" / "-workspaces-MyProject"
        jsonl_path = jsonl_dir / "abc-123.jsonl"
        jsonl_path.parent.mkdir(parents=True)
        jsonl_path.write_text("{}\n")

        with (
            patch("supercharge.metrics._user_config_dir", return_value=config_dir),
            patch("supercharge.metrics._project_dir", return_value="/workspaces/MyProject"),
        ):
            result = _find_session_jsonl("abc-123")

        assert result == jsonl_path

    def test_returns_none_for_missing(self, tmp_path: Path):
        from supercharge.metrics import _find_session_jsonl

        config_dir = tmp_path / "claude_config"

        with (
            patch("supercharge.metrics._user_config_dir", return_value=config_dir),
            patch("supercharge.metrics._project_dir", return_value="/workspaces/MyProject"),
        ):
            result = _find_session_jsonl("nonexistent")

        assert result is None


# ── Update session stats tests ───────────────────────────────────────────────


class TestUpdateSessionStats:
    """Test _update_session_stats incremental parsing and DB upsert."""

    def test_initial_update(self, tmp_path: Path):
        """First update creates a new row in session_stats."""
        from supercharge.metrics import _update_session_stats

        _make_db(tmp_path)
        jsonl_dir = tmp_path / "projects" / "-test-project"
        jsonl_path = jsonl_dir / "s1.jsonl"
        _make_jsonl(jsonl_path, _sample_jsonl_entries())

        with (
            _patch_db(tmp_path),
            patch("supercharge.metrics._find_session_jsonl", return_value=jsonl_path),
        ):
            _update_session_stats("s1")

        conn = sqlite3.connect(str(tmp_path / "metrics.db"))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM session_stats WHERE session_id = 's1'"
        ).fetchone()
        assert row is not None
        assert row["custom_name"] == "Renamed Session"
        assert row["total_input_tokens"] == 25
        assert row["total_output_tokens"] == 50
        assert row["message_count"] == 2
        assert row["last_parsed_line"] == 5
        conn.close()

    def test_incremental_update(self, tmp_path: Path):
        """Second update only parses new lines and accumulates."""
        from supercharge.metrics import _update_session_stats

        _make_db(tmp_path)
        jsonl_dir = tmp_path / "projects" / "-test-project"
        jsonl_path = jsonl_dir / "s1.jsonl"

        # First: write 3 lines
        initial_entries = _sample_jsonl_entries()[:3]
        _make_jsonl(jsonl_path, initial_entries)

        with (
            _patch_db(tmp_path),
            patch("supercharge.metrics._find_session_jsonl", return_value=jsonl_path),
        ):
            _update_session_stats("s1")

        # Verify first parse
        conn = sqlite3.connect(str(tmp_path / "metrics.db"))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM session_stats WHERE session_id = 's1'"
        ).fetchone()
        assert row["total_input_tokens"] == 10
        assert row["message_count"] == 1
        assert row["last_parsed_line"] == 3
        conn.close()

        # Now append more lines
        all_entries = _sample_jsonl_entries()
        _make_jsonl(jsonl_path, all_entries)

        with (
            _patch_db(tmp_path),
            patch("supercharge.metrics._find_session_jsonl", return_value=jsonl_path),
        ):
            _update_session_stats("s1")

        conn = sqlite3.connect(str(tmp_path / "metrics.db"))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM session_stats WHERE session_id = 's1'"
        ).fetchone()
        assert row["total_input_tokens"] == 25  # 10 + 15
        assert row["total_output_tokens"] == 50  # 20 + 30
        assert row["message_count"] == 2
        assert row["custom_name"] == "Renamed Session"
        assert row["last_parsed_line"] == 5
        conn.close()


# ── Session rename API tests ────────────────────────────────────────────────


class TestSessionRenameApi:
    """Test POST /api/sessions/{session_id}/name endpoint."""

    @pytest.fixture(autouse=True)
    def setup_db(self, tmp_path):
        self.tmp_path = tmp_path
        _make_db(tmp_path)
        self.db_patch = _patch_db(tmp_path)
        self.db_patch.start()
        yield
        self.db_patch.stop()

    @pytest.fixture()
    def client(self):
        from supercharge.dashboard import _create_app

        app = _create_app()
        return TestClient(app)

    def test_rename_creates_stats_row(self, client):
        """POST /api/sessions/s1/name creates or updates session_stats."""
        resp = client.post(
            "/api/sessions/s1/name",
            json={"name": "New Name"},
        )
        assert resp.status_code == 200

        conn = sqlite3.connect(str(self.tmp_path / "metrics.db"))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT custom_name FROM session_stats WHERE session_id = 's1'"
        ).fetchone()
        assert row is not None
        assert row["custom_name"] == "New Name"
        conn.close()

    def test_rename_appends_to_jsonl(self, client, tmp_path):
        """Rename appends a custom-title entry to the JSONL file."""
        jsonl_path = tmp_path / "session.jsonl"
        jsonl_path.write_text("")

        with patch("supercharge.metrics._find_session_jsonl", return_value=jsonl_path):
            resp = client.post(
                "/api/sessions/s1/name",
                json={"name": "Test Title"},
            )

        assert resp.status_code == 200
        lines = jsonl_path.read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["type"] == "custom-title"
        assert entry["customTitle"] == "Test Title"
        assert entry["sessionId"] == "s1"
        assert "timestamp" in entry

    def test_rename_missing_name_returns_400(self, client):
        """POST without 'name' field returns 400."""
        resp = client.post(
            "/api/sessions/s1/name",
            json={},
        )
        assert resp.status_code == 400

    def test_rename_no_jsonl_still_updates_db(self, client):
        """If JSONL file doesn't exist, DB is still updated."""
        with patch("supercharge.metrics._find_session_jsonl", return_value=None):
            resp = client.post(
                "/api/sessions/s1/name",
                json={"name": "DB Only"},
            )

        assert resp.status_code == 200
        conn = sqlite3.connect(str(self.tmp_path / "metrics.db"))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT custom_name FROM session_stats WHERE session_id = 's1'"
        ).fetchone()
        assert row["custom_name"] == "DB Only"
        conn.close()


# ── Global tool stats tests ─────────────────────────────────────────────────


class TestQueryGlobalToolStats:
    """Test _query_global_tool_stats returns grouped tool usage."""

    def test_groups_by_agent_type_and_tool(self, tmp_path: Path):
        rows = [
            ("2026-01-10T10:00:00+00:00", "tool_use", "s1", "a1", "code", "t1", "", "", "Bash", '{"command": "ls"}'),
            ("2026-01-10T10:00:01+00:00", "tool_use", "s1", "a1", "code", "t1", "", "", "Bash", '{"command": "pwd"}'),
            ("2026-01-10T10:00:02+00:00", "tool_use", "s1", "a1", "code", "t1", "", "", "Read", '{}'),
            ("2026-01-10T10:00:03+00:00", "tool_use", "s1", "a2", "orchestrator", "", "", "", "Task", '{}'),
        ]
        _make_db(tmp_path, rows)

        from supercharge.metrics import _query_global_tool_stats

        with _patch_db(tmp_path):
            result = _query_global_tool_stats()

        assert "agent_types" in result
        assert "totals" in result
        assert result["agent_types"]["code"]["Bash"] == 2
        assert result["agent_types"]["code"]["Read"] == 1
        assert result["agent_types"]["orchestrator"]["Task"] == 1
        assert result["totals"]["Bash"] == 2
        assert result["totals"]["Read"] == 1
        assert result["totals"]["Task"] == 1

    def test_detects_supercharge_bash_calls(self, tmp_path: Path):
        rows = [
            ("2026-01-10T10:00:00+00:00", "tool_use", "s1", "a1", "code", "t1", "", "", "Bash", '{"command": "supercharge subtask init"}'),
            ("2026-01-10T10:00:01+00:00", "tool_use", "s1", "a1", "code", "t1", "", "", "Bash", '{"command": "ls -la"}'),
            ("2026-01-10T10:00:02+00:00", "tool_use", "s1", "a1", "code", "t1", "", "", "Bash", '{"command": "supercharge task status"}'),
        ]
        _make_db(tmp_path, rows)

        from supercharge.metrics import _query_global_tool_stats

        with _patch_db(tmp_path):
            result = _query_global_tool_stats()

        assert result["totals"]["supercharge"] == 2

    def test_empty_db(self, tmp_path: Path):
        _make_db(tmp_path, [])

        from supercharge.metrics import _query_global_tool_stats

        with _patch_db(tmp_path):
            result = _query_global_tool_stats()

        assert result == {"agent_types": {}, "totals": {}}

    def test_resolves_agent_type_from_time_ranges(self, tmp_path: Path):
        """tool_use events with empty agent_type are attributed via subagent time ranges."""
        rows = [
            # subagent_start for a code agent
            ("2026-01-10T10:00:00+00:00", "subagent_start", "s1", "agent-abc", "code", "", "", "", "", ""),
            # tool_use during agent window — empty agent_type (from CLI hook)
            ("2026-01-10T10:00:05+00:00", "tool_use", "s1", "", "", "", "", "", "Bash", '{"command": "ls"}'),
            ("2026-01-10T10:00:06+00:00", "tool_use", "s1", "", "", "", "", "", "Read", "{}"),
            # subagent_stop
            ("2026-01-10T10:00:10+00:00", "subagent_stop", "s1", "agent-abc", "code", "", "", "", "", ""),
            # tool_use outside agent window — should be orchestrator
            ("2026-01-10T10:00:15+00:00", "tool_use", "s1", "", "", "", "", "", "Write", "{}"),
        ]
        _make_db(tmp_path, rows)

        from supercharge.metrics import _query_global_tool_stats

        with _patch_db(tmp_path):
            result = _query_global_tool_stats()

        assert result["agent_types"]["code"]["Bash"] == 1
        assert result["agent_types"]["code"]["Read"] == 1
        assert result["agent_types"]["orchestrator"]["Write"] == 1
        assert result["totals"]["Bash"] == 1
        assert result["totals"]["Read"] == 1
        assert result["totals"]["Write"] == 1


# ── Tool stats API endpoint tests ────────────────────────────────────────────


class TestToolStatsApi:
    """Test GET /api/stats/tools endpoint."""

    @pytest.fixture(autouse=True)
    def setup_db(self, tmp_path):
        self.tmp_path = tmp_path
        rows = [
            ("2026-01-10T10:00:00+00:00", "tool_use", "s1", "a1", "code", "t1", "", "", "Bash", '{"command": "ls"}'),
            ("2026-01-10T10:00:01+00:00", "tool_use", "s1", "a1", "code", "t1", "", "", "Read", '{}'),
        ]
        _make_db(tmp_path, rows)
        self.db_patch = _patch_db(tmp_path)
        self.db_patch.start()
        yield
        self.db_patch.stop()

    @pytest.fixture()
    def client(self):
        from supercharge.dashboard import _create_app

        app = _create_app()
        return TestClient(app)

    def test_returns_tool_stats(self, client):
        resp = client.get("/api/stats/tools")
        assert resp.status_code == 200
        data = resp.json()
        assert "agent_types" in data
        assert "totals" in data
        assert data["totals"]["Bash"] == 1
        assert data["totals"]["Read"] == 1


# ── Sessions API enrichment tests ───────────────────────────────────────────


class TestSessionsApiEnrichment:
    """Test /api/sessions returns enriched data with names and token counts."""

    @pytest.fixture(autouse=True)
    def setup_db(self, tmp_path):
        self.tmp_path = tmp_path
        rows = [
            ("2026-01-10T10:00:00+00:00", "session_start", "s1", "a1", "orchestrator", "", "", "", "", ""),
            ("2026-01-10T10:00:01+00:00", "tool_use", "s1", "a2", "code", "t1", "", "", "Bash", ""),
        ]
        _make_db(tmp_path, rows)
        self.db_patch = _patch_db(tmp_path)
        self.db_patch.start()
        yield
        self.db_patch.stop()

    @pytest.fixture()
    def client(self):
        from supercharge.dashboard import _create_app

        app = _create_app()
        return TestClient(app)

    def test_sessions_include_stats_fields(self, client):
        """Verify sessions response includes token/name fields (even if zeroed)."""
        with patch("supercharge.metrics._update_all_session_stats"):
            resp = client.get("/api/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        session = data[0]
        # These fields should exist (may be 0/"" if no JSONL)
        assert "name" in session
        assert "input_tokens" in session
        assert "output_tokens" in session
        assert "cache_creation_tokens" in session
        assert "cache_read_tokens" in session


# ── Skill usage parsing tests ────────────────────────────────────────────────


def _skill_jsonl_entries() -> list[dict]:
    """JSONL entries with Skill tool_use blocks."""
    return [
        {"type": "user", "message": {"role": "user", "content": "Use pdf skill"}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me use the pdf skill."},
                    {"type": "tool_use", "name": "Skill", "input": {"command": "pdf"}},
                ],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 20,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "Skill", "input": {"command": "pdf extra args"}},
                    {"type": "tool_use", "name": "Skill", "input": {"command": "web https://example.com"}},
                ],
                "usage": {
                    "input_tokens": 5,
                    "output_tokens": 10,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                ],
                "usage": {
                    "input_tokens": 3,
                    "output_tokens": 5,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        },
    ]


class TestSkillUsageParsing:
    """Test skill detection in _parse_session_jsonl."""

    def test_detects_skill_usage(self, tmp_path: Path):
        """Skill tool_use blocks are counted by skill name."""
        from supercharge.metrics import _parse_session_jsonl

        jsonl_path = tmp_path / "projects" / "-test" / "s1.jsonl"
        _make_jsonl(jsonl_path, _skill_jsonl_entries())

        with patch("supercharge.metrics._find_session_jsonl", return_value=jsonl_path):
            result = _parse_session_jsonl("s1")

        assert result["skills"] == {"pdf": 2, "web": 1}

    def test_skill_counts_accumulate(self, tmp_path: Path):
        """Multiple Skill blocks across messages accumulate correctly."""
        from supercharge.metrics import _parse_session_jsonl

        jsonl_path = tmp_path / "projects" / "-test" / "s1.jsonl"
        _make_jsonl(jsonl_path, _skill_jsonl_entries())

        with patch("supercharge.metrics._find_session_jsonl", return_value=jsonl_path):
            result = _parse_session_jsonl("s1")

        total = sum(result["skills"].values())
        assert total == 3  # 2 pdf + 1 web

    def test_non_skill_tool_use_ignored(self, tmp_path: Path):
        """tool_use blocks with name != 'Skill' are not counted."""
        from supercharge.metrics import _parse_session_jsonl

        jsonl_path = tmp_path / "projects" / "-test" / "s1.jsonl"
        _make_jsonl(jsonl_path, _skill_jsonl_entries())

        with patch("supercharge.metrics._find_session_jsonl", return_value=jsonl_path):
            result = _parse_session_jsonl("s1")

        assert "Bash" not in result["skills"]
        assert "ls" not in result["skills"]

    def test_empty_command_ignored(self, tmp_path: Path):
        """Skill blocks with empty command are skipped."""
        from supercharge.metrics import _parse_session_jsonl

        entries = [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "name": "Skill", "input": {"command": ""}},
                    ],
                    "usage": {"input_tokens": 1, "output_tokens": 1,
                              "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
                },
            },
        ]
        jsonl_path = tmp_path / "s1.jsonl"
        _make_jsonl(jsonl_path, entries)

        with patch("supercharge.metrics._find_session_jsonl", return_value=jsonl_path):
            result = _parse_session_jsonl("s1")

        assert result["skills"] == {}

    def test_no_skills_returns_empty_dict(self, tmp_path: Path):
        """Sessions without skills return empty skills dict."""
        from supercharge.metrics import _parse_session_jsonl

        jsonl_path = tmp_path / "projects" / "-test" / "s1.jsonl"
        _make_jsonl(jsonl_path, _sample_jsonl_entries())

        with patch("supercharge.metrics._find_session_jsonl", return_value=jsonl_path):
            result = _parse_session_jsonl("s1")

        assert result["skills"] == {}

    def test_skill_command_first_token_only(self, tmp_path: Path):
        """Skill name is the first whitespace-delimited token of command."""
        from supercharge.metrics import _parse_session_jsonl

        entries = [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "name": "Skill", "input": {"command": "pdf /path/to/file.pdf --page 3"}},
                    ],
                    "usage": {"input_tokens": 1, "output_tokens": 1,
                              "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
                },
            },
        ]
        jsonl_path = tmp_path / "s1.jsonl"
        _make_jsonl(jsonl_path, entries)

        with patch("supercharge.metrics._find_session_jsonl", return_value=jsonl_path):
            result = _parse_session_jsonl("s1")

        assert result["skills"] == {"pdf": 1}


class TestSkillUsageMigration:
    """Test migration 6 adds skill_usage column."""

    def test_fresh_db_has_skill_usage_column(self, tmp_path: Path):
        """Fresh DB should have skill_usage column in session_stats."""
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        _init_db(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(session_stats)").fetchall()}
        assert "skill_usage" in cols
        conn.close()

    def test_migration_on_v4_db(self, tmp_path: Path):
        """Running migration 6 on a v4 DB should add skill_usage column."""
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        # Create v4 schema manually
        conn.executescript("""
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
                detail TEXT NOT NULL DEFAULT '',
                project TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
            INSERT INTO schema_version (version) VALUES (1);
            INSERT INTO schema_version (version) VALUES (2);
            INSERT INTO schema_version (version) VALUES (3);
            INSERT INTO schema_version (version) VALUES (4);
            CREATE TABLE session_stats (
                session_id TEXT PRIMARY KEY,
                custom_name TEXT DEFAULT '',
                total_input_tokens INTEGER DEFAULT 0,
                total_output_tokens INTEGER DEFAULT 0,
                total_cache_creation_tokens INTEGER DEFAULT 0,
                total_cache_read_tokens INTEGER DEFAULT 0,
                message_count INTEGER DEFAULT 0,
                last_parsed_line INTEGER DEFAULT 0,
                project TEXT NOT NULL DEFAULT '',
                project_name TEXT NOT NULL DEFAULT ''
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
            CREATE TABLE projects (
                project_path TEXT PRIMARY KEY,
                project_slug TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                user_edited BOOLEAN NOT NULL DEFAULT 0,
                last_updated TEXT NOT NULL DEFAULT ''
            );
        """)
        conn.commit()

        # Run _init_db which should trigger migration 6
        _init_db(conn)

        # Verify skill_usage column was added
        cols = {r[1] for r in conn.execute("PRAGMA table_info(session_stats)").fetchall()}
        assert "skill_usage" in cols

        # Verify all migrations ran (6 + 7)
        version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert version == 7
        conn.close()

    def test_skill_usage_default_value(self, tmp_path: Path):
        """New rows should default skill_usage to '{}'."""
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        _init_db(conn)
        conn.execute(
            "INSERT INTO session_stats (session_id) VALUES ('test')"
        )
        conn.commit()
        row = conn.execute(
            "SELECT skill_usage FROM session_stats WHERE session_id = 'test'"
        ).fetchone()
        assert row[0] == "{}"
        conn.close()


class TestSkillUsagePersistence:
    """Test that _update_session_stats persists skill_usage to DB."""

    def test_skills_stored_in_db(self, tmp_path: Path):
        """Skills from JSONL are stored as JSON in skill_usage column."""
        from supercharge.metrics import _update_session_stats

        _make_db(tmp_path)
        jsonl_path = tmp_path / "projects" / "-test" / "s1.jsonl"
        _make_jsonl(jsonl_path, _skill_jsonl_entries())

        with (
            _patch_db(tmp_path),
            patch("supercharge.metrics._find_session_jsonl", return_value=jsonl_path),
        ):
            _update_session_stats("s1")

        conn = sqlite3.connect(str(tmp_path / "metrics.db"))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT skill_usage FROM session_stats WHERE session_id = 's1'"
        ).fetchone()
        assert row is not None
        skills = json.loads(row["skill_usage"])
        assert skills == {"pdf": 2, "web": 1}
        conn.close()

    def test_skills_accumulate_incrementally(self, tmp_path: Path):
        """Incremental updates accumulate skill counts."""
        from supercharge.metrics import _update_session_stats

        _make_db(tmp_path)

        # First parse: one skill usage
        entries_1 = [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "name": "Skill", "input": {"command": "pdf"}},
                    ],
                    "usage": {"input_tokens": 5, "output_tokens": 5,
                              "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
                },
            },
        ]
        jsonl_path = tmp_path / "projects" / "-test" / "s1.jsonl"
        _make_jsonl(jsonl_path, entries_1)

        with (
            _patch_db(tmp_path),
            patch("supercharge.metrics._find_session_jsonl", return_value=jsonl_path),
        ):
            _update_session_stats("s1")

        # Second parse: add more entries
        entries_2 = entries_1 + [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "name": "Skill", "input": {"command": "pdf"}},
                        {"type": "tool_use", "name": "Skill", "input": {"command": "web"}},
                    ],
                    "usage": {"input_tokens": 3, "output_tokens": 3,
                              "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
                },
            },
        ]
        _make_jsonl(jsonl_path, entries_2)

        with (
            _patch_db(tmp_path),
            patch("supercharge.metrics._find_session_jsonl", return_value=jsonl_path),
        ):
            _update_session_stats("s1")

        conn = sqlite3.connect(str(tmp_path / "metrics.db"))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT skill_usage FROM session_stats WHERE session_id = 's1'"
        ).fetchone()
        skills = json.loads(row["skill_usage"])
        assert skills == {"pdf": 2, "web": 1}
        conn.close()


# ── Session segments tests ───────────────────────────────────────────────────


def _ts(minutes: int) -> str:
    """Return an ISO timestamp offset by *minutes* from a base time."""
    from datetime import datetime, timedelta, timezone
    base = datetime(2026, 3, 15, 10, 0, 0, tzinfo=timezone.utc)
    return (base + timedelta(minutes=minutes)).isoformat()


class TestComputeSegments:
    """Test _compute_segments() directly."""

    def test_single_continuous_session(self, tmp_path: Path):
        """All messages within gap threshold produce 1 segment."""
        from supercharge.metrics import _compute_segments

        db = _make_db(tmp_path)
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row

        timestamps = [
            (_ts(0), "user"),
            (_ts(1), "assistant"),
            (_ts(5), "user"),
            (_ts(6), "assistant"),
        ]
        segments = _compute_segments(timestamps, "s1", conn)
        assert len(segments) == 1
        assert segments[0]["start"] == _ts(0)
        assert segments[0]["end"] == _ts(6)
        conn.close()

    def test_gap_produces_two_segments(self, tmp_path: Path):
        """A 45-minute gap between assistant reply and next user message splits."""
        from supercharge.metrics import _compute_segments

        db = _make_db(tmp_path)
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row

        timestamps = [
            (_ts(0), "user"),
            (_ts(1), "assistant"),
            (_ts(46), "user"),     # 45-min gap from assistant at t=1
            (_ts(47), "assistant"),
        ]
        segments = _compute_segments(timestamps, "s1", conn)
        assert len(segments) == 2
        assert segments[0]["end"] == _ts(1)
        assert segments[1]["start"] == _ts(46)
        conn.close()

    def test_gap_with_active_agent_no_split(self, tmp_path: Path):
        """A 45-minute gap with an active agent during the gap does NOT split."""
        from supercharge.metrics import _compute_segments

        # Create DB with subagent_start/stop events during the gap
        rows = [
            (_ts(5), "subagent_start", "s1", "agent-abc", "code", "", "", "", "", ""),
            (_ts(40), "subagent_stop", "s1", "agent-abc", "code", "", "", "", "", ""),
        ]
        db = _make_db(tmp_path, rows)
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row

        timestamps = [
            (_ts(0), "user"),
            (_ts(1), "assistant"),
            (_ts(46), "user"),     # 45-min gap, but agent active t=5..t=40
            (_ts(47), "assistant"),
        ]
        segments = _compute_segments(timestamps, "s1", conn)
        assert len(segments) == 1
        conn.close()

    def test_multiple_gaps(self, tmp_path: Path):
        """Multiple gaps produce the correct number of segments."""
        from supercharge.metrics import _compute_segments

        db = _make_db(tmp_path)
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row

        timestamps = [
            (_ts(0), "user"),
            (_ts(1), "assistant"),
            (_ts(60), "user"),      # gap 1
            (_ts(61), "assistant"),
            (_ts(120), "user"),     # gap 2
            (_ts(121), "assistant"),
        ]
        segments = _compute_segments(timestamps, "s1", conn)
        assert len(segments) == 3
        conn.close()

    def test_empty_timestamps(self, tmp_path: Path):
        """Empty timestamps list returns empty segments."""
        from supercharge.metrics import _compute_segments

        db = _make_db(tmp_path)
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row

        segments = _compute_segments([], "s1", conn)
        assert segments == []
        conn.close()

    def test_single_message(self, tmp_path: Path):
        """Single message returns one segment with same start and end."""
        from supercharge.metrics import _compute_segments

        db = _make_db(tmp_path)
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row

        timestamps = [(_ts(0), "user")]
        segments = _compute_segments(timestamps, "s1", conn)
        assert len(segments) == 1
        assert segments[0]["start"] == _ts(0)
        assert segments[0]["end"] == _ts(0)
        conn.close()


class TestMigration7:
    """Test migration 7 adds segments column."""

    def test_fresh_db_has_segments_column(self, tmp_path: Path):
        """Fresh DB should have segments column in session_stats."""
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        _init_db(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(session_stats)").fetchall()}
        assert "segments" in cols
        conn.close()

    def test_segments_default_value(self, tmp_path: Path):
        """New rows should default segments to '[]'."""
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        _init_db(conn)
        conn.execute("INSERT INTO session_stats (session_id) VALUES ('test')")
        conn.commit()
        row = conn.execute(
            "SELECT segments FROM session_stats WHERE session_id = 'test'"
        ).fetchone()
        assert row[0] == "[]"
        conn.close()

    def test_migration_on_v6_db(self, tmp_path: Path):
        """Running migration 7 on a v6 DB should add segments column."""
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        # Create v6 schema manually (minimal)
        conn.executescript("""
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
                detail TEXT NOT NULL DEFAULT '',
                project TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
            INSERT INTO schema_version (version) VALUES (1);
            INSERT INTO schema_version (version) VALUES (2);
            INSERT INTO schema_version (version) VALUES (3);
            INSERT INTO schema_version (version) VALUES (4);
            INSERT INTO schema_version (version) VALUES (5);
            INSERT INTO schema_version (version) VALUES (6);
            CREATE TABLE session_stats (
                session_id TEXT PRIMARY KEY,
                custom_name TEXT DEFAULT '',
                total_input_tokens INTEGER DEFAULT 0,
                total_output_tokens INTEGER DEFAULT 0,
                total_cache_creation_tokens INTEGER DEFAULT 0,
                total_cache_read_tokens INTEGER DEFAULT 0,
                message_count INTEGER DEFAULT 0,
                last_parsed_line INTEGER DEFAULT 0,
                project TEXT NOT NULL DEFAULT '',
                project_name TEXT NOT NULL DEFAULT '',
                skill_usage TEXT DEFAULT '{}'
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
            CREATE TABLE projects (
                project_path TEXT PRIMARY KEY,
                project_slug TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                user_edited BOOLEAN NOT NULL DEFAULT 0,
                last_updated TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE worker_result_stats (
                worker_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL DEFAULT '',
                agent_type TEXT NOT NULL DEFAULT '',
                task_uuid TEXT NOT NULL DEFAULT '',
                duration_ms INTEGER DEFAULT 0,
                duration_api_ms INTEGER DEFAULT 0,
                num_turns INTEGER DEFAULT 0,
                cost_usd REAL DEFAULT 0,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cache_creation_tokens INTEGER DEFAULT 0,
                cache_read_tokens INTEGER DEFAULT 0,
                is_error BOOLEAN DEFAULT 0,
                timestamp TEXT NOT NULL DEFAULT ''
            );
        """)
        conn.commit()

        _init_db(conn)

        cols = {r[1] for r in conn.execute("PRAGMA table_info(session_stats)").fetchall()}
        assert "segments" in cols
        version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert version == 7
        conn.close()


class TestSpansApiResponseShape:
    """Test that /api/sessions/{id}/spans returns {spans, segments} shape."""

    @pytest.fixture(autouse=True)
    def setup_db(self, tmp_path):
        self.tmp_path = tmp_path
        rows = [
            ("2026-01-10T10:00:00+00:00", "subagent_start", "s1", "a1", "code", "", "", "", "", ""),
            ("2026-01-10T10:00:10+00:00", "subagent_stop", "s1", "a1", "code", "", "", "", "", ""),
        ]
        _make_db(tmp_path, rows)
        self.db_patch = _patch_db(tmp_path)
        self.db_patch.start()
        yield
        self.db_patch.stop()

    @pytest.fixture()
    def client(self):
        from supercharge.dashboard import _create_app
        app = _create_app()
        return TestClient(app)

    def test_response_has_spans_and_segments(self, client):
        """GET /api/sessions/s1/spans returns {spans: [...], segments: [...]}."""
        resp = client.get("/api/sessions/s1/spans")
        assert resp.status_code == 200
        data = resp.json()
        assert "spans" in data
        assert "segments" in data
        assert isinstance(data["spans"], list)
        assert isinstance(data["segments"], list)
