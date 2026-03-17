"""Metrics collection module — SQLite-backed event store for SuperchargeAI.

Fire-and-forget event emitter for tracking session activity, task lifecycle,
and tool usage. Each call opens a new connection (hooks run as separate
subprocesses with no shared state). Uses WAL mode for concurrent access.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from supercharge.paths import _project_dir, _user_config_dir


def _normalize_agent_type(raw: str) -> str:
    """Strip 'supercharge-ai:' prefix from agent type names."""
    if raw.startswith("supercharge-ai:"):
        return raw[len("supercharge-ai:"):]
    return raw


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
    except Exception as e:
        print(f"supercharge: _emit failed: {type(e).__name__}: {e}", file=sys.stderr)
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


# ── JSONL parser ─────────────────────────────────────────────────────────────


def _find_session_jsonl(session_id: str) -> Path | None:
    """Locate the JSONL transcript file for a session.

    Returns the Path if found, None otherwise. The JSONL lives at:
    ``_user_config_dir() / "projects" / project_slug / f"{session_id}.jsonl"``
    where project_slug encodes the project directory by replacing ``/`` with ``-``
    (matches the pattern in hooks.py ``_ensure_project_dir``).
    """
    project_slug = _project_dir().replace("/", "-")
    jsonl_path = _user_config_dir() / "projects" / project_slug / f"{session_id}.jsonl"
    if jsonl_path.is_file():
        return jsonl_path
    return None


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
    }

    jsonl_path = _find_session_jsonl(session_id)
    if jsonl_path is None:
        return result

    try:
        total_lines = 0
        with jsonl_path.open() as f:
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
                    usage = message.get("usage") if isinstance(message, dict) else None
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

        result["last_parsed_line"] = total_lines
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

        parsed = _parse_session_jsonl(session_id, start_line=start_line)

        # If no new lines were parsed, skip the upsert
        if parsed["last_parsed_line"] <= start_line and not parsed["custom_name"] and not parsed.get("first_user_message"):
            return

        # Merge: accumulate token sums, use latest name if found
        new_name = parsed["custom_name"] or existing_name or parsed.get("first_user_message", "")
        new_input = existing_input + parsed["total_input_tokens"]
        new_output = existing_output + parsed["total_output_tokens"]
        new_cache_creation = existing_cache_creation + parsed["total_cache_creation_tokens"]
        new_cache_read = existing_cache_read + parsed["total_cache_read_tokens"]
        new_msg_count = existing_msg_count + parsed["message_count"]
        new_last_line = parsed["last_parsed_line"]

        conn.execute(
            """\
            INSERT INTO session_stats
                (session_id, custom_name, total_input_tokens, total_output_tokens,
                 total_cache_creation_tokens, total_cache_read_tokens,
                 message_count, last_parsed_line)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                custom_name = excluded.custom_name,
                total_input_tokens = excluded.total_input_tokens,
                total_output_tokens = excluded.total_output_tokens,
                total_cache_creation_tokens = excluded.total_cache_creation_tokens,
                total_cache_read_tokens = excluded.total_cache_read_tokens,
                message_count = excluded.message_count,
                last_parsed_line = excluded.last_parsed_line
            """,
            (
                session_id, new_name, new_input, new_output,
                new_cache_creation, new_cache_read, new_msg_count, new_last_line,
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


def _update_all_session_stats() -> None:
    """Update session_stats for all known sessions. Never raises."""
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_readonly()
        rows = conn.execute(
            "SELECT DISTINCT session_id FROM events WHERE session_id != ''"
        ).fetchall()
        conn.close()
        conn = None

        for row in rows:
            _update_session_stats(row["session_id"])
    except Exception:
        pass
    finally:
        if conn is not None:
            try:
                conn.close()
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
                    usage = message.get("usage") if isinstance(message, dict) else None
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


def _query_global_tool_stats() -> dict:
    """Return tool usage grouped by agent_type and tool_name.

    Returns ``{agent_types: {code: {Bash: 50, ...}, ...}, totals: {Bash: 80, ...}}``.
    Bash events whose ``detail`` contains a ``"command"`` with "supercharge" are
    counted under a separate ``supercharge`` key in totals.
    """
    conn: sqlite3.Connection | None = None
    try:
        conn = _open_readonly()

        rows = conn.execute(
            "SELECT agent_type, tool_name, detail, COUNT(*) as count "
            "FROM events "
            "WHERE event_type = 'tool_use' AND tool_name != '' "
            "GROUP BY agent_type, tool_name, detail"
        ).fetchall()

        agent_types: dict[str, dict[str, int]] = {}
        totals: dict[str, int] = {}
        supercharge_count = 0

        for row in rows:
            atype = _normalize_agent_type(row["agent_type"]) if row["agent_type"] else "unknown"
            tool = row["tool_name"]
            count = row["count"]

            agent_types.setdefault(atype, {})
            agent_types[atype][tool] = agent_types[atype].get(tool, 0) + count
            totals[tool] = totals.get(tool, 0) + count

            # Detect supercharge bash calls
            if tool == "Bash":
                try:
                    detail = json.loads(row["detail"]) if row["detail"] else {}
                    command = detail.get("command", "")
                    if "supercharge" in command:
                        supercharge_count += count
                except (json.JSONDecodeError, TypeError, AttributeError):
                    pass

        if supercharge_count > 0:
            totals["supercharge"] = supercharge_count

        return {"agent_types": agent_types, "totals": totals}
    except Exception:
        return {"agent_types": {}, "totals": {}}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
