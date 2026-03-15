"""Tests for archive flow: task archive CLI, stale-task template, memory agent definition."""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from supercharge.cli import supercharge

# ── _archive_root ─────────────────────────────────────────────────────────────


class TestArchiveRoot:
    def test_returns_path_under_project(self):
        from supercharge.paths import _archive_root

        result = _archive_root()
        assert str(result).endswith(".claude/SuperchargeAI/archive")


# ── task archive ──────────────────────────────────────────────────────────────


def _make_task_dir(
    root: Path, agent_type: str, task_uuid: str, *, task_md: str, result_md: str | None = None
) -> Path:
    """Helper to create a task directory with files."""
    task_dir = root / agent_type / task_uuid
    task_dir.mkdir(parents=True)
    (task_dir / "task.md").write_text(task_md)
    if result_md is not None:
        (task_dir / "result.md").write_text(result_md)
    return task_dir


class TestTaskArchive:
    def test_archive_plan_task(self, tmp_path: Path):
        task_uuid = str(uuid.uuid4())
        task_root = tmp_path / "tasks"
        archive_root = tmp_path / "archive"
        _make_task_dir(
            task_root,
            "plan",
            task_uuid,
            task_md="# Plan something\n\nDetails here.\n",
            result_md="# Result\n\n## Report\n\nPlan complete.\n",
        )
        runner = CliRunner()
        with (
            patch("supercharge.cli._task_root", return_value=task_root),
            patch("supercharge.cli._find_task_dir", return_value=task_root / "plan" / task_uuid),
            patch("supercharge.cli._archive_root", return_value=archive_root),
        ):
            result = runner.invoke(supercharge, ["task", "archive", task_uuid])

        assert result.exit_code == 0, result.output
        # Archive file should exist
        archive_files = list(archive_root.glob("*.md"))
        assert len(archive_files) == 1
        content = archive_files[0].read_text()
        assert task_uuid in content
        assert "plan" in content.lower()
        # Original dir should be deleted
        assert not (task_root / "plan" / task_uuid).exists()

    def test_archive_research_task(self, tmp_path: Path):
        task_uuid = str(uuid.uuid4())
        task_root = tmp_path / "tasks"
        archive_root = tmp_path / "archive"
        _make_task_dir(
            task_root,
            "research",
            task_uuid,
            task_md="# Research topic\n\nDetails.\n",
            result_md="# Result\n\n## Report\n\nFindings here.\n",
        )
        runner = CliRunner()
        with (
            patch("supercharge.cli._task_root", return_value=task_root),
            patch("supercharge.cli._find_task_dir", return_value=task_root / "research" / task_uuid),
            patch("supercharge.cli._archive_root", return_value=archive_root),
        ):
            result = runner.invoke(supercharge, ["task", "archive", task_uuid])

        assert result.exit_code == 0, result.output
        archive_files = list(archive_root.glob("*.md"))
        assert len(archive_files) == 1
        assert not (task_root / "research" / task_uuid).exists()

    def test_archive_rejects_code_task(self, tmp_path: Path):
        task_uuid = str(uuid.uuid4())
        task_root = tmp_path / "tasks"
        task_dir = _make_task_dir(
            task_root,
            "code",
            task_uuid,
            task_md="# Code task\n",
            result_md="# Result\n\n## Report\n\nDone.\n",
        )
        runner = CliRunner()
        with (
            patch("supercharge.cli._task_root", return_value=task_root),
            patch("supercharge.cli._find_task_dir", return_value=task_dir),
            patch("supercharge.cli._archive_root", return_value=tmp_path / "archive"),
        ):
            result = runner.invoke(supercharge, ["task", "archive", task_uuid])

        assert "research/plan" in result.output.lower() or "research/plan" in (result.output + getattr(result, 'stderr', '')).lower()
        # Task dir should still exist (not archived)
        assert task_dir.exists()

    def test_archive_missing_result_no_force(self, tmp_path: Path):
        task_uuid = str(uuid.uuid4())
        task_root = tmp_path / "tasks"
        task_dir = _make_task_dir(
            task_root,
            "plan",
            task_uuid,
            task_md="# Plan\n",
            result_md=None,
        )
        runner = CliRunner()
        with (
            patch("supercharge.cli._task_root", return_value=task_root),
            patch("supercharge.cli._find_task_dir", return_value=task_dir),
            patch("supercharge.cli._archive_root", return_value=tmp_path / "archive"),
        ):
            result = runner.invoke(supercharge, ["task", "archive", task_uuid])

        # Should report error about missing result.md
        assert "result.md" in result.output.lower() or result.exit_code != 0
        # Task dir should still exist
        assert task_dir.exists()

    def test_archive_missing_result_with_force(self, tmp_path: Path):
        task_uuid = str(uuid.uuid4())
        task_root = tmp_path / "tasks"
        task_dir = _make_task_dir(
            task_root,
            "plan",
            task_uuid,
            task_md="# Plan forced\n\nContent.\n",
            result_md=None,
        )
        runner = CliRunner()
        with (
            patch("supercharge.cli._task_root", return_value=task_root),
            patch("supercharge.cli._find_task_dir", return_value=task_dir),
            patch("supercharge.cli._archive_root", return_value=tmp_path / "archive"),
        ):
            result = runner.invoke(supercharge, ["task", "archive", "--force", task_uuid])

        assert result.exit_code == 0, result.output
        archive_files = list((tmp_path / "archive").glob("*.md"))
        assert len(archive_files) == 1
        content = archive_files[0].read_text()
        assert "Plan forced" in content
        # Original deleted
        assert not task_dir.exists()

    def test_archive_extracts_report_section(self, tmp_path: Path):
        task_uuid = str(uuid.uuid4())
        task_root = tmp_path / "tasks"
        result_md = (
            "# Result\n\n"
            "## Report\n\n"
            "Important findings here.\n\n"
            "## Memory\n\n"
            "### Code\n\nInternal notes.\n"
        )
        task_dir = _make_task_dir(
            task_root,
            "research",
            task_uuid,
            task_md="# Research\n",
            result_md=result_md,
        )
        runner = CliRunner()
        with (
            patch("supercharge.cli._task_root", return_value=task_root),
            patch("supercharge.cli._find_task_dir", return_value=task_dir),
            patch("supercharge.cli._archive_root", return_value=tmp_path / "archive"),
        ):
            result = runner.invoke(supercharge, ["task", "archive", task_uuid])

        assert result.exit_code == 0, result.output
        archive_files = list((tmp_path / "archive").glob("*.md"))
        content = archive_files[0].read_text()
        assert "Important findings here." in content
        assert "Internal notes." not in content

    def test_archive_default_title_from_heading(self, tmp_path: Path):
        task_uuid = str(uuid.uuid4())
        task_root = tmp_path / "tasks"
        _make_task_dir(
            task_root,
            "plan",
            task_uuid,
            task_md="# My Task Title\n\nContent.\n",
            result_md="# Result\n\n## Report\n\nDone.\n",
        )
        runner = CliRunner()
        with (
            patch("supercharge.cli._task_root", return_value=task_root),
            patch("supercharge.cli._find_task_dir", return_value=task_root / "plan" / task_uuid),
            patch("supercharge.cli._archive_root", return_value=tmp_path / "archive"),
        ):
            result = runner.invoke(supercharge, ["task", "archive", task_uuid])

        assert result.exit_code == 0, result.output
        archive_files = list((tmp_path / "archive").glob("*.md"))
        filename = archive_files[0].name
        assert "my-task-title" in filename

    def test_archive_explicit_title(self, tmp_path: Path):
        task_uuid = str(uuid.uuid4())
        task_root = tmp_path / "tasks"
        _make_task_dir(
            task_root,
            "plan",
            task_uuid,
            task_md="# Some heading\n",
            result_md="# Result\n\n## Report\n\nDone.\n",
        )
        runner = CliRunner()
        with (
            patch("supercharge.cli._task_root", return_value=task_root),
            patch("supercharge.cli._find_task_dir", return_value=task_root / "plan" / task_uuid),
            patch("supercharge.cli._archive_root", return_value=tmp_path / "archive"),
        ):
            result = runner.invoke(supercharge, ["task", "archive", "--title", "custom", task_uuid])

        assert result.exit_code == 0, result.output
        archive_files = list((tmp_path / "archive").glob("*.md"))
        assert "custom" in archive_files[0].name

    def test_archive_multiple_uuids(self, tmp_path: Path):
        uuid1 = str(uuid.uuid4())
        uuid2 = str(uuid.uuid4())
        task_root = tmp_path / "tasks"
        archive_root = tmp_path / "archive"
        dir1 = _make_task_dir(task_root, "plan", uuid1, task_md="# Task 1\n", result_md="# Result\n\n## Report\n\nR1.\n")
        dir2 = _make_task_dir(task_root, "research", uuid2, task_md="# Task 2\n", result_md="# Result\n\n## Report\n\nR2.\n")

        def mock_find(uid):
            if uid == uuid1:
                return dir1
            if uid == uuid2:
                return dir2
            return None

        runner = CliRunner()
        with (
            patch("supercharge.cli._task_root", return_value=task_root),
            patch("supercharge.cli._find_task_dir", side_effect=mock_find),
            patch("supercharge.cli._archive_root", return_value=archive_root),
        ):
            result = runner.invoke(supercharge, ["task", "archive", uuid1, uuid2])

        assert result.exit_code == 0, result.output
        archive_files = list(archive_root.glob("*.md"))
        assert len(archive_files) == 2
        assert not dir1.exists()
        assert not dir2.exists()

    def test_archive_continues_on_error(self, tmp_path: Path):
        bad_uuid = str(uuid.uuid4())
        good_uuid = str(uuid.uuid4())
        task_root = tmp_path / "tasks"
        archive_root = tmp_path / "archive"
        good_dir = _make_task_dir(
            task_root, "plan", good_uuid,
            task_md="# Good task\n",
            result_md="# Result\n\n## Report\n\nGood.\n",
        )

        def mock_find(uid):
            if uid == good_uuid:
                return good_dir
            return None  # bad_uuid not found

        runner = CliRunner()
        with (
            patch("supercharge.cli._task_root", return_value=task_root),
            patch("supercharge.cli._find_task_dir", side_effect=mock_find),
            patch("supercharge.cli._archive_root", return_value=archive_root),
        ):
            result = runner.invoke(supercharge, ["task", "archive", bad_uuid, good_uuid])

        # Good one should be archived despite bad one failing
        archive_files = list(archive_root.glob("*.md"))
        assert len(archive_files) == 1
        assert not good_dir.exists()


# ── task cleanup multi-UUID ───────────────────────────────────────────────────


class TestTaskCleanupMultiUuid:
    def test_cleanup_multiple_uuids(self, tmp_path: Path):
        uuid1 = str(uuid.uuid4())
        uuid2 = str(uuid.uuid4())
        task_root = tmp_path / "tasks"
        dir1 = task_root / "code" / uuid1
        dir1.mkdir(parents=True)
        (dir1 / "task.md").write_text("# Task\n")
        dir2 = task_root / "code" / uuid2
        dir2.mkdir(parents=True)
        (dir2 / "task.md").write_text("# Task\n")

        def mock_find(uid):
            if uid == uuid1:
                return dir1
            if uid == uuid2:
                return dir2
            return None

        runner = CliRunner()
        with (
            patch("supercharge.cli._task_root", return_value=task_root),
            patch("supercharge.cli._find_task_dir", side_effect=mock_find),
        ):
            result = runner.invoke(supercharge, ["task", "cleanup", uuid1, uuid2])

        assert result.exit_code == 0, result.output
        assert not dir1.exists()
        assert not dir2.exists()

    def test_cleanup_continues_on_error(self, tmp_path: Path):
        bad_uuid = str(uuid.uuid4())
        good_uuid = str(uuid.uuid4())
        task_root = tmp_path / "tasks"
        good_dir = task_root / "code" / good_uuid
        good_dir.mkdir(parents=True)
        (good_dir / "task.md").write_text("# Task\n")

        def mock_find(uid):
            if uid == good_uuid:
                return good_dir
            return None

        runner = CliRunner()
        with (
            patch("supercharge.cli._task_root", return_value=task_root),
            patch("supercharge.cli._find_task_dir", side_effect=mock_find),
        ):
            result = runner.invoke(supercharge, ["task", "cleanup", bad_uuid, good_uuid])

        # Good one should still be deleted
        assert not good_dir.exists()
        # Error about bad one should be in output
        assert "not found" in result.output.lower() or bad_uuid in result.output


# ── stale-task template ───────────────────────────────────────────────────────


class TestStaleTaskTemplate:
    def test_template_mentions_archive(self):
        template_path = Path(__file__).resolve().parents[1] / "templates" / "memory-stale-task.md"
        content = template_path.read_text()
        assert "supercharge task archive" in content

    def test_template_mentions_cleanup(self):
        template_path = Path(__file__).resolve().parents[1] / "templates" / "memory-stale-task.md"
        content = template_path.read_text()
        assert "supercharge task cleanup" in content


# ── memory agent definition ───────────────────────────────────────────────────


class TestMemoryAgentDefinition:
    def test_memory_agent_mentions_archive(self):
        agent_path = Path(__file__).resolve().parents[1] / "agents" / "memory.md"
        content = agent_path.read_text()
        assert "supercharge task archive" in content

    def test_memory_agent_mentions_archive_ref(self):
        agent_path = Path(__file__).resolve().parents[1] / "agents" / "memory.md"
        content = agent_path.read_text()
        assert "archive_ref" in content
