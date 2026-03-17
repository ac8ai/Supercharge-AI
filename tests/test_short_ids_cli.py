"""Tests for CLI-level short-ID features: --name, --full, task resolve, prefix cleanup/archive."""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from supercharge.cli import _name_to_slug, supercharge
from supercharge.paths import AmbiguousPrefixError


# ── Helpers ───────────────────────────────────────────────────────────────


def _make_task_folder(
    root: Path,
    agent: str,
    folder_name: str,
    task_uuid: str | None = None,
) -> Path:
    """Create a task folder with frontmatter in task.md."""
    folder = root / agent / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    uid = task_uuid or folder_name
    (folder / "task.md").write_text(
        f"---\ntask_uuid: {uid}\nagent_type: {agent}\n---\n\n# Task\n"
    )
    return folder


def _make_worker_file(task_dir: Path, worker_id: str) -> Path:
    workers = task_dir / "workers"
    workers.mkdir(parents=True, exist_ok=True)
    f = workers / f"{worker_id}.md"
    f.write_text(f"# Worker {worker_id}\n")
    return f


# ── _name_to_slug ─────────────────────────────────────────────────────────


class TestNameToSlug:
    def test_basic_name(self):
        assert _name_to_slug("Implement Auth Middleware") == "implement-auth-middleware"

    def test_special_chars_stripped(self):
        assert _name_to_slug("Fix: Login Page (v2)!") == "fix-login-page-v2"

    def test_multiple_spaces(self):
        assert _name_to_slug("too   many   spaces") == "too-many-spaces"

    def test_unicode_stripped(self):
        # Unicode non-ASCII stripped, ASCII letters kept
        result = _name_to_slug("Add emoji support")
        assert result == "add-emoji-support"

    def test_leading_trailing_hyphens(self):
        assert _name_to_slug("--leading and trailing--") == "leading-and-trailing"

    def test_empty_after_strip(self):
        assert _name_to_slug("!!!") == ""

    def test_hyphens_preserved(self):
        assert _name_to_slug("add-new-feature") == "add-new-feature"

    def test_numbers_preserved(self):
        assert _name_to_slug("Fix bug 42") == "fix-bug-42"


# ── task init --name / --full ─────────────────────────────────────────────


def _mock_copy_template(name: str, dest: Path) -> None:
    """Mock _copy_template that creates an empty file."""
    dest.touch()


class TestTaskInitName:
    def test_init_prints_8_chars_by_default(self, tmp_path: Path):
        task_root = tmp_path / "tasks"
        task_root.mkdir(parents=True)
        runner = CliRunner()
        with (
            patch("supercharge.cli._task_root", return_value=task_root),
            patch("supercharge.cli._copy_template", side_effect=_mock_copy_template),
            patch("supercharge.cli._emit"),
        ):
            result = runner.invoke(supercharge, ["task", "init", "--name", "Test Task", "code"])
        assert result.exit_code == 0, result.output
        output = result.output.strip()
        assert len(output) == 8
        # Should be first 8 hex chars of a UUID
        assert re.match(r"^[0-9a-f]{8}$", output)

    def test_init_full_flag_prints_uuid(self, tmp_path: Path):
        task_root = tmp_path / "tasks"
        task_root.mkdir(parents=True)
        runner = CliRunner()
        with (
            patch("supercharge.cli._task_root", return_value=task_root),
            patch("supercharge.cli._copy_template", side_effect=_mock_copy_template),
            patch("supercharge.cli._emit"),
        ):
            result = runner.invoke(supercharge, ["task", "init", "--full", "--name", "Test Task", "code"])
        assert result.exit_code == 0, result.output
        output = result.output.strip()
        # Full UUID is 36 chars
        assert len(output) == 36
        assert re.match(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
            output,
        )

    def test_init_with_name_creates_slug_folder(self, tmp_path: Path):
        task_root = tmp_path / "tasks"
        task_root.mkdir(parents=True)
        runner = CliRunner()
        with (
            patch("supercharge.cli._task_root", return_value=task_root),
            patch("supercharge.cli._copy_template", side_effect=_mock_copy_template),
            patch("supercharge.cli._emit"),
        ):
            result = runner.invoke(
                supercharge, ["task", "init", "--name", "My Task", "code"]
            )
        assert result.exit_code == 0, result.output
        short_id = result.output.strip()
        # Folder should be <8hex>-my-task
        code_dir = task_root / "code"
        folders = list(code_dir.iterdir())
        assert len(folders) == 1
        folder_name = folders[0].name
        assert folder_name.startswith(short_id)
        assert folder_name.endswith("-my-task")

    def test_init_with_name_frontmatter(self, tmp_path: Path):
        task_root = tmp_path / "tasks"
        task_root.mkdir(parents=True)
        runner = CliRunner()
        with (
            patch("supercharge.cli._task_root", return_value=task_root),
            patch("supercharge.cli._copy_template", side_effect=_mock_copy_template),
            patch("supercharge.cli._emit"),
        ):
            result = runner.invoke(
                supercharge,
                ["task", "init", "--name", "Implement Auth Middleware", "plan"],
            )
        assert result.exit_code == 0, result.output
        plan_dir = task_root / "plan"
        folders = list(plan_dir.iterdir())
        task_md = (folders[0] / "task.md").read_text()
        assert "task_name: Implement Auth Middleware" in task_md
        assert "# Implement Auth Middleware\n" in task_md

    def test_init_with_name_header_comes_after_frontmatter(self, tmp_path: Path):
        task_root = tmp_path / "tasks"
        task_root.mkdir(parents=True)
        runner = CliRunner()
        with (
            patch("supercharge.cli._task_root", return_value=task_root),
            patch("supercharge.cli._copy_template", side_effect=_mock_copy_template),
            patch("supercharge.cli._emit"),
        ):
            result = runner.invoke(
                supercharge,
                ["task", "init", "--name", "Test Task", "code"],
            )
        assert result.exit_code == 0, result.output
        code_dir = task_root / "code"
        folders = list(code_dir.iterdir())
        content = (folders[0] / "task.md").read_text()
        # Frontmatter should come first, then header
        fm_end = content.index("---\n\n", 4)  # skip opening ---
        header_pos = content.index("# Test Task")
        assert header_pos > fm_end


# ── task cleanup with short prefixes ──────────────────────────────────────


class TestTaskCleanupShortPrefix:
    def test_cleanup_with_short_prefix(self, tmp_path: Path):
        full_uuid = "abcdef01-1111-2222-3333-444444444444"
        task_root = tmp_path / "tasks"
        task_dir = _make_task_folder(task_root, "code", full_uuid)

        runner = CliRunner()
        with (
            patch("supercharge.cli._task_root", return_value=task_root),
            patch("supercharge.paths._task_root", return_value=task_root),
            patch("supercharge.cli._emit"),
        ):
            result = runner.invoke(supercharge, ["task", "cleanup", "--agent-type", "memory", "abcdef01"])
        assert result.exit_code == 0, result.output
        assert not task_dir.exists()

    def test_cleanup_with_named_folder(self, tmp_path: Path):
        full_uuid = "5b6d9c66-3bfe-4ae6-9b32-863841ddbd38"
        folder_name = "5b6d9c66-implement-auth"
        task_root = tmp_path / "tasks"
        task_dir = _make_task_folder(task_root, "code", folder_name, task_uuid=full_uuid)

        runner = CliRunner()
        with (
            patch("supercharge.cli._task_root", return_value=task_root),
            patch("supercharge.paths._task_root", return_value=task_root),
            patch("supercharge.cli._emit"),
        ):
            result = runner.invoke(supercharge, ["task", "cleanup", "--agent-type", "memory", "5b6d9c66"])
        assert result.exit_code == 0, result.output
        assert not task_dir.exists()

    def test_cleanup_ambiguous_prefix(self, tmp_path: Path):
        task_root = tmp_path / "tasks"
        _make_task_folder(task_root, "code", "abcdef01-1111-2222-3333-444444444444")
        _make_task_folder(
            task_root,
            "plan",
            "abcdef01-fix-bug",
            task_uuid="abcdef01-aaaa-bbbb-cccc-dddddddddddd",
        )

        runner = CliRunner()
        with (
            patch("supercharge.cli._task_root", return_value=task_root),
            patch("supercharge.paths._task_root", return_value=task_root),
            patch("supercharge.cli._emit"),
        ):
            result = runner.invoke(supercharge, ["task", "cleanup", "--agent-type", "memory", "abcdef01"])
        assert "ambiguous" in result.output.lower() or "Ambiguous" in result.output


# ── task archive with short prefixes ──────────────────────────────────────


class TestTaskArchiveShortPrefix:
    def test_archive_with_short_prefix(self, tmp_path: Path):
        full_uuid = "abcdef01-1111-2222-3333-444444444444"
        task_root = tmp_path / "tasks"
        archive_root = tmp_path / "archive"
        task_dir = _make_task_folder(task_root, "plan", full_uuid)
        (task_dir / "result.md").write_text("# Result\n\n## Report\n\nDone.\n")

        runner = CliRunner()
        with (
            patch("supercharge.cli._task_root", return_value=task_root),
            patch("supercharge.paths._task_root", return_value=task_root),
            patch("supercharge.cli._archive_root", return_value=archive_root),
            patch("supercharge.cli._emit"),
        ):
            result = runner.invoke(supercharge, ["task", "archive", "--agent-type", "memory", "abcdef01"])
        assert result.exit_code == 0, result.output
        assert not task_dir.exists()
        archive_files = list(archive_root.glob("*.md"))
        assert len(archive_files) == 1


# ── task resolve ──────────────────────────────────────────────────────────


class TestTaskResolve:
    def test_resolve_happy_path(self, tmp_path: Path):
        full_uuid = "abcdef01-1111-2222-3333-444444444444"
        task_root = tmp_path / "tasks"
        _make_task_folder(task_root, "code", full_uuid)

        runner = CliRunner()
        with patch("supercharge.paths._task_root", return_value=task_root):
            result = runner.invoke(supercharge, ["task", "resolve", "abcdef01"])
        assert result.exit_code == 0
        assert full_uuid in result.output.strip()

    def test_resolve_with_named_folder(self, tmp_path: Path):
        full_uuid = "5b6d9c66-3bfe-4ae6-9b32-863841ddbd38"
        folder_name = "5b6d9c66-implement-auth"
        task_root = tmp_path / "tasks"
        _make_task_folder(task_root, "code", folder_name, task_uuid=full_uuid)

        runner = CliRunner()
        with patch("supercharge.paths._task_root", return_value=task_root):
            result = runner.invoke(supercharge, ["task", "resolve", "5b6d9c66"])
        assert result.exit_code == 0
        assert full_uuid in result.output.strip()

    def test_resolve_ambiguous(self, tmp_path: Path):
        task_root = tmp_path / "tasks"
        _make_task_folder(task_root, "code", "abcdef01-1111-2222-3333-444444444444")
        _make_task_folder(
            task_root,
            "plan",
            "abcdef01-fix-bug",
            task_uuid="abcdef01-aaaa-bbbb-cccc-dddddddddddd",
        )

        runner = CliRunner()
        with patch("supercharge.paths._task_root", return_value=task_root):
            result = runner.invoke(supercharge, ["task", "resolve", "abcdef01"])
        assert result.exit_code != 0

    def test_resolve_not_found(self, tmp_path: Path):
        task_root = tmp_path / "tasks"
        task_root.mkdir(parents=True)

        runner = CliRunner()
        with patch("supercharge.paths._task_root", return_value=task_root):
            result = runner.invoke(supercharge, ["task", "resolve", "deadbeef"])
        assert result.exit_code != 0
        assert "not found" in result.output.lower() or "No task" in result.output


# ── _validate_author with short prefixes ──────────────────────────────────


class TestValidateAuthorShortPrefix:
    def test_author_task_short_prefix(self, tmp_path: Path):
        """Short task prefix in author should resolve."""
        full_uuid = "abcdef01-1111-2222-3333-444444444444"
        task_root = tmp_path / "tasks"
        _make_task_folder(task_root, "code", full_uuid)

        from supercharge.cli import _validate_author

        with patch("supercharge.paths._task_root", return_value=task_root):
            result = _validate_author("task:abcdef01")
        assert result == "task:abcdef01"

    def test_author_worker_short_prefix(self, tmp_path: Path):
        """Short worker prefix in author should resolve."""
        full_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        task_root = tmp_path / "tasks"
        task_dir = _make_task_folder(task_root, "code", full_uuid)
        worker_id = "deadbeefcafebabe1234567890abcdef"
        _make_worker_file(task_dir, worker_id)

        from supercharge.cli import _validate_author

        with patch("supercharge.permissions._task_root", return_value=task_root):
            result = _validate_author("worker:deadbeef")
        assert result == "worker:deadbeef"

    def test_author_task_ambiguous(self, tmp_path: Path):
        """Ambiguous task prefix in author should raise ClickException."""
        task_root = tmp_path / "tasks"
        _make_task_folder(task_root, "code", "abcdef01-1111-2222-3333-444444444444")
        _make_task_folder(
            task_root,
            "plan",
            "abcdef01-fix-bug",
            task_uuid="abcdef01-aaaa-bbbb-cccc-dddddddddddd",
        )

        import click

        from supercharge.cli import _validate_author

        with (
            patch("supercharge.paths._task_root", return_value=task_root),
            pytest.raises(click.ClickException, match="[Aa]mbiguous"),
        ):
            _validate_author("task:abcdef01")


# ── subtask init JSON output ──────────────────────────────────────────────


class TestSubtaskInitOutput:
    def test_subtask_init_full_worker_id_by_default(self, tmp_path: Path):
        """Default JSON output contains full worker_id."""
        full_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        task_root = tmp_path / "tasks"
        task_dir = _make_task_folder(task_root, "code", full_uuid)

        runner = CliRunner()
        mock_result = {"worker_id": "11111111-2222-3333-4444-555555555555", "result": "ok"}
        with (
            patch("supercharge.cli._task_root", return_value=task_root),
            patch("supercharge.paths._task_root", return_value=task_root),
            patch("supercharge.cli._find_task_dir", return_value=task_dir),
            patch("supercharge.cli._is_fast_mode", return_value=True),
            patch("supercharge.cli._emit"),
            patch("supercharge.cli.asyncio.run", return_value=mock_result),
            patch("supercharge.cli._resolve_prefix"),
        ):
            result = runner.invoke(
                supercharge,
                ["subtask", "init", "code", "do stuff", "--task-uuid", full_uuid],
            )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output.strip())
        # Full worker_id should be in output by default
        assert len(data["worker_id"]) == 36

    def test_subtask_init_short_flag(self, tmp_path: Path):
        """--short flag returns worker_id[:8] in JSON."""
        full_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        task_root = tmp_path / "tasks"
        task_dir = _make_task_folder(task_root, "code", full_uuid)

        runner = CliRunner()
        mock_result = {"worker_id": "11111111-2222-3333-4444-555555555555", "result": "ok"}
        with (
            patch("supercharge.cli._task_root", return_value=task_root),
            patch("supercharge.paths._task_root", return_value=task_root),
            patch("supercharge.cli._find_task_dir", return_value=task_dir),
            patch("supercharge.cli._is_fast_mode", return_value=True),
            patch("supercharge.cli._emit"),
            patch("supercharge.cli.asyncio.run", return_value=mock_result),
            patch("supercharge.cli._resolve_prefix"),
        ):
            result = runner.invoke(
                supercharge,
                [
                    "subtask", "init", "--short",
                    "code", "do stuff", "--task-uuid", full_uuid,
                ],
            )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output.strip())
        assert len(data["worker_id"]) == 8
        assert data["worker_id"] == "11111111"


class TestMaxTurnsValidation:
    def test_invalid_max_turns_gives_clean_error(self, tmp_path: Path):
        """Non-numeric SUPERCHARGE_MAX_TURNS produces a ClickException, not a traceback."""
        full_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        task_root = tmp_path / "tasks"
        task_dir = _make_task_folder(task_root, "code", full_uuid)

        runner = CliRunner(env={"SUPERCHARGE_MAX_TURNS": "not_a_number"})
        with (
            patch("supercharge.cli._task_root", return_value=task_root),
            patch("supercharge.paths._task_root", return_value=task_root),
            patch("supercharge.cli._find_task_dir", return_value=task_dir),
            patch("supercharge.cli._resolve_prefix"),
        ):
            result = runner.invoke(
                supercharge,
                ["subtask", "init", "code", "do stuff", "--task-uuid", full_uuid],
            )
        assert result.exit_code != 0
        assert "SUPERCHARGE_MAX_TURNS must be an integer" in result.output
