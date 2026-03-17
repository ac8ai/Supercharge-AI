"""Tests for per-agent token stats: parsing, incremental updates, and session linking."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from supercharge.metrics import (
    _init_db,
    _parse_agent_transcript,
    _query_agent_tokens,
    _update_agent_token_stats,
    _update_all_session_stats,
)


def _patch_db(tmp_path: Path):
    return patch("supercharge.metrics._db_path", return_value=tmp_path / "metrics.db")


def _make_assistant_line(input_tokens: int, output_tokens: int,
                         cache_creation: int = 0, cache_read: int = 0) -> str:
    """Create a JSONL line for an assistant message with usage data."""
    entry = {
        "type": "assistant",
        "message": {
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": cache_creation,
                "cache_read_input_tokens": cache_read,
            }
        },
    }
    return json.dumps(entry)


def _make_transcript(tmp_path: Path, name: str, lines: list[str]) -> Path:
    """Write a JSONL transcript file and return its path."""
    transcript = tmp_path / f"{name}.jsonl"
    transcript.write_text("\n".join(lines) + "\n")
    return transcript


# ── _parse_agent_transcript ──────────────────────────────────────────────────


class TestParseAgentTranscript:
    """Test parsing an agent's JSONL transcript for token usage."""

    def test_sums_tokens_from_assistant_messages(self, tmp_path: Path):
        transcript = _make_transcript(tmp_path, "agent1", [
            _make_assistant_line(100, 50, 10, 20),
            _make_assistant_line(200, 80, 5, 30),
        ])

        result = _parse_agent_transcript(str(transcript))

        assert result["total_input_tokens"] == 300
        assert result["total_output_tokens"] == 130
        assert result["total_cache_creation_tokens"] == 15
        assert result["total_cache_read_tokens"] == 50
        assert result["message_count"] == 2
        assert result["last_parsed_line"] == 2

    def test_skips_non_assistant_lines(self, tmp_path: Path):
        transcript = _make_transcript(tmp_path, "agent2", [
            json.dumps({"type": "user", "message": {"content": "hello"}}),
            _make_assistant_line(100, 50),
            json.dumps({"type": "system", "data": "something"}),
        ])

        result = _parse_agent_transcript(str(transcript))

        assert result["total_input_tokens"] == 100
        assert result["total_output_tokens"] == 50
        assert result["message_count"] == 1
        assert result["last_parsed_line"] == 3

    def test_returns_zeros_for_missing_file(self):
        result = _parse_agent_transcript("/nonexistent/path.jsonl")

        assert result["total_input_tokens"] == 0
        assert result["total_output_tokens"] == 0
        assert result["message_count"] == 0

    def test_handles_malformed_json_lines(self, tmp_path: Path):
        transcript = _make_transcript(tmp_path, "agent3", [
            _make_assistant_line(100, 50),
            "not valid json {{{",
            _make_assistant_line(200, 80),
        ])

        result = _parse_agent_transcript(str(transcript))

        assert result["total_input_tokens"] == 300
        assert result["total_output_tokens"] == 130
        assert result["message_count"] == 2

    def test_handles_empty_file(self, tmp_path: Path):
        transcript = tmp_path / "empty.jsonl"
        transcript.write_text("")

        result = _parse_agent_transcript(str(transcript))

        assert result["message_count"] == 0
        assert result["last_parsed_line"] == 0


# ── Incremental parsing ─────────────────────────────────────────────────────


class TestIncrementalParsing:
    """Test that start_line enables incremental (resume) parsing."""

    def test_skips_already_parsed_lines(self, tmp_path: Path):
        transcript = _make_transcript(tmp_path, "agent4", [
            _make_assistant_line(100, 50),
            _make_assistant_line(200, 80),
            _make_assistant_line(300, 120),
        ])

        # Parse first 2 lines
        result1 = _parse_agent_transcript(str(transcript), start_line=0)
        assert result1["message_count"] == 3
        assert result1["last_parsed_line"] == 3

        # Parse only new lines (starting from line 2)
        result2 = _parse_agent_transcript(str(transcript), start_line=2)
        assert result2["total_input_tokens"] == 300
        assert result2["total_output_tokens"] == 120
        assert result2["message_count"] == 1
        assert result2["last_parsed_line"] == 3

    def test_returns_no_new_data_when_fully_parsed(self, tmp_path: Path):
        transcript = _make_transcript(tmp_path, "agent5", [
            _make_assistant_line(100, 50),
        ])

        result = _parse_agent_transcript(str(transcript), start_line=1)
        assert result["message_count"] == 0
        assert result["total_input_tokens"] == 0
        assert result["last_parsed_line"] == 1


# ── _update_agent_token_stats ────────────────────────────────────────────────


class TestUpdateAgentTokenStats:
    """Test upserting agent token stats from subagent_stop events."""

    def _seed_subagent_stop(self, tmp_path: Path, session_id: str,
                            agent_id: str, agent_type: str,
                            transcript_path: str) -> None:
        """Insert a subagent_stop event into the DB."""
        db = tmp_path / "metrics.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        _init_db(conn)
        conn.execute(
            "INSERT INTO events (timestamp, event_type, session_id, agent_id, "
            "agent_type, detail) VALUES (?, ?, ?, ?, ?, ?)",
            ("2024-01-01T00:00:00", "subagent_stop", session_id,
             agent_id, agent_type, transcript_path),
        )
        conn.commit()
        conn.close()

    def test_populates_agent_token_stats(self, tmp_path: Path):
        """Parsing an agent transcript should populate agent_token_stats."""
        transcript = _make_transcript(tmp_path, "agent-abc", [
            _make_assistant_line(500, 200, 50, 100),
            _make_assistant_line(300, 150, 25, 75),
        ])

        self._seed_subagent_stop(tmp_path, "sess-1", "agent-abc", "code",
                                 str(transcript))

        with _patch_db(tmp_path):
            _update_agent_token_stats("sess-1")

        db = tmp_path / "metrics.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM agent_token_stats WHERE agent_id = 'agent-abc'"
        ).fetchone()
        conn.close()

        assert row is not None
        assert row["session_id"] == "sess-1"
        assert row["agent_type"] == "code"
        assert row["total_input_tokens"] == 800
        assert row["total_output_tokens"] == 350
        assert row["total_cache_creation_tokens"] == 75
        assert row["total_cache_read_tokens"] == 175
        assert row["message_count"] == 2
        assert row["last_parsed_line"] == 2

    def test_incremental_update_accumulates(self, tmp_path: Path):
        """A second update after new lines should accumulate tokens."""
        transcript_path = tmp_path / "agent-inc.jsonl"

        # First write: 1 line
        transcript_path.write_text(
            _make_assistant_line(100, 50, 10, 20) + "\n"
        )
        self._seed_subagent_stop(tmp_path, "sess-2", "agent-inc", "plan",
                                 str(transcript_path))

        with _patch_db(tmp_path):
            _update_agent_token_stats("sess-2")

        # Append a second line
        with transcript_path.open("a") as f:
            f.write(_make_assistant_line(200, 80, 5, 30) + "\n")

        with _patch_db(tmp_path):
            _update_agent_token_stats("sess-2")

        db = tmp_path / "metrics.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM agent_token_stats WHERE agent_id = 'agent-inc'"
        ).fetchone()
        conn.close()

        # Should be cumulative
        assert row["total_input_tokens"] == 300
        assert row["total_output_tokens"] == 130
        assert row["total_cache_creation_tokens"] == 15
        assert row["total_cache_read_tokens"] == 50
        assert row["message_count"] == 2
        assert row["last_parsed_line"] == 2

    def test_skips_when_no_new_lines(self, tmp_path: Path):
        """Re-running update with no new lines should not duplicate tokens."""
        transcript = _make_transcript(tmp_path, "agent-skip", [
            _make_assistant_line(100, 50),
        ])
        self._seed_subagent_stop(tmp_path, "sess-3", "agent-skip", "review",
                                 str(transcript))

        with _patch_db(tmp_path):
            _update_agent_token_stats("sess-3")
            _update_agent_token_stats("sess-3")  # second call, no new lines

        db = tmp_path / "metrics.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM agent_token_stats WHERE agent_id = 'agent-skip'"
        ).fetchone()
        conn.close()

        # Should NOT have doubled
        assert row["total_input_tokens"] == 100
        assert row["total_output_tokens"] == 50
        assert row["message_count"] == 1


# ── _query_agent_tokens ──────────────────────────────────────────────────────


class TestQueryAgentTokens:
    """Test querying per-agent token breakdown for a session."""

    def test_returns_agent_breakdown(self, tmp_path: Path):
        """Should return per-agent token stats keyed by agent_id."""
        transcript1 = _make_transcript(tmp_path, "agent-a", [
            _make_assistant_line(100, 50, 10, 20),
        ])
        transcript2 = _make_transcript(tmp_path, "agent-b", [
            _make_assistant_line(200, 80, 5, 30),
        ])

        db = tmp_path / "metrics.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        _init_db(conn)

        for agent_id, agent_type, path in [
            ("agent-a", "code", str(transcript1)),
            ("agent-b", "plan", str(transcript2)),
        ]:
            conn.execute(
                "INSERT INTO events (timestamp, event_type, session_id, agent_id, "
                "agent_type, detail) VALUES (?, ?, ?, ?, ?, ?)",
                ("2024-01-01", "subagent_stop", "sess-q", agent_id,
                 agent_type, path),
            )
        conn.commit()
        conn.close()

        with _patch_db(tmp_path):
            result = _query_agent_tokens("sess-q")

        assert "agent-a" in result
        assert "agent-b" in result
        assert result["agent-a"]["agent_type"] == "code"
        assert result["agent-a"]["input_tokens"] == 100
        assert result["agent-b"]["agent_type"] == "plan"
        assert result["agent-b"]["input_tokens"] == 200

    def test_returns_empty_for_no_agents(self, tmp_path: Path):
        db = tmp_path / "metrics.db"
        conn = sqlite3.connect(str(db))
        _init_db(conn)
        conn.close()

        with _patch_db(tmp_path):
            result = _query_agent_tokens("nonexistent-session")

        assert result == {}


# ── Integration with _update_all_session_stats ───────────────────────────────


class TestUpdateAllSessionStatsIntegration:
    """Test that _update_all_session_stats also updates agent token stats."""

    def test_calls_agent_token_update(self, tmp_path: Path):
        """_update_all_session_stats should update agent_token_stats for sessions."""
        # Create a transcript
        transcript = _make_transcript(tmp_path, "agent-int", [
            _make_assistant_line(500, 200, 50, 100),
        ])

        # Create a session JSONL (minimal, just so the session is discovered)
        slug_dir = tmp_path / "projects" / "test-slug"
        slug_dir.mkdir(parents=True)
        session_jsonl = slug_dir / "sess-int.jsonl"
        session_jsonl.write_text(
            _make_assistant_line(1000, 500) + "\n"
        )

        # Seed the events DB with a subagent_stop event
        db = tmp_path / "metrics.db"
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        _init_db(conn)
        conn.execute(
            "INSERT INTO events (timestamp, event_type, session_id, agent_id, "
            "agent_type, detail) VALUES (?, ?, ?, ?, ?, ?)",
            ("2024-01-01", "subagent_stop", "sess-int", "agent-int", "code",
             str(transcript)),
        )
        conn.commit()
        conn.close()

        with _patch_db(tmp_path), \
             patch("supercharge.metrics._user_config_dir", return_value=tmp_path), \
             patch("supercharge.metrics._find_session_jsonl", return_value=session_jsonl), \
             patch("supercharge.metrics._import_legacy_dbs"):
            _update_all_session_stats()

        # Verify agent_token_stats was populated
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM agent_token_stats WHERE agent_id = 'agent-int'"
        ).fetchone()
        conn.close()

        assert row is not None
        assert row["session_id"] == "sess-int"
        assert row["total_input_tokens"] == 500
        assert row["total_output_tokens"] == 200
