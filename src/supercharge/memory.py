"""Background memory harvesting: scanning, stamping, and spawning logic.

Pure scanning and stamping functions with no Agent SDK dependencies.
Used by hook_session_start() to detect work and spawn background memory agents.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_ENV_SESSION_AGE_HOURS = "SUPERCHARGE_MEMORY_SESSION_AGE_HOURS"
_ENV_STALE_DAYS = "SUPERCHARGE_MEMORY_STALE_DAYS"

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

_STAMP_TYPE = "supercharge-memory-reviewed"


# ── Template loading ───────────────────────────────────────────────────────


def _load_template(name: str) -> str:
    """Read a template file from the templates/ directory."""
    from supercharge.paths import _cli_data_dir

    path = _cli_data_dir() / "templates" / name
    return path.read_text()


def _format_transcript_task(transcripts: list[tuple[Path, int | None]], memory_dir: str) -> str:
    """Format the transcript harvesting task.md from template."""
    lines = []
    for path, start_line in transcripts:
        if start_line is not None:
            lines.append(
                f"- `{path}` (start reading from line {start_line}"
                " -- skip previously reviewed content)"
            )
        else:
            lines.append(f"- `{path}`")
    transcript_list = "\n".join(lines)
    template = _load_template("memory-transcript-task.md")
    return template.format(transcript_list=transcript_list, memory_dir=memory_dir)


def _format_stale_folders_task(folders: list[Path], memory_dir: str) -> str:
    """Format the stale folder harvesting task.md from template."""
    folder_list = "\n".join(f"- `{p}`" for p in folders)
    template = _load_template("memory-stale-task.md")
    return template.format(folder_list=folder_list, memory_dir=memory_dir)


# ── Scanning functions ─────────────────────────────────────────────────────


def _scan_unreviewed_transcripts(
    transcript_path: str,
    min_age_hours: float | None = None,
) -> list[tuple[Path, int | None]]:
    """Find unreviewed or partially-reviewed transcript files.

    Args:
        transcript_path: Path to the current session's transcript file.
            The parent directory is scanned for all .jsonl files.
        min_age_hours: Minimum age in hours (by mtime) before a transcript
            is eligible. Defaults to env SUPERCHARGE_MEMORY_SESSION_AGE_HOURS
            or 1 hour.

    Returns:
        List of (path, start_line) tuples. start_line is the 1-based line
        to begin reading from (None = read entire file, i.e. no prior stamp).
    """
    if min_age_hours is None:
        min_age_hours = float(os.environ.get(_ENV_SESSION_AGE_HOURS, "1"))

    transcript = Path(transcript_path)
    parent = transcript.parent

    if not parent.is_dir():
        return []

    current_name = transcript.name
    cutoff = time.time() - (min_age_hours * 3600)
    results: list[tuple[Path, int | None]] = []

    for f in parent.iterdir():
        if not f.is_file() or f.suffix != ".jsonl":
            continue
        # Skip current session
        if f.name == current_name:
            continue
        # Skip too-recent files
        try:
            if f.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        # Check stamp status
        is_fully_reviewed, last_stamp_line = _stamp_status(f)
        if is_fully_reviewed:
            continue
        if last_stamp_line is not None:
            results.append((f, last_stamp_line + 1))
        else:
            results.append((f, None))

    return results


def _stamp_status(path: Path) -> tuple[bool, int | None]:
    """Check stamp status of a transcript file.

    Reads the full file to determine if a reviewed stamp exists and
    whether new content has been appended after the last stamp.

    Returns:
        (is_fully_reviewed, last_stamp_line) where last_stamp_line is 1-based.
        - (True, N): stamp at line N is the last meaningful entry -> skip
        - (False, N): stamp at line N but new content after it -> re-process from N+1
        - (False, None): no stamp at all -> process entire file
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return (False, None)

    lines = text.splitlines()
    if not lines:
        return (False, None)

    last_stamp_line: int | None = None
    has_content_after_stamp = False

    for i, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            entry = json.loads(stripped)
            if isinstance(entry, dict) and entry.get("type") == _STAMP_TYPE:
                last_stamp_line = i
                has_content_after_stamp = False
                continue
        except (json.JSONDecodeError, ValueError):
            pass
        # Non-stamp, non-blank line
        if last_stamp_line is not None:
            has_content_after_stamp = True

    if last_stamp_line is None:
        return (False, None)
    if has_content_after_stamp:
        return (False, last_stamp_line)
    return (True, last_stamp_line)


def _stamp_transcript(transcript_path: Path) -> None:
    """Append a reviewed-stamp JSONL entry to a transcript file.

    The stamp prevents future scans from re-processing this transcript.
    """
    from supercharge import __version__

    stamp = {
        "type": _STAMP_TYPE,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": __version__,
    }
    with transcript_path.open("a") as fh:
        fh.write(json.dumps(stamp) + "\n")


def _scan_stale_task_folders(
    task_root: Path,
    max_age_days: float | None = None,
) -> list[Path]:
    """Find task folders whose newest file is older than max_age_days.

    Scans task_root for agent-type directories, then UUID subdirectories.
    Excludes non-UUID directory names.

    Args:
        task_root: Path to .claude/SuperchargeAI/tasks/ directory.
        max_age_days: Maximum age in days before a folder is considered stale.
            Defaults to env SUPERCHARGE_MEMORY_STALE_DAYS or 2 days.

    Returns:
        List of stale task folder Paths.
    """
    if max_age_days is None:
        max_age_days = float(os.environ.get(_ENV_STALE_DAYS, "2"))

    if not task_root.is_dir():
        return []

    cutoff = time.time() - (max_age_days * 86400)
    results: list[Path] = []

    for agent_dir in task_root.iterdir():
        if not agent_dir.is_dir():
            continue

        for task_dir in agent_dir.iterdir():
            if not task_dir.is_dir():
                continue
            # Only process UUID-named directories
            if not _UUID_RE.match(task_dir.name):
                continue

            newest_mtime = _newest_mtime(task_dir)
            if newest_mtime is not None and newest_mtime < cutoff:
                results.append(task_dir)

    return results


def _newest_mtime(folder: Path) -> float | None:
    """Return mtime of the most recently modified file in folder (recursive).

    Returns None if the folder is empty or unreadable.
    """
    newest = None
    try:
        for item in folder.rglob("*"):
            if item.is_file():
                try:
                    mtime = item.stat().st_mtime
                    if newest is None or mtime > newest:
                        newest = mtime
                except OSError:
                    continue
    except OSError:
        pass
    return newest


# ── Background spawning ────────────────────────────────────────────────────


def _spawn_background_memory(task_md_content: str, project_dir: str) -> str | None:
    """Create a memory task workspace and spawn a background memory agent.

    Runs ``supercharge task init memory`` to create the workspace, writes
    the task.md content, then spawns ``supercharge memory run <uuid>`` as
    a fully detached background process.

    Args:
        task_md_content: Content for the task.md file.
        project_dir: Project root directory (for CLAUDE_PROJECT_DIR env).

    Returns:
        The task UUID on success, or None on error. Never raises.
    """
    try:
        # Create workspace
        result = subprocess.run(
            ["supercharge", "task", "init", "memory"],
            capture_output=True,
            text=True,
            env={**os.environ, "CLAUDE_PROJECT_DIR": project_dir},
            timeout=10,
        )
        if result.returncode != 0:
            print(
                f"[SuperchargeAI] memory task init failed: {result.stderr.strip()}",
                file=sys.stderr,
            )
            return None

        task_uuid = result.stdout.strip()
        if not task_uuid:
            print("[SuperchargeAI] memory task init returned empty UUID", file=sys.stderr)
            return None

        # Write task.md to the workspace
        task_dir = Path(project_dir) / ".claude" / "SuperchargeAI" / "tasks" / "memory" / task_uuid
        task_md = task_dir / "task.md"
        task_md.write_text(task_md_content)

        # Spawn background process (fully detached)
        subprocess.Popen(
            ["supercharge", "memory", "run", task_uuid],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "CLAUDE_PROJECT_DIR": project_dir},
        )

        return task_uuid
    except Exception as exc:
        print(f"[SuperchargeAI] background memory spawn failed: {exc}", file=sys.stderr)
        return None
