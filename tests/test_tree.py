"""Tests for session tree reconstruction."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

from supercharge.metrics import _init_db
from supercharge.tree import _build_session_tree


def _patch_db(tmp_path: Path):
    return patch("supercharge.metrics._db_path", return_value=tmp_path / "metrics.db")


def _make_db(tmp_path: Path, rows: list[tuple]) -> None:
    """Create a metrics DB with the given event rows."""
    db = tmp_path / "metrics.db"
    conn = sqlite3.connect(str(db))
    _init_db(conn)
    for row in rows:
        conn.execute(
            "INSERT INTO events (timestamp, event_type, session_id, agent_id, "
            "agent_type, task_uuid, worker_id, parent_id, tool_name, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            row,
        )
    conn.commit()
    conn.close()


class TestSimpleSession:
    """A session with one orchestrator and one code agent."""

    def _seed(self, tmp_path: Path):
        _make_db(tmp_path, [
            ("2026-01-10T10:00:00+00:00", "session_start", "s1", "orch-1", "orchestrator", "", "", "", "", ""),
            ("2026-01-10T10:00:01+00:00", "task_init", "s1", "agent-c1", "code", "task-1", "", "orchestrator:s1", "", ""),
            ("2026-01-10T10:00:02+00:00", "tool_use", "s1", "agent-c1", "code", "task-1", "", "", "Bash", "ls"),
            ("2026-01-10T10:00:03+00:00", "tool_use", "s1", "agent-c1", "code", "task-1", "", "", "Read", "/f"),
            ("2026-01-10T10:00:04+00:00", "task_cleanup", "s1", "agent-c1", "code", "task-1", "", "", "", ""),
        ])

    def test_root_is_session(self, tmp_path: Path):
        self._seed(tmp_path)
        with _patch_db(tmp_path):
            tree = _build_session_tree("s1")
        assert tree["type"] == "session"
        assert tree["id"] == "s1"

    def test_root_has_children(self, tmp_path: Path):
        self._seed(tmp_path)
        with _patch_db(tmp_path):
            tree = _build_session_tree("s1")
        assert len(tree["children"]) >= 1

    def test_agent_node_has_tool_calls(self, tmp_path: Path):
        self._seed(tmp_path)
        with _patch_db(tmp_path):
            tree = _build_session_tree("s1")
        agent_node = None
        for child in tree["children"]:
            if child.get("agent_type") == "code":
                agent_node = child
                break
        assert agent_node is not None
        assert agent_node["tool_calls"] == 2

    def test_tool_use_not_in_children(self, tmp_path: Path):
        self._seed(tmp_path)
        with _patch_db(tmp_path):
            tree = _build_session_tree("s1")
        agent_node = next(c for c in tree["children"] if c.get("agent_type") == "code")
        event_children = [c for c in agent_node["children"] if c["type"] == "event"
                          and c.get("agent_type") == "code"]
        # tool_use events should not appear as child nodes
        assert len(event_children) == 0

    def test_tools_dict_populated(self, tmp_path: Path):
        self._seed(tmp_path)
        with _patch_db(tmp_path):
            tree = _build_session_tree("s1")
        agent_node = next(c for c in tree["children"] if c.get("agent_type") == "code")
        assert agent_node["tools"] == {"Bash": 1, "Read": 1}

    def test_node_has_required_fields(self, tmp_path: Path):
        self._seed(tmp_path)
        with _patch_db(tmp_path):
            tree = _build_session_tree("s1")
        required = {"type", "id", "started_at", "duration_seconds", "tool_calls", "tools", "children"}
        assert required.issubset(tree.keys())


class TestNestedWorkers:
    """A session with an agent that spawns a worker."""

    def _seed(self, tmp_path: Path):
        _make_db(tmp_path, [
            ("2026-01-10T10:00:00+00:00", "session_start", "s1", "orch-1", "orchestrator", "", "", "", "", ""),
            ("2026-01-10T10:00:01+00:00", "task_init", "s1", "agent-c1", "code", "task-1", "", "orchestrator:s1", "", ""),
            ("2026-01-10T10:00:02+00:00", "subtask_init", "s1", "agent-c1", "code", "task-1", "w1", "task:task-1", "", ""),
            ("2026-01-10T10:00:03+00:00", "worker_start", "s1", "agent-w1", "code", "", "w1", "", "", ""),
            ("2026-01-10T10:00:04+00:00", "tool_use", "s1", "agent-w1", "code", "", "w1", "", "Write", "f.py"),
            ("2026-01-10T10:00:05+00:00", "worker_end", "s1", "agent-w1", "code", "", "w1", "", "", ""),
            ("2026-01-10T10:00:06+00:00", "task_cleanup", "s1", "agent-c1", "code", "task-1", "", "", "", ""),
        ])

    def test_worker_is_child_of_agent(self, tmp_path: Path):
        self._seed(tmp_path)
        with _patch_db(tmp_path):
            tree = _build_session_tree("s1")
        agent_node = None
        for child in tree["children"]:
            if child.get("agent_type") == "code" and child["type"] == "agent":
                agent_node = child
                break
        assert agent_node is not None
        worker_children = [c for c in agent_node["children"] if c["type"] == "worker"]
        assert len(worker_children) == 1
        assert worker_children[0]["id"] == "w1"

    def test_worker_has_tool_calls(self, tmp_path: Path):
        self._seed(tmp_path)
        with _patch_db(tmp_path):
            tree = _build_session_tree("s1")
        agent_node = next(c for c in tree["children"] if c.get("agent_type") == "code" and c["type"] == "agent")
        worker = next(c for c in agent_node["children"] if c["type"] == "worker")
        assert worker["tool_calls"] == 1

    def test_worker_tools_dict(self, tmp_path: Path):
        self._seed(tmp_path)
        with _patch_db(tmp_path):
            tree = _build_session_tree("s1")
        agent_node = next(c for c in tree["children"] if c.get("agent_type") == "code" and c["type"] == "agent")
        worker = next(c for c in agent_node["children"] if c["type"] == "worker")
        assert worker["tools"] == {"Write": 1}

    def test_worker_no_event_children(self, tmp_path: Path):
        self._seed(tmp_path)
        with _patch_db(tmp_path):
            tree = _build_session_tree("s1")
        agent_node = next(c for c in tree["children"] if c.get("agent_type") == "code" and c["type"] == "agent")
        worker = next(c for c in agent_node["children"] if c["type"] == "worker")
        event_children = [c for c in worker["children"] if c["type"] == "event"]
        assert len(event_children) == 0

    def test_worker_has_duration(self, tmp_path: Path):
        self._seed(tmp_path)
        with _patch_db(tmp_path):
            tree = _build_session_tree("s1")
        agent_node = next(c for c in tree["children"] if c.get("agent_type") == "code" and c["type"] == "agent")
        worker = next(c for c in agent_node["children"] if c["type"] == "worker")
        assert worker["duration_seconds"] == 2.0


class TestSubWorkers:
    """A worker that spawns a sub-worker."""

    def _seed(self, tmp_path: Path):
        _make_db(tmp_path, [
            ("2026-01-10T10:00:00+00:00", "session_start", "s1", "orch-1", "orchestrator", "", "", "", "", ""),
            ("2026-01-10T10:00:01+00:00", "task_init", "s1", "agent-c1", "code", "task-1", "", "orchestrator:s1", "", ""),
            ("2026-01-10T10:00:02+00:00", "subtask_init", "s1", "agent-c1", "code", "task-1", "w1", "task:task-1", "", ""),
            ("2026-01-10T10:00:03+00:00", "worker_start", "s1", "agent-w1", "code", "", "w1", "", "", ""),
            ("2026-01-10T10:00:04+00:00", "subtask_init", "s1", "agent-w1", "code", "", "w2", "worker:w1", "", ""),
            ("2026-01-10T10:00:05+00:00", "worker_start", "s1", "agent-w2", "code", "", "w2", "", "", ""),
            ("2026-01-10T10:00:06+00:00", "tool_use", "s1", "agent-w2", "code", "", "w2", "", "Bash", "echo"),
            ("2026-01-10T10:00:07+00:00", "worker_end", "s1", "agent-w2", "code", "", "w2", "", "", ""),
            ("2026-01-10T10:00:08+00:00", "worker_end", "s1", "agent-w1", "code", "", "w1", "", "", ""),
            ("2026-01-10T10:00:09+00:00", "task_cleanup", "s1", "agent-c1", "code", "task-1", "", "", "", ""),
        ])

    def test_sub_worker_nested_under_parent_worker(self, tmp_path: Path):
        self._seed(tmp_path)
        with _patch_db(tmp_path):
            tree = _build_session_tree("s1")
        agent_node = next(c for c in tree["children"] if c.get("agent_type") == "code" and c["type"] == "agent")
        w1 = next(c for c in agent_node["children"] if c["type"] == "worker" and c["id"] == "w1")
        sub_workers = [c for c in w1["children"] if c["type"] == "worker"]
        assert len(sub_workers) == 1
        assert sub_workers[0]["id"] == "w2"
        assert sub_workers[0]["tool_calls"] == 1


class TestOrphans:
    """Events without a parent_id should be attached to session root."""

    def _seed(self, tmp_path: Path):
        _make_db(tmp_path, [
            ("2026-01-10T10:00:00+00:00", "session_start", "s1", "orch-1", "orchestrator", "", "", "", "", ""),
            ("2026-01-10T10:00:01+00:00", "memory_spawn", "s1", "agent-m1", "memory", "", "", "", "", ""),
        ])

    def test_orphan_attached_to_root(self, tmp_path: Path):
        self._seed(tmp_path)
        with _patch_db(tmp_path):
            tree = _build_session_tree("s1")
        children_types = [c.get("agent_type", "") for c in tree["children"]]
        assert "memory" in children_types


class TestEmptySession:
    """An empty or nonexistent session."""

    def test_nonexistent_session(self, tmp_path: Path):
        _make_db(tmp_path, [])
        with _patch_db(tmp_path):
            tree = _build_session_tree("nonexistent")
        assert tree["type"] == "session"
        assert tree["id"] == "nonexistent"
        assert tree["children"] == []

    def test_error_returns_empty_tree(self):
        with patch("supercharge.metrics._db_path", side_effect=RuntimeError):
            tree = _build_session_tree("s1")
        assert tree["type"] == "session"
        assert tree["children"] == []


class TestWorkerWithoutTaskInit:
    """Workers that have subtask_init but no task_init parent."""

    def _seed(self, tmp_path: Path):
        _make_db(tmp_path, [
            ("2026-01-10T10:00:00+00:00", "session_start", "s1", "orch-1", "orchestrator", "", "", "", "", ""),
            ("2026-01-10T10:00:02+00:00", "subtask_init", "s1", "agent-x", "code", "task-missing", "w1", "task:task-missing", "", ""),
            ("2026-01-10T10:00:03+00:00", "worker_start", "s1", "agent-w1", "code", "", "w1", "", "", ""),
            ("2026-01-10T10:00:04+00:00", "worker_end", "s1", "agent-w1", "code", "", "w1", "", "", ""),
        ])

    def test_worker_attached_to_root_when_parent_missing(self, tmp_path: Path):
        self._seed(tmp_path)
        with _patch_db(tmp_path):
            tree = _build_session_tree("s1")
        all_ids = []
        def collect_ids(node):
            all_ids.append(node.get("id"))
            for child in node.get("children", []):
                collect_ids(child)
        collect_ids(tree)
        assert "w1" in all_ids
