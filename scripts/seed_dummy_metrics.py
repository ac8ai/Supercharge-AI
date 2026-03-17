#!/usr/bin/env python3
"""Seed the metrics DB with dummy traces for dashboard testing.

All dummy events have detail='__dummy__' for easy cleanup:
    DELETE FROM events WHERE detail = '__dummy__';
    DELETE FROM session_stats WHERE session_id LIKE 'dummy-%';

Usage:
    python scripts/seed_dummy_metrics.py
"""

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from supercharge.metrics import _db_path, _init_db

DUMMY = "__dummy__"


def _ts(base: datetime, offset_sec: float) -> str:
    return (base + timedelta(seconds=offset_sec)).isoformat()


def _insert(conn, ts, event_type, **kw):
    conn.execute(
        "INSERT INTO events (timestamp, event_type, "
        "session_id, agent_id, agent_type, task_uuid, "
        "worker_id, parent_id, tool_name, detail) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ts, event_type,
            kw.get("session_id", ""),
            kw.get("agent_id", ""),
            kw.get("agent_type", ""),
            kw.get("task_uuid", ""),
            kw.get("worker_id", ""),
            kw.get("parent_id", ""),
            kw.get("tool_name", ""),
            DUMMY,
        ),
    )


def seed_session_1(conn):
    """Complex session: plan -> 2 parallel code agents -> review -> memory.
    One code agent gets resumed. Workers involved."""
    sid = "dummy-complex-" + uuid.uuid4().hex[:8]
    t0 = datetime(2026, 3, 16, 20, 0, 0, tzinfo=timezone.utc)
    parent = f"orchestrator:{sid}"

    plan_aid = uuid.uuid4().hex[:12]
    code1_aid = uuid.uuid4().hex[:12]
    code2_aid = uuid.uuid4().hex[:12]
    review_aid = uuid.uuid4().hex[:12]
    memory_aid = uuid.uuid4().hex[:12]

    plan_tid = str(uuid.uuid4())
    code1_tid = str(uuid.uuid4())
    code2_tid = str(uuid.uuid4())
    review_tid = str(uuid.uuid4())
    memory_tid = str(uuid.uuid4())

    worker1_id = uuid.uuid4().hex[:12]
    worker2_id = uuid.uuid4().hex[:12]

    # Session start
    _insert(conn, _ts(t0, 0), "session_start", session_id=sid)

    # Plan agent: 0s -> 45s
    _insert(conn, _ts(t0, 2), "subagent_start", session_id=sid, agent_id=plan_aid, agent_type="plan", parent_id=parent)
    _insert(conn, _ts(t0, 3), "task_init", session_id=sid, agent_type="plan", task_uuid=plan_tid, parent_id=parent)
    for t, tool in [(5, "Read"), (8, "Read"), (12, "Grep"), (18, "Read"), (25, "Write"), (30, "Read"), (35, "Glob")]:
        _insert(conn, _ts(t0, t), "tool_use", session_id=sid, agent_id=plan_aid, tool_name=tool, task_uuid=plan_tid)
    _insert(conn, _ts(t0, 45), "subagent_stop", session_id=sid, agent_id=plan_aid, agent_type="plan", parent_id=parent)

    # Code agent 1: 50s -> 180s (with a worker at 80-120s)
    _insert(conn, _ts(t0, 50), "subagent_start", session_id=sid, agent_id=code1_aid, agent_type="code", parent_id=parent)
    _insert(conn, _ts(t0, 51), "task_init", session_id=sid, agent_type="code", task_uuid=code1_tid, parent_id=parent)
    for t, tool in [(55, "Read"), (60, "Grep"), (65, "Edit"), (70, "Edit"), (75, "Write")]:
        _insert(conn, _ts(t0, t), "tool_use", session_id=sid, agent_id=code1_aid, tool_name=tool, task_uuid=code1_tid)

    # Worker 1 spawned by code1
    _insert(conn, _ts(t0, 80), "subtask_init", session_id=sid, worker_id=worker1_id, agent_type="code", parent_id=f"task:{code1_tid}")
    _insert(conn, _ts(t0, 82), "worker_start", session_id=sid, worker_id=worker1_id, agent_type="code")
    for t, tool in [(85, "Read"), (90, "Edit"), (95, "Bash"), (100, "Edit"), (110, "Bash")]:
        _insert(conn, _ts(t0, t), "tool_use", session_id=sid, worker_id=worker1_id, tool_name=tool)
    _insert(conn, _ts(t0, 120), "worker_end", session_id=sid, worker_id=worker1_id, agent_type="code")

    for t, tool in [(130, "Bash"), (140, "Read"), (150, "Edit"), (160, "Bash"), (170, "Write")]:
        _insert(conn, _ts(t0, t), "tool_use", session_id=sid, agent_id=code1_aid, tool_name=tool, task_uuid=code1_tid)
    _insert(conn, _ts(t0, 180), "subagent_stop", session_id=sid, agent_id=code1_aid, agent_type="code", parent_id=parent)

    # Code agent 2 (parallel, starts at 55s -> 160s, with a resume)
    _insert(conn, _ts(t0, 55), "subagent_start", session_id=sid, agent_id=code2_aid, agent_type="code", parent_id=parent)
    _insert(conn, _ts(t0, 56), "task_init", session_id=sid, agent_type="code", task_uuid=code2_tid, parent_id=parent)
    for t, tool in [(60, "Read"), (65, "Grep"), (70, "Write"), (80, "Edit"), (90, "Bash")]:
        _insert(conn, _ts(t0, t), "tool_use", session_id=sid, agent_id=code2_aid, tool_name=tool, task_uuid=code2_tid)
    # Agent stops, gets resumed
    _insert(conn, _ts(t0, 95), "subagent_stop", session_id=sid, agent_id=code2_aid, agent_type="code", parent_id=parent)

    # Resume at 100s
    _insert(conn, _ts(t0, 100), "subagent_start", session_id=sid, agent_id=code2_aid, agent_type="code", parent_id=parent)
    for t, tool in [(105, "Read"), (110, "Edit"), (120, "Edit"), (130, "Bash"), (140, "Write"), (150, "Bash")]:
        _insert(conn, _ts(t0, t), "tool_use", session_id=sid, agent_id=code2_aid, tool_name=tool, task_uuid=code2_tid)
    _insert(conn, _ts(t0, 160), "subagent_stop", session_id=sid, agent_id=code2_aid, agent_type="code", parent_id=parent)

    # Review agent: 185s -> 240s
    _insert(conn, _ts(t0, 185), "subagent_start", session_id=sid, agent_id=review_aid, agent_type="review", parent_id=parent)
    _insert(conn, _ts(t0, 186), "task_init", session_id=sid, agent_type="review", task_uuid=review_tid, parent_id=parent)
    for t, tool in [(190, "Read"), (195, "Read"), (200, "Grep"), (210, "Read"), (220, "Read"), (230, "Read")]:
        _insert(conn, _ts(t0, t), "tool_use", session_id=sid, agent_id=review_aid, tool_name=tool, task_uuid=review_tid)
    _insert(conn, _ts(t0, 240), "subagent_stop", session_id=sid, agent_id=review_aid, agent_type="review", parent_id=parent)

    # Memory agent: 245s -> 270s
    _insert(conn, _ts(t0, 245), "subagent_start", session_id=sid, agent_id=memory_aid, agent_type="memory", parent_id=parent)
    _insert(conn, _ts(t0, 246), "task_init", session_id=sid, agent_type="memory", task_uuid=memory_tid, parent_id=parent)
    for t, tool in [(250, "Read"), (255, "Write"), (260, "Write"), (265, "Glob")]:
        _insert(conn, _ts(t0, t), "tool_use", session_id=sid, agent_id=memory_aid, tool_name=tool, task_uuid=memory_tid)
    _insert(conn, _ts(t0, 270), "subagent_stop", session_id=sid, agent_id=memory_aid, agent_type="memory", parent_id=parent)

    # Seed session_stats
    conn.execute(
        "INSERT OR REPLACE INTO session_stats "
        "(session_id, custom_name, total_input_tokens, total_output_tokens, "
        "total_cache_creation_tokens, total_cache_read_tokens, message_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sid, "Feature: Auth Middleware", 450_000, 38_000, 120_000, 280_000, 42),
    )

    print(f"  Session 1 (complex): {sid} — 5 agents, 1 worker, 1 resume, 270s")
    return sid


def seed_session_2(conn):
    """Simple session: plan -> code -> memory. Quick bug fix."""
    sid = "dummy-bugfix-" + uuid.uuid4().hex[:8]
    t0 = datetime(2026, 3, 16, 21, 0, 0, tzinfo=timezone.utc)
    parent = f"orchestrator:{sid}"

    plan_aid = uuid.uuid4().hex[:12]
    code_aid = uuid.uuid4().hex[:12]
    memory_aid = uuid.uuid4().hex[:12]

    plan_tid = str(uuid.uuid4())
    code_tid = str(uuid.uuid4())

    _insert(conn, _ts(t0, 0), "session_start", session_id=sid)

    # Plan: 0s -> 20s
    _insert(conn, _ts(t0, 2), "subagent_start", session_id=sid, agent_id=plan_aid, agent_type="plan", parent_id=parent)
    _insert(conn, _ts(t0, 3), "task_init", session_id=sid, agent_type="plan", task_uuid=plan_tid, parent_id=parent)
    for t, tool in [(5, "Read"), (10, "Grep"), (15, "Read")]:
        _insert(conn, _ts(t0, t), "tool_use", session_id=sid, agent_id=plan_aid, tool_name=tool, task_uuid=plan_tid)
    _insert(conn, _ts(t0, 20), "subagent_stop", session_id=sid, agent_id=plan_aid, agent_type="plan", parent_id=parent)

    # Code: 22s -> 90s
    _insert(conn, _ts(t0, 22), "subagent_start", session_id=sid, agent_id=code_aid, agent_type="code", parent_id=parent)
    _insert(conn, _ts(t0, 23), "task_init", session_id=sid, agent_type="code", task_uuid=code_tid, parent_id=parent)
    for t, tool in [(25, "Read"), (30, "Read"), (35, "Edit"), (45, "Bash"), (55, "Edit"), (65, "Bash"), (75, "Bash"), (80, "Read")]:
        _insert(conn, _ts(t0, t), "tool_use", session_id=sid, agent_id=code_aid, tool_name=tool, task_uuid=code_tid)
    _insert(conn, _ts(t0, 90), "subagent_stop", session_id=sid, agent_id=code_aid, agent_type="code", parent_id=parent)

    # Memory: 92s -> 105s
    _insert(conn, _ts(t0, 92), "subagent_start", session_id=sid, agent_id=memory_aid, agent_type="memory", parent_id=parent)
    for t, tool in [(95, "Read"), (100, "Write")]:
        _insert(conn, _ts(t0, t), "tool_use", session_id=sid, agent_id=memory_aid, tool_name=tool)
    _insert(conn, _ts(t0, 105), "subagent_stop", session_id=sid, agent_id=memory_aid, agent_type="memory", parent_id=parent)

    conn.execute(
        "INSERT OR REPLACE INTO session_stats "
        "(session_id, custom_name, total_input_tokens, total_output_tokens, "
        "total_cache_creation_tokens, total_cache_read_tokens, message_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sid, "Bugfix: CLAUDE_CONFIG_DIR", 120_000, 15_000, 30_000, 85_000, 18),
    )

    print(f"  Session 2 (bugfix):  {sid} — 3 agents, 105s")
    return sid


def seed_session_3(conn):
    """Heavy session: plan -> research -> plan -> 3 code (with workers) -> review -> consistency -> document -> memory."""
    sid = "dummy-heavy-" + uuid.uuid4().hex[:8]
    t0 = datetime(2026, 3, 16, 22, 0, 0, tzinfo=timezone.utc)
    parent = f"orchestrator:{sid}"

    aids = {k: uuid.uuid4().hex[:12] for k in [
        "plan1", "research", "plan2", "code1", "code2", "code3",
        "review", "consistency", "document", "memory"
    ]}
    tids = {k: str(uuid.uuid4()) for k in aids}
    wids = {f"w{i}": uuid.uuid4().hex[:12] for i in range(1, 5)}

    _insert(conn, _ts(t0, 0), "session_start", session_id=sid)

    # Plan 1: 0-30s
    _insert(conn, _ts(t0, 2), "subagent_start", session_id=sid, agent_id=aids["plan1"], agent_type="plan", parent_id=parent)
    _insert(conn, _ts(t0, 3), "task_init", session_id=sid, agent_type="plan", task_uuid=tids["plan1"], parent_id=parent)
    for t in [5, 10, 15, 20, 25]:
        _insert(conn, _ts(t0, t), "tool_use", session_id=sid, agent_id=aids["plan1"], tool_name="Read", task_uuid=tids["plan1"])
    _insert(conn, _ts(t0, 30), "subagent_stop", session_id=sid, agent_id=aids["plan1"], agent_type="plan", parent_id=parent)

    # Research: 32-90s
    _insert(conn, _ts(t0, 32), "subagent_start", session_id=sid, agent_id=aids["research"], agent_type="research", parent_id=parent)
    _insert(conn, _ts(t0, 33), "task_init", session_id=sid, agent_type="research", task_uuid=tids["research"], parent_id=parent)
    for t, tool in [(35, "WebSearch"), (40, "WebFetch"), (50, "WebFetch"), (60, "Read"), (70, "WebSearch"), (80, "Write")]:
        _insert(conn, _ts(t0, t), "tool_use", session_id=sid, agent_id=aids["research"], tool_name=tool, task_uuid=tids["research"])
    _insert(conn, _ts(t0, 90), "subagent_stop", session_id=sid, agent_id=aids["research"], agent_type="research", parent_id=parent)

    # Plan 2: 92-110s
    _insert(conn, _ts(t0, 92), "subagent_start", session_id=sid, agent_id=aids["plan2"], agent_type="plan", parent_id=parent)
    _insert(conn, _ts(t0, 93), "task_init", session_id=sid, agent_type="plan", task_uuid=tids["plan2"], parent_id=parent)
    for t in [95, 100, 105]:
        _insert(conn, _ts(t0, t), "tool_use", session_id=sid, agent_id=aids["plan2"], tool_name="Read", task_uuid=tids["plan2"])
    _insert(conn, _ts(t0, 110), "subagent_stop", session_id=sid, agent_id=aids["plan2"], agent_type="plan", parent_id=parent)

    # Code 1: 115-300s (with 2 workers)
    _insert(conn, _ts(t0, 115), "subagent_start", session_id=sid, agent_id=aids["code1"], agent_type="code", parent_id=parent)
    _insert(conn, _ts(t0, 116), "task_init", session_id=sid, agent_type="code", task_uuid=tids["code1"], parent_id=parent)
    for t, tool in [(120, "Read"), (130, "Grep"), (140, "Edit"), (150, "Write")]:
        _insert(conn, _ts(t0, t), "tool_use", session_id=sid, agent_id=aids["code1"], tool_name=tool, task_uuid=tids["code1"])

    # Worker 1
    _insert(conn, _ts(t0, 155), "subtask_init", session_id=sid, worker_id=wids["w1"], agent_type="code", parent_id=f"task:{tids['code1']}")
    _insert(conn, _ts(t0, 157), "worker_start", session_id=sid, worker_id=wids["w1"], agent_type="code")
    for t, tool in [(160, "Read"), (170, "Edit"), (180, "Bash")]:
        _insert(conn, _ts(t0, t), "tool_use", session_id=sid, worker_id=wids["w1"], tool_name=tool)
    _insert(conn, _ts(t0, 190), "worker_end", session_id=sid, worker_id=wids["w1"], agent_type="code")

    # Worker 2
    _insert(conn, _ts(t0, 160), "subtask_init", session_id=sid, worker_id=wids["w2"], agent_type="code", parent_id=f"task:{tids['code1']}")
    _insert(conn, _ts(t0, 162), "worker_start", session_id=sid, worker_id=wids["w2"], agent_type="code")
    for t, tool in [(165, "Read"), (175, "Edit"), (185, "Bash"), (195, "Edit")]:
        _insert(conn, _ts(t0, t), "tool_use", session_id=sid, worker_id=wids["w2"], tool_name=tool)
    _insert(conn, _ts(t0, 200), "worker_end", session_id=sid, worker_id=wids["w2"], agent_type="code")

    for t, tool in [(210, "Bash"), (220, "Read"), (240, "Edit"), (260, "Bash"), (280, "Write")]:
        _insert(conn, _ts(t0, t), "tool_use", session_id=sid, agent_id=aids["code1"], tool_name=tool, task_uuid=tids["code1"])
    _insert(conn, _ts(t0, 300), "subagent_stop", session_id=sid, agent_id=aids["code1"], agent_type="code", parent_id=parent)

    # Code 2: 120-250s (parallel, lighter)
    _insert(conn, _ts(t0, 120), "subagent_start", session_id=sid, agent_id=aids["code2"], agent_type="code", parent_id=parent)
    _insert(conn, _ts(t0, 121), "task_init", session_id=sid, agent_type="code", task_uuid=tids["code2"], parent_id=parent)
    for t, tool in [(125, "Read"), (135, "Edit"), (150, "Bash"), (170, "Edit"), (190, "Bash"), (210, "Write"), (230, "Bash")]:
        _insert(conn, _ts(t0, t), "tool_use", session_id=sid, agent_id=aids["code2"], tool_name=tool, task_uuid=tids["code2"])
    _insert(conn, _ts(t0, 250), "subagent_stop", session_id=sid, agent_id=aids["code2"], agent_type="code", parent_id=parent)

    # Code 3: 125-220s (parallel, with worker + resume)
    _insert(conn, _ts(t0, 125), "subagent_start", session_id=sid, agent_id=aids["code3"], agent_type="code", parent_id=parent)
    _insert(conn, _ts(t0, 126), "task_init", session_id=sid, agent_type="code", task_uuid=tids["code3"], parent_id=parent)
    for t, tool in [(130, "Read"), (140, "Edit"), (150, "Bash")]:
        _insert(conn, _ts(t0, t), "tool_use", session_id=sid, agent_id=aids["code3"], tool_name=tool, task_uuid=tids["code3"])
    _insert(conn, _ts(t0, 155), "subagent_stop", session_id=sid, agent_id=aids["code3"], agent_type="code", parent_id=parent)

    # Resume code3
    _insert(conn, _ts(t0, 160), "subagent_start", session_id=sid, agent_id=aids["code3"], agent_type="code", parent_id=parent)
    # Worker 3
    _insert(conn, _ts(t0, 165), "subtask_init", session_id=sid, worker_id=wids["w3"], agent_type="code", parent_id=f"task:{tids['code3']}")
    _insert(conn, _ts(t0, 167), "worker_start", session_id=sid, worker_id=wids["w3"], agent_type="code")
    for t, tool in [(170, "Read"), (180, "Edit"), (190, "Bash")]:
        _insert(conn, _ts(t0, t), "tool_use", session_id=sid, worker_id=wids["w3"], tool_name=tool)
    _insert(conn, _ts(t0, 195), "worker_end", session_id=sid, worker_id=wids["w3"], agent_type="code")
    for t, tool in [(200, "Read"), (210, "Bash")]:
        _insert(conn, _ts(t0, t), "tool_use", session_id=sid, agent_id=aids["code3"], tool_name=tool, task_uuid=tids["code3"])
    _insert(conn, _ts(t0, 220), "subagent_stop", session_id=sid, agent_id=aids["code3"], agent_type="code", parent_id=parent)

    # Review: 305-360s
    _insert(conn, _ts(t0, 305), "subagent_start", session_id=sid, agent_id=aids["review"], agent_type="review", parent_id=parent)
    _insert(conn, _ts(t0, 306), "task_init", session_id=sid, agent_type="review", task_uuid=tids["review"], parent_id=parent)
    for t in [310, 320, 330, 340, 350]:
        _insert(conn, _ts(t0, t), "tool_use", session_id=sid, agent_id=aids["review"], tool_name="Read", task_uuid=tids["review"])
    _insert(conn, _ts(t0, 360), "subagent_stop", session_id=sid, agent_id=aids["review"], agent_type="review", parent_id=parent)

    # Consistency: 362-390s
    _insert(conn, _ts(t0, 362), "subagent_start", session_id=sid, agent_id=aids["consistency"], agent_type="consistency", parent_id=parent)
    _insert(conn, _ts(t0, 363), "task_init", session_id=sid, agent_type="consistency", task_uuid=tids["consistency"], parent_id=parent)
    for t, tool in [(365, "Grep"), (370, "Read"), (375, "Grep"), (380, "Read")]:
        _insert(conn, _ts(t0, t), "tool_use", session_id=sid, agent_id=aids["consistency"], tool_name=tool, task_uuid=tids["consistency"])
    _insert(conn, _ts(t0, 390), "subagent_stop", session_id=sid, agent_id=aids["consistency"], agent_type="consistency", parent_id=parent)

    # Document: 392-420s
    _insert(conn, _ts(t0, 392), "subagent_start", session_id=sid, agent_id=aids["document"], agent_type="document", parent_id=parent)
    _insert(conn, _ts(t0, 393), "task_init", session_id=sid, agent_type="document", task_uuid=tids["document"], parent_id=parent)
    for t, tool in [(395, "Read"), (400, "Edit"), (410, "Write")]:
        _insert(conn, _ts(t0, t), "tool_use", session_id=sid, agent_id=aids["document"], tool_name=tool, task_uuid=tids["document"])
    _insert(conn, _ts(t0, 420), "subagent_stop", session_id=sid, agent_id=aids["document"], agent_type="document", parent_id=parent)

    # Memory: 422-440s
    _insert(conn, _ts(t0, 422), "subagent_start", session_id=sid, agent_id=aids["memory"], agent_type="memory", parent_id=parent)
    for t, tool in [(425, "Read"), (430, "Write"), (435, "Write")]:
        _insert(conn, _ts(t0, t), "tool_use", session_id=sid, agent_id=aids["memory"], tool_name=tool)
    _insert(conn, _ts(t0, 440), "subagent_stop", session_id=sid, agent_id=aids["memory"], agent_type="memory", parent_id=parent)

    conn.execute(
        "INSERT OR REPLACE INTO session_stats "
        "(session_id, custom_name, total_input_tokens, total_output_tokens, "
        "total_cache_creation_tokens, total_cache_read_tokens, message_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sid, "Major: Metrics Dashboard v1", 1_200_000, 95_000, 350_000, 800_000, 87),
    )

    print(f"  Session 3 (heavy):   {sid} — 10 agents, 4 workers, 2 resumes, 440s")
    return sid


def main():
    db = _db_path()
    db.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    _init_db(conn)

    print("Seeding dummy metrics data...")
    seed_session_1(conn)
    seed_session_2(conn)
    seed_session_3(conn)

    conn.commit()
    conn.close()

    print("\nDone! Cleanup later with:")
    print("  DELETE FROM events WHERE detail = '__dummy__';")
    print("  DELETE FROM session_stats WHERE session_id LIKE 'dummy-%';")


if __name__ == "__main__":
    main()
