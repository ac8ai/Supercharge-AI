"""Dashboard server — Starlette ASGI app for SuperchargeAI metrics UI.

Provides a web interface and JSON API for browsing sessions, events,
execution trees, and global statistics. Uses a PID file for singleton
process management.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import socket
from pathlib import Path

try:
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, JSONResponse
    from starlette.routing import Mount, Route
    from starlette.staticfiles import StaticFiles
except ImportError as _exc:
    raise ImportError(
        "Dashboard dependencies are not installed. "
        "Install them with: pip install supercharge-ai[dashboard]"
    ) from _exc

from supercharge import browse, metrics, tree
from supercharge.paths import _find_task_dir, _project_dir

# ── PID file management ─────────────────────────────────────────────────────


def _pidfile_path() -> Path:
    """Return the path to the global dashboard PID file (user-level)."""
    return Path.home() / ".claude" / "SuperchargeAI" / "dashboard.pid"


def _read_pidfile() -> tuple[int, int] | None:
    """Read (pid, port) from the PID file, or None if missing/corrupt."""
    path = _pidfile_path()
    try:
        text = path.read_text().strip()
        lines = text.split("\n")
        if len(lines) < 2:
            return None
        pid = int(lines[0])
        port = int(lines[1])
        return (pid, port)
    except (FileNotFoundError, ValueError, OSError):
        return None


def _write_pidfile(pid: int, port: int) -> None:
    """Write the current pid and port to the PID file."""
    path = _pidfile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pid}\n{port}\n")


def _cleanup_pidfile() -> None:
    """Remove the PID file if it exists. Never raises."""
    try:
        path = _pidfile_path()
        path.unlink(missing_ok=True)
    except Exception:
        pass


def _is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is alive."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


# ── Port selection ───────────────────────────────────────────────────────────


def _find_free_port(default: int = 9333, max_attempts: int = 10) -> int:
    """Try ports starting from *default*, return the first available one.

    Raises RuntimeError if no free port is found within *max_attempts*.
    """
    for i in range(max_attempts):
        port = default + i
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(
        f"No free port found in range {default}-{default + max_attempts - 1}"
    )


# ── API handlers ─────────────────────────────────────────────────────────────

_HTML_SHELL: str | None = None


def _get_html_shell() -> str:
    """Return the HTML shell, reading and caching on first call."""
    global _HTML_SHELL
    if _HTML_SHELL is None:
        html_path = Path(__file__).parent / "data" / "dashboard" / "index.html"
        try:
            _HTML_SHELL = html_path.read_text()
        except FileNotFoundError:
            _HTML_SHELL = (
                "<html><body><h1>Dashboard Error</h1>"
                "<p>index.html not found. The dashboard data files may not be installed.</p>"
                "</body></html>"
            )
    return _HTML_SHELL


async def _handle_root(request: Request) -> HTMLResponse:
    """Serve the HTML shell."""
    return HTMLResponse(_get_html_shell())


async def _handle_sessions(request: Request) -> JSONResponse:
    """Return session list with summary stats, enriched with names and token counts."""
    data = metrics._query_sessions()

    # Enrich sessions with stats from session_stats table
    session_ids = [s["session_id"] for s in data]
    stats = metrics._query_session_stats(session_ids)

    for session in data:
        sid = session["session_id"]
        ss = stats.get(sid, {})
        session["name"] = ss.get("name", "")
        session["input_tokens"] = ss.get("input_tokens", 0)
        session["output_tokens"] = ss.get("output_tokens", 0)
        session["cache_creation_tokens"] = ss.get("cache_creation_tokens", 0)
        session["cache_read_tokens"] = ss.get("cache_read_tokens", 0)

    return JSONResponse(data)


async def _handle_session_tree(request: Request) -> JSONResponse:
    """Return the execution tree for a session."""
    session_id = request.path_params["session_id"]
    data = tree._build_session_tree(session_id)
    return JSONResponse(data)


async def _handle_stats(request: Request) -> JSONResponse:
    """Return global statistics."""
    data = metrics._query_stats()
    return JSONResponse(data)


async def _handle_events(request: Request) -> JSONResponse:
    """Return paginated event list with optional filters."""
    params = request.query_params

    event_type = params.get("event_type")
    session_id = params.get("session_id")
    task_uuid = params.get("task_uuid")
    since = params.get("since")
    until = params.get("until")

    try:
        limit = min(int(params.get("limit", "100")), 1000)
        offset = int(params.get("offset", "0"))
    except (ValueError, TypeError):
        return JSONResponse(
            {"error": "limit and offset must be integers"}, status_code=400
        )

    order = params.get("order", "asc")
    if order not in ("asc", "desc"):
        return JSONResponse(
            {"error": "order must be 'asc' or 'desc'"}, status_code=400
        )

    events = metrics._query_events(
        event_type=event_type,
        session_id=session_id,
        task_uuid=task_uuid,
        limit=limit,
        offset=offset,
        order=order,
        since=since,
        until=until,
    )

    total = metrics._event_count(
        event_type=event_type,
        session_id=session_id,
        task_uuid=task_uuid,
        since=since,
        until=until,
    )

    return JSONResponse({"events": events, "total": total, "limit": limit, "offset": offset})


async def _handle_events_stream(request: Request):
    """SSE endpoint — polls DB every 1s for new events."""
    from sse_starlette import EventSourceResponse

    # Get initial cursor from Last-Event-ID header or query param
    last_event_id_str = request.headers.get("Last-Event-ID") or request.query_params.get(
        "last_event_id", "0"
    )
    try:
        last_seen_id = int(last_event_id_str)
    except (ValueError, TypeError):
        last_seen_id = 0

    async def event_generator():
        nonlocal last_seen_id
        try:
            while True:
                events = metrics._query_events(after_id=last_seen_id, limit=100, order="asc")
                if events:
                    last_seen_id = events[-1]["id"]
                    yield {
                        "event": "events",
                        "id": str(last_seen_id),
                        "data": json.dumps(events),
                    }
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            return

    return EventSourceResponse(event_generator())


async def _handle_session_tools(request: Request) -> JSONResponse:
    """Return per-agent tool breakdown for a session."""
    session_id = request.path_params["session_id"]
    data = metrics._query_session_tools(session_id)
    return JSONResponse(data)


async def _handle_session_spans(request: Request) -> JSONResponse:
    """Return per-invocation spans for the trace timeline view, enriched with tokens."""
    session_id = request.path_params["session_id"]
    data = metrics._query_session_spans(session_id)

    # Enrich with cached per-agent token data (read-only, non-blocking)
    agent_tokens = {}
    try:
        import sqlite3
        db = metrics._db_path()
        if db.exists():
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT agent_id, agent_type, total_input_tokens, total_output_tokens, "
                "total_cache_creation_tokens, total_cache_read_tokens "
                "FROM agent_token_stats WHERE session_id = ?",
                (session_id,),
            ).fetchall()
            for r in rows:
                total = (r["total_input_tokens"] + r["total_output_tokens"]
                         + r["total_cache_creation_tokens"] + r["total_cache_read_tokens"])
                agent_tokens[r["agent_id"]] = {
                    "agent_type": r["agent_type"],
                    "input_tokens": r["total_input_tokens"],
                    "output_tokens": r["total_output_tokens"],
                    "cache_creation": r["total_cache_creation_tokens"],
                    "cache_read": r["total_cache_read_tokens"],
                    "total": total,
                }
            conn.close()
    except Exception:
        pass
    for span in data:
        if span.get("type") == "agent" and span.get("id") in agent_tokens:
            span["tokens"] = agent_tokens[span["id"]]

    return JSONResponse(data)


async def _handle_browse(request: Request) -> JSONResponse:
    """Return the filesystem tree for the SuperchargeAI workspace."""
    data = browse._build_browse_response()
    return JSONResponse(data)


async def _handle_agent_tokens(request: Request) -> JSONResponse:
    """Return per-agent token stats for a session (lazily parsed)."""
    session_id = request.path_params["session_id"]
    data = metrics._query_agent_tokens(session_id)
    return JSONResponse(data)


async def _handle_session_rename(request: Request) -> JSONResponse:
    """Rename a session (update DB + append to JSONL)."""
    session_id = request.path_params["session_id"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        return JSONResponse({"error": "'name' must be a non-empty string"}, status_code=400)
    name = name.strip()[:200]

    metrics._rename_session(session_id, name)
    return JSONResponse({"ok": True})


async def _handle_global_tools(request: Request) -> JSONResponse:
    """Return global tool usage stats grouped by agent type."""
    data = metrics._query_global_tool_stats()
    return JSONResponse(data)


async def _handle_projects(request: Request) -> JSONResponse:
    """Return all projects with aggregated stats."""
    data = metrics._query_projects()
    return JSONResponse(data)


async def _handle_project_sessions(request: Request) -> JSONResponse:
    """Return sessions for a project, enriched with names and token counts."""
    slug = request.path_params["slug"]
    if metrics._get_project_by_slug(slug) is None:
        return JSONResponse({"error": "Project not found"}, status_code=404)
    data = metrics._query_project_sessions(slug)
    return JSONResponse(data)


async def _handle_project_tokens(request: Request) -> JSONResponse:
    """Return per-agent token stats for a project."""
    slug = request.path_params["slug"]
    data = metrics._query_project_tokens(slug)
    return JSONResponse(data)


async def _handle_project_tools(request: Request) -> JSONResponse:
    """Return tool usage stats for a project grouped by agent type."""
    slug = request.path_params["slug"]
    data = metrics._query_project_tools(slug)
    return JSONResponse(data)


async def _handle_project_rename(request: Request) -> JSONResponse:
    """Rename a project (update display_name in DB)."""
    slug = request.path_params["slug"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        return JSONResponse({"error": "'name' must be a non-empty string"}, status_code=400)
    name = name.strip()[:200]

    updated = metrics._rename_project(slug, name)
    if not updated:
        return JSONResponse({"error": "Project not found"}, status_code=404)
    project = metrics._get_project_by_slug(slug)
    if project is None:
        return JSONResponse({"error": "Project not found"}, status_code=404)
    return JSONResponse(project)


async def _handle_task_content(request: Request) -> JSONResponse:
    """Return task.md, result.md, and notes.md content for a task."""
    task_uuid = request.path_params["task_uuid"]

    task_dir = _find_task_dir(task_uuid)
    # Also search archive if not found in active tasks
    if task_dir is None:
        from supercharge.paths import _archive_root
        archive = _archive_root()
        if archive.exists():
            for agent_dir in archive.iterdir():
                if not agent_dir.is_dir():
                    continue
                for candidate in agent_dir.iterdir():
                    if candidate.is_dir() and task_uuid in candidate.name:
                        task_dir = candidate
                        break
                if task_dir:
                    break
    if task_dir is None:
        return JSONResponse({"error": "Task not found"}, status_code=404)

    result: dict[str, str] = {"task_uuid": task_uuid, "source": str(task_dir)}
    for key, filename in [
        ("task_md", "task.md"),
        ("result_md", "result.md"),
        ("notes_md", "notes.md"),
    ]:
        path = task_dir / filename
        try:
            result[key] = path.read_text()
        except (FileNotFoundError, OSError):
            result[key] = ""

    return JSONResponse(result)


async def _handle_find_task(request: Request) -> JSONResponse:
    """Find a task by agent_type, searching active tasks and archive."""
    agent_type = request.query_params.get("agent_type", "")
    if not agent_type:
        return JSONResponse({"error": "agent_type required"}, status_code=400)

    from supercharge.paths import _archive_root, _task_root

    # Search active tasks first, then archive
    results = []
    for root in [_task_root(), _archive_root()]:
        if not root.exists():
            continue
        type_dir = root / agent_type
        if not type_dir.is_dir():
            continue
        for task_dir in sorted(type_dir.iterdir(), reverse=True):
            if not task_dir.is_dir():
                continue
            task_md = task_dir / "task.md"
            if task_md.exists():
                # Extract UUID from folder name
                folder = task_dir.name
                uuid_part = folder.split("-")[0] if "-" in folder else folder
                results.append({
                    "task_uuid": folder,
                    "path": str(task_dir),
                })
                if len(results) >= 20:
                    break

    if not results:
        return JSONResponse({"error": "No tasks found"}, status_code=404)

    # Return the first match's content
    task_dir_path = Path(results[0]["path"])
    result: dict[str, str] = {"task_uuid": results[0]["task_uuid"]}
    for key, filename in [
        ("task_md", "task.md"),
        ("result_md", "result.md"),
        ("notes_md", "notes.md"),
    ]:
        path = task_dir_path / filename
        try:
            result[key] = path.read_text()
        except (FileNotFoundError, OSError):
            result[key] = ""

    # Include list of all matches if multiple
    if len(results) > 1:
        result["other_tasks"] = results[1:]

    return JSONResponse(result)


# ── App factory ──────────────────────────────────────────────────────────────


def _create_app() -> Starlette:
    """Build the Starlette app with all routes."""
    routes = [
        Route("/", _handle_root, methods=["GET"]),
        Route("/api/sessions", _handle_sessions, methods=["GET"]),
        Route("/api/sessions/{session_id}/tree", _handle_session_tree, methods=["GET"]),
        Route("/api/sessions/{session_id}/tools", _handle_session_tools, methods=["GET"]),
        Route("/api/sessions/{session_id}/spans", _handle_session_spans, methods=["GET"]),
        Route("/api/sessions/{session_id}/tokens", _handle_agent_tokens, methods=["GET"]),
        Route("/api/sessions/{session_id}/name", _handle_session_rename, methods=["POST"]),
        Route("/api/stats", _handle_stats, methods=["GET"]),
        Route("/api/stats/tools", _handle_global_tools, methods=["GET"]),
        Route("/api/events", _handle_events, methods=["GET"]),
        Route("/api/events/stream", _handle_events_stream, methods=["GET"]),
        Route("/api/browse", _handle_browse, methods=["GET"]),
        Route("/api/tasks/{task_uuid}/content", _handle_task_content, methods=["GET"]),
        Route("/api/tasks/find", _handle_find_task, methods=["GET"]),
        Route("/api/projects", _handle_projects, methods=["GET"]),
        Route("/api/projects/{slug}/sessions", _handle_project_sessions, methods=["GET"]),
        Route("/api/projects/{slug}/tokens", _handle_project_tokens, methods=["GET"]),
        Route("/api/projects/{slug}/tools", _handle_project_tools, methods=["GET"]),
        Route("/api/projects/{slug}/name", _handle_project_rename, methods=["PUT"]),
        Mount(
            "/static",
            app=StaticFiles(
                directory=str(Path(__file__).parent / "data" / "dashboard")
            ),
            name="static",
        ),
    ]
    return Starlette(routes=routes)


# ── Server runner ────────────────────────────────────────────────────────────


def _run_server(
    host: str = "127.0.0.1",
    port: int | None = None,
    open_browser: bool = False,
) -> None:
    """Start the dashboard server with singleton PID management.

    If another instance is already running (live PID), prints its URL and
    returns. Otherwise finds a free port, writes the PID file, and starts
    uvicorn.
    """
    import uvicorn

    # Check for existing instance
    existing = _read_pidfile()
    if existing is not None:
        pid, existing_port = existing
        if _is_process_alive(pid):
            if open_browser:
                # User wants to open the dashboard — reuse existing instance
                url = f"http://{host}:{existing_port}"
                print(f"Dashboard already running at {url} (pid {pid})")
                import webbrowser
                webbrowser.open(url)
                return
            else:
                # Kill existing instance to restart fresh on the same port
                try:
                    import signal
                    os.kill(pid, signal.SIGTERM)
                    import time
                    time.sleep(0.5)
                except Exception:
                    pass
        _cleanup_pidfile()

    # Determine port
    if port is None:
        port = _find_free_port()

    # Write PID file and register cleanup
    _write_pidfile(os.getpid(), port)
    atexit.register(_cleanup_pidfile)

    url = f"http://{host}:{port}"
    print(f"Starting dashboard at {url}")

    if open_browser:
        import webbrowser

        webbrowser.open(url)

    # Parse JSONL session stats in background before serving
    import threading
    threading.Thread(target=metrics._update_all_session_stats, daemon=True).start()

    app = _create_app()
    uvicorn.run(app, host=host, port=port, log_level="info")
