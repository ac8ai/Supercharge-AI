"""Metrics collection module — SQLite-backed event store for SuperchargeAI.

Fire-and-forget event emitter for tracking session activity, task lifecycle,
and tool usage. Each call opens a new connection (hooks run as separate
subprocesses with no shared state). Uses WAL mode for concurrent access.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from supercharge.paths import _project_dir, _user_config_dir


def _normalize_agent_type(raw: str) -> str:
    """Strip 'supercharge-ai:' prefix from agent type names."""
    if raw.startswith("supercharge-ai:"):
        return raw[len("supercharge-ai:"):]
    return raw


_INACTIVITY_GAP_MINUTES = 30

_COLUMNS = (
    'session_id',
    'agent_id',
    'agent_type',
    'task_uuid',
    'worker_id',
    'parent_id',
    'tool_name',
    'detail',
    'project',
)


def _db_path() -> Path:
    """Return path to the global metrics database (user-level)."""
    return _user_config_dir() / 'SuperchargeAI' / 'metrics.db'


def _init_db(conn: sqlite3.Connection) -> None:
    """Create the events table, indexes, and run pending migrations."""
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
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY
        );
        """
    )
    _run_migrations(conn)


def _get_schema_version(conn: sqlite3.Connection) -> int:
    """Return the current schema version, or 0 if no migrations have run."""
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        return row[0] if row[0] is not None else 0
    except Exception:
        return 0


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Run pending schema migrations sequentially."""
    current = _get_schema_version(conn)

    if current < 1:
        # Migration 1: Normalize agent_type — strip 'supercharge-ai:' prefix
        conn.execute(
            "UPDATE events SET agent_type = REPLACE(agent_type, 'supercharge-ai:', '') "
            "WHERE agent_type LIKE 'supercharge-ai:%'"
        )
        conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (1)")
        conn.commit()

    if current < 2:
        # Migration 2: Create session_stats table
        conn.executescript(
            """\
            CREATE TABLE IF NOT EXISTS session_stats (
                session_id TEXT PRIMARY KEY,
                custom_name TEXT DEFAULT '',
                total_input_tokens INTEGER DEFAULT 0,
                total_output_tokens INTEGER DEFAULT 0,
                total_cache_creation_tokens INTEGER DEFAULT 0,
                total_cache_read_tokens INTEGER DEFAULT 0,
                message_count INTEGER DEFAULT 0,
                last_parsed_line INTEGER DEFAULT 0
            );
            """
        )
        conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (2)")
        conn.commit()

    if current < 3:
        # Migration 3: Create agent_token_stats table for per-agent token tracking
        conn.executescript(
            """\
            CREATE TABLE IF NOT EXISTS agent_token_stats (
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
            CREATE INDEX IF NOT EXISTS idx_agent_tokens_session
                ON agent_token_stats(session_id);
            """
        )
        conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (3)")
        conn.commit()

    if current < 4:
        # Migration 4: Add project columns, projects table, and project indexes
        # ALTER TABLE ADD COLUMN is not idempotent — use try/except for thread safety
        for stmt in [
            "ALTER TABLE events ADD COLUMN project TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE session_stats ADD COLUMN project TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE session_stats ADD COLUMN project_name TEXT NOT NULL DEFAULT ''",
        ]:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # Column already exists (concurrent caller added it)
        conn.executescript(
            """\
            CREATE TABLE IF NOT EXISTS projects (
                project_path TEXT PRIMARY KEY,
                project_slug TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                user_edited BOOLEAN NOT NULL DEFAULT 0,
                last_updated TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_events_project ON events(project);
            CREATE INDEX IF NOT EXISTS idx_session_stats_project ON session_stats(project);
            """
        )
        conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (4)")
        conn.commit()

    if current < 5:
        # Migration 5: Create worker_result_stats table for SDK ResultMessage data
        conn.executescript(
            """\
            CREATE TABLE IF NOT EXISTS worker_result_stats (
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
            CREATE INDEX IF NOT EXISTS idx_worker_stats_session ON worker_result_stats(session_id);
            CREATE INDEX IF NOT EXISTS idx_worker_stats_task ON worker_result_stats(task_uuid);
            """
        )
        conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (5)")
        conn.commit()

    if current < 6:
        # Migration 6: Add skill_usage column to session_stats
        try:
            conn.execute(
                "ALTER TABLE session_stats ADD COLUMN skill_usage TEXT DEFAULT '{}'"
            )
        except sqlite3.OperationalError:
            pass  # Column already exists
        conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (6)")
        conn.commit()

    if current < 7:
        # Migration 7: Add segments column to session_stats for session splitting
        try:
            conn.execute(
                "ALTER TABLE session_stats ADD COLUMN segments TEXT NOT NULL DEFAULT '[]'"
            )
        except sqlite3.OperationalError:
            pass  # Column already exists
        conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (7)")
        conn.commit()


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
        values = {col: kwargs.get(col, '') for col in _COLUMNS}

        # Auto-derive project from CWD/env if not explicitly provided
        if not values['project']:
            try:
                values['project'] = _strip_task_folder(_project_dir())
            except Exception:
                pass
        else:
            values['project'] = _strip_task_folder(values['project'])

        conn.execute(
            'INSERT INTO events (timestamp, event_type, '
            'session_id, agent_id, agent_type, task_uuid, '
            'worker_id, parent_id, tool_name, detail, project) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (
                ts,
                event_type,
                values['session_id'],
                values['agent_id'],
                values['agent_type'],
                values['task_uuid'],
                values['worker_id'],
                values['parent_id'],
                values['tool_name'],
                values['detail'],
                values['project'],
            ),
        )
        conn.commit()
    except Exception as e:
        print(f"supercharge: _emit failed: {type(e).__name__}: {e}", file=sys.stderr)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _emit_worker_result(
    worker_id: str,
    result_msg: object,
    agent_type: str,
    task_uuid: str,
) -> None:
    """Record ResultMessage stats from a worker run. Never raises."""
    conn: sqlite3.Connection | None = None
    try:
        db = _db_path()
        db.parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(str(db))
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA busy_timeout=5000')
        _init_db(conn)

        usage = getattr(result_msg, 'usage', None) or {}
        ts = datetime.now(timezone.utc).isoformat()

        conn.execute(
            'INSERT OR REPLACE INTO worker_result_stats '
            '(worker_id, session_id, agent_type, task_uuid, '
            'duration_ms, duration_api_ms, num_turns, cost_usd, '
            'input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens, '
            'is_error, timestamp) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (
                worker_id,
                getattr(result_msg, 'session_id', '') or '',
                agent_type,
                task_uuid,
                getattr(result_msg, 'duration_ms', 0) or 0,
                getattr(result_msg, 'duration_api_ms', 0) or 0,
                getattr(result_msg, 'num_turns', 0) or 0,
                getattr(result_msg, 'total_cost_usd', 0.0) or 0.0,
                usage.get('input_tokens', 0) or 0,
                usage.get('output_tokens', 0) or 0,
                usage.get('cache_creation_tokens', 0) or 0,
                usage.get('cache_read_tokens', 0) or 0,
                1 if getattr(result_msg, 'is_error', False) else 0,
                ts,
            ),
        )
        conn.commit()
    except Exception as e:
        print(f'supercharge: _emit_worker_result failed: {type(e).__name__}: {e}', file=sys.stderr)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _build_where(
    *,
    event_type: str | None = None,
    session_id: str | None = None,
    task_uuid: str | None = None,
    since: str | None = None,
    until: str | None = None,
    after_id: int | None = None,
) -> tuple[str, list]:
    """Build a WHERE clause and params list from common filters."""
    clauses: list[str] = []
    params: list = []

    if event_type is not None:
        clauses.append("event_type = ?")
        params.append(event_type)
    if session_id is not None:
        clauses.append("session_id = ?")
        params.append(session_id)
    if task_uuid is not None:
        clauses.append("task_uuid = ?")
        params.append(task_uuid)
    if since is not None:
        clauses.append("timestamp >= ?")
        params.append(since)
    if until is not None:
        clauses.append("timestamp <= ?")
        params.append(until)
    if after_id is not None:
        clauses.append("id > ?")
        params.append(after_id)

    where = ""
    if clauses:
        where = " WHERE " + " AND ".join(clauses)
    return where, params


def _open_readonly() -> sqlite3.Connection:
    """Open a read-only connection to the metrics database."""
    db = _db_path()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _query_events(
    *,
    event_type: str | None = None,
    session_id: str | None = None,
    task_uuid: str | None = None,
    limit: int = 100,
    offset: int = 0,
    order: str = "asc",
    since: str | None = None,
    until: str | None = None,
    after_id: int | None = None,
) -> list[dict]:
    """Query events from the metrics database. Never raises (returns [] on error)."""
    conn: sqlite3.Connection | None = None
    try:
        limit = min(limit, 100_000)
        conn = _open_readonly()

        where, params = _build_where(
            event_type=event_type,
            session_id=session_id,
            task_uuid=task_uuid,
            since=since,
            until=until,
            after_id=after_id,
        )

        direction = "DESC" if order.lower() == "desc" else "ASC"
        query = f"SELECT * FROM events{where} ORDER BY id {direction} LIMIT ? OFFSET ?"
        params.extend([limit, offset])

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


def _event_count(
    *,
    event_type: str | None = None,
    session_id: str | None = None,
    task_uuid: str | None = None,
    since: str | None = None,
    until: str | None = None,
    after_id: int | None = None,
) -> int:
    """Return total event count matching filters. Never raises (returns 0 on error)."""
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_readonly()
        where, params = _build_where(
            event_type=event_type,
            session_id=session_id,
            task_uuid=task_uuid,
            since=since,
            until=until,
            after_id=after_id,
        )
        row = conn.execute(f"SELECT COUNT(*) FROM events{where}", params).fetchone()
        return row[0]
    except Exception:
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _query_session_events(session_id: str) -> list[dict]:
    """Return all events for a session, ordered by timestamp asc. Never raises."""
    return _query_events(session_id=session_id, limit=100_000, order="asc")


def _query_session_tools(session_id: str) -> dict:
    """Return per-agent tool breakdown for a session. Never raises."""
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_readonly()

        rows = conn.execute(
            "SELECT agent_id, agent_type, tool_name, COUNT(*) as count "
            "FROM events "
            "WHERE session_id = ? AND event_type = 'tool_use' AND tool_name != '' "
            "GROUP BY agent_id, agent_type, tool_name "
            "ORDER BY agent_type, count DESC",
            (session_id,),
        ).fetchall()

        # Group by (agent_id, agent_type)
        agents_map: dict[tuple[str, str], list[dict]] = {}
        totals: dict[str, int] = {}
        for row in rows:
            norm_type = _normalize_agent_type(row["agent_type"])
            key = (row["agent_id"], norm_type)
            tool_entry = {"tool_name": row["tool_name"], "count": row["count"]}
            agents_map.setdefault(key, []).append(tool_entry)
            totals[row["tool_name"]] = totals.get(row["tool_name"], 0) + row["count"]

        agents = [
            {"agent_id": aid, "agent_type": atype, "tools": tools}
            for (aid, atype), tools in agents_map.items()
        ]

        return {"agents": agents, "totals": totals}
    except Exception:
        return {"agents": [], "totals": {}}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _query_session_spans(session_id: str) -> list[dict]:
    """Return per-invocation spans for a session (start/stop pairs).

    Each span represents one invocation of an agent or worker:
    - type: "agent" or "worker"
    - id: agent_id or worker_id
    - agent_type: normalized agent type
    - parent_id: who spawned this span
    - start: ISO timestamp of start event
    - end: ISO timestamp of stop event (or None if still running)
    - is_resume: True if this agent_id appeared in an earlier span

    Spans are ordered by start time. Never raises.
    """
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_readonly()

        rows = conn.execute(
            "SELECT id, timestamp, event_type, agent_id, agent_type, "
            "worker_id, parent_id, task_uuid "
            "FROM events "
            "WHERE session_id = ? AND event_type IN "
            "('subagent_start','subagent_stop','worker_start','worker_end',"
            "'subtask_init','task_init') "
            "ORDER BY id ASC",
            (session_id,),
        ).fetchall()

        spans: list[dict] = []
        # Track open spans by key for matching stops
        open_spans: dict[str, dict] = {}
        # Track how many times each id has been seen (for is_resume)
        seen_ids: dict[str, int] = {}

        for row in rows:
            etype = row["event_type"]
            ts = row["timestamp"]
            agent_id = row["agent_id"] or ""
            worker_id = row["worker_id"] or ""
            agent_type = _normalize_agent_type(row["agent_type"] or "")
            parent_id = row["parent_id"] or ""
            task_uuid = row["task_uuid"] or ""

            if etype == "subagent_start":
                count = seen_ids.get(agent_id, 0)
                seen_ids[agent_id] = count + 1
                span = {
                    "type": "agent",
                    "id": agent_id,
                    "task_uuid": "",
                    "agent_type": agent_type,
                    "parent_id": parent_id,
                    "start": ts,
                    "end": None,
                    "is_resume": count > 0,
                }
                spans.append(span)
                if agent_id:
                    open_spans[f"agent:{agent_id}"] = span

            elif etype == "task_init":
                # Associate task_uuid with the most recent open agent span
                # that has the same agent_type (task_init follows subagent_start)
                if task_uuid:
                    # Find the open agent span to attach task_uuid
                    for sp in reversed(spans):
                        if sp["type"] == "agent" and not sp["task_uuid"] and sp["agent_type"] == agent_type:
                            sp["task_uuid"] = task_uuid
                            break

            elif etype == "subagent_stop":
                key = f"agent:{agent_id}" if agent_id else ""
                if key and key in open_spans:
                    open_spans[key]["end"] = ts
                    del open_spans[key]

            elif etype in ("subtask_init", "worker_start"):
                wid = worker_id
                key = f"worker:{wid}" if wid else ""
                if key and key in open_spans:
                    # worker_start after subtask_init — just update start time
                    if etype == "worker_start":
                        open_spans[key]["start"] = ts
                    continue
                count = seen_ids.get(wid, 0)
                seen_ids[wid] = count + 1
                span = {
                    "type": "worker",
                    "id": wid,
                    "agent_type": agent_type,
                    "parent_id": parent_id,
                    "start": ts,
                    "end": None,
                    "is_resume": count > 0,
                }
                spans.append(span)
                if wid:
                    open_spans[key] = span

            elif etype == "worker_end":
                key = f"worker:{worker_id}" if worker_id else ""
                if key and key in open_spans:
                    open_spans[key]["end"] = ts
                    del open_spans[key]

        # Enrich spans with tool_calls count
        # Query tool_use events grouped by agent_id and worker_id
        tool_rows = conn.execute(
            "SELECT agent_id, worker_id, task_uuid, COUNT(*) as cnt "
            "FROM events "
            "WHERE session_id = ? AND event_type = 'tool_use' "
            "GROUP BY agent_id, worker_id, task_uuid",
            (session_id,),
        ).fetchall()

        # Build lookup: (agent_id or worker_id) -> tool count
        tool_by_agent: dict[str, int] = {}
        tool_by_worker: dict[str, int] = {}
        tool_by_task: dict[str, int] = {}
        for tr in tool_rows:
            aid = tr["agent_id"] or ""
            wid = tr["worker_id"] or ""
            tid = tr["task_uuid"] or ""
            cnt = tr["cnt"]
            if wid:
                tool_by_worker[wid] = tool_by_worker.get(wid, 0) + cnt
            elif aid:
                tool_by_agent[aid] = tool_by_agent.get(aid, 0) + cnt
            if tid:
                tool_by_task[tid] = tool_by_task.get(tid, 0) + cnt

        for span in spans:
            if span["type"] == "worker":
                span["tool_calls"] = tool_by_worker.get(span["id"], 0)
            elif span["type"] == "agent":
                # Try agent_id first, then task_uuid
                span["tool_calls"] = tool_by_agent.get(span["id"], 0)
                if span["tool_calls"] == 0 and span.get("task_uuid"):
                    span["tool_calls"] = tool_by_task.get(span["task_uuid"], 0)

        # Also try to find task_uuid for agents that don't have one
        # by looking at task_init events with matching agent_type and close timestamps
        if any(s["type"] == "agent" and not s.get("task_uuid") for s in spans):
            task_inits = conn.execute(
                "SELECT timestamp, agent_type, task_uuid FROM events "
                "WHERE session_id = ? AND event_type = 'task_init' AND task_uuid != '' "
                "ORDER BY id ASC",
                (session_id,),
            ).fetchall()

            # Match each agent span without task_uuid to the closest task_init
            used_tasks: set[str] = set()
            for span in spans:
                if span["type"] != "agent" or span.get("task_uuid"):
                    continue
                best_match = None
                best_delta = float("inf")
                span_type = _normalize_agent_type(span.get("agent_type", ""))
                for ti in task_inits:
                    ti_uuid = ti["task_uuid"]
                    if ti_uuid in used_tasks:
                        continue
                    ti_type = _normalize_agent_type(ti["agent_type"] or "")
                    if ti_type != span_type:
                        continue
                    try:
                        ti_ts = datetime.fromisoformat(ti["timestamp"]).timestamp()
                        sp_ts = datetime.fromisoformat(span["start"]).timestamp()
                        delta = abs(ti_ts - sp_ts)
                    except Exception:
                        continue
                    if delta < best_delta and delta < 10:  # within 10 seconds
                        best_delta = delta
                        best_match = ti_uuid
                if best_match:
                    span["task_uuid"] = best_match
                    used_tasks.add(best_match)
                    # Also try to get tool count
                    if span["tool_calls"] == 0:
                        span["tool_calls"] = tool_by_task.get(best_match, 0)

        # For spans missing an end time (no subagent_stop event), infer
        # a synthetic end from the next span's start or the session's last event.
        # This is only used for tool attribution, not displayed as real end time.
        last_ts_row = conn.execute(
            "SELECT MAX(timestamp) AS t FROM events WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        session_end = last_ts_row["t"] if last_ts_row else None
        sorted_spans = sorted(spans, key=lambda s: s.get("start") or "")
        for i, span in enumerate(sorted_spans):
            if span.get("start") and not span.get("end"):
                # Use next span's start as boundary, or session end
                next_start = None
                for j in range(i + 1, len(sorted_spans)):
                    ns = sorted_spans[j].get("start")
                    if ns and ns > span["start"]:
                        next_start = ns
                        break
                span["_inferred_end"] = next_start or session_end

        # Timestamp-based tool attribution: assign each unattributed tool_use
        # event to the narrowest (most specific) span that contains it.
        # This avoids double-counting when spans overlap (e.g., orchestrator
        # span contains child agent spans).
        spans_with_bounds = [
            s for s in spans
            if s.get("start") and (s.get("end") or s.get("_inferred_end"))
        ]
        if spans_with_bounds:
            # Fetch tool_use events that were NOT already attributed by agent_id/task_uuid
            # (i.e., those with empty agent_id — real sessions emit tool_use without agent context)
            all_tools = conn.execute(
                "SELECT timestamp FROM events "
                "WHERE session_id = ? AND event_type = 'tool_use' "
                "AND (agent_id = '' OR agent_id IS NULL) "
                "ORDER BY timestamp ASC",
                (session_id,),
            ).fetchall()

            if all_tools:
                tool_timestamps = [t["timestamp"] for t in all_tools]
                # For each tool event, find the narrowest containing span
                ts_counts: dict[int, int] = {}  # span index -> count
                for ts in tool_timestamps:
                    best_idx = -1
                    best_duration = float("inf")
                    for i, span in enumerate(spans_with_bounds):
                        s_end = span.get("end") or span.get("_inferred_end")
                        if span["start"] <= ts <= s_end:
                            try:
                                dur = (
                                    datetime.fromisoformat(s_end).timestamp()
                                    - datetime.fromisoformat(span["start"]).timestamp()
                                )
                            except Exception:
                                dur = float("inf")
                            if dur < best_duration:
                                best_duration = dur
                                best_idx = i
                    if best_idx >= 0:
                        ts_counts[best_idx] = ts_counts.get(best_idx, 0) + 1

                for idx, count in ts_counts.items():
                    span = spans_with_bounds[idx]
                    if span.get("tool_calls", 0) == 0:
                        span["tool_calls"] = count
                    # If span already has tool_calls from agent_id match, keep the higher value
                    elif count > span["tool_calls"]:
                        span["tool_calls"] = count

            # For spans still at 0 tool_calls, also try with ALL tool_use events
            # (handles cases where agent_id IS set but doesn't match span id)
            still_zero = [
                s for s in spans
                if s.get("tool_calls", 0) == 0
                and s.get("start")
                and (s.get("end") or s.get("_inferred_end"))
            ]
            if still_zero:
                all_tools_full = conn.execute(
                    "SELECT timestamp FROM events "
                    "WHERE session_id = ? AND event_type = 'tool_use' "
                    "ORDER BY timestamp ASC",
                    (session_id,),
                ).fetchall()
                if all_tools_full:
                    full_timestamps = [t["timestamp"] for t in all_tools_full]
                    ts_counts2: dict[int, int] = {}
                    for ts in full_timestamps:
                        best_idx = -1
                        best_duration = float("inf")
                        for i, span in enumerate(still_zero):
                            s_end = span.get("end") or span.get("_inferred_end")
                            if span["start"] <= ts <= s_end:
                                try:
                                    dur = (
                                        datetime.fromisoformat(s_end).timestamp()
                                        - datetime.fromisoformat(span["start"]).timestamp()
                                    )
                                except Exception:
                                    dur = float("inf")
                                if dur < best_duration:
                                    best_duration = dur
                                    best_idx = i
                        if best_idx >= 0:
                            ts_counts2[best_idx] = ts_counts2.get(best_idx, 0) + 1
                    for idx, count in ts_counts2.items():
                        if count > 0:
                            still_zero[idx]["tool_calls"] = count

        # Remove internal _inferred_end before returning
        for span in spans:
            span.pop("_inferred_end", None)

        return spans
    except Exception:
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _query_sessions() -> list[dict]:
    """Return distinct sessions with aggregate stats. Never raises (returns [] on error)."""
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_readonly()

        rows = conn.execute(
            """\
            SELECT
                session_id,
                MIN(timestamp) AS first_timestamp,
                MAX(timestamp) AS last_timestamp,
                COUNT(*) AS event_count,
                COUNT(DISTINCT CASE WHEN agent_id != '' THEN agent_id END) AS agent_count,
                COUNT(DISTINCT CASE WHEN worker_id != '' THEN worker_id END) AS worker_count,
                SUM(CASE WHEN event_type = 'tool_use' THEN 1 ELSE 0 END) AS tool_call_count,
                GROUP_CONCAT(DISTINCT CASE WHEN agent_type != '' THEN agent_type END) AS agent_types_csv,
                (SELECT GROUP_CONCAT(at, ',') FROM (
                    SELECT agent_type AS at
                    FROM events e2
                    WHERE e2.session_id = events.session_id
                      AND e2.agent_type != ''
                    GROUP BY e2.agent_type
                    ORDER BY MIN(e2.timestamp)
                )) AS agent_types_ordered
            FROM events
            WHERE session_id != ''
            GROUP BY session_id
            HAVING COUNT(*) > 1
              AND (COUNT(DISTINCT CASE WHEN agent_id != '' THEN agent_id END) > 0
                   OR COUNT(DISTINCT CASE WHEN worker_id != '' THEN worker_id END) > 0
                   OR SUM(CASE WHEN event_type = 'tool_use' THEN 1 ELSE 0 END) > 0)
            ORDER BY first_timestamp DESC
            """
        ).fetchall()

        results: list[dict] = []
        for row in rows:
            first_ts = row["first_timestamp"]
            last_ts = row["last_timestamp"]
            try:
                t0 = datetime.fromisoformat(first_ts)
                t1 = datetime.fromisoformat(last_ts)
                duration = (t1 - t0).total_seconds()
            except Exception:
                duration = 0.0

            agent_types_csv = row["agent_types_ordered"] or row["agent_types_csv"] or ""
            agent_types_raw = [a for a in agent_types_csv.split(",") if a]
            # Normalize and deduplicate, preserving temporal order
            seen: set[str] = set()
            agent_types: list[str] = []
            for a in agent_types_raw:
                norm = _normalize_agent_type(a)
                if norm not in seen:
                    seen.add(norm)
                    agent_types.append(norm)

            results.append(
                {
                    "session_id": row["session_id"],
                    "first_timestamp": first_ts,
                    "last_timestamp": last_ts,
                    "duration_seconds": duration,
                    "event_count": row["event_count"],
                    "agent_count": row["agent_count"],
                    "worker_count": row["worker_count"],
                    "tool_call_count": row["tool_call_count"],
                    "agent_types": agent_types,
                }
            )
        # Enrich results with project and project_name from session_stats
        if results:
            try:
                sids = [r["session_id"] for r in results]
                placeholders = ",".join("?" for _ in sids)
                proj_rows = conn.execute(
                    f"SELECT session_id, project, project_name FROM session_stats "
                    f"WHERE session_id IN ({placeholders})",
                    sids,
                ).fetchall()
                proj_map: dict[str, tuple[str, str]] = {
                    r["session_id"]: (r["project"] or "", r["project_name"] or "")
                    for r in proj_rows
                }
            except Exception:
                proj_map = {}
            for result in results:
                proj, proj_name = proj_map.get(result["session_id"], ("", ""))
                result["project"] = proj
                result["project_name"] = proj_name
        return results
    except Exception:
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _query_stats() -> dict:
    """Return global aggregate statistics. Never raises (returns {} on error)."""
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_readonly()

        # --- totals ---
        row = conn.execute(
            """\
            SELECT
                COUNT(*) AS events,
                COUNT(DISTINCT CASE WHEN session_id != '' THEN session_id END) AS sessions,
                COUNT(DISTINCT CASE WHEN agent_id != '' THEN agent_id END) AS agents,
                COUNT(DISTINCT CASE WHEN worker_id != '' THEN worker_id END) AS workers,
                SUM(CASE WHEN event_type = 'tool_use' THEN 1 ELSE 0 END) AS tool_calls
            FROM events
            """
        ).fetchone()
        totals = {
            "sessions": row["sessions"],
            "events": row["events"],
            "agents": row["agents"],
            "workers": row["workers"],
            "tool_calls": row["tool_calls"],
        }

        # --- by_agent_type ---
        rows = conn.execute(
            "SELECT agent_type, COUNT(*) AS cnt FROM events "
            "WHERE agent_type != '' GROUP BY agent_type"
        ).fetchall()
        by_agent_type: dict[str, int] = {}
        for r in rows:
            key = _normalize_agent_type(r["agent_type"])
            by_agent_type[key] = by_agent_type.get(key, 0) + r["cnt"]

        # --- by_event_type ---
        rows = conn.execute(
            "SELECT event_type, COUNT(*) AS cnt FROM events GROUP BY event_type"
        ).fetchall()
        by_event_type = {r["event_type"]: r["cnt"] for r in rows}

        # --- path_frequencies ---
        # Ordered sequence of agent_types per session (by timestamp)
        rows = conn.execute(
            """\
            SELECT session_id, GROUP_CONCAT(agent_type, '->') AS path
            FROM (
                SELECT session_id, agent_type
                FROM events
                WHERE agent_type != '' AND session_id != ''
                GROUP BY session_id, agent_type
                ORDER BY session_id, MIN(timestamp)
            )
            GROUP BY session_id
            """
        ).fetchall()
        path_counts: dict[str, int] = {}
        for r in rows:
            p = r["path"]
            path_counts[p] = path_counts.get(p, 0) + 1
        path_frequencies = [
            {"path": [_normalize_agent_type(a) for a in p.split("->")], "count": c}
            for p, c in sorted(path_counts.items(), key=lambda x: -x[1])
        ]

        # --- averages ---
        session_rows = conn.execute(
            """\
            SELECT
                session_id,
                MIN(timestamp) AS first_ts,
                MAX(timestamp) AS last_ts,
                COUNT(DISTINCT CASE WHEN agent_type != '' THEN agent_type END) AS agent_cnt
            FROM events
            WHERE session_id != ''
            GROUP BY session_id
            """
        ).fetchall()
        durations: list[float] = []
        agent_counts: list[int] = []
        for sr in session_rows:
            try:
                t0 = datetime.fromisoformat(sr["first_ts"])
                t1 = datetime.fromisoformat(sr["last_ts"])
                durations.append((t1 - t0).total_seconds())
            except Exception:
                pass
            agent_counts.append(sr["agent_cnt"])

        avg_duration = sum(durations) / len(durations) if durations else 0.0
        avg_agents = sum(agent_counts) / len(agent_counts) if agent_counts else 0.0

        # --- timeline (hourly buckets) ---
        rows = conn.execute(
            """\
            SELECT
                SUBSTR(timestamp, 1, 13) AS hour,
                COUNT(*) AS count
            FROM events
            GROUP BY hour
            ORDER BY hour
            """
        ).fetchall()
        timeline = [{"hour": r["hour"], "count": r["count"]} for r in rows]

        return {
            "totals": totals,
            "by_agent_type": by_agent_type,
            "by_event_type": by_event_type,
            "path_frequencies": path_frequencies,
            "averages": {
                "session_duration": avg_duration,
                "agents_per_session": avg_agents,
            },
            "timeline": timeline,
        }
    except Exception:
        return {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ── Read-write connection helper ─────────────────────────────────────────────


def _open_readwrite() -> sqlite3.Connection:
    """Open a read-write connection to the metrics database.

    Used by session stats updater and rename operations. Callers must
    close the connection when done.
    """
    db = _db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    _init_db(conn)
    return conn


# ── Helpers ──────────────────────────────────────────────────────────────────


def _strip_task_folder(path: str) -> str:
    """Strip SuperchargeAI task folder suffix to get the real project path.

    Agent subprocesses run with CWD set to their task folder, e.g.
    /workspaces/MyProject/.claude/SuperchargeAI/tasks/code/abc123
    This strips to /workspaces/MyProject.
    """
    idx = path.find('/.claude/SuperchargeAI/')
    if idx > 0:
        return path[:idx]
    return path


def _is_junk_project_path(path: str) -> bool:
    """Return True for paths that should not be tracked as real projects.

    Filters out SuperchargeAI task folders and temporary directories so they
    are never stored as projects in the DB.
    """
    if '/.claude/SuperchargeAI/' in path:
        return True
    if path.startswith('/tmp/'):
        return True
    return False


# ── JSONL parser ─────────────────────────────────────────────────────────────


def _find_session_jsonl(session_id: str) -> Path | None:
    """Locate the JSONL transcript file for a session across all projects.

    Scans all slug directories in ~/.claude/projects/ for the session's
    JSONL file, checking most recently modified directories first for efficiency.

    Returns the Path if found, None otherwise.
    """
    projects_dir = _user_config_dir() / 'projects'
    if not projects_dir.is_dir():
        return None

    # Collect slug dirs sorted by modification time (most recent first)
    try:
        slug_dirs = sorted(
            (d for d in projects_dir.iterdir() if d.is_dir()),
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None

    for slug_dir in slug_dirs:
        jsonl_path = slug_dir / f'{session_id}.jsonl'
        if jsonl_path.is_file():
            return jsonl_path

    return None


def _vote_session_project(session_id: str) -> str | None:
    """Determine which project a session belongs to by voting on CWD values.

    Reads cwd values from messages in the session's JSONL file with early
    settlement rules:
    - Rule 1: 5 consecutive identical CWDs with no other CWD ever seen -> settled
    - Rule 2: After 20 votes, if one CWD has >80% share -> settled
    - Rule 3: After 50 messages max, return the most frequent CWD

    Returns the winning CWD path, or None if no CWD found.
    """
    jsonl_path = _find_session_jsonl(session_id)
    if jsonl_path is None:
        return None

    from collections import Counter
    cwd_counts: Counter[str] = Counter()
    consecutive_count = 0
    last_cwd: str | None = None
    total_votes = 0

    try:
        with jsonl_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue

                cwd = entry.get('cwd')
                if not cwd or not isinstance(cwd, str):
                    continue

                total_votes += 1
                cwd_counts[cwd] += 1

                # Track consecutive same CWDs
                if cwd == last_cwd:
                    consecutive_count += 1
                else:
                    consecutive_count = 1
                    last_cwd = cwd

                # Rule 1: 5 consecutive identical with no other CWD ever seen
                if consecutive_count >= 5 and len(cwd_counts) == 1:
                    return cwd

                # Rule 2: After 20 votes, >80% share
                if total_votes >= 20:
                    top_cwd, top_count = cwd_counts.most_common(1)[0]
                    if top_count / total_votes > 0.8:
                        return top_cwd

                # Rule 3: Stop after 50 messages
                if total_votes >= 50:
                    return cwd_counts.most_common(1)[0][0]
    except Exception:
        pass

    if not cwd_counts:
        # Fallback: derive project from JSONL file path
        # Path is ~/.claude/projects/<slug>/<session>.jsonl
        # Claude's slug replaces / . and other chars with -, so reversal is lossy.
        # Match against known project slugs in the DB instead.
        if jsonl_path is not None:
            slug = jsonl_path.parent.name
            # Strip task subfolder suffixes
            base_slug = slug.split("--claude-")[0] if "--claude-" in slug else slug
            try:
                conn = _open_readonly()
                if conn:
                    rows = conn.execute(
                        "SELECT project_path, project_slug FROM projects"
                    ).fetchall()
                    conn.close()
                    for row in rows:
                        db_slug = row["project_slug"]
                        # Claude's slug also replaces dots with hyphens
                        db_slug_normalized = db_slug.replace(".", "-")
                        if db_slug in (slug, base_slug) or db_slug_normalized in (slug, base_slug):
                            return row["project_path"]
            except Exception:
                pass
        return None

    return cwd_counts.most_common(1)[0][0]


def _parse_session_jsonl(session_id: str, start_line: int = 0) -> dict:
    """Parse a session's JSONL transcript, extracting token usage and custom name.

    Reads from *start_line* forward for incremental parsing. Returns a dict with:
    - custom_name: latest custom-title found (empty string if none)
    - total_input_tokens, total_output_tokens, total_cache_creation_tokens,
      total_cache_read_tokens: summed from assistant message usage objects
    - message_count: number of assistant messages with usage data
    - last_parsed_line: total line count after parsing (for incremental resume)
    """
    result = {
        "custom_name": "",
        "first_user_message": "",
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cache_creation_tokens": 0,
        "total_cache_read_tokens": 0,
        "message_count": 0,
        "last_parsed_line": start_line,
        "skills": {},
        "message_timestamps": [],
    }

    jsonl_path = _find_session_jsonl(session_id)
    if jsonl_path is None:
        return result

    try:
        total_lines = 0
        with jsonl_path.open() as f:
            for line_num, line in enumerate(f):
                total_lines = line_num + 1
                line = line.strip()
                if not line:
                    if line_num < start_line:
                        continue
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    if line_num < start_line:
                        continue
                    continue

                entry_type = entry.get("type", "")

                # Collect timestamps from user/assistant messages
                # (always, even for lines before start_line)
                if entry_type in ("user", "assistant"):
                    ts = entry.get("timestamp")
                    if ts:
                        result["message_timestamps"].append((ts, entry_type))

                if line_num < start_line:
                    continue

                if entry_type == "custom-title":
                    title = entry.get("customTitle", "")
                    if title:
                        result["custom_name"] = title

                elif entry_type == "user" and not result["first_user_message"]:
                    msg = entry.get("message", {})
                    if isinstance(msg, dict):
                        content = msg.get("content", "")
                        if isinstance(content, list):
                            text = next(
                                (c.get("text", "") for c in content if c.get("type") == "text"),
                                "",
                            )
                        elif isinstance(content, str):
                            text = content
                        else:
                            text = ""
                        # Strip IDE tags and truncate
                        import re
                        text = re.sub(r"<ide_[^>]*>.*?</ide_[^>]*>", "", text).strip()
                        if text:
                            result["first_user_message"] = text[:120]

                elif entry_type == "assistant":
                    message = entry.get("message", {})
                    if isinstance(message, dict):
                        usage = message.get("usage")
                        if usage and isinstance(usage, dict):
                            result["total_input_tokens"] += usage.get("input_tokens", 0)
                            result["total_output_tokens"] += usage.get("output_tokens", 0)
                            result["total_cache_creation_tokens"] += usage.get(
                                "cache_creation_input_tokens", 0
                            )
                            result["total_cache_read_tokens"] += usage.get(
                                "cache_read_input_tokens", 0
                            )
                            result["message_count"] += 1

                        # Detect skill usage in content blocks
                        content = message.get("content", [])
                        if isinstance(content, list):
                            for block in content:
                                if (
                                    isinstance(block, dict)
                                    and block.get("type") == "tool_use"
                                    and block.get("name") == "Skill"
                                ):
                                    inp = block.get("input", {})
                                    cmd = inp.get("command", "") if isinstance(inp, dict) else ""
                                    if cmd:
                                        skill_name = cmd.split()[0]
                                        result["skills"][skill_name] = result["skills"].get(skill_name, 0) + 1

        result["last_parsed_line"] = total_lines
    except Exception:
        pass

    return result


def _compute_segments(
    message_timestamps: list[tuple[str, str]],
    session_id: str,
    conn: sqlite3.Connection,
    gap_minutes: int = _INACTIVITY_GAP_MINUTES,
) -> list[dict]:
    """Compute session segments by detecting inactivity gaps.

    Walks the sorted list of ``(timestamp, type)`` tuples and splits when:
    1. A gap between the last assistant reply and the next user message exceeds
       *gap_minutes*, AND
    2. No agent or worker is active during that gap (checked via subagent_start /
       subagent_stop events).

    Returns a list of ``{"start": iso_ts, "end": iso_ts}`` dicts.
    """
    if not message_timestamps:
        return []

    sorted_ts = sorted(message_timestamps, key=lambda t: t[0])

    if len(sorted_ts) < 2:
        ts = sorted_ts[0][0]
        return [{"start": ts, "end": ts}]

    # Build active agent/worker time windows from events
    active_windows: list[tuple[str, str]] = []
    try:
        rows = conn.execute(
            "SELECT timestamp, event_type, agent_id, worker_id "
            "FROM events "
            "WHERE session_id = ? AND event_type IN "
            "('subagent_start','subagent_stop','worker_start','worker_end') "
            "ORDER BY id ASC",
            (session_id,),
        ).fetchall()

        open_starts: dict[str, str] = {}
        for row in rows:
            etype = row["event_type"] if isinstance(row, sqlite3.Row) else row[1]
            ts = row["timestamp"] if isinstance(row, sqlite3.Row) else row[0]
            if isinstance(row, sqlite3.Row):
                key = row["agent_id"] or row["worker_id"] or ""
            else:
                key = row[2] or row[3] or ""

            if etype in ("subagent_start", "worker_start"):
                if key:
                    open_starts[key] = ts
            elif etype in ("subagent_stop", "worker_end"):
                if key and key in open_starts:
                    active_windows.append((open_starts.pop(key), ts))
    except Exception:
        pass

    def _is_agent_active_during(gap_start: str, gap_end: str) -> bool:
        """Return True if any agent/worker window overlaps the gap."""
        for win_start, win_end in active_windows:
            # Overlap: window starts before gap ends AND window ends after gap starts
            if win_start < gap_end and win_end > gap_start:
                return True
        return False

    threshold_seconds = gap_minutes * 60
    segments: list[dict] = []
    segment_start = sorted_ts[0][0]

    for i in range(len(sorted_ts) - 1):
        curr_ts, curr_type = sorted_ts[i]
        next_ts, next_type = sorted_ts[i + 1]

        # Only split on assistant->user transition
        if curr_type == "assistant" and next_type == "user":
            try:
                t1 = datetime.fromisoformat(curr_ts)
                t2 = datetime.fromisoformat(next_ts)
                gap = (t2 - t1).total_seconds()
            except (ValueError, TypeError):
                continue

            if gap >= threshold_seconds and not _is_agent_active_during(curr_ts, next_ts):
                segments.append({"start": segment_start, "end": curr_ts})
                segment_start = next_ts

    # Close the last segment
    segments.append({"start": segment_start, "end": sorted_ts[-1][0]})
    return segments


def _resolve_project_name(project_path: str) -> str:
    """Resolve a human-readable display name for the given project path.

    Checks project metadata files in order:
    1. .devcontainer/devcontainer.json → ``name`` field
    2. pyproject.toml → ``[project] name`` (tomllib)
    3. package.json → ``name`` field
    4. Cargo.toml → ``[package] name`` (tomllib)
    5. go.mod → module name (last path component after ``/``)

    Falls back to humanizing the last path component of *project_path*:
    - camelCase → space-separated words
    - underscores / hyphens → title-cased words
    """
    base = Path(project_path)

    # 1. .devcontainer/devcontainer.json → name
    try:
        p = base / ".devcontainer" / "devcontainer.json"
        if p.is_file():
            data = json.loads(p.read_text())
            name = data.get("name", "")
            if name:
                return str(name)
    except Exception:
        pass

    # 2. pyproject.toml → [project] name
    try:
        p = base / "pyproject.toml"
        if p.is_file():
            with p.open("rb") as f:
                data = tomllib.load(f)
            name = data.get("project", {}).get("name", "")
            if name:
                return str(name)
    except Exception:
        pass

    # 3. package.json → name
    try:
        p = base / "package.json"
        if p.is_file():
            data = json.loads(p.read_text())
            name = data.get("name", "")
            if name:
                return str(name)
    except Exception:
        pass

    # 4. Cargo.toml → [package] name
    try:
        p = base / "Cargo.toml"
        if p.is_file():
            with p.open("rb") as f:
                data = tomllib.load(f)
            name = data.get("package", {}).get("name", "")
            if name:
                return str(name)
    except Exception:
        pass

    # 5. go.mod → module name (last path component)
    try:
        p = base / "go.mod"
        if p.is_file():
            first_line = p.read_text().splitlines()[0]
            if first_line.startswith("module "):
                module_name = first_line[len("module "):].strip()
                name = module_name.split("/")[-1]
                if name:
                    return name
    except Exception:
        pass

    # Fallback: humanize the directory name
    dir_name = os.path.basename(project_path.rstrip("/\\"))
    if not dir_name:
        return project_path
    # Split on camelCase boundaries
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", dir_name)
    # Split on underscores, hyphens, and whitespace; title-case each part
    parts = re.split(r"[_\-\s]+", s)
    return " ".join(part.title() for part in parts if part)


def _get_or_create_project(conn: sqlite3.Connection, project_path: str) -> dict:
    """Ensure *project_path* has an entry in the ``projects`` table.

    - Creates a new entry if missing, resolving ``display_name`` via
      :func:`_resolve_project_name`.
    - Re-resolves ``display_name`` when ``user_edited=0`` and ``last_updated``
      is more than 24 hours old.
    - Never overwrites ``display_name`` when ``user_edited=1``.

    Returns a dict with ``project_path``, ``project_slug``, ``display_name``.
    """
    now = datetime.now(timezone.utc)
    result: dict = {
        "project_path": project_path,
        "project_slug": project_path.replace("/", "-"),
        "display_name": "",
    }
    if _is_junk_project_path(project_path):
        return result
    try:
        row = conn.execute(
            "SELECT * FROM projects WHERE project_path = ?", (project_path,)
        ).fetchone()

        if row is None:
            # New project: resolve name and insert
            display_name = _resolve_project_name(project_path)
            project_slug = project_path.replace("/", "-")
            conn.execute(
                """\
                INSERT INTO projects (project_path, project_slug, display_name, user_edited, last_updated)
                VALUES (?, ?, ?, 0, ?)
                """,
                (project_path, project_slug, display_name, now.isoformat()),
            )
            conn.commit()
            result["display_name"] = display_name
        else:
            result["project_slug"] = row["project_slug"]
            result["display_name"] = row["display_name"]
            # Re-resolve if not user-edited and last_updated is stale (>24h)
            if not row["user_edited"]:
                try:
                    last_updated = datetime.fromisoformat(row["last_updated"])
                    if last_updated.tzinfo is None:
                        last_updated = last_updated.replace(tzinfo=timezone.utc)
                    age_seconds = (now - last_updated).total_seconds()
                except (ValueError, TypeError):
                    age_seconds = float("inf")
                if age_seconds > 86400:
                    display_name = _resolve_project_name(project_path)
                    conn.execute(
                        """\
                        UPDATE projects SET display_name = ?, last_updated = ?
                        WHERE project_path = ?
                        """,
                        (display_name, now.isoformat(), project_path),
                    )
                    conn.commit()
                    result["display_name"] = display_name
    except Exception:
        pass
    return result


def _update_session_stats(session_id: str) -> None:
    """Parse JSONL incrementally and upsert session_stats row. Never raises."""
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_readwrite()

        # Read existing stats
        row = conn.execute(
            "SELECT * FROM session_stats WHERE session_id = ?", (session_id,)
        ).fetchone()

        start_line = row["last_parsed_line"] if row else 0
        existing_name = row["custom_name"] if row else ""
        existing_input = row["total_input_tokens"] if row else 0
        existing_output = row["total_output_tokens"] if row else 0
        existing_cache_creation = row["total_cache_creation_tokens"] if row else 0
        existing_cache_read = row["total_cache_read_tokens"] if row else 0
        existing_msg_count = row["message_count"] if row else 0
        existing_skills: dict[str, int] = {}
        if row:
            try:
                existing_skills = json.loads(row["skill_usage"] or "{}")
            except (json.JSONDecodeError, KeyError):
                pass

        parsed = _parse_session_jsonl(session_id, start_line=start_line)

        # Determine project: use existing value, or vote from JSONL CWDs
        existing_project = row['project'] if row else ''

        # If no new lines were parsed, skip — unless project is empty (re-vote)
        no_new_data = parsed["last_parsed_line"] <= start_line and not parsed["custom_name"] and not parsed.get("first_user_message")
        if no_new_data and existing_project:
            return
        if existing_project:
            project_path = _strip_task_folder(existing_project)
        else:
            voted = _vote_session_project(session_id)
            project_path = _strip_task_folder(voted) if voted else ''

        # Resolve project name (creates projects table entry if needed)
        project_name = ''
        if project_path:
            project_info = _get_or_create_project(conn, project_path)
            project_name = project_info["display_name"]

        # Merge: accumulate token sums, use latest name if found
        new_name = parsed["custom_name"] or existing_name or parsed.get("first_user_message", "")
        new_input = existing_input + parsed["total_input_tokens"]
        new_output = existing_output + parsed["total_output_tokens"]
        new_cache_creation = existing_cache_creation + parsed["total_cache_creation_tokens"]
        new_cache_read = existing_cache_read + parsed["total_cache_read_tokens"]
        new_msg_count = existing_msg_count + parsed["message_count"]
        new_last_line = parsed["last_parsed_line"]

        # Merge skill usage counts
        merged_skills = dict(existing_skills)
        for skill, count in parsed.get("skills", {}).items():
            merged_skills[skill] = merged_skills.get(skill, 0) + count
        skill_usage_json = json.dumps(merged_skills) if merged_skills else "{}"

        # Compute session segments from message timestamps
        # message_timestamps always covers all lines (even before start_line)
        segments = _compute_segments(
            parsed.get("message_timestamps", []), session_id, conn
        )
        segments_json = json.dumps(segments) if segments else "[]"

        conn.execute(
            """\
            INSERT INTO session_stats
                (session_id, custom_name, total_input_tokens, total_output_tokens,
                 total_cache_creation_tokens, total_cache_read_tokens,
                 message_count, last_parsed_line, project, project_name,
                 skill_usage, segments)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                custom_name = excluded.custom_name,
                total_input_tokens = excluded.total_input_tokens,
                total_output_tokens = excluded.total_output_tokens,
                total_cache_creation_tokens = excluded.total_cache_creation_tokens,
                total_cache_read_tokens = excluded.total_cache_read_tokens,
                message_count = excluded.message_count,
                last_parsed_line = excluded.last_parsed_line,
                project = CASE WHEN session_stats.project != '' THEN session_stats.project ELSE excluded.project END,
                project_name = CASE WHEN session_stats.project_name != '' THEN session_stats.project_name ELSE excluded.project_name END,
                skill_usage = excluded.skill_usage,
                segments = excluded.segments
            """,
            (
                session_id, new_name, new_input, new_output,
                new_cache_creation, new_cache_read, new_msg_count, new_last_line,
                project_path, project_name, skill_usage_json, segments_json,
            ),
        )
        conn.commit()
    except Exception:
        pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# DEPRECATED: Legacy import — will be removed in v3.1 or v3.2
def _import_legacy_dbs() -> None:
    """Import per-project legacy metrics DBs into the global DB. Never raises.

    Scans ~/.claude/projects/ for slug directories, reverse-maps each slug to
    its project path, and imports events/session_stats/agent_token_stats from
    any unmitigated legacy metrics.db found there.
    """
    try:
        projects_dir = _user_config_dir() / 'projects'
        if not projects_dir.is_dir():
            return

        for slug_dir in projects_dir.iterdir():
            if not slug_dir.is_dir():
                continue

            # Verify it's a real project slug dir (has .jsonl files)
            if not any(slug_dir.glob('*.jsonl')):
                continue

            # Reverse-map slug to project path (Claude replaces '/' with '-')
            slug = slug_dir.name
            project_path = slug.replace('-', '/')

            legacy_db = Path(project_path) / '.claude' / 'SuperchargeAI' / 'metrics.db'
            migrated_marker = legacy_db.parent / 'metrics.db.migrated'

            if not legacy_db.is_file():
                continue

            if migrated_marker.exists():
                continue

            # Import this legacy DB into the global DB
            legacy_conn: sqlite3.Connection | None = None
            global_conn: sqlite3.Connection | None = None
            try:
                legacy_conn = sqlite3.connect(f'file:{legacy_db}?mode=ro', uri=True)
                legacy_conn.row_factory = sqlite3.Row
                global_conn = _open_readwrite()

                # Copy events (legacy DB has no project column)
                try:
                    events = legacy_conn.execute(
                        'SELECT timestamp, event_type, session_id, agent_id, agent_type, '
                        'task_uuid, worker_id, parent_id, tool_name, detail FROM events'
                    ).fetchall()
                    global_conn.executemany(
                        'INSERT OR IGNORE INTO events '
                        '(timestamp, event_type, session_id, agent_id, agent_type, '
                        'task_uuid, worker_id, parent_id, tool_name, detail, project) '
                        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                        [(*tuple(row), project_path) for row in events],
                    )
                except Exception:
                    pass

                # Copy session_stats (legacy DB has no project or project_name columns)
                try:
                    stats = legacy_conn.execute(
                        'SELECT session_id, custom_name, total_input_tokens, '
                        'total_output_tokens, total_cache_creation_tokens, '
                        'total_cache_read_tokens, message_count, last_parsed_line '
                        'FROM session_stats'
                    ).fetchall()
                    global_conn.executemany(
                        'INSERT OR IGNORE INTO session_stats '
                        '(session_id, custom_name, total_input_tokens, total_output_tokens, '
                        'total_cache_creation_tokens, total_cache_read_tokens, '
                        'message_count, last_parsed_line, project) '
                        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                        [(*tuple(row), project_path) for row in stats],
                    )
                except Exception:
                    pass

                # Copy agent_token_stats (table may not exist in legacy DB)
                try:
                    agent_stats = legacy_conn.execute(
                        'SELECT agent_id, session_id, agent_type, transcript_path, '
                        'total_input_tokens, total_output_tokens, '
                        'total_cache_creation_tokens, total_cache_read_tokens, '
                        'message_count, last_parsed_line FROM agent_token_stats'
                    ).fetchall()
                    global_conn.executemany(
                        'INSERT OR IGNORE INTO agent_token_stats '
                        '(agent_id, session_id, agent_type, transcript_path, '
                        'total_input_tokens, total_output_tokens, '
                        'total_cache_creation_tokens, total_cache_read_tokens, '
                        'message_count, last_parsed_line) '
                        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                        [tuple(row) for row in agent_stats],
                    )
                except Exception:
                    pass

                global_conn.commit()

                # Mark legacy DB as migrated so we don't import it again
                legacy_db.rename(migrated_marker)
            except Exception:
                pass
            finally:
                if legacy_conn is not None:
                    try:
                        legacy_conn.close()
                    except Exception:
                        pass
                if global_conn is not None:
                    try:
                        global_conn.close()
                    except Exception:
                        pass
    except Exception:
        pass


def _cleanup_projects() -> None:
    """Delete junk project entries and re-vote empty-project sessions.

    Removes task-folder and ``/tmp/`` entries from the ``projects`` table,
    clears those stale project references from ``session_stats``, then
    re-votes any sessions left with an empty ``project``.  Never raises.

    Can also be called manually::

        python -c "from supercharge.metrics import _cleanup_projects; _cleanup_projects()"
    """
    conn: sqlite3.Connection | None = None
    empty_session_ids: list[str] = []
    try:
        conn = _open_readwrite()

        # 1. Delete junk rows from projects table
        conn.execute(
            "DELETE FROM projects"
            " WHERE project_path LIKE '%/.claude/SuperchargeAI/%'"
            "    OR project_path LIKE '/tmp/%'"
        )

        # 2. Clear project/project_name in session_stats for junk paths
        conn.execute(
            """\
            UPDATE session_stats SET project = '', project_name = ''
            WHERE project LIKE '%/.claude/SuperchargeAI/%'
               OR project LIKE '/tmp/%'
            """
        )
        conn.commit()

        # 3. Collect sessions with empty project for re-voting
        rows = conn.execute(
            "SELECT session_id FROM session_stats WHERE project = ''"
        ).fetchall()
        empty_session_ids = [row['session_id'] for row in rows]
    except Exception:
        return
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    # Re-vote each empty-project session
    for session_id in empty_session_ids:
        try:
            voted = _vote_session_project(session_id)
            if not voted:
                continue
            project_path = _strip_task_folder(voted)
            if not project_path or _is_junk_project_path(project_path):
                continue
            rw: sqlite3.Connection | None = None
            try:
                rw = _open_readwrite()
                project_info = _get_or_create_project(rw, project_path)
                rw.execute(
                    """\
                    UPDATE session_stats SET project = ?, project_name = ?
                    WHERE session_id = ? AND project = ''
                    """,
                    (project_path, project_info['display_name'], session_id),
                )
                rw.commit()
            except Exception:
                pass
            finally:
                if rw is not None:
                    try:
                        rw.close()
                    except Exception:
                        pass
        except Exception:
            continue

    # Second pass: for sessions still empty after re-voting, resolve project
    # path from the JSONL file location (slug dir → project path).
    still_empty: list[str] = []
    try:
        conn2: sqlite3.Connection | None = None
        try:
            conn2 = _open_readonly()
            rows2 = conn2.execute(
                "SELECT session_id FROM session_stats WHERE project = ''"
            ).fetchall()
            still_empty = [row['session_id'] for row in rows2]
        except Exception:
            pass
        finally:
            if conn2 is not None:
                try:
                    conn2.close()
                except Exception:
                    pass
    except Exception:
        pass

    for session_id in still_empty:
        try:
            # First try: look up the project from the events table.  This is
            # authoritative and avoids the lossy slug → path reversal below.
            project_path = ''
            conn_ev: sqlite3.Connection | None = None
            try:
                conn_ev = _open_readonly()
                ev_rows = conn_ev.execute(
                    "SELECT DISTINCT project FROM events"
                    " WHERE session_id = ? AND project != ''",
                    (session_id,),
                ).fetchall()
                for ev_row in ev_rows:
                    candidate = ev_row['project']
                    if candidate and not _is_junk_project_path(candidate):
                        project_path = candidate
                        break
            except Exception:
                pass
            finally:
                if conn_ev is not None:
                    try:
                        conn_ev.close()
                    except Exception:
                        pass

            # Second try: reverse the JSONL slug to a directory path, but
            # only accept it when the resulting path exists on disk.  The
            # simple replace('-', '/') is lossy (a slug like
            # '--workspaces-Supercharge-AI' maps to '/workspaces/Supercharge/AI'
            # instead of '/workspaces/Supercharge-AI'), so we gate it behind an
            # is_dir() check to avoid accepting a wrong path.
            if not project_path:
                jsonl_path = _find_session_jsonl(session_id)
                if jsonl_path:
                    slug = jsonl_path.parent.name
                    candidate = slug.replace('-', '/')
                    if (
                        candidate
                        and Path(candidate).is_dir()
                        and not _is_junk_project_path(candidate)
                    ):
                        project_path = candidate

            if not project_path or _is_junk_project_path(project_path):
                continue
            rw2: sqlite3.Connection | None = None
            try:
                rw2 = _open_readwrite()
                project_info = _get_or_create_project(rw2, project_path)
                rw2.execute(
                    """\
                    UPDATE session_stats SET project = ?, project_name = ?
                    WHERE session_id = ? AND project = ''
                    """,
                    (project_path, project_info['display_name'], session_id),
                )
                rw2.commit()
            except Exception:
                pass
            finally:
                if rw2 is not None:
                    try:
                        rw2.close()
                    except Exception:
                        pass
        except Exception:
            continue


def _update_all_session_stats() -> None:
    """Update session_stats for all known sessions. Never raises."""
    session_ids: set[str] = set()

    # 1. Collect session IDs from the events table
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_readonly()
        rows = conn.execute(
            "SELECT DISTINCT session_id FROM events WHERE session_id != ''"
        ).fetchall()
        conn.close()
        conn = None
        session_ids.update(row['session_id'] for row in rows)
    except Exception:
        pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    # 2. Discover sessions from JSONL files across all project slug dirs
    try:
        projects_dir = _user_config_dir() / 'projects'
        if projects_dir.is_dir():
            for slug_dir in projects_dir.iterdir():
                if not slug_dir.is_dir():
                    continue
                try:
                    for jsonl_file in slug_dir.glob('*.jsonl'):
                        sid = jsonl_file.stem
                        if sid:
                            session_ids.add(sid)
                except OSError:
                    continue
    except Exception:
        pass

    # 3. Update stats for each discovered session
    for sid in session_ids:
        _update_session_stats(sid)

    # 4. Update per-agent token stats for each session
    for sid in session_ids:
        _update_agent_token_stats(sid)

    _import_legacy_dbs()

    try:
        _cleanup_projects()
    except Exception:
        pass


def _query_session_stats(session_ids: list[str]) -> dict[str, dict]:
    """Batch fetch session_stats for the given session IDs. Never raises."""
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_readonly()
        if not session_ids:
            return {}

        placeholders = ",".join("?" for _ in session_ids)
        rows = conn.execute(
            f"SELECT * FROM session_stats WHERE session_id IN ({placeholders})",
            session_ids,
        ).fetchall()

        return {
            row["session_id"]: {
                "name": row["custom_name"],
                "input_tokens": row["total_input_tokens"],
                "output_tokens": row["total_output_tokens"],
                "cache_creation_tokens": row["total_cache_creation_tokens"],
                "cache_read_tokens": row["total_cache_read_tokens"],
                "message_count": row["message_count"],
            }
            for row in rows
        }
    except Exception:
        return {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _query_session_segments(session_id: str) -> list[dict]:
    """Return the segments list for a session. Never raises."""
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_readonly()
        row = conn.execute(
            "SELECT segments FROM session_stats WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row and row["segments"]:
            return json.loads(row["segments"])
        return []
    except Exception:
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _query_aggregated_session_tokens(session_ids: list[str]) -> dict[str, dict]:
    """Sum tokens across agent_token_stats and worker_result_stats for sessions.

    Returns ``{session_id: {input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens}}``.
    Combines orchestrator tokens (from session_stats) with agent and worker tokens.
    Never raises.
    """
    conn: sqlite3.Connection | None = None
    try:
        if not session_ids:
            return {}
        conn = _open_readonly()
        placeholders = ",".join("?" for _ in session_ids)

        result: dict[str, dict] = {}

        # Start with session_stats (orchestrator tokens)
        rows = conn.execute(
            f"SELECT session_id, total_input_tokens, total_output_tokens, "
            f"total_cache_creation_tokens, total_cache_read_tokens "
            f"FROM session_stats WHERE session_id IN ({placeholders})",
            session_ids,
        ).fetchall()
        for r in rows:
            result[r["session_id"]] = {
                "input_tokens": r["total_input_tokens"] or 0,
                "output_tokens": r["total_output_tokens"] or 0,
                "cache_creation_tokens": r["total_cache_creation_tokens"] or 0,
                "cache_read_tokens": r["total_cache_read_tokens"] or 0,
            }

        # Add agent tokens (non-orchestrator agents)
        rows = conn.execute(
            f"SELECT session_id, "
            f"SUM(total_input_tokens) as inp, SUM(total_output_tokens) as outp, "
            f"SUM(total_cache_creation_tokens) as cc, SUM(total_cache_read_tokens) as cr "
            f"FROM agent_token_stats WHERE session_id IN ({placeholders}) "
            f"AND agent_type != 'orchestrator' "
            f"GROUP BY session_id",
            session_ids,
        ).fetchall()
        for r in rows:
            sid = r["session_id"]
            if sid not in result:
                result[sid] = {
                    "input_tokens": 0, "output_tokens": 0,
                    "cache_creation_tokens": 0, "cache_read_tokens": 0,
                }
            result[sid]["input_tokens"] += r["inp"] or 0
            result[sid]["output_tokens"] += r["outp"] or 0
            result[sid]["cache_creation_tokens"] += r["cc"] or 0
            result[sid]["cache_read_tokens"] += r["cr"] or 0

        # Add worker tokens
        rows = conn.execute(
            f"SELECT session_id, "
            f"SUM(input_tokens) as inp, SUM(output_tokens) as outp, "
            f"SUM(cache_creation_tokens) as cc, SUM(cache_read_tokens) as cr "
            f"FROM worker_result_stats WHERE session_id IN ({placeholders}) "
            f"GROUP BY session_id",
            session_ids,
        ).fetchall()
        for r in rows:
            sid = r["session_id"]
            if sid not in result:
                result[sid] = {
                    "input_tokens": 0, "output_tokens": 0,
                    "cache_creation_tokens": 0, "cache_read_tokens": 0,
                }
            result[sid]["input_tokens"] += r["inp"] or 0
            result[sid]["output_tokens"] += r["outp"] or 0
            result[sid]["cache_creation_tokens"] += r["cc"] or 0
            result[sid]["cache_read_tokens"] += r["cr"] or 0

        return result
    except Exception:
        return {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _rename_session(session_id: str, name: str) -> None:
    """Update session name in DB and optionally append to JSONL. Never raises."""
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_readwrite()

        conn.execute(
            """\
            INSERT INTO session_stats (session_id, custom_name)
            VALUES (?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                custom_name = excluded.custom_name
            """,
            (session_id, name),
        )
        conn.commit()
    except Exception:
        pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    # Append custom-title entry to JSONL (best-effort)
    try:
        jsonl_path = _find_session_jsonl(session_id)
        if jsonl_path is not None:
            entry = {
                "type": "custom-title",
                "customTitle": name,
                "sessionId": session_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            with jsonl_path.open("a") as f:
                f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


# ── Per-agent token stats ────────────────────────────────────────────────────


def _parse_agent_transcript(transcript_path: str, start_line: int = 0) -> dict:
    """Parse an agent's JSONL transcript for token usage. Same format as session JSONL."""
    result = {
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cache_creation_tokens": 0,
        "total_cache_read_tokens": 0,
        "message_count": 0,
        "last_parsed_line": start_line,
        "skills": {},
    }

    path = Path(transcript_path)
    if not path.is_file():
        return result

    try:
        total_lines = 0
        with path.open() as f:
            for line_num, line in enumerate(f):
                total_lines = line_num + 1
                if line_num < start_line:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue

                entry_type = entry.get("type", "")
                if entry_type == "assistant":
                    message = entry.get("message", {})
                    if isinstance(message, dict):
                        usage = message.get("usage")
                        if usage and isinstance(usage, dict):
                            result["total_input_tokens"] += usage.get("input_tokens", 0)
                            result["total_output_tokens"] += usage.get("output_tokens", 0)
                            result["total_cache_creation_tokens"] += usage.get(
                                "cache_creation_input_tokens", 0
                            )
                            result["total_cache_read_tokens"] += usage.get(
                                "cache_read_input_tokens", 0
                            )
                            result["message_count"] += 1

                        # Detect skill usage in content blocks
                        content = message.get("content", [])
                        if isinstance(content, list):
                            for block in content:
                                if (
                                    isinstance(block, dict)
                                    and block.get("type") == "tool_use"
                                    and block.get("name") == "Skill"
                                ):
                                    inp = block.get("input", {})
                                    cmd = inp.get("command", "") if isinstance(inp, dict) else ""
                                    if cmd:
                                        skill_name = cmd.split()[0]
                                        result["skills"][skill_name] = result["skills"].get(skill_name, 0) + 1

        result["last_parsed_line"] = total_lines
    except Exception:
        pass
    return result


def _update_agent_token_stats(session_id: str) -> None:
    """Lazily parse agent transcripts for a session and cache token stats.

    Finds all subagent_stop events with transcript paths, parses each
    transcript incrementally, and upserts into agent_token_stats.
    """
    conn: sqlite3.Connection | None = None
    try:
        db = _db_path()
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        _init_db(conn)

        # Find subagent_stop events with transcript paths
        stops = conn.execute(
            "SELECT agent_id, agent_type, detail FROM events "
            "WHERE session_id = ? AND event_type = 'subagent_stop' AND detail != ''",
            (session_id,),
        ).fetchall()

        for stop in stops:
            agent_id = stop["agent_id"]
            agent_type = stop["agent_type"]
            transcript_path = stop["detail"]
            if not agent_id or not transcript_path:
                continue

            # Check existing stats
            existing = conn.execute(
                "SELECT last_parsed_line, total_input_tokens, total_output_tokens, "
                "total_cache_creation_tokens, total_cache_read_tokens, message_count "
                "FROM agent_token_stats WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()

            start_line = existing["last_parsed_line"] if existing else 0
            ex_input = existing["total_input_tokens"] if existing else 0
            ex_output = existing["total_output_tokens"] if existing else 0
            ex_cache_c = existing["total_cache_creation_tokens"] if existing else 0
            ex_cache_r = existing["total_cache_read_tokens"] if existing else 0
            ex_msgs = existing["message_count"] if existing else 0

            parsed = _parse_agent_transcript(transcript_path, start_line)
            if parsed["last_parsed_line"] <= start_line:
                continue

            conn.execute(
                """\
                INSERT INTO agent_token_stats
                    (agent_id, session_id, agent_type, transcript_path,
                     total_input_tokens, total_output_tokens,
                     total_cache_creation_tokens, total_cache_read_tokens,
                     message_count, last_parsed_line)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    total_input_tokens = excluded.total_input_tokens,
                    total_output_tokens = excluded.total_output_tokens,
                    total_cache_creation_tokens = excluded.total_cache_creation_tokens,
                    total_cache_read_tokens = excluded.total_cache_read_tokens,
                    message_count = excluded.message_count,
                    last_parsed_line = excluded.last_parsed_line
                """,
                (
                    agent_id, session_id, agent_type, transcript_path,
                    ex_input + parsed["total_input_tokens"],
                    ex_output + parsed["total_output_tokens"],
                    ex_cache_c + parsed["total_cache_creation_tokens"],
                    ex_cache_r + parsed["total_cache_read_tokens"],
                    ex_msgs + parsed["message_count"],
                    parsed["last_parsed_line"],
                ),
            )

        conn.commit()
    except Exception:
        pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _query_agent_tokens(session_id: str) -> dict[str, dict]:
    """Return per-agent token stats for a session. Lazily parses transcripts first.

    Returns: {agent_id: {input_tokens, output_tokens, cache_creation, cache_read, total}}
    """
    _update_agent_token_stats(session_id)

    conn: sqlite3.Connection | None = None
    try:
        conn = _open_readonly()
        rows = conn.execute(
            "SELECT agent_id, agent_type, total_input_tokens, total_output_tokens, "
            "total_cache_creation_tokens, total_cache_read_tokens, message_count "
            "FROM agent_token_stats WHERE session_id = ?",
            (session_id,),
        ).fetchall()

        result = {}
        for r in rows:
            total = (r["total_input_tokens"] + r["total_output_tokens"]
                     + r["total_cache_creation_tokens"] + r["total_cache_read_tokens"])
            result[r["agent_id"]] = {
                "agent_type": r["agent_type"],
                "input_tokens": r["total_input_tokens"],
                "output_tokens": r["total_output_tokens"],
                "cache_creation": r["total_cache_creation_tokens"],
                "cache_read": r["total_cache_read_tokens"],
                "total": total,
                "messages": r["message_count"],
            }
        return result
    except Exception:
        return {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ── Global tool stats ────────────────────────────────────────────────────────


def _build_agent_time_ranges(
    conn: sqlite3.Connection,
) -> dict[str, list[tuple[str, str, str]]]:
    """Build per-session list of agent active time ranges.

    CLI PreToolUse hooks don't include agent_type. All tool_use events
    (orchestrator + agents) share the orchestrator's session_id.  We pair
    subagent_start / subagent_stop timestamps so callers can resolve which
    agent was active when a tool_use event fired.

    Returns ``{session_id: [(start_ts, stop_ts, agent_type), ...]}``.
    """
    agent_ranges: dict[str, list[tuple[str, str, str]]] = {}
    range_rows = conn.execute(
        "SELECT session_id, agent_id, agent_type, timestamp, event_type "
        "FROM events "
        "WHERE event_type IN ('subagent_start', 'subagent_stop') "
        "ORDER BY timestamp"
    ).fetchall()
    starts: dict[str, tuple[str, str, str]] = {}  # agent_id -> (ts, type, sid)
    for rr in range_rows:
        aid = rr["agent_id"]
        if rr["event_type"] == "subagent_start":
            starts[aid] = (rr["timestamp"], _normalize_agent_type(rr["agent_type"]), rr["session_id"])
        elif rr["event_type"] == "subagent_stop" and aid in starts:
            start_ts, atype, sid = starts.pop(aid)
            agent_ranges.setdefault(sid, []).append((start_ts, rr["timestamp"], atype))
    # Still-running agents (no stop yet)
    for _aid, (start_ts, atype, sid) in starts.items():
        agent_ranges.setdefault(sid, []).append((start_ts, "9999-12-31", atype))
    return agent_ranges


def _resolve_tool_agent_type(
    session_id: str,
    timestamp: str,
    agent_ranges: dict[str, list[tuple[str, str, str]]],
) -> str:
    """Find which agent was active at *timestamp*, or ``'orchestrator'``."""
    ranges = agent_ranges.get(session_id)
    if not ranges:
        return "orchestrator"
    for start, stop, atype in ranges:
        if start <= timestamp <= stop:
            return atype
    return "orchestrator"


def _aggregate_tool_rows(
    rows: list,
    agent_ranges: dict[str, list[tuple[str, str, str]]],
) -> dict:
    """Aggregate tool_use rows into ``{agent_types: ..., totals: ...}``."""
    agent_types: dict[str, dict[str, int]] = {}
    totals: dict[str, int] = {}
    supercharge_count = 0

    for row in rows:
        atype = _normalize_agent_type(row["agent_type"]) if row["agent_type"] else ""
        if not atype:
            atype = _resolve_tool_agent_type(row["session_id"] or "", row["timestamp"] or "", agent_ranges)
        tool = row["tool_name"]

        agent_types.setdefault(atype, {})
        agent_types[atype][tool] = agent_types[atype].get(tool, 0) + 1
        totals[tool] = totals.get(tool, 0) + 1

        if tool == "Bash":
            try:
                detail = json.loads(row["detail"]) if row["detail"] else {}
                command = detail.get("command", "")
                if "supercharge" in command:
                    supercharge_count += 1
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass

    if supercharge_count > 0:
        totals["supercharge"] = supercharge_count

    return {"agent_types": agent_types, "totals": totals}


def _query_global_tool_stats() -> dict:
    """Return tool usage grouped by agent_type and tool_name.

    Returns ``{agent_types: {code: {Bash: 50, ...}, ...}, totals: {Bash: 80, ...}}``.
    """
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_readonly()

        agent_ranges = _build_agent_time_ranges(conn)

        rows = conn.execute(
            "SELECT agent_type, session_id, timestamp, tool_name, detail "
            "FROM events "
            "WHERE event_type = 'tool_use' AND tool_name != ''"
        ).fetchall()

        return _aggregate_tool_rows(rows, agent_ranges)
    except Exception:
        return {"agent_types": {}, "totals": {}}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ── Project-aware query functions ────────────────────────────────────────────


def _query_projects() -> list[dict]:
    """Return all projects with aggregated stats. Never raises (returns [] on error).

    Joins the ``projects`` table with aggregated ``session_stats`` (tokens,
    session count) and ``events`` (event count, tool calls, timestamps).

    Each dict contains: ``project_path``, ``project_slug``, ``display_name``,
    ``session_count``, ``total_input_tokens``, ``total_output_tokens``,
    ``total_cache_read_tokens``, ``total_events``, ``total_tool_calls``,
    ``first_timestamp``, ``last_timestamp``.
    """
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_readonly()

        rows = conn.execute(
            """\
            SELECT
                p.project_path, p.project_slug, p.display_name,
                COALESCE(ss.session_count, 0) as session_count,
                COALESCE(ss.total_input_tokens, 0) as total_input_tokens,
                COALESCE(ss.total_output_tokens, 0) as total_output_tokens,
                COALESCE(ss.total_cache_read_tokens, 0) as total_cache_read_tokens,
                COALESCE(ev.total_events, 0) as total_events,
                COALESCE(ev.total_tool_calls, 0) as total_tool_calls,
                ev.first_timestamp, ev.last_timestamp
            FROM projects p
            LEFT JOIN (
                SELECT project,
                       COUNT(DISTINCT session_id) as session_count,
                       SUM(total_input_tokens) as total_input_tokens,
                       SUM(total_output_tokens) as total_output_tokens,
                       SUM(total_cache_read_tokens) as total_cache_read_tokens
                FROM session_stats
                WHERE project != ''
                GROUP BY project
            ) ss ON p.project_path = ss.project
            LEFT JOIN (
                SELECT project,
                       COUNT(*) as total_events,
                       SUM(CASE WHEN event_type = 'tool_use' THEN 1 ELSE 0 END) as total_tool_calls,
                       MIN(timestamp) as first_timestamp,
                       MAX(timestamp) as last_timestamp
                FROM events
                WHERE project != ''
                GROUP BY project
            ) ev ON p.project_path = ev.project
            WHERE p.project_path NOT LIKE '%/.claude/SuperchargeAI/%'
              AND p.project_path NOT LIKE '/tmp/%'
            ORDER BY ev.last_timestamp DESC
            """
        ).fetchall()

        return [
            {
                "project_path": row["project_path"],
                "project_slug": row["project_slug"],
                "display_name": row["display_name"],
                "session_count": row["session_count"],
                "total_input_tokens": row["total_input_tokens"],
                "total_output_tokens": row["total_output_tokens"],
                "total_cache_read_tokens": row["total_cache_read_tokens"],
                "total_events": row["total_events"],
                "total_tool_calls": row["total_tool_calls"],
                "first_timestamp": row["first_timestamp"],
                "last_timestamp": row["last_timestamp"],
            }
            for row in rows
        ]
    except Exception:
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _query_project_sessions_by_path(project_path: str) -> list[dict]:
    """Return sessions for a specific project with aggregate stats.

    Filtered version of :func:`_query_sessions` for the given *project_path*.
    Adds ``project`` and ``project_name`` keys to each session dict.
    Never raises (returns [] on error).
    """
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_readonly()

        rows = conn.execute(
            """\
            SELECT
                session_id,
                MIN(timestamp) AS first_timestamp,
                MAX(timestamp) AS last_timestamp,
                COUNT(*) AS event_count,
                COUNT(DISTINCT CASE WHEN agent_id != '' THEN agent_id END) AS agent_count,
                COUNT(DISTINCT CASE WHEN worker_id != '' THEN worker_id END) AS worker_count,
                SUM(CASE WHEN event_type = 'tool_use' THEN 1 ELSE 0 END) AS tool_call_count,
                GROUP_CONCAT(DISTINCT CASE WHEN agent_type != '' THEN agent_type END) AS agent_types_csv,
                (SELECT GROUP_CONCAT(at, ',') FROM (
                    SELECT agent_type AS at
                    FROM events e2
                    WHERE e2.session_id = events.session_id
                      AND e2.agent_type != ''
                    GROUP BY e2.agent_type
                    ORDER BY MIN(e2.timestamp)
                )) AS agent_types_ordered
            FROM events
            WHERE session_id != '' AND project = ?
            GROUP BY session_id
            HAVING COUNT(*) > 1
              AND (COUNT(DISTINCT CASE WHEN agent_id != '' THEN agent_id END) > 0
                   OR COUNT(DISTINCT CASE WHEN worker_id != '' THEN worker_id END) > 0
                   OR SUM(CASE WHEN event_type = 'tool_use' THEN 1 ELSE 0 END) > 0)
            ORDER BY first_timestamp DESC
            """,
            (project_path,),
        ).fetchall()

        results: list[dict] = []
        for row in rows:
            first_ts = row["first_timestamp"]
            last_ts = row["last_timestamp"]
            try:
                t0 = datetime.fromisoformat(first_ts)
                t1 = datetime.fromisoformat(last_ts)
                duration = (t1 - t0).total_seconds()
            except Exception:
                duration = 0.0

            agent_types_csv = row["agent_types_ordered"] or row["agent_types_csv"] or ""
            agent_types_raw = [a for a in agent_types_csv.split(",") if a]
            # Normalize and deduplicate, preserving temporal order
            seen: set[str] = set()
            agent_types: list[str] = []
            for a in agent_types_raw:
                norm = _normalize_agent_type(a)
                if norm not in seen:
                    seen.add(norm)
                    agent_types.append(norm)

            results.append(
                {
                    "session_id": row["session_id"],
                    "first_timestamp": first_ts,
                    "last_timestamp": last_ts,
                    "duration_seconds": duration,
                    "event_count": row["event_count"],
                    "agent_count": row["agent_count"],
                    "worker_count": row["worker_count"],
                    "tool_call_count": row["tool_call_count"],
                    "agent_types": agent_types,
                }
            )

        # Enrich results with project and project_name from session_stats
        if results:
            try:
                sids = [r["session_id"] for r in results]
                placeholders = ",".join("?" for _ in sids)
                proj_rows = conn.execute(
                    f"SELECT session_id, project, project_name FROM session_stats "
                    f"WHERE session_id IN ({placeholders})",
                    sids,
                ).fetchall()
                proj_map: dict[str, tuple[str, str]] = {
                    r["session_id"]: (r["project"] or "", r["project_name"] or "")
                    for r in proj_rows
                }
            except Exception:
                proj_map = {}
            for result in results:
                proj, proj_name = proj_map.get(result["session_id"], ("", ""))
                result["project"] = proj
                result["project_name"] = proj_name

        return results
    except Exception:
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _query_project_tool_stats(project_path: str) -> dict:
    """Return tool usage for a project grouped by agent_type and tool_name.

    Filtered version of :func:`_query_global_tool_stats` for *project_path*.
    Returns ``{agent_types: {code: {Bash: 50, ...}, ...}, totals: {Bash: 80, ...}}``.
    Never raises (returns ``{"agent_types": {}, "totals": {}}`` on error).
    """
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_readonly()

        agent_ranges = _build_agent_time_ranges(conn)

        rows = conn.execute(
            "SELECT agent_type, session_id, timestamp, tool_name, detail "
            "FROM events "
            "WHERE event_type = 'tool_use' AND tool_name != '' AND project = ?",
            (project_path,),
        ).fetchall()

        return _aggregate_tool_rows(rows, agent_ranges)
    except Exception:
        return {"agent_types": {}, "totals": {}}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _query_project_token_stats(project_path: str) -> list[dict]:
    """Return token usage by agent type for a project. Never raises (returns [] on error).

    Aggregates ``agent_token_stats`` for all sessions belonging to *project_path*,
    grouped by ``agent_type``.

    Each dict contains: ``agent_type``, ``input_tokens``, ``output_tokens``,
    ``cache_creation_tokens``, ``cache_read_tokens``, ``agent_count``.
    """
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_readonly()

        rows = conn.execute(
            """\
            SELECT ats.agent_type,
                   SUM(ats.total_input_tokens) as input_tokens,
                   SUM(ats.total_output_tokens) as output_tokens,
                   SUM(ats.total_cache_creation_tokens) as cache_creation_tokens,
                   SUM(ats.total_cache_read_tokens) as cache_read_tokens,
                   COUNT(*) as agent_count
            FROM agent_token_stats ats
            INNER JOIN session_stats ss ON ats.session_id = ss.session_id
            WHERE ss.project = ?
            GROUP BY ats.agent_type
            ORDER BY (SUM(ats.total_input_tokens) + SUM(ats.total_output_tokens)) DESC
            """,
            (project_path,),
        ).fetchall()

        return [
            {
                "agent_type": _normalize_agent_type(row["agent_type"]),
                "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"],
                "cache_creation_tokens": row["cache_creation_tokens"],
                "cache_read_tokens": row["cache_read_tokens"],
                "agent_count": row["agent_count"],
            }
            for row in rows
        ]
    except Exception:
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ── Slug-based project query functions ───────────────────────────────────────


def _query_project_sessions(project_slug: str) -> list[dict]:
    """Return sessions for a project identified by slug, enriched with stats.

    Resolves *project_slug* to a ``project_path`` via the ``projects`` table,
    then returns session data filtered to that project (same shape as
    :func:`_query_sessions`).  Enriches results with
    :func:`_query_session_stats` to add ``name``, ``input_tokens``,
    ``output_tokens``, ``cache_creation_tokens``, and ``cache_read_tokens``.
    Never raises (returns [] on error).
    """
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_readonly()
        row = conn.execute(
            "SELECT project_path FROM projects WHERE project_slug = ?",
            (project_slug,),
        ).fetchone()
        if row is None:
            return []
        project_path = row["project_path"]
    except Exception:
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    results = _query_project_sessions_by_path(project_path)

    # Enrich with session stats (name, tokens)
    if results:
        session_ids = [s["session_id"] for s in results]
        stats = _query_session_stats(session_ids)
        for session in results:
            sid = session["session_id"]
            ss = stats.get(sid, {})
            session["name"] = ss.get("name", "")
            session["input_tokens"] = ss.get("input_tokens", 0)
            session["output_tokens"] = ss.get("output_tokens", 0)
            session["cache_creation_tokens"] = ss.get("cache_creation_tokens", 0)
            session["cache_read_tokens"] = ss.get("cache_read_tokens", 0)

    return results


def _query_project_tokens(project_slug: str) -> list[dict]:
    """Return token usage by agent type for a project identified by slug.

    Resolves *project_slug* to ``project_path``, then aggregates
    ``agent_token_stats`` for all sessions whose events have
    ``project = project_path``, grouped by ``agent_type``.

    Each dict contains: ``agent_type``, ``total_input_tokens``,
    ``total_output_tokens``, ``total_cache_creation_tokens``,
    ``total_cache_read_tokens``.
    Never raises (returns [] on error).
    """
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_readonly()

        row = conn.execute(
            "SELECT project_path FROM projects WHERE project_slug = ?",
            (project_slug,),
        ).fetchone()
        if row is None:
            return []
        project_path = row["project_path"]

        rows = conn.execute(
            """\
            SELECT ats.agent_type,
                   SUM(ats.total_input_tokens) AS total_input_tokens,
                   SUM(ats.total_output_tokens) AS total_output_tokens,
                   SUM(ats.total_cache_creation_tokens) AS total_cache_creation_tokens,
                   SUM(ats.total_cache_read_tokens) AS total_cache_read_tokens
            FROM agent_token_stats ats
            WHERE ats.session_id IN (
                SELECT DISTINCT session_id FROM events WHERE project = ?
            )
            GROUP BY ats.agent_type
            ORDER BY (SUM(ats.total_input_tokens) + SUM(ats.total_output_tokens)) DESC
            """,
            (project_path,),
        ).fetchall()

        return [
            {
                "agent_type": _normalize_agent_type(row["agent_type"]) if row["agent_type"] else "orchestrator",
                "total_input_tokens": row["total_input_tokens"] or 0,
                "total_output_tokens": row["total_output_tokens"] or 0,
                "total_cache_creation_tokens": row["total_cache_creation_tokens"] or 0,
                "total_cache_read_tokens": row["total_cache_read_tokens"] or 0,
            }
            for row in rows
        ]
    except Exception:
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _query_project_tools(project_slug: str) -> dict:
    """Return tool usage for a project identified by slug.

    Resolves *project_slug* to ``project_path``, then delegates to
    :func:`_query_project_tool_stats`.

    Returns ``{agent_types: {code: {Bash: N, ...}, ...}, totals: {Bash: N, ...}}``.
    Never raises (returns ``{"agent_types": {}, "totals": {}}`` on error).
    """
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_readonly()
        row = conn.execute(
            "SELECT project_path FROM projects WHERE project_slug = ?",
            (project_slug,),
        ).fetchone()
        if row is None:
            return {"agent_types": {}, "totals": {}}
        project_path = row["project_path"]
    except Exception:
        return {"agent_types": {}, "totals": {}}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    return _query_project_tool_stats(project_path)


def _get_project_by_slug(project_slug: str) -> dict | None:
    """Return a project row dict for the given slug, or None if not found. Never raises."""
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_readonly()
        row = conn.execute(
            "SELECT project_path, project_slug, display_name, user_edited, last_updated "
            "FROM projects WHERE project_slug = ?",
            (project_slug,),
        ).fetchone()
        if row is None:
            return None
        return dict(row)
    except Exception:
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _rename_project(project_slug: str, name: str) -> bool:
    """Update the display name for a project identified by slug.

    Sets ``display_name``, ``user_edited = 1``, and ``last_updated`` in the
    ``projects`` table.  Returns ``True`` if a row was updated, ``False``
    if the slug was not found or an error occurred.
    Never raises.
    """
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_readwrite()
        now = datetime.now(timezone.utc).isoformat()
        cursor = conn.execute(
            "UPDATE projects SET display_name = ?, user_edited = 1, last_updated = ? "
            "WHERE project_slug = ?",
            (name, now, project_slug),
        )
        conn.commit()
        return cursor.rowcount > 0
    except Exception:
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _query_worker_stats(worker_ids: list[str]) -> dict[str, dict]:
    """Batch fetch worker_result_stats for the given worker IDs.

    Returns a dict mapping worker_id to its stats (tokens, duration, cost, etc.).
    Never raises.
    """
    conn: sqlite3.Connection | None = None
    try:
        if not worker_ids:
            return {}
        conn = _open_readonly()
        placeholders = ",".join("?" for _ in worker_ids)
        rows = conn.execute(
            f"SELECT * FROM worker_result_stats WHERE worker_id IN ({placeholders})",
            worker_ids,
        ).fetchall()

        result: dict[str, dict] = {}
        for row in rows:
            result[row["worker_id"]] = {
                "session_id": row["session_id"],
                "agent_type": row["agent_type"],
                "task_uuid": row["task_uuid"],
                "duration_ms": row["duration_ms"],
                "duration_api_ms": row["duration_api_ms"],
                "num_turns": row["num_turns"],
                "cost_usd": row["cost_usd"],
                "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"],
                "cache_creation_tokens": row["cache_creation_tokens"],
                "cache_read_tokens": row["cache_read_tokens"],
                "is_error": bool(row["is_error"]),
            }
        return result
    except Exception:
        return {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _query_session_skills(session_id: str) -> dict[str, int]:
    """Read skill_usage from session_stats for a session.

    Returns a dict like ``{"pdf": 3, "xlsx": 1}`` or empty dict.
    Never raises.
    """
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_readonly()
        row = conn.execute(
            "SELECT skill_usage FROM session_stats WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row and row["skill_usage"]:
            return json.loads(row["skill_usage"])
        return {}
    except Exception:
        return {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _query_memory_status() -> list[dict]:
    """Query recent memory_spawn / memory_end event pairs.

    Returns a list of dicts with status, task_uuid, duration, and timestamps.
    Pairs events by task_uuid (not session_id) since memory agents emit
    task_uuid only.  Never raises.
    """
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_readonly()
        rows = conn.execute(
            "SELECT id, timestamp, event_type, task_uuid, detail "
            "FROM events "
            "WHERE event_type IN ('memory_spawn', 'memory_end') "
            "ORDER BY id DESC LIMIT 50",
        ).fetchall()

        # Pair memory_end -> memory_spawn by task_uuid
        ends: dict[str, dict] = {}
        result: list[dict] = []
        seen_tasks: set[str] = set()

        for row in rows:
            tuuid = row["task_uuid"] or ""
            etype = row["event_type"]
            ts = row["timestamp"]

            if etype == "memory_end":
                if tuuid not in ends:
                    ends[tuuid] = {"timestamp": ts, "detail": row["detail"] or ""}
            elif etype == "memory_spawn":
                if tuuid in seen_tasks:
                    continue
                seen_tasks.add(tuuid)
                end_info = ends.get(tuuid)
                entry: dict = {
                    "task_uuid": tuuid,
                    "start": ts,
                    "end": end_info["timestamp"] if end_info else None,
                    "status": "completed" if end_info else "running",
                }
                if end_info:
                    try:
                        start_dt = datetime.fromisoformat(ts)
                        end_dt = datetime.fromisoformat(end_info["timestamp"])
                        entry["duration_ms"] = int(
                            (end_dt - start_dt).total_seconds() * 1000
                        )
                    except Exception:
                        entry["duration_ms"] = None
                    detail = end_info.get("detail", "")
                    if detail and "error" in detail.lower():
                        entry["status"] = "failed"
                else:
                    entry["duration_ms"] = None
                result.append(entry)
                if len(result) >= 10:
                    break

        return result
    except Exception:
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
