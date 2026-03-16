"""Tests for the community contribution pipeline module."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from supercharge.contribute import (
    _format_comment_body,
    _format_new_issue_body,
    check_gh_available,
    find_similar_issues,
    keyword_similarity,
    list_candidates,
    mark_rejected,
    mark_submitted,
    strip_context,
    submit_contribution,
)


# ── Test helpers ───────────────────────────────────────────────────────────


def _write_memory_file(
    path: Path,
    title: str = "Test Memory",
    keywords: list[str] | None = None,
    contribution_candidate: bool = True,
    contribution_status: str | None = None,
    contribution_url: str | None = None,
    body: str = "# Content\n\nMemory content here.\n",
) -> None:
    """Write a memory markdown file with simple YAML frontmatter."""
    if keywords is None:
        keywords = ["testing", "automation"]
    kw_str = ", ".join(keywords)
    lines = [
        "---",
        f"title: {title}",
        f"keywords: [{kw_str}]",
        f"contribution_candidate: {'true' if contribution_candidate else 'false'}",
    ]
    if contribution_status is not None:
        lines.append(f"contribution_status: {contribution_status}")
    if contribution_url is not None:
        lines.append(f"contribution_url: {contribution_url}")
    lines.extend(["---", "", body])
    path.write_text("\n".join(lines))


# ── strip_context ──────────────────────────────────────────────────────────


class TestStripContext:
    """Test context stripping from memory file content."""

    def test_removes_notes_section(self):
        content = "# Content\n\nSome content.\n\n# Notes\n\nPrivate notes here.\n"
        result = strip_context(content)
        assert "# Notes" not in result
        assert "Private notes here" not in result
        assert "Some content." in result

    def test_removes_notes_with_subsections(self):
        content = (
            "# Content\n\nPublic info.\n\n"
            "# Notes\n\n## My notes\n\nPrivate.\n\n### Sub\n\nAlso private.\n"
        )
        result = strip_context(content)
        assert "# Notes" not in result
        assert "## My notes" not in result
        assert "Also private" not in result
        assert "Public info." in result

    def test_removes_source_lines(self):
        content = (
            "# Content\n\nSome info.\n"
            "Source: /home/user/project/file.md\n"
            "More info.\n"
        )
        result = strip_context(content)
        assert "Source:" not in result
        assert "Some info." in result
        assert "More info." in result

    def test_removes_source_line_at_start(self):
        content = "Source: internal docs\nSome useful text.\n"
        result = strip_context(content)
        assert "Source:" not in result
        assert "Some useful text." in result

    def test_replaces_home_paths(self):
        content = "See file at /home/username/projects/myapp/config.py for details."
        result = strip_context(content)
        assert "/home/username" not in result
        assert "<path>" in result

    def test_replaces_users_paths(self):
        content = "Config is at /Users/john/Documents/project/settings.json."
        result = strip_context(content)
        assert "/Users/john" not in result
        assert "<path>" in result

    def test_replaces_tilde_paths(self):
        content = "Run ~/scripts/deploy.sh to deploy."
        result = strip_context(content)
        assert "~/" not in result
        assert "<path>" in result

    def test_replaces_workspaces_paths(self):
        content = "The module is at /workspaces/MyProject/src/module.py."
        result = strip_context(content)
        assert "/workspaces/MyProject" not in result
        assert "<path>" in result

    def test_replaces_full_uuid(self):
        content = "Task ID: a1b2c3d4-e5f6-7890-abcd-ef1234567890 was the culprit."
        result = strip_context(content)
        assert "a1b2c3d4-e5f6-7890-abcd-ef1234567890" not in result
        assert "<uuid>" in result

    def test_replaces_multiple_uuids(self):
        content = (
            "First: a1b2c3d4-e5f6-7890-abcd-ef1234567890, "
            "second: 00000000-1111-2222-3333-444444444444."
        )
        result = strip_context(content)
        assert "a1b2c3d4-e5f6-7890-abcd-ef1234567890" not in result
        assert "00000000-1111-2222-3333-444444444444" not in result
        assert result.count("<uuid>") == 2

    def test_replaces_task_references(self):
        content = "Spawned by task:abc12345 for processing."
        result = strip_context(content)
        assert "task:abc12345" not in result
        assert "<reference>" in result

    def test_replaces_worker_references(self):
        content = "Created by worker:def67890 at startup."
        result = strip_context(content)
        assert "worker:def67890" not in result
        assert "<reference>" in result

    def test_replaces_longer_task_references(self):
        content = "See task:abcdef01 and worker:12345678 for context."
        result = strip_context(content)
        assert "task:abcdef01" not in result
        assert "worker:12345678" not in result
        assert result.count("<reference>") == 2

    def test_strips_leading_trailing_whitespace(self):
        content = "\n\n  Some content.  \n\n"
        result = strip_context(content)
        assert result == result.strip()
        assert "Some content." in result

    def test_combined_stripping(self):
        content = (
            "# Content\n\n"
            "Pattern found in /home/user/project/src/module.py.\n"
            "Task task:abc12345 triggered this.\n"
            "Source: internal documentation\n"
            "UUID: a1b2c3d4-e5f6-7890-abcd-ef1234567890\n\n"
            "# Notes\n\n"
            "## Private notes\n"
            "This should be removed.\n"
        )
        result = strip_context(content)
        assert "/home/user" not in result
        assert "task:abc12345" not in result
        assert "Source:" not in result
        assert "a1b2c3d4-e5f6-7890-abcd-ef1234567890" not in result
        assert "Private notes" not in result
        assert "<path>" in result
        assert "<reference>" in result
        assert "<uuid>" in result

    def test_empty_string_returns_empty(self):
        assert strip_context("") == ""

    def test_no_sensitive_data_preserved(self):
        content = "# Content\n\nThis is generic content with no sensitive data.\n"
        result = strip_context(content)
        assert "generic content" in result

    def test_notes_section_only_removed_not_earlier_hashes(self):
        content = "## Some section\n\nPublic.\n\n# Notes\n\nPrivate.\n"
        result = strip_context(content)
        assert "## Some section" in result
        assert "Public." in result
        assert "Private." not in result


# ── keyword_similarity ─────────────────────────────────────────────────────


class TestKeywordSimilarity:
    """Test Jaccard similarity between keyword lists."""

    def test_identical_lists_returns_1(self):
        keywords = ["python", "testing", "automation"]
        assert keyword_similarity(keywords, list(keywords)) == 1.0

    def test_disjoint_lists_returns_0(self):
        a = ["python", "testing"]
        b = ["javascript", "frontend"]
        assert keyword_similarity(a, b) == 0.0

    def test_partial_overlap_correct_jaccard(self):
        a = ["python", "testing", "automation"]
        b = ["python", "testing", "javascript"]
        # intersection: {python, testing} = 2
        # union: {python, testing, automation, javascript} = 4
        # Jaccard: 2/4 = 0.5
        assert keyword_similarity(a, b) == pytest.approx(0.5)

    def test_both_empty_returns_0(self):
        assert keyword_similarity([], []) == 0.0

    def test_first_empty_returns_0(self):
        assert keyword_similarity([], ["python", "testing"]) == 0.0

    def test_second_empty_returns_0(self):
        assert keyword_similarity(["python", "testing"], []) == 0.0

    def test_duplicates_treated_as_set(self):
        # Duplicates should not inflate intersection or union
        a = ["python", "python", "testing"]
        b = ["python", "testing", "testing"]
        # As sets: a = {python, testing}, b = {python, testing} -> 1.0
        assert keyword_similarity(a, b) == 1.0

    def test_single_match(self):
        assert keyword_similarity(["python"], ["python"]) == 1.0

    def test_single_no_match(self):
        assert keyword_similarity(["python"], ["javascript"]) == 0.0

    def test_one_element_overlap(self):
        a = ["python", "testing"]
        b = ["python", "docs"]
        # intersection: {python} = 1, union: {python, testing, docs} = 3
        assert keyword_similarity(a, b) == pytest.approx(1 / 3)


# ── list_candidates ────────────────────────────────────────────────────────


class TestListCandidates:
    """Test scanning methodology directory for contribution candidates."""

    def test_finds_candidate_file(self, tmp_path: Path):
        behavior_dir = tmp_path / "behavior"
        behavior_dir.mkdir()
        f = behavior_dir / "my-pattern.md"
        _write_memory_file(f, title="My Pattern", keywords=["pattern", "agents"])

        results = list_candidates(tmp_path)
        assert len(results) == 1
        assert results[0]["title"] == "My Pattern"

    def test_skips_file_without_candidate_key(self, tmp_path: Path):
        behavior_dir = tmp_path / "behavior"
        behavior_dir.mkdir()
        f = behavior_dir / "no-candidate.md"
        f.write_text(
            "---\ntitle: No Candidate\nkeywords: [python]\n---\n\n# Content\n\nContent.\n"
        )

        results = list_candidates(tmp_path)
        assert results == []

    def test_skips_candidate_false(self, tmp_path: Path):
        behavior_dir = tmp_path / "behavior"
        behavior_dir.mkdir()
        f = behavior_dir / "rejected.md"
        _write_memory_file(f, contribution_candidate=False)

        results = list_candidates(tmp_path)
        assert results == []

    def test_skips_submitted_files(self, tmp_path: Path):
        behavior_dir = tmp_path / "behavior"
        behavior_dir.mkdir()
        f = behavior_dir / "submitted.md"
        _write_memory_file(
            f,
            contribution_candidate=True,
            contribution_status="submitted",
            contribution_url="https://github.com/ac8ai/Supercharge-AI/issues/42",
        )

        results = list_candidates(tmp_path)
        assert results == []

    def test_scans_behavior_subdir(self, tmp_path: Path):
        behavior_dir = tmp_path / "behavior"
        behavior_dir.mkdir()
        f = behavior_dir / "behavior-pattern.md"
        _write_memory_file(f, title="Behavior Pattern")

        results = list_candidates(tmp_path)
        assert len(results) == 1
        assert results[0]["category"] == "behavior"

    def test_scans_flows_subdir(self, tmp_path: Path):
        flows_dir = tmp_path / "flows"
        flows_dir.mkdir()
        f = flows_dir / "flow-pattern.md"
        _write_memory_file(f, title="Flow Pattern")

        results = list_candidates(tmp_path)
        assert len(results) == 1
        assert results[0]["category"] == "flows"

    def test_returns_correct_fields(self, tmp_path: Path):
        behavior_dir = tmp_path / "behavior"
        behavior_dir.mkdir()
        f = behavior_dir / "my-pattern.md"
        _write_memory_file(
            f,
            title="My Pattern",
            keywords=["pattern", "agents", "workflow"],
        )

        results = list_candidates(tmp_path)
        assert len(results) == 1
        r = results[0]
        assert r["title"] == "My Pattern"
        assert set(r["keywords"]) == {"pattern", "agents", "workflow"}
        assert r["category"] == "behavior"
        assert r["path"] == f
        assert isinstance(r["content"], str)
        assert "content" in r

    def test_returns_content_field(self, tmp_path: Path):
        behavior_dir = tmp_path / "behavior"
        behavior_dir.mkdir()
        f = behavior_dir / "pattern.md"
        _write_memory_file(
            f,
            title="Pattern",
            body="# Content\n\nSpecific pattern details here.\n",
        )

        results = list_candidates(tmp_path)
        assert len(results) == 1
        assert "Specific pattern details here." in results[0]["content"]

    def test_empty_dir_returns_empty(self, tmp_path: Path):
        assert list_candidates(tmp_path) == []

    def test_both_subdirs_scanned(self, tmp_path: Path):
        for subdir in ("behavior", "flows"):
            d = tmp_path / subdir
            d.mkdir()
            f = d / f"{subdir}-pattern.md"
            _write_memory_file(f, title=f"{subdir.capitalize()} Pattern")

        results = list_candidates(tmp_path)
        assert len(results) == 2
        categories = {r["category"] for r in results}
        assert categories == {"behavior", "flows"}

    def test_multiple_candidates_in_subdir(self, tmp_path: Path):
        behavior_dir = tmp_path / "behavior"
        behavior_dir.mkdir()
        for i in range(3):
            f = behavior_dir / f"pattern-{i}.md"
            _write_memory_file(f, title=f"Pattern {i}")

        results = list_candidates(tmp_path)
        assert len(results) == 3

    def test_mixes_candidates_and_non_candidates(self, tmp_path: Path):
        behavior_dir = tmp_path / "behavior"
        behavior_dir.mkdir()

        _write_memory_file(behavior_dir / "candidate.md", title="Candidate")
        (behavior_dir / "non-candidate.md").write_text(
            "---\ntitle: Non Candidate\n---\n\n# Content\n\nContent.\n"
        )

        results = list_candidates(tmp_path)
        assert len(results) == 1
        assert results[0]["title"] == "Candidate"


# ── Issue formatting ───────────────────────────────────────────────────────


class TestFormatNewIssueBody:
    """Test new issue body formatting."""

    def test_contains_memory_contribution_header(self):
        body = _format_new_issue_body(
            title="Test",
            keywords=["kw"],
            category="behavior",
            content="Content.",
            version="1.0.0",
        )
        assert "## Memory Contribution" in body

    def test_contains_title(self):
        body = _format_new_issue_body(
            title="My Pattern",
            keywords=["python"],
            category="behavior",
            content="Content.",
            version="1.0.0",
        )
        assert "**Title:** My Pattern" in body

    def test_contains_keywords(self):
        body = _format_new_issue_body(
            title="Test",
            keywords=["python", "testing", "automation"],
            category="behavior",
            content="Content.",
            version="1.0.0",
        )
        assert "**Keywords:**" in body
        assert "python" in body
        assert "testing" in body
        assert "automation" in body

    def test_contains_category(self):
        body = _format_new_issue_body(
            title="Test",
            keywords=["kw"],
            category="behavior",
            content="Content.",
            version="1.0.0",
        )
        assert "**Category:** behavior" in body

    def test_contains_content_section(self):
        body = _format_new_issue_body(
            title="Test",
            keywords=["kw"],
            category="flows",
            content="Pattern description here.",
            version="0.1.0",
        )
        assert "### Content" in body
        assert "Pattern description here." in body

    def test_contains_version_footer(self):
        body = _format_new_issue_body(
            title="Test",
            keywords=["kw"],
            category="behavior",
            content="Content.",
            version="2.5.0",
        )
        assert "SuperchargeAI" in body
        assert "v2.5.0" in body

    def test_keywords_comma_separated(self):
        body = _format_new_issue_body(
            title="Test",
            keywords=["alpha", "beta", "gamma"],
            category="behavior",
            content="Content.",
            version="1.0.0",
        )
        # All keywords present; they should appear comma-separated
        kw_line = next(
            (line for line in body.splitlines() if "**Keywords:**" in line), ""
        )
        assert "alpha" in kw_line
        assert "beta" in kw_line
        assert "gamma" in kw_line

    def test_flows_category(self):
        body = _format_new_issue_body(
            title="Test",
            keywords=["kw"],
            category="flows",
            content="Content.",
            version="1.0.0",
        )
        assert "**Category:** flows" in body


class TestFormatCommentBody:
    """Test comment body formatting."""

    def test_contains_another_user_header(self):
        body = _format_comment_body(
            title="Test",
            keywords=["kw"],
            content="Content.",
            version="1.0.0",
        )
        assert "Another user encountered this pattern" in body

    def test_contains_title(self):
        body = _format_comment_body(
            title="My Pattern",
            keywords=["python"],
            content="Content.",
            version="1.0.0",
        )
        assert "**Title:** My Pattern" in body

    def test_contains_keywords(self):
        body = _format_comment_body(
            title="Test",
            keywords=["python", "testing"],
            content="Content.",
            version="1.0.0",
        )
        assert "**Keywords:**" in body
        assert "python" in body
        assert "testing" in body

    def test_contains_content_section(self):
        body = _format_comment_body(
            title="Test",
            keywords=["kw"],
            content="Pattern details here.",
            version="0.1.0",
        )
        assert "### Content" in body
        assert "Pattern details here." in body

    def test_contains_version_footer(self):
        body = _format_comment_body(
            title="Test",
            keywords=["kw"],
            content="Content.",
            version="3.1.4",
        )
        assert "SuperchargeAI" in body
        assert "v3.1.4" in body

    def test_no_category_field(self):
        # Comment bodies do not include a Category field
        body = _format_comment_body(
            title="Test",
            keywords=["kw"],
            content="Content.",
            version="1.0.0",
        )
        assert "**Category:**" not in body


# ── mark_submitted / mark_rejected ────────────────────────────────────────


class TestMarkSubmitted:
    """Test marking a memory file as submitted."""

    def test_sets_contribution_status_submitted(self, tmp_path: Path):
        f = tmp_path / "memory.md"
        _write_memory_file(f, title="Test")

        mark_submitted(f, "https://github.com/ac8ai/Supercharge-AI/issues/42")

        content = f.read_text()
        assert "contribution_status" in content
        assert "submitted" in content

    def test_sets_contribution_url(self, tmp_path: Path):
        f = tmp_path / "memory.md"
        _write_memory_file(f, title="Test")
        issue_url = "https://github.com/ac8ai/Supercharge-AI/issues/42"

        mark_submitted(f, issue_url)

        content = f.read_text()
        assert issue_url in content

    def test_preserves_title_in_frontmatter(self, tmp_path: Path):
        f = tmp_path / "memory.md"
        _write_memory_file(f, title="My Memory", keywords=["python", "testing"])

        mark_submitted(f, "https://github.com/ac8ai/Supercharge-AI/issues/1")

        content = f.read_text()
        assert "My Memory" in content

    def test_preserves_keywords_in_frontmatter(self, tmp_path: Path):
        f = tmp_path / "memory.md"
        _write_memory_file(f, title="Test", keywords=["python", "testing"])

        mark_submitted(f, "https://github.com/ac8ai/Supercharge-AI/issues/1")

        content = f.read_text()
        assert "python" in content
        assert "testing" in content

    def test_preserves_body_content(self, tmp_path: Path):
        f = tmp_path / "memory.md"
        _write_memory_file(
            f, title="Test", body="# Content\n\nThis is the body content.\n"
        )

        mark_submitted(f, "https://github.com/ac8ai/Supercharge-AI/issues/1")

        content = f.read_text()
        assert "This is the body content." in content

    def test_file_still_has_frontmatter_delimiters(self, tmp_path: Path):
        f = tmp_path / "memory.md"
        _write_memory_file(f, title="Test")

        mark_submitted(f, "https://github.com/ac8ai/Supercharge-AI/issues/1")

        content = f.read_text()
        assert content.count("---") >= 2


class TestMarkRejected:
    """Test marking a memory file as rejected (not a candidate)."""

    def test_sets_contribution_candidate_false(self, tmp_path: Path):
        f = tmp_path / "memory.md"
        _write_memory_file(f, title="Test", contribution_candidate=True)

        mark_rejected(f)

        content = f.read_text()
        assert "contribution_candidate" in content
        assert "false" in content

    def test_preserves_title(self, tmp_path: Path):
        f = tmp_path / "memory.md"
        _write_memory_file(f, title="My Memory", keywords=["agents"])

        mark_rejected(f)

        content = f.read_text()
        assert "My Memory" in content

    def test_preserves_keywords(self, tmp_path: Path):
        f = tmp_path / "memory.md"
        _write_memory_file(f, title="Test", keywords=["agents", "workflow"])

        mark_rejected(f)

        content = f.read_text()
        assert "agents" in content
        assert "workflow" in content

    def test_preserves_body_content(self, tmp_path: Path):
        f = tmp_path / "memory.md"
        _write_memory_file(
            f, title="Test", body="# Content\n\nImportant body content.\n"
        )

        mark_rejected(f)

        content = f.read_text()
        assert "Important body content." in content

    def test_file_still_has_frontmatter_delimiters(self, tmp_path: Path):
        f = tmp_path / "memory.md"
        _write_memory_file(f, title="Test")

        mark_rejected(f)

        content = f.read_text()
        assert content.count("---") >= 2


# ── check_gh_available ─────────────────────────────────────────────────────


class TestCheckGhAvailable:
    """Test GitHub CLI availability and authentication check."""

    def test_available_and_authenticated_returns_true(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Logged in to github.com\n"
        mock_result.stderr = ""

        with patch("supercharge.contribute.subprocess.run", return_value=mock_result):
            available, msg = check_gh_available()

        assert available is True
        assert msg == ""

    def test_file_not_found_returns_false_with_message(self):
        with patch(
            "supercharge.contribute.subprocess.run",
            side_effect=FileNotFoundError("No such file or directory: 'gh'"),
        ):
            available, msg = check_gh_available()

        assert available is False
        assert isinstance(msg, str)
        assert len(msg) > 0

    def test_nonzero_returncode_returns_false_with_message(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "You are not logged into any GitHub hosts"

        with patch("supercharge.contribute.subprocess.run", return_value=mock_result):
            available, msg = check_gh_available()

        assert available is False
        assert isinstance(msg, str)
        assert len(msg) > 0

    def test_error_message_mentions_auth(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "not authenticated"

        with patch("supercharge.contribute.subprocess.run", return_value=mock_result):
            available, msg = check_gh_available()

        assert available is False
        # Error message should indicate something about gh or authentication
        assert msg  # non-empty


# ── find_similar_issues ────────────────────────────────────────────────────


class TestFindSimilarIssues:
    """Test finding similar GitHub Issues by keyword similarity."""

    def test_empty_issue_list_returns_empty(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "[]"

        with patch("supercharge.contribute.subprocess.run", return_value=mock_result):
            results = find_similar_issues(["python", "testing"], "ac8ai/Supercharge-AI")

        assert results == []

    def test_similar_issue_included_with_score(self):
        issue_body = (
            "## Memory Contribution\n\n"
            "**Title:** Similar Pattern\n"
            "**Keywords:** python, testing, automation\n"
            "**Category:** behavior\n\n"
            "### Content\nContent here.\n"
        )
        issues_json = json.dumps([
            {
                "number": 42,
                "url": "https://github.com/ac8ai/Supercharge-AI/issues/42",
                "title": "[Community Memory] Similar Pattern",
                "body": issue_body,
            }
        ])
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = issues_json

        with patch("supercharge.contribute.subprocess.run", return_value=mock_result):
            results = find_similar_issues(
                ["python", "testing", "automation"], "ac8ai/Supercharge-AI"
            )

        assert len(results) == 1
        assert results[0]["number"] == 42
        assert "similarity" in results[0]
        assert results[0]["similarity"] >= 0.5

    def test_dissimilar_issue_filtered_out(self):
        issue_body = (
            "## Memory Contribution\n\n"
            "**Title:** Unrelated Pattern\n"
            "**Keywords:** javascript, frontend, react\n"
            "**Category:** behavior\n\n"
            "### Content\nContent here.\n"
        )
        issues_json = json.dumps([
            {
                "number": 99,
                "url": "https://github.com/ac8ai/Supercharge-AI/issues/99",
                "title": "[Community Memory] Unrelated Pattern",
                "body": issue_body,
            }
        ])
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = issues_json

        with patch("supercharge.contribute.subprocess.run", return_value=mock_result):
            results = find_similar_issues(
                ["python", "testing", "automation"], "ac8ai/Supercharge-AI"
            )

        assert results == []

    def test_filters_by_similarity_threshold_0_5(self):
        # One issue with similarity >= 0.5, one below threshold
        body_similar = (
            "## Memory Contribution\n\n"
            "**Title:** Similar\n"
            "**Keywords:** python, testing\n\n"
            "### Content\nContent.\n"
        )
        body_dissimilar = (
            "## Memory Contribution\n\n"
            "**Title:** Dissimilar\n"
            "**Keywords:** javascript\n\n"
            "### Content\nContent.\n"
        )
        issues_json = json.dumps([
            {
                "number": 1,
                "url": "https://github.com/ac8ai/Supercharge-AI/issues/1",
                "title": "[Community Memory] Similar",
                "body": body_similar,
            },
            {
                "number": 2,
                "url": "https://github.com/ac8ai/Supercharge-AI/issues/2",
                "title": "[Community Memory] Dissimilar",
                "body": body_dissimilar,
            },
        ])
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = issues_json

        with patch("supercharge.contribute.subprocess.run", return_value=mock_result):
            results = find_similar_issues(["python", "testing"], "ac8ai/Supercharge-AI")

        assert len(results) == 1
        assert results[0]["number"] == 1

    def test_result_contains_required_fields(self):
        issue_body = (
            "## Memory Contribution\n\n"
            "**Title:** Pattern\n"
            "**Keywords:** python, testing\n\n"
            "### Content\nContent.\n"
        )
        issues_json = json.dumps([
            {
                "number": 7,
                "url": "https://github.com/ac8ai/Supercharge-AI/issues/7",
                "title": "[Community Memory] Pattern",
                "body": issue_body,
            }
        ])
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = issues_json

        with patch("supercharge.contribute.subprocess.run", return_value=mock_result):
            results = find_similar_issues(["python", "testing"], "ac8ai/Supercharge-AI")

        assert len(results) == 1
        r = results[0]
        assert "number" in r
        assert "url" in r
        assert "similarity" in r


# ── submit_contribution ────────────────────────────────────────────────────


class TestSubmitContribution:
    """Test full contribution submission pipeline."""

    def _make_candidate_file(
        self, tmp_path: Path, keywords: list[str] | None = None
    ) -> Path:
        """Create a ready-to-submit memory file."""
        if keywords is None:
            keywords = ["python", "testing", "automation"]
        f = tmp_path / "my-pattern.md"
        _write_memory_file(
            f,
            title="My Pattern",
            keywords=keywords,
            body="# Content\n\nPattern description here.\n",
        )
        return f

    def test_creates_new_issue_when_no_similar(self, tmp_path: Path):
        memory_file = self._make_candidate_file(tmp_path)

        with patch("supercharge.contribute.subprocess.run") as mock_run:
            mock_run.side_effect = [
                # First call: gh issue list -> no similar issues
                MagicMock(returncode=0, stdout="[]", stderr=""),
                # Second call: gh issue create -> returns URL
                MagicMock(
                    returncode=0,
                    stdout="https://github.com/ac8ai/Supercharge-AI/issues/42\n",
                    stderr="",
                ),
            ]

            result = submit_contribution(memory_file, repo="ac8ai/Supercharge-AI")

        assert result["action"] == "created"
        assert "issue_url" in result
        assert "issue_number" in result
        assert result["issue_number"] == 42

    def test_comments_when_similar_issue_found(self, tmp_path: Path):
        # Use keywords that produce similarity >= 0.7 with existing issue
        memory_file = self._make_candidate_file(tmp_path, keywords=["python", "testing"])

        issue_body = (
            "## Memory Contribution\n\n"
            "**Title:** Existing Pattern\n"
            "**Keywords:** python, testing\n"
            "**Category:** behavior\n\n"
            "### Content\nExisting content.\n"
        )

        with patch("supercharge.contribute.subprocess.run") as mock_run:
            mock_run.side_effect = [
                # First call: gh issue list -> returns similar issue (similarity=1.0)
                MagicMock(
                    returncode=0,
                    stdout=json.dumps([
                        {
                            "number": 42,
                            "url": "https://github.com/ac8ai/Supercharge-AI/issues/42",
                            "title": "[Community Memory] Existing Pattern",
                            "body": issue_body,
                        }
                    ]),
                    stderr="",
                ),
                # Second call: gh issue comment -> returns comment URL
                MagicMock(
                    returncode=0,
                    stdout=(
                        "https://github.com/ac8ai/Supercharge-AI/issues/42"
                        "#issuecomment-123\n"
                    ),
                    stderr="",
                ),
            ]

            result = submit_contribution(memory_file, repo="ac8ai/Supercharge-AI")

        assert result["action"] == "commented"
        assert result["issue_number"] == 42
        assert "issue_url" in result

    def test_returns_correct_dict_keys(self, tmp_path: Path):
        memory_file = self._make_candidate_file(tmp_path)

        with patch("supercharge.contribute.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="[]", stderr=""),
                MagicMock(
                    returncode=0,
                    stdout="https://github.com/ac8ai/Supercharge-AI/issues/1\n",
                    stderr="",
                ),
            ]

            result = submit_contribution(memory_file)

        assert set(result.keys()) >= {"action", "issue_url", "issue_number"}

    def test_issue_url_in_result_when_creating(self, tmp_path: Path):
        memory_file = self._make_candidate_file(tmp_path)
        expected_url = "https://github.com/ac8ai/Supercharge-AI/issues/99"

        with patch("supercharge.contribute.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="[]", stderr=""),
                MagicMock(returncode=0, stdout=expected_url + "\n", stderr=""),
            ]

            result = submit_contribution(memory_file, repo="ac8ai/Supercharge-AI")

        assert result["issue_url"] == expected_url
        assert result["issue_number"] == 99

    def test_default_repo_used_when_not_specified(self, tmp_path: Path):
        memory_file = self._make_candidate_file(tmp_path)

        with patch("supercharge.contribute.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="[]", stderr=""),
                MagicMock(
                    returncode=0,
                    stdout="https://github.com/ac8ai/Supercharge-AI/issues/1\n",
                    stderr="",
                ),
            ]
            # Should not raise even without explicit repo param
            result = submit_contribution(memory_file)

        assert result["action"] == "created"
