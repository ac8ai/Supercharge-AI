"""Tests for worker_result_stats: migration 5, _emit_worker_result, and worker integration."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from supercharge.metrics import _emit_worker_result, _init_db


def _patch_db(tmp_path: Path):
    return patch("supercharge.metrics._db_path", return_value=tmp_path / "metrics.db")


def _make_result_msg(
    *,
    usage: dict | None = None,
    duration_ms: int = 5000,
    duration_api_ms: int = 4000,
    num_turns: int = 3,
    total_cost_usd: float = 0.05,
    is_error: bool = False,
    session_id: str = "sess-abc",
    result: str = "done",
) -> SimpleNamespace:
    """Create a mock ResultMessage with the expected fields."""
    return SimpleNamespace(
        usage=usage
        or {
            "input_tokens": 1000,
            "output_tokens": 500,
            "cache_creation_tokens": 200,
            "cache_read_tokens": 300,
        },
        duration_ms=duration_ms,
        duration_api_ms=duration_api_ms,
        num_turns=num_turns,
        total_cost_usd=total_cost_usd,
        is_error=is_error,
        session_id=session_id,
        result=result,
    )


# ── Migration 5 ─────────────────────────────────────────────────────────────


class TestMigration5:
    """Verify migration 5 creates the worker_result_stats table."""

    def test_table_created(self, tmp_path: Path):
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        _init_db(conn)

        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='worker_result_stats'"
        )
        assert cursor.fetchone() is not None
        conn.close()

    def test_table_columns(self, tmp_path: Path):
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        _init_db(conn)

        cursor = conn.execute("PRAGMA table_info(worker_result_stats)")
        columns = {row[1] for row in cursor.fetchall()}
        expected = {
            "worker_id",
            "session_id",
            "agent_type",
            "task_uuid",
            "duration_ms",
            "duration_api_ms",
            "num_turns",
            "cost_usd",
            "input_tokens",
            "output_tokens",
            "cache_creation_tokens",
            "cache_read_tokens",
            "is_error",
            "timestamp",
        }
        assert columns == expected
        conn.close()

    def test_indexes_created(self, tmp_path: Path):
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        _init_db(conn)

        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_worker_stats_%'"
        )
        indexes = {row[0] for row in cursor.fetchall()}
        assert "idx_worker_stats_session" in indexes
        assert "idx_worker_stats_task" in indexes
        conn.close()

    def test_schema_version_set(self, tmp_path: Path):
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        _init_db(conn)

        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        assert row[0] >= 5
        conn.close()


# ── _emit_worker_result ─────────────────────────────────────────────────────


class TestEmitWorkerResult:
    """Test _emit_worker_result inserts correct data."""

    def test_basic_insert(self, tmp_path: Path):
        msg = _make_result_msg()
        with _patch_db(tmp_path):
            _emit_worker_result("w-001", msg, "code", "task-abc")

        db = tmp_path / "metrics.db"
        conn = sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT * FROM worker_result_stats WHERE worker_id = ?", ("w-001",)
        ).fetchone()
        conn.close()

        assert row is not None
        # row order matches CREATE TABLE column order
        assert row[0] == "w-001"  # worker_id
        assert row[1] == "sess-abc"  # session_id
        assert row[2] == "code"  # agent_type
        assert row[3] == "task-abc"  # task_uuid
        assert row[4] == 5000  # duration_ms
        assert row[5] == 4000  # duration_api_ms
        assert row[6] == 3  # num_turns
        assert row[7] == pytest.approx(0.05)  # cost_usd
        assert row[8] == 1000  # input_tokens
        assert row[9] == 500  # output_tokens
        assert row[10] == 200  # cache_creation_tokens
        assert row[11] == 300  # cache_read_tokens
        assert row[12] == 0  # is_error (False -> 0)

    def test_error_flag(self, tmp_path: Path):
        msg = _make_result_msg(is_error=True)
        with _patch_db(tmp_path):
            _emit_worker_result("w-err", msg, "code", "task-err")

        db = tmp_path / "metrics.db"
        conn = sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT is_error FROM worker_result_stats WHERE worker_id = ?",
            ("w-err",),
        ).fetchone()
        conn.close()
        assert row[0] == 1

    def test_missing_usage(self, tmp_path: Path):
        """ResultMessage with usage=None should default to 0 tokens."""
        msg = _make_result_msg(usage=None)
        # Clear usage attr to simulate missing
        msg.usage = None
        with _patch_db(tmp_path):
            _emit_worker_result("w-nousage", msg, "code", "task-x")

        db = tmp_path / "metrics.db"
        conn = sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens "
            "FROM worker_result_stats WHERE worker_id = ?",
            ("w-nousage",),
        ).fetchone()
        conn.close()
        assert row == (0, 0, 0, 0)

    def test_replace_on_duplicate(self, tmp_path: Path):
        """INSERT OR REPLACE should update existing record."""
        msg1 = _make_result_msg(num_turns=1)
        msg2 = _make_result_msg(num_turns=5)
        with _patch_db(tmp_path):
            _emit_worker_result("w-dup", msg1, "code", "task-dup")
            _emit_worker_result("w-dup", msg2, "code", "task-dup")

        db = tmp_path / "metrics.db"
        conn = sqlite3.connect(str(db))
        rows = conn.execute(
            "SELECT num_turns FROM worker_result_stats WHERE worker_id = ?",
            ("w-dup",),
        ).fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0][0] == 5

    def test_never_raises(self, tmp_path: Path):
        """_emit_worker_result should swallow exceptions."""
        msg = _make_result_msg()
        with patch("supercharge.metrics._db_path", side_effect=RuntimeError("boom")):
            # Should not raise
            _emit_worker_result("w-fail", msg, "code", "task-fail")

    def test_missing_session_id(self, tmp_path: Path):
        """ResultMessage with session_id=None should default to empty string."""
        msg = _make_result_msg(session_id=None)  # type: ignore[arg-type]
        with _patch_db(tmp_path):
            _emit_worker_result("w-nosess", msg, "code", "task-y")

        db = tmp_path / "metrics.db"
        conn = sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT session_id FROM worker_result_stats WHERE worker_id = ?",
            ("w-nosess",),
        ).fetchone()
        conn.close()
        assert row[0] == ""
