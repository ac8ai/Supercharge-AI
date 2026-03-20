"""Contribution nudge for SessionStart hook.

Scans methodology memory for contribution candidates and injects a
reminder into the orchestrator additionalContext. Uses atomic file
claiming to prevent duplicate nudges across concurrent sessions.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from supercharge.paths import _read_frontmatter, _user_config_dir

_LOCK_FILENAME = ".contribution-nudge-lock"
_STALE_THRESHOLD_SECONDS = 24 * 60 * 60


def _nudge_lock_path() -> Path:
    """Return the path to the contribution nudge lock file."""
    return _user_config_dir() / "SuperchargeAI" / _LOCK_FILENAME


def _check_gh_available() -> bool:
    """Return True if the gh CLI is available on PATH."""
    return shutil.which("gh") is not None


def _scan_contribution_candidates(methodology_dir: Path) -> int:
    """Count methodology memory files flagged with contribution_candidate: true.

    Reads YAML frontmatter from each .md file in methodology_dir and counts
    those where the frontmatter contains the line 'contribution_candidate: true'.
    Returns 0 if methodology_dir does not exist or is not a directory.
    """
    if not methodology_dir.is_dir():
        return 0

    count = 0
    for md_file in methodology_dir.rglob("*.md"):
        fm = _read_frontmatter(md_file)
        if fm.get("contribution_candidate", "").lower().strip() == "true":
            count += 1

    return count


def _try_claim_nudge_lock(session_id: str) -> bool:
    """Atomically claim the nudge lock for this session.

    Uses O_CREAT | O_EXCL for atomic creation. If the lock already exists,
    checks the timestamp: if stale (>= 24h), deletes it and retries once.

    Returns True if the lock was successfully created, False otherwise.
    """
    lock_path = _nudge_lock_path()
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False

    payload = json.dumps({"session_id": session_id, "timestamp": time.time()})

    for _attempt in range(2):
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            # Lock exists — check if it's stale
            try:
                data = json.loads(lock_path.read_text())
                age = time.time() - float(data.get("timestamp", time.time()))
                if age >= _STALE_THRESHOLD_SECONDS:
                    try:
                        lock_path.unlink()
                    except OSError:
                        return False
                    # Retry once after removing stale lock
                    continue
                else:
                    # Fresh lock held by another session
                    return False
            except (OSError, json.JSONDecodeError, ValueError, KeyError):
                return False
        except OSError:
            return False
        else:
            # Write session info and close
            try:
                os.write(fd, payload.encode())
            finally:
                os.close(fd)
            return True

    return False


def _build_nudge_text(count: int) -> str:
    """Build the nudge text to inject into the orchestrator's additionalContext."""
    return (
        f"You have {count} methodology memories flagged for community contribution. "
        "When appropriate, mention this to the user: they can review with "
        "`supercharge contribute review` or say \"review contributions\" in conversation."
    )


def get_contribution_nudge(session_id: str) -> str | None:
    """Return a nudge string if contribution candidates are pending, else None.

    Main entry point for the SessionStart hook. Returns None on any error
    to ensure the hook never blocks session start.
    """
    try:
        if not _check_gh_available():
            return None

        from supercharge.paths import _user_methodology_dir  # noqa: PLC0415

        methodology_dir = _user_methodology_dir()
        count = _scan_contribution_candidates(methodology_dir)
        if count == 0:
            return None

        if not _try_claim_nudge_lock(session_id):
            return None

        return _build_nudge_text(count)
    except Exception:
        return None


def clear_nudge_lock() -> bool:
    """Remove the nudge lock file so the next batch can trigger a nudge.

    Returns True if the lock was removed, False if it did not exist or
    could not be removed.
    """
    lock_path = _nudge_lock_path()
    try:
        lock_path.unlink()
        return True
    except (FileNotFoundError, OSError):
        return False
