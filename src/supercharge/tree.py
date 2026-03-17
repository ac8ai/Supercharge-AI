"""Session tree reconstruction from metrics events.

Builds a nested execution graph from flat event data, representing the
parent-child relationships between orchestrators, agents, and workers.
"""

from __future__ import annotations

from datetime import datetime

from supercharge.metrics import _query_session_events


def _build_session_tree(session_id: str) -> dict:
    """Reconstruct a session's execution graph as a nested dict tree.

    Queries all events for the given session and builds a tree based on
    parent_id relationships. Never raises (returns minimal root on error).

    Node format::

        {
            "type": "session" | "agent" | "worker" | "event",
            "id": str,
            "agent_type": str | None,
            "started_at": str | None,
            "duration_seconds": float | None,
            "tool_calls": int,
            "tools": {"Bash": 2, "Read": 1, ...},
            "children": [...]
        }
    """
    root: dict = {
        "type": "session",
        "id": session_id,
        "agent_type": None,
        "started_at": None,
        "duration_seconds": None,
        "tool_calls": 0,
        "tools": {},
        "children": [],
    }

    try:
        events = _query_session_events(session_id)
    except Exception:
        return root

    if not events:
        return root

    # Track nodes by their identifying key so children can find parents.
    # Keys: "task:<task_uuid>", "worker:<worker_id>"
    nodes_by_key: dict[str, dict] = {}

    # Set root timestamps.
    first_ts = events[0].get("timestamp")
    last_ts = events[-1].get("timestamp")
    root["started_at"] = first_ts
    try:
        t0 = datetime.fromisoformat(first_ts)
        t1 = datetime.fromisoformat(last_ts)
        root["duration_seconds"] = (t1 - t0).total_seconds()
    except Exception:
        root["duration_seconds"] = 0.0

    total_tool_calls = 0

    for ev in events:
        etype = ev.get("event_type", "")
        parent_id = ev.get("parent_id", "")
        task_uuid = ev.get("task_uuid", "")
        worker_id = ev.get("worker_id", "")
        timestamp = ev.get("timestamp", "")

        # Skip session_start -- represented by the root node.
        if etype == "session_start":
            continue

        if etype == "subagent_start":
            # Hook-originated agent event. Has agent_id + agent_type.
            # The same agent_id may appear multiple times if the orchestrator
            # resumes an agent — merge into one node, track invocation count.
            agent_id = ev.get("agent_id", "")
            akey = f"agent:{agent_id}" if agent_id else ""
            if akey and akey in nodes_by_key:
                # Same agent resumed — bump invocations, update last start time
                existing = nodes_by_key[akey]
                existing["invocations"] = existing.get("invocations", 1) + 1
                existing["last_started_at"] = timestamp
            else:
                node = _make_node("agent", ev, id_val=agent_id)
                node["invocations"] = 1
                if agent_id:
                    nodes_by_key[akey] = node
                _attach_to_parent(node, parent_id, root, nodes_by_key)

        elif etype == "task_init":
            node = _make_node("agent", ev, id_val=task_uuid)
            if task_uuid:
                nodes_by_key[f"task:{task_uuid}"] = node
            _attach_to_parent(node, parent_id, root, nodes_by_key)

        elif etype == "subtask_init":
            node = _make_node("worker", ev, id_val=worker_id)
            if worker_id:
                nodes_by_key[f"worker:{worker_id}"] = node
            _attach_to_parent(node, parent_id, root, nodes_by_key)

        elif etype == "worker_start":
            key = f"worker:{worker_id}" if worker_id else ""
            if key and key in nodes_by_key:
                target = nodes_by_key[key]
                target["started_at"] = timestamp
            else:
                # Orphan worker_start -- create a worker node at root.
                node = _make_node("worker", ev, id_val=worker_id)
                if worker_id:
                    nodes_by_key[f"worker:{worker_id}"] = node
                root["children"].append(node)

        elif etype == "worker_end":
            key = f"worker:{worker_id}" if worker_id else ""
            if key and key in nodes_by_key:
                _set_duration(nodes_by_key[key], timestamp)

        elif etype == "subagent_stop":
            # Match to the corresponding subagent_start node by agent_id
            agent_id = ev.get("agent_id", "")
            akey = f"agent:{agent_id}" if agent_id else ""
            if akey and akey in nodes_by_key:
                _set_duration(nodes_by_key[akey], timestamp)

        elif etype == "tool_use":
            total_tool_calls += 1
            tool_name = ev.get("tool_name", "") or "unknown"
            # Try to attach to worker first, then agent via task_uuid.
            wkey = f"worker:{worker_id}" if worker_id else ""
            tkey = f"task:{task_uuid}" if task_uuid else ""
            if wkey and wkey in nodes_by_key:
                target = nodes_by_key[wkey]
                target["tool_calls"] += 1
                target["tools"][tool_name] = target["tools"].get(tool_name, 0) + 1
            elif tkey and tkey in nodes_by_key:
                target = nodes_by_key[tkey]
                target["tool_calls"] += 1
                target["tools"][tool_name] = target["tools"].get(tool_name, 0) + 1
            else:
                root["tools"][tool_name] = root["tools"].get(tool_name, 0) + 1

        else:
            # Other events (task_cleanup, memory_spawn, etc.)
            node = _make_node("event", ev)
            _attach_to_parent(node, parent_id, root, nodes_by_key)

    root["tool_calls"] = total_tool_calls
    return root


def _normalize_agent_type(raw: str | None) -> str | None:
    """Normalize agent_type: strip 'supercharge-ai:' prefix if present."""
    if not raw:
        return None
    if raw.startswith("supercharge-ai:"):
        return raw[len("supercharge-ai:"):]
    return raw


def _make_node(node_type: str, ev: dict, id_val: str | None = None) -> dict:
    """Create a tree node from an event."""
    return {
        "type": node_type,
        "id": id_val if id_val is not None else str(ev.get("id", "")),
        "agent_type": _normalize_agent_type(ev.get("agent_type")),
        "started_at": ev.get("timestamp"),
        "duration_seconds": None,
        "tool_calls": 0,
        "tools": {},
        "children": [],
    }


def _attach_to_parent(
    node: dict,
    parent_id: str,
    root: dict,
    nodes_by_key: dict[str, dict],
) -> None:
    """Attach a node to its parent, or to root if parent not found."""
    if parent_id and parent_id in nodes_by_key:
        nodes_by_key[parent_id]["children"].append(node)
    elif parent_id and parent_id.startswith("orchestrator:"):
        root["children"].append(node)
    else:
        # Orphan -- attach to root.
        root["children"].append(node)


def _set_duration(node: dict, end_timestamp: str) -> None:
    """Calculate and set duration_seconds on a node."""
    start = node.get("started_at")
    if not start:
        return
    try:
        t0 = datetime.fromisoformat(start)
        t1 = datetime.fromisoformat(end_timestamp)
        node["duration_seconds"] = (t1 - t0).total_seconds()
    except Exception:
        pass
