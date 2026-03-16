"""Tests for supercharge.nudge — contribution nudge feature."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

from supercharge.nudge import (
    _build_nudge_text,
    _check_gh_available,
    _scan_contribution_candidates,
    _try_claim_nudge_lock,
    clear_nudge_lock,
    get_contribution_nudge,
)

# ── _scan_contribution_candidates ───────────────────────────────────────────


class TestScanContributionCandidates:
    """Test _scan_contribution_candidates(methodology_dir: Path) -> int."""

    def test_empty_dir(self, tmp_path: Path) -> None:
        assert _scan_contribution_candidates(tmp_path) == 0

    def test_nonexistent_dir(self, tmp_path: Path) -> None:
        assert _scan_contribution_candidates(tmp_path / "nonexistent") == 0

    def test_counts_candidates(self, tmp_path: Path) -> None:
        candidate = "---\ntitle: Test\ncontribution_candidate: true\n---\n# Content\n"
        non_candidate = "---\ntitle: Other\nkeywords: [foo]\n---\n# Content\n"
        (tmp_path / "a.md").write_text(candidate)
        (tmp_path / "b.md").write_text(candidate)
        (tmp_path / "c.md").write_text(non_candidate)
        assert _scan_contribution_candidates(tmp_path) == 2

    def test_ignores_non_md_files(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text(
            "---\ntitle: Test\ncontribution_candidate: true\n---\n"
        )
        assert _scan_contribution_candidates(tmp_path) == 0

    def test_skips_files_without_frontmatter(self, tmp_path: Path) -> None:
        (tmp_path / "a.md").write_text("# Title\nNo frontmatter here.\n")
        assert _scan_contribution_candidates(tmp_path) == 0


# ── _check_gh_available ──────────────────────────────────────────────────────


class TestCheckGhAvailable:
    """Test _check_gh_available() -> bool."""

    def test_gh_available(self) -> None:
        with patch("shutil.which", return_value="/usr/bin/gh"):
            assert _check_gh_available() is True

    def test_gh_not_available(self) -> None:
        with patch("shutil.which", return_value=None):
            assert _check_gh_available() is False


# ── _try_claim_nudge_lock ────────────────────────────────────────────────────


class TestTryClaimNudgeLock:
    """Test _try_claim_nudge_lock(session_id: str) -> bool.

    All tests patch _nudge_lock_path to a file inside tmp_path so they
    never touch the real user home directory.
    """

    def test_first_claim_succeeds(self, tmp_path: Path) -> None:
        lock_file = tmp_path / ".contribution-nudge-lock"
        with patch("supercharge.nudge._nudge_lock_path", return_value=lock_file):
            result = _try_claim_nudge_lock("session-abc")
        assert result is True
        assert lock_file.exists()
        data = json.loads(lock_file.read_text())
        assert data["session_id"] == "session-abc"

    def test_second_claim_fails(self, tmp_path: Path) -> None:
        lock_file = tmp_path / ".contribution-nudge-lock"
        with patch("supercharge.nudge._nudge_lock_path", return_value=lock_file):
            first = _try_claim_nudge_lock("session-1")
            second = _try_claim_nudge_lock("session-2")
        assert first is True
        assert second is False

    def test_stale_lock_recovered(self, tmp_path: Path) -> None:
        lock_file = tmp_path / ".contribution-nudge-lock"
        stale_payload = json.dumps(
            {"session_id": "old-session", "timestamp": time.time() - 25 * 3600}
        )
        lock_file.write_text(stale_payload)
        with patch("supercharge.nudge._nudge_lock_path", return_value=lock_file):
            result = _try_claim_nudge_lock("new-session")
        assert result is True

    def test_fresh_lock_not_replaced(self, tmp_path: Path) -> None:
        lock_file = tmp_path / ".contribution-nudge-lock"
        fresh_payload = json.dumps(
            {"session_id": "active-session", "timestamp": time.time() - 1}
        )
        lock_file.write_text(fresh_payload)
        with patch("supercharge.nudge._nudge_lock_path", return_value=lock_file):
            result = _try_claim_nudge_lock("new-session")
        assert result is False


# ── _build_nudge_text ────────────────────────────────────────────────────────


class TestBuildNudgeText:
    """Test _build_nudge_text(count: int) -> str."""

    def test_text_format(self) -> None:
        result = _build_nudge_text(3)
        assert "3 methodology memories" in result
        assert "supercharge contribute review" in result


# ── get_contribution_nudge ───────────────────────────────────────────────────


class TestGetContributionNudge:
    """Test get_contribution_nudge(session_id: str) -> str | None."""

    def test_returns_nudge_when_candidates_exist(self, tmp_path: Path) -> None:
        with (
            patch("supercharge.nudge._check_gh_available", return_value=True),
            patch("supercharge.nudge._scan_contribution_candidates", return_value=2),
            patch("supercharge.nudge._try_claim_nudge_lock", return_value=True),
            patch("supercharge.paths._user_methodology_dir", return_value=tmp_path),
        ):
            result = get_contribution_nudge("session-xyz")
        assert result is not None
        assert "2 methodology memories" in result

    def test_returns_none_when_no_gh(self) -> None:
        with patch("supercharge.nudge._check_gh_available", return_value=False):
            result = get_contribution_nudge("session-xyz")
        assert result is None

    def test_returns_none_when_no_candidates(self) -> None:
        with (
            patch("supercharge.nudge._check_gh_available", return_value=True),
            patch("supercharge.nudge._scan_contribution_candidates", return_value=0),
        ):
            result = get_contribution_nudge("session-xyz")
        assert result is None

    def test_returns_none_when_lock_held(self, tmp_path: Path) -> None:
        with (
            patch("supercharge.nudge._check_gh_available", return_value=True),
            patch("supercharge.nudge._scan_contribution_candidates", return_value=2),
            patch("supercharge.nudge._try_claim_nudge_lock", return_value=False),
            patch("supercharge.paths._user_methodology_dir", return_value=tmp_path),
        ):
            result = get_contribution_nudge("session-xyz")
        assert result is None

    def test_swallows_exceptions(self) -> None:
        with patch(
            "supercharge.nudge._check_gh_available", side_effect=RuntimeError("boom")
        ):
            result = get_contribution_nudge("session-xyz")
        assert result is None


# ── clear_nudge_lock ─────────────────────────────────────────────────────────


class TestClearNudgeLock:
    """Test clear_nudge_lock() -> bool."""

    def test_removes_existing_lock(self, tmp_path: Path) -> None:
        lock_file = tmp_path / ".contribution-nudge-lock"
        lock_file.write_text('{"session_id": "x", "timestamp": 0}')
        with patch("supercharge.nudge._nudge_lock_path", return_value=lock_file):
            result = clear_nudge_lock()
        assert result is True
        assert not lock_file.exists()

    def test_returns_false_when_no_lock(self, tmp_path: Path) -> None:
        non_existent = tmp_path / "no-lock-here"
        with patch("supercharge.nudge._nudge_lock_path", return_value=non_existent):
            result = clear_nudge_lock()
        assert result is False
