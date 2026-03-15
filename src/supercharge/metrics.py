"""Metrics collection module — SQLite-backed event store for SuperchargeAI.

Fire-and-forget event emitter for tracking session activity, task lifecycle,
and tool usage. Each call opens a new connection (hooks run as separate
subprocesses with no shared state). Uses WAL mode for concurrent access.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from supercharge.paths import _project_dir

_COLUMNS = (
    "session_id",
    "agent_id",
    "agent_type",
    "task_uuid",
    "worker_id",
    "parent_id",
    "tool_name",
    "detail",
)


def _db_path() -> Path:
    """Return path to the metrics database."""
    return Path(_project_dir()) / ".claude" / "SuperchargeAI" / "metrics.db"


def _init_db(conn: sqlite3.Connection) -> None:
    """Create the events table and indexes if they don't exist."""
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
        CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
        CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
        CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_uuid);
        """
    )


def _emit(event_type: str, **kwargs: str) -> None:
    """Fire-and-forget event emitter. Never raises."""
    conn: sqlite3.Connection | None = None
    try:
        db = _db_path()
        db.parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(str(db))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")

        _init_db(conn)

        ts = datetime.now(timezone.utc).isoformat()
        values = {col: kwargs.get(col, "") for col in _COLUMNS}

        conn.execute(
            "INSERT INTO events (timestamp, event_type, "
            "session_id, agent_id, agent_type, task_uuid, "
            "worker_id, parent_id, tool_name, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ts,
                event_type,
                values["session_id"],
                values["agent_id"],
                values["agent_type"],
                values["task_uuid"],
                values["worker_id"],
                values["parent_id"],
                values["tool_name"],
                values["detail"],
            ),
        )
        conn.commit()
    except Exception:
        print(f"supercharge: metrics emit error", file=sys.stderr)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _query_events(
    event_type: str | None = None,
    session_id: str | None = None,
    task_uuid: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Query events from the metrics database. Never raises (returns [] on error)."""
    conn: sqlite3.Connection | None = None
    try:
        db = _db_path()
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row

        clauses: list[str] = []
        params: list[str] = []

        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type)
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if task_uuid is not None:
            clauses.append("task_uuid = ?")
            params.append(task_uuid)

        where = ""
        if clauses:
            where = " WHERE " + " AND ".join(clauses)

        query = f"SELECT * FROM events{where} ORDER BY id LIMIT ?"
        params.append(str(limit))

        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]
    except Exception:
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass