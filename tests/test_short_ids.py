"""Tests for prefix-based task and worker lookup (short IDs)."""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest

from supercharge.paths import AmbiguousPrefixError, _find_task_dir, _resolve_prefix
from supercharge.permissions import _find_worker_file, _resolve_worker_prefix


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
    uuid = task_uuid or folder_name
    (folder / "task.md").write_text(
        f"---\ntask_uuid: {uuid}\nagent_type: {agent}\n---\n\n# Task\n"
    )
    return folder


def _make_worker_file(task_dir: Path, worker_id: str) -> Path:
    """Create a worker .md file inside a task's workers/ directory."""
    workers = task_dir / "workers"
    workers.mkdir(parents=True, exist_ok=True)
    f = workers / f"{worker_id}.md"
    f.write_text(f"# Worker {worker_id}\n")
    return f


# ── _resolve_prefix ──────────────────────────────────────────────────────


class TestResolvePrefix:
    """Test _resolve_prefix() in paths.py."""

    def test_full_uuid_match(self, tmp_path: Path):
        uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        _make_task_folder(tmp_path, "code", uuid)
        with patch("supercharge.paths._task_root", return_value=tmp_path):
            result = _resolve_prefix(uuid)
        assert result == (uuid, uuid)

    def test_full_uuid_no_match(self, tmp_path: Path):
        _make_task_folder(tmp_path, "code", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        with patch("supercharge.paths._task_root", return_value=tmp_path):
            result = _resolve_prefix("bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee")
        assert result is None

    def test_8char_prefix_match(self, tmp_path: Path):
        uuid = "abcdef01-1111-2222-3333-444444444444"
        _make_task_folder(tmp_path, "plan", uuid)
        with patch("supercharge.paths._task_root", return_value=tmp_path):
            result = _resolve_prefix("abcdef01")
        assert result is not None
        assert result[0] == uuid
        assert result[1] == uuid

    def test_8char_prefix_with_short_folder(self, tmp_path: Path):
        full_uuid = "5b6d9c66-3bfe-4ae6-9b32-863841ddbd38"
        folder_name = "5b6d9c66-implement-auth"
        _make_task_folder(tmp_path, "code", folder_name, task_uuid=full_uuid)
        with patch("supercharge.paths._task_root", return_value=tmp_path):
            result = _resolve_prefix("5b6d9c66")
        assert result is not None
        assert result[0] == full_uuid
        assert result[1] == folder_name

    def test_prefix_no_match(self, tmp_path: Path):
        _make_task_folder(tmp_path, "code", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        with patch("supercharge.paths._task_root", return_value=tmp_path):
            result = _resolve_prefix("bbbbbbbb")
        assert result is None

    def test_prefix_ambiguous(self, tmp_path: Path):
        _make_task_folder(
            tmp_path,
            "code",
            "abcdef01-1111-2222-3333-444444444444",
        )
        _make_task_folder(
            tmp_path,
            "plan",
            "abcdef01-fix-bug",
            task_uuid="abcdef01-aaaa-bbbb-cccc-dddddddddddd",
        )
        with patch("supercharge.paths._task_root", return_value=tmp_path):
            with pytest.raises(AmbiguousPrefixError) as exc_info:
                _resolve_prefix("abcdef01")
        assert len(exc_info.value.matches) == 2

    def test_prefix_too_short_raises_value_error(self, tmp_path: Path):
        with patch("supercharge.paths._task_root", return_value=tmp_path):
            with pytest.raises(ValueError, match="at least 8"):
                _resolve_prefix("abcdef0")

    def test_exact_folder_name_match(self, tmp_path: Path):
        full_uuid = "5b6d9c66-3bfe-4ae6-9b32-863841ddbd38"
        folder_name = "5b6d9c66-implement-auth"
        _make_task_folder(tmp_path, "code", folder_name, task_uuid=full_uuid)
        with patch("supercharge.paths._task_root", return_value=tmp_path):
            result = _resolve_prefix(folder_name)
        assert result is not None
        assert result[0] == full_uuid
        assert result[1] == folder_name

    def test_nonexistent_root_returns_none(self, tmp_path: Path):
        with patch("supercharge.paths._task_root", return_value=tmp_path / "nope"):
            result = _resolve_prefix("abcdef01")
        assert result is None

    def test_longer_prefix_narrows_match(self, tmp_path: Path):
        _make_task_folder(
            tmp_path,
            "code",
            "abcdef01-1111-2222-3333-444444444444",
        )
        _make_task_folder(
            tmp_path,
            "plan",
            "abcdef02-fix-bug",
            task_uuid="abcdef02-aaaa-bbbb-cccc-dddddddddddd",
        )
        with patch("supercharge.paths._task_root", return_value=tmp_path):
            result = _resolve_prefix("abcdef01")
        assert result is not None
        assert result[1] == "abcdef01-1111-2222-3333-444444444444"

    def test_non_hex_string_returns_none(self, tmp_path: Path):
        with patch("supercharge.paths._task_root", return_value=tmp_path):
            result = _resolve_prefix("not-hex-at-all")
        assert result is None


# ── _find_task_dir ────────────────────────────────────────────────────────


class TestFindTaskDir:
    """Test _find_task_dir() with both old and new folder formats."""

    def test_full_uuid_old_format(self, tmp_path: Path):
        uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        folder = _make_task_folder(tmp_path, "code", uuid)
        with patch("supercharge.paths._task_root", return_value=tmp_path):
            result = _find_task_dir(uuid)
        assert result == folder

    def test_short_prefix_new_format(self, tmp_path: Path):
        full_uuid = "5b6d9c66-3bfe-4ae6-9b32-863841ddbd38"
        folder_name = "5b6d9c66-implement-auth"
        folder = _make_task_folder(tmp_path, "code", folder_name, task_uuid=full_uuid)
        with patch("supercharge.paths._task_root", return_value=tmp_path):
            result = _find_task_dir("5b6d9c66")
        assert result == folder

    def test_full_folder_name(self, tmp_path: Path):
        full_uuid = "5b6d9c66-3bfe-4ae6-9b32-863841ddbd38"
        folder_name = "5b6d9c66-implement-auth"
        folder = _make_task_folder(tmp_path, "code", folder_name, task_uuid=full_uuid)
        with patch("supercharge.paths._task_root", return_value=tmp_path):
            result = _find_task_dir(folder_name)
        assert result == folder

    def test_not_found(self, tmp_path: Path):
        with patch("supercharge.paths._task_root", return_value=tmp_path):
            result = _find_task_dir("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        assert result is None

    def test_full_uuid_finds_short_named_folder(self, tmp_path: Path):
        """Full UUID from frontmatter resolves to a short-named folder."""
        full_uuid = "5b6d9c66-3bfe-4ae6-9b32-863841ddbd38"
        folder_name = "5b6d9c66-implement-auth"
        folder = _make_task_folder(tmp_path, "code", folder_name, task_uuid=full_uuid)
        with patch("supercharge.paths._task_root", return_value=tmp_path):
            result = _find_task_dir(full_uuid)
        assert result == folder

    def test_ambiguous_raises(self, tmp_path: Path):
        _make_task_folder(tmp_path, "code", "abcdef01-1111-2222-3333-444444444444")
        _make_task_folder(
            tmp_path,
            "plan",
            "abcdef01-fix-bug",
            task_uuid="abcdef01-aaaa-bbbb-cccc-dddddddddddd",
        )
        with patch("supercharge.paths._task_root", return_value=tmp_path):
            with pytest.raises(AmbiguousPrefixError):
                _find_task_dir("abcdef01")


# ── _resolve_worker_prefix ───────────────────────────────────────────────


class TestResolveWorkerPrefix:
    """Test _resolve_worker_prefix() in permissions.py."""

    def test_exact_worker_id(self, tmp_path: Path):
        uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        task = _make_task_folder(tmp_path, "code", uuid)
        worker_id = "w1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6"
        wf = _make_worker_file(task, worker_id)
        with patch("supercharge.permissions._task_root", return_value=tmp_path):
            result = _resolve_worker_prefix(worker_id)
        assert result is not None
        assert result[0] == worker_id
        assert result[1] == wf

    def test_worker_prefix_match(self, tmp_path: Path):
        uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        task = _make_task_folder(tmp_path, "code", uuid)
        worker_id = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
        wf = _make_worker_file(task, worker_id)
        with patch("supercharge.permissions._task_root", return_value=tmp_path):
            result = _resolve_worker_prefix("a1b2c3d4")
        assert result is not None
        assert result[0] == worker_id
        assert result[1] == wf

    def test_worker_no_match(self, tmp_path: Path):
        uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        task = _make_task_folder(tmp_path, "code", uuid)
        _make_worker_file(task, "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6")
        with patch("supercharge.permissions._task_root", return_value=tmp_path):
            result = _resolve_worker_prefix("ffffffff")
        assert result is None

    def test_worker_ambiguous(self, tmp_path: Path):
        uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        task = _make_task_folder(tmp_path, "code", uuid)
        _make_worker_file(task, "abcdef01aaaabbbbccccddddeeeeeeee")
        _make_worker_file(task, "abcdef01ffffffffffffffffffffffff")
        with patch("supercharge.permissions._task_root", return_value=tmp_path):
            with pytest.raises(AmbiguousPrefixError) as exc_info:
                _resolve_worker_prefix("abcdef01")
        assert len(exc_info.value.matches) == 2

    def test_worker_prefix_too_short(self, tmp_path: Path):
        with patch("supercharge.permissions._task_root", return_value=tmp_path):
            with pytest.raises(ValueError, match="at least 8"):
                _resolve_worker_prefix("abc")

    def test_worker_nonexistent_root(self, tmp_path: Path):
        with patch("supercharge.permissions._task_root", return_value=tmp_path / "nope"):
            result = _resolve_worker_prefix("abcdef01")
        assert result is None


# ── _find_worker_file ─────────────────────────────────────────────────────


class TestFindWorkerFile:
    """Test _find_worker_file() with prefix resolution."""

    def test_exact_id(self, tmp_path: Path):
        uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        task = _make_task_folder(tmp_path, "code", uuid)
        worker_id = "deadbeefcafebabe1234567890abcdef"
        wf = _make_worker_file(task, worker_id)
        with patch("supercharge.permissions._task_root", return_value=tmp_path):
            result = _find_worker_file(worker_id)
        assert result == wf

    def test_prefix_lookup(self, tmp_path: Path):
        uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        task = _make_task_folder(tmp_path, "code", uuid)
        worker_id = "deadbeefcafebabe1234567890abcdef"
        wf = _make_worker_file(task, worker_id)
        with patch("supercharge.permissions._task_root", return_value=tmp_path):
            result = _find_worker_file("deadbeef")
        assert result == wf

    def test_not_found(self, tmp_path: Path):
        with patch("supercharge.permissions._task_root", return_value=tmp_path):
            result = _find_worker_file("deadbeef")
        assert result is None

    def test_worker_in_new_folder_format(self, tmp_path: Path):
        full_uuid = "5b6d9c66-3bfe-4ae6-9b32-863841ddbd38"
        folder_name = "5b6d9c66-implement-auth"
        task = _make_task_folder(tmp_path, "code", folder_name, task_uuid=full_uuid)
        worker_id = "abcdef01abcdef01abcdef01abcdef01"
        wf = _make_worker_file(task, worker_id)
        with patch("supercharge.permissions._task_root", return_value=tmp_path):
            result = _find_worker_file(worker_id)
        assert result == wf


# ── memory._UUID_RE ──────────────────────────────────────────────────────


class TestMemoryUUIDRegex:
    """Test that the updated _UUID_RE matches both old and new folder formats."""

    def test_matches_full_uuid(self):
        from supercharge.memory import _UUID_RE

        assert _UUID_RE.match("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

    def test_matches_short_id_with_slug(self):
        from supercharge.memory import _UUID_RE

        assert _UUID_RE.match("5b6d9c66-implement-auth")

    def test_matches_short_id_with_long_slug(self):
        from supercharge.memory import _UUID_RE

        assert _UUID_RE.match("abcdef01-fix-login-page-responsive-layout")

    def test_rejects_no_dash(self):
        from supercharge.memory import _UUID_RE

        assert not _UUID_RE.match("abcdef01")

    def test_rejects_non_hex_prefix(self):
        from supercharge.memory import _UUID_RE

        assert not _UUID_RE.match("not-a-hex-prefix")

    def test_rejects_short_hex_prefix(self):
        from supercharge.memory import _UUID_RE

        assert not _UUID_RE.match("abcdef0-something")
