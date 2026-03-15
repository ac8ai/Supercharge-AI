"""Tests for lineage tracking: frontmatter injection, author validation, _read_frontmatter."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

from supercharge.cli import supercharge

# ── B4: _read_frontmatter ───────────────────────────────────────────────────


class TestReadFrontmatter:
    """Test _read_frontmatter utility in paths.py."""

    def test_valid_frontmatter(self, tmp_path: Path):
        from supercharge.paths import _read_frontmatter

        p = tmp_path / "test.md"
        p.write_text("---\ntask_uuid: abc-123\nagent_type: code\n---\n\n# Task\n")
        result = _read_frontmatter(p)
        assert result == {"task_uuid": "abc-123", "agent_type": "code"}

    def test_no_frontmatter(self, tmp_path: Path):
        from supercharge.paths import _read_frontmatter

        p = tmp_path / "test.md"
        p.write_text("# Task\n\nNo frontmatter here.\n")
        result = _read_frontmatter(p)
        assert result == {}

    def test_nonexistent_file(self, tmp_path: Path):
        from supercharge.paths import _read_frontmatter

        p = tmp_path / "nonexistent.md"
        result = _read_frontmatter(p)
        assert result == {}

    def test_colon_in_value(self, tmp_path: Path):
        """created_by: task:abc123 should parse correctly (colon in value)."""
        from supercharge.paths import _read_frontmatter

        p = tmp_path / "test.md"
        p.write_text("---\ncreated_by: task:abc123\nmodel: deep\n---\n")
        result = _read_frontmatter(p)
        assert result["created_by"] == "task:abc123"
        assert result["model"] == "deep"

    def test_empty_frontmatter(self, tmp_path: Path):
        from supercharge.paths import _read_frontmatter

        p = tmp_path / "test.md"
        p.write_text("---\n---\n\n# Task\n")
        result = _read_frontmatter(p)
        assert result == {}


# ── B2: _validate_author ────────────────────────────────────────────────────


class TestValidateAuthor:
    """Test _validate_author() in cli.py."""

    def test_orchestrator_valid(self):
        from supercharge.cli import _validate_author

        result = _validate_author("orchestrator:abc123")
        assert result == "orchestrator:abc123"

    def test_orchestrator_empty_id_raises(self):
        from supercharge.cli import _validate_author

        with pytest.raises(click.ClickException):
            _validate_author("orchestrator:")

    def test_task_valid(self, tmp_path: Path):
        """Valid task UUID that exists on disk."""
        from supercharge.cli import _validate_author

        task_uuid = str(uuid.uuid4())
        task_dir = tmp_path / "code" / task_uuid
        task_dir.mkdir(parents=True)

        with patch("supercharge.cli._find_task_dir", return_value=task_dir):
            result = _validate_author(f"task:{task_uuid}")
        assert result == f"task:{task_uuid}"

    def test_task_nonexistent_raises(self):
        from supercharge.cli import _validate_author

        with patch("supercharge.cli._find_task_dir", return_value=None):
            with pytest.raises(click.ClickException, match="not found"):
                _validate_author("task:nonexistent-uuid")

    def test_worker_valid(self, tmp_path: Path):
        from supercharge.cli import _validate_author

        worker_file = tmp_path / "workers" / "worker-abc.md"
        worker_file.parent.mkdir(parents=True)
        worker_file.touch()

        with patch("supercharge.cli._find_worker_file", return_value=worker_file):
            result = _validate_author("worker:worker-abc")
        assert result == "worker:worker-abc"

    def test_worker_nonexistent_raises(self):
        from supercharge.cli import _validate_author

        with patch("supercharge.cli._find_worker_file", return_value=None):
            with pytest.raises(click.ClickException, match="not found"):
                _validate_author("worker:nonexistent-id")

    def test_invalid_format_raises(self):
        from supercharge.cli import _validate_author

        with pytest.raises(click.ClickException, match="Invalid author format"):
            _validate_author("invalid_format")

    def test_unknown_type_raises(self):
        from supercharge.cli import _validate_author

        with pytest.raises(click.ClickException, match="Unknown author type"):
            _validate_author("unknown_type:abc")


# ── B2: task init frontmatter ───────────────────────────────────────────────


class TestTaskInitFrontmatter:
    """Test that task init injects YAML frontmatter into task.md."""

    def test_task_init_creates_frontmatter(self, tmp_path: Path):
        runner = CliRunner()
        with patch("supercharge.cli._task_root", return_value=tmp_path / "tasks"):
            result = runner.invoke(supercharge, ["task", "init", "code"])

        assert result.exit_code == 0
        task_uuid = result.output.strip()

        task_md = tmp_path / "tasks" / "code" / task_uuid / "task.md"
        content = task_md.read_text()
        assert content.startswith("---\n")
        assert f"task_uuid: {task_uuid}" in content
        assert "agent_type: code" in content
        assert "created_at:" in content
        # No created_by when --author not given
        assert "created_by" not in content
        # Template content should still be present after frontmatter
        assert "# Task" in content

    def test_task_init_with_author(self, tmp_path: Path):
        runner = CliRunner()
        with patch("supercharge.cli._task_root", return_value=tmp_path / "tasks"):
            result = runner.invoke(
                supercharge,
                ["task", "init", "code", "--author", "orchestrator:sess123"],
            )

        assert result.exit_code == 0
        task_uuid = result.output.strip()

        task_md = tmp_path / "tasks" / "code" / task_uuid / "task.md"
        content = task_md.read_text()
        assert "created_by: orchestrator:sess123" in content

    def test_task_init_without_author_no_created_by(self, tmp_path: Path):
        runner = CliRunner()
        with patch("supercharge.cli._task_root", return_value=tmp_path / "tasks"):
            result = runner.invoke(supercharge, ["task", "init", "plan"])

        assert result.exit_code == 0
        task_uuid = result.output.strip()

        task_md = tmp_path / "tasks" / "plan" / task_uuid / "task.md"
        content = task_md.read_text()
        assert "created_by" not in content


# ── B3: _prepare_worker_file frontmatter ────────────────────────────────────


class TestPrepareWorkerFileFrontmatter:
    """Test frontmatter injection in _prepare_worker_file."""

    def test_worker_file_with_author(self, tmp_path: Path):
        from supercharge.workers import _prepare_worker_file

        task_dir = tmp_path / "code" / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        task_dir.mkdir(parents=True)

        worker_id = "worker-123"
        prompt = "Do something"

        worker_file = _prepare_worker_file(task_dir, worker_id, prompt, author="task:some-uuid")

        content = worker_file.read_text()
        assert content.startswith("---\n")
        assert f"worker_id: {worker_id}" in content
        assert "agent_type: code" in content
        assert "spawned_at:" in content
        assert "model: deep" in content
        assert "created_by: task:some-uuid" in content
        # Assignment text should still be present
        assert prompt in content

    def test_worker_file_without_author_with_task_uuid_env(self, tmp_path: Path):
        from supercharge.permissions import _ENV_TASK_UUID
        from supercharge.workers import _prepare_worker_file

        task_dir = tmp_path / "code" / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        task_dir.mkdir(parents=True)

        worker_id = "worker-456"
        task_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

        with patch.dict(os.environ, {_ENV_TASK_UUID: task_uuid}):
            worker_file = _prepare_worker_file(task_dir, worker_id, "test")

        content = worker_file.read_text()
        assert f"created_by: task:{task_uuid}" in content

    def test_worker_file_frontmatter_fields(self, tmp_path: Path):
        from supercharge.workers import _prepare_worker_file

        task_dir = tmp_path / "plan" / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        task_dir.mkdir(parents=True)

        worker_id = "worker-789"
        worker_file = _prepare_worker_file(task_dir, worker_id, "assignment text")

        content = worker_file.read_text()
        assert f"worker_id: {worker_id}" in content
        assert "agent_type: plan" in content
        assert "spawned_at:" in content
        assert "model: deep" in content
        # Assignment text still present after frontmatter
        assert "assignment text" in content

    def test_worker_file_template_preserved(self, tmp_path: Path):
        from supercharge.workers import _prepare_worker_file

        task_dir = tmp_path / "code" / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        task_dir.mkdir(parents=True)

        worker_file = _prepare_worker_file(task_dir, "w-abc", "my task")
        content = worker_file.read_text()
        # Template sections should still be present
        assert "## Assignment" in content
        assert "## Progress" in content
        assert "## Result" in content


# ── B3: _build_options includes SUPERCHARGE_WORKER_ID ───────────────────────


class TestBuildOptionsWorkerIdEnv:
    """Test that _build_options sets SUPERCHARGE_WORKER_ID in env."""

    def test_deep_worker_gets_worker_id_env(self, tmp_path: Path):
        from supercharge.workers import _build_options

        with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(tmp_path)}):
            options = _build_options(
                tmp_path,
                remaining_depth=2,
                max_turns=None,
                model=None,
                agent_type="code",
                worker_id="my-worker-id",
            )
        assert options.env["SUPERCHARGE_WORKER_ID"] == "my-worker-id"

    def test_fast_worker_gets_empty_worker_id_env(self, tmp_path: Path):
        from supercharge.workers import _build_options

        with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(tmp_path)}):
            options = _build_options(
                tmp_path,
                remaining_depth=1,
                max_turns=None,
                model=None,
                agent_type="code",
                worker_id=None,
            )
        assert options.env["SUPERCHARGE_WORKER_ID"] == ""
