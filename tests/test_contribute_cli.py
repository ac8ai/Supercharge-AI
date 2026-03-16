"""Tests for the contribute CLI commands (list, review)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from supercharge.cli import supercharge


# ── Test helpers ───────────────────────────────────────────────────────────


def _make_candidate(
    title: str = "Test Pattern",
    keywords: list[str] | None = None,
    category: str = "behavior",
    content: str = "# Content\n\nPattern description here.\n",
    path: Path | None = None,
) -> dict:
    """Create a candidate dict matching list_candidates() output."""
    if keywords is None:
        keywords = ["testing", "automation"]
    if path is None:
        path = Path("/fake/methodology/behavior/test-pattern.md")
    return {
        "title": title,
        "keywords": keywords,
        "category": category,
        "content": content,
        "path": path,
    }


# ── contribute list ───────────────────────────────────────────────────────


class TestContributeList:
    """Test `supercharge contribute list` command."""

    def test_no_candidates_shows_message(self):
        runner = CliRunner()
        with patch("supercharge.contribute.list_candidates", return_value=[]), \
             patch("supercharge.paths._user_methodology_dir", return_value=Path("/fake")):
            result = runner.invoke(supercharge, ["contribute", "list"])

        assert result.exit_code == 0
        assert "No contribution candidates found." in result.output

    def test_shows_table_output(self):
        candidates = [
            _make_candidate(title="Pattern A", keywords=["python", "testing"], category="behavior"),
            _make_candidate(title="Pattern B", keywords=["workflow"], category="flows"),
        ]
        runner = CliRunner()
        with patch("supercharge.contribute.list_candidates", return_value=candidates), \
             patch("supercharge.paths._user_methodology_dir", return_value=Path("/fake")):
            result = runner.invoke(supercharge, ["contribute", "list"])

        assert result.exit_code == 0
        assert "Pattern A" in result.output
        assert "Pattern B" in result.output
        assert "behavior" in result.output
        assert "flows" in result.output

    def test_json_output(self):
        candidates = [
            _make_candidate(title="JSON Pattern", keywords=["json", "cli"]),
        ]
        runner = CliRunner()
        with patch("supercharge.contribute.list_candidates", return_value=candidates), \
             patch("supercharge.paths._user_methodology_dir", return_value=Path("/fake")):
            result = runner.invoke(supercharge, ["contribute", "list", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["title"] == "JSON Pattern"
        assert data[0]["keywords"] == ["json", "cli"]
        assert "path" in data[0]


# ── contribute review ─────────────────────────────────────────────────────


class TestContributeReview:
    """Test `supercharge contribute review` command."""

    def test_no_candidates_shows_message(self):
        runner = CliRunner()
        with patch("supercharge.contribute.list_candidates", return_value=[]), \
             patch("supercharge.paths._user_methodology_dir", return_value=Path("/fake")), \
             patch("supercharge.contribute.check_gh_available", return_value=(True, "")):
            result = runner.invoke(supercharge, ["contribute", "review"])

        assert result.exit_code == 0
        assert "No contribution candidates found." in result.output

    def test_gh_check_failure_exits(self):
        runner = CliRunner()
        with patch("supercharge.contribute.check_gh_available",
                    return_value=(False, "gh CLI is not installed. Install from https://cli.github.com/")):
            result = runner.invoke(supercharge, ["contribute", "review"])

        assert result.exit_code != 0
        assert "gh CLI is not installed" in result.output

    def test_interactive_approve(self, tmp_path: Path):
        memory_file = tmp_path / "pattern.md"
        memory_file.write_text("---\ntitle: Test\n---\n\n# Content\n\nContent.\n")
        candidates = [_make_candidate(path=memory_file)]

        runner = CliRunner()
        with patch("supercharge.contribute.list_candidates", return_value=candidates), \
             patch("supercharge.paths._user_methodology_dir", return_value=Path("/fake")), \
             patch("supercharge.contribute.check_gh_available", return_value=(True, "")), \
             patch("supercharge.contribute.submit_contribution",
                    return_value={"action": "created", "issue_url": "https://github.com/test/issues/1", "issue_number": 1}) as mock_submit, \
             patch("supercharge.contribute.mark_submitted") as mock_mark, \
             patch("supercharge.nudge.clear_nudge_lock", return_value=True):
            result = runner.invoke(supercharge, ["contribute", "review"], input="y\n")

        assert result.exit_code == 0
        assert "Approved 1, rejected 0, skipped 0" in result.output
        mock_submit.assert_called_once()
        mock_mark.assert_called_once()

    def test_interactive_reject(self, tmp_path: Path):
        memory_file = tmp_path / "pattern.md"
        memory_file.write_text("---\ntitle: Test\n---\n\n# Content\n\nContent.\n")
        candidates = [_make_candidate(path=memory_file)]

        runner = CliRunner()
        with patch("supercharge.contribute.list_candidates", return_value=candidates), \
             patch("supercharge.paths._user_methodology_dir", return_value=Path("/fake")), \
             patch("supercharge.contribute.check_gh_available", return_value=(True, "")), \
             patch("supercharge.contribute.mark_rejected") as mock_reject, \
             patch("supercharge.nudge.clear_nudge_lock", return_value=True):
            result = runner.invoke(supercharge, ["contribute", "review"], input="n\n")

        assert result.exit_code == 0
        assert "Approved 0, rejected 1, skipped 0" in result.output
        mock_reject.assert_called_once()

    def test_interactive_skip(self, tmp_path: Path):
        memory_file = tmp_path / "pattern.md"
        memory_file.write_text("---\ntitle: Test\n---\n\n# Content\n\nContent.\n")
        candidates = [_make_candidate(path=memory_file)]

        runner = CliRunner()
        with patch("supercharge.contribute.list_candidates", return_value=candidates), \
             patch("supercharge.paths._user_methodology_dir", return_value=Path("/fake")), \
             patch("supercharge.contribute.check_gh_available", return_value=(True, "")):
            result = runner.invoke(supercharge, ["contribute", "review"], input="s\n")

        assert result.exit_code == 0
        assert "Approved 0, rejected 0, skipped 1" in result.output

    def test_accept_all_flag(self, tmp_path: Path):
        files = []
        candidates = []
        for i in range(3):
            f = tmp_path / f"pattern-{i}.md"
            f.write_text(f"---\ntitle: Pattern {i}\n---\n\n# Content\n\nContent {i}.\n")
            files.append(f)
            candidates.append(_make_candidate(title=f"Pattern {i}", path=f))

        runner = CliRunner()
        with patch("supercharge.contribute.list_candidates", return_value=candidates), \
             patch("supercharge.paths._user_methodology_dir", return_value=Path("/fake")), \
             patch("supercharge.contribute.check_gh_available", return_value=(True, "")), \
             patch("supercharge.contribute.submit_contribution",
                    return_value={"action": "created", "issue_url": "https://github.com/test/issues/1", "issue_number": 1}) as mock_submit, \
             patch("supercharge.contribute.mark_submitted") as mock_mark, \
             patch("supercharge.nudge.clear_nudge_lock", return_value=True):
            result = runner.invoke(supercharge, ["contribute", "review", "--accept-all"])

        assert result.exit_code == 0
        assert "Approved 3, rejected 0, skipped 0" in result.output
        assert mock_submit.call_count == 3
        assert mock_mark.call_count == 3

    def test_dry_run_flag(self, tmp_path: Path):
        memory_file = tmp_path / "pattern.md"
        memory_file.write_text("---\ntitle: Test\n---\n\n# Content\n\nContent.\n")
        candidates = [_make_candidate(title="Dry Run Pattern", path=memory_file)]

        runner = CliRunner()
        with patch("supercharge.contribute.list_candidates", return_value=candidates), \
             patch("supercharge.paths._user_methodology_dir", return_value=Path("/fake")), \
             patch("supercharge.contribute.submit_contribution") as mock_submit, \
             patch("supercharge.contribute.mark_submitted") as mock_mark:
            result = runner.invoke(supercharge, ["contribute", "review", "--dry-run"], input="y\n")

        assert result.exit_code == 0
        assert "[dry-run] Would submit: Dry Run Pattern" in result.output
        assert "Approved 1" in result.output
        # Should NOT actually submit or mark
        mock_submit.assert_not_called()
        mock_mark.assert_not_called()

    def test_dry_run_skips_gh_check(self):
        """Dry run should not require gh to be available."""
        runner = CliRunner()
        with patch("supercharge.contribute.list_candidates", return_value=[]), \
             patch("supercharge.paths._user_methodology_dir", return_value=Path("/fake")), \
             patch("supercharge.contribute.check_gh_available") as mock_check:
            result = runner.invoke(supercharge, ["contribute", "review", "--dry-run"])

        assert result.exit_code == 0
        # check_gh_available should NOT be called in dry-run mode
        mock_check.assert_not_called()

    def test_dry_run_reject_does_not_mark(self, tmp_path: Path):
        memory_file = tmp_path / "pattern.md"
        memory_file.write_text("---\ntitle: Test\n---\n\n# Content\n\nContent.\n")
        candidates = [_make_candidate(path=memory_file)]

        runner = CliRunner()
        with patch("supercharge.contribute.list_candidates", return_value=candidates), \
             patch("supercharge.paths._user_methodology_dir", return_value=Path("/fake")), \
             patch("supercharge.contribute.mark_rejected") as mock_reject:
            result = runner.invoke(supercharge, ["contribute", "review", "--dry-run"], input="n\n")

        assert result.exit_code == 0
        mock_reject.assert_not_called()

    def test_nudge_lock_cleared_after_approve(self, tmp_path: Path):
        memory_file = tmp_path / "pattern.md"
        memory_file.write_text("---\ntitle: Test\n---\n\n# Content\n\nContent.\n")
        candidates = [_make_candidate(path=memory_file)]

        runner = CliRunner()
        with patch("supercharge.contribute.list_candidates", return_value=candidates), \
             patch("supercharge.paths._user_methodology_dir", return_value=Path("/fake")), \
             patch("supercharge.contribute.check_gh_available", return_value=(True, "")), \
             patch("supercharge.contribute.submit_contribution",
                    return_value={"action": "created", "issue_url": "https://github.com/test/issues/1", "issue_number": 1}), \
             patch("supercharge.contribute.mark_submitted"), \
             patch("supercharge.nudge.clear_nudge_lock", return_value=True) as mock_clear:
            result = runner.invoke(supercharge, ["contribute", "review"], input="y\n")

        assert result.exit_code == 0
        mock_clear.assert_called_once()

    def test_nudge_lock_not_cleared_when_only_skipped(self, tmp_path: Path):
        memory_file = tmp_path / "pattern.md"
        memory_file.write_text("---\ntitle: Test\n---\n\n# Content\n\nContent.\n")
        candidates = [_make_candidate(path=memory_file)]

        runner = CliRunner()
        with patch("supercharge.contribute.list_candidates", return_value=candidates), \
             patch("supercharge.paths._user_methodology_dir", return_value=Path("/fake")), \
             patch("supercharge.contribute.check_gh_available", return_value=(True, "")), \
             patch("supercharge.nudge.clear_nudge_lock") as mock_clear:
            result = runner.invoke(supercharge, ["contribute", "review"], input="s\n")

        assert result.exit_code == 0
        mock_clear.assert_not_called()

    def test_accept_all_with_dry_run(self, tmp_path: Path):
        """Accept-all + dry-run should show all as would-submit without actual submission."""
        files = []
        candidates = []
        for i in range(2):
            f = tmp_path / f"pattern-{i}.md"
            f.write_text(f"---\ntitle: Pattern {i}\n---\n\n# Content\n\nContent.\n")
            files.append(f)
            candidates.append(_make_candidate(title=f"Pattern {i}", path=f))

        runner = CliRunner()
        with patch("supercharge.contribute.list_candidates", return_value=candidates), \
             patch("supercharge.paths._user_methodology_dir", return_value=Path("/fake")), \
             patch("supercharge.contribute.submit_contribution") as mock_submit:
            result = runner.invoke(supercharge, ["contribute", "review", "--accept-all", "--dry-run"])

        assert result.exit_code == 0
        assert "[dry-run] Would submit: Pattern 0" in result.output
        assert "[dry-run] Would submit: Pattern 1" in result.output
        assert "Approved 2, rejected 0, skipped 0" in result.output
        mock_submit.assert_not_called()
