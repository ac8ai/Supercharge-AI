"""Tests for project name resolution: _resolve_project_name and _get_or_create_project."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from supercharge.metrics import _get_or_create_project, _init_db, _is_junk_project_path, _resolve_project_name


# ── _resolve_project_name ─────────────────────────────────────────────────────


class TestResolveProjectName:
    """Test _resolve_project_name returns the correct display name."""

    def test_devcontainer_json(self, tmp_path: Path):
        """Reads name from .devcontainer/devcontainer.json."""
        dc_dir = tmp_path / ".devcontainer"
        dc_dir.mkdir()
        (dc_dir / "devcontainer.json").write_text(json.dumps({"name": "My Project"}))

        result = _resolve_project_name(str(tmp_path))

        assert result == "My Project"

    def test_pyproject_toml(self, tmp_path: Path):
        """Reads name from pyproject.toml [project] section."""
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = \"my-package\"\n"
        )

        result = _resolve_project_name(str(tmp_path))

        assert result == "my-package"

    def test_package_json(self, tmp_path: Path):
        """Reads name from package.json."""
        (tmp_path / "package.json").write_text(json.dumps({"name": "my-npm-pkg"}))

        result = _resolve_project_name(str(tmp_path))

        assert result == "my-npm-pkg"

    def test_cargo_toml(self, tmp_path: Path):
        """Reads name from Cargo.toml [package] section."""
        (tmp_path / "Cargo.toml").write_text(
            "[package]\nname = \"my-crate\"\n"
        )

        result = _resolve_project_name(str(tmp_path))

        assert result == "my-crate"

    def test_go_mod(self, tmp_path: Path):
        """Reads module name from go.mod, extracts last path component."""
        (tmp_path / "go.mod").write_text("module github.com/user/my-module\n\ngo 1.21\n")

        result = _resolve_project_name(str(tmp_path))

        assert result == "my-module"

    def test_priority_devcontainer_wins(self, tmp_path: Path):
        """devcontainer.json takes priority over pyproject.toml and package.json."""
        dc_dir = tmp_path / ".devcontainer"
        dc_dir.mkdir()
        (dc_dir / "devcontainer.json").write_text(json.dumps({"name": "DevContainer Name"}))
        (tmp_path / "pyproject.toml").write_text("[project]\nname = \"py-name\"\n")
        (tmp_path / "package.json").write_text(json.dumps({"name": "npm-name"}))

        result = _resolve_project_name(str(tmp_path))

        assert result == "DevContainer Name"

    def test_fallback_camelcase(self):
        """CamelCase directory name is split into space-separated title-cased words."""
        result = _resolve_project_name("/some/path/TurnThisThing")

        assert result == "Turn This Thing"

    def test_fallback_snake_case(self):
        """snake_case directory name is split and title-cased."""
        result = _resolve_project_name("/some/path/turn_this_thing")

        assert result == "Turn This Thing"

    def test_fallback_kebab_case(self):
        """kebab-case directory name is split and title-cased."""
        result = _resolve_project_name("/some/path/turn-this-thing")

        assert result == "Turn This Thing"

    def test_fallback_mixed(self):
        """Mixed camelCase, underscores, and hyphens are all handled."""
        result = _resolve_project_name("/some/path/myProject_stuff-here")

        # Should split on camel, underscores, and hyphens; title-case each word
        assert result == "My Project Stuff Here"


# ── _is_junk_project_path ─────────────────────────────────────────────────────


class TestIsJunkProjectPath:
    """Test _is_junk_project_path correctly identifies paths that should not be tracked."""

    def test_supercharge_task_folder(self):
        """Paths containing /.claude/SuperchargeAI/tasks/ are junk."""
        assert (
            _is_junk_project_path(
                "/workspaces/MyProject/.claude/SuperchargeAI/tasks/code/abc123"
            )
            is True
        )

    def test_tmp_path(self):
        """Paths starting with /tmp/ are junk."""
        assert _is_junk_project_path("/tmp/pytest-123/test_0") is True

    def test_real_project_path(self):
        """Real project paths in /workspaces are not junk."""
        assert _is_junk_project_path("/workspaces/MyProject") is False

    def test_home_project_path(self):
        """Real project paths in /home are not junk."""
        assert _is_junk_project_path("/home/user/code/myapp") is False


# ── _get_or_create_project ────────────────────────────────────────────────────


def _make_conn() -> sqlite3.Connection:
    """Create an in-memory SQLite connection with the metrics schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _init_db(conn)
    return conn


class TestGetOrCreateProject:
    """Test _get_or_create_project creates and caches project entries correctly."""

    def test_creates_new_entry(self):
        """A new project_path not in the DB gets inserted with correct fields."""
        conn = _make_conn()
        project_path = "/workspaces/test-project"

        result = _get_or_create_project(conn, project_path)

        # Return value has required keys
        assert result["project_path"] == project_path
        assert result["project_slug"] == project_path.replace("/", "-")
        assert result["display_name"]  # non-empty (fallback at minimum)

        # Row exists in DB
        row = conn.execute(
            "SELECT * FROM projects WHERE project_path = ?", (project_path,)
        ).fetchone()
        assert row is not None
        assert row["project_slug"] == project_path.replace("/", "-")
        assert row["user_edited"] == 0
        assert row["last_updated"]  # non-empty timestamp

    def test_returns_existing_entry(self):
        """Calling the function twice returns the same result with no duplicate rows."""
        conn = _make_conn()
        project_path = "/workspaces/test-project"

        result1 = _get_or_create_project(conn, project_path)
        result2 = _get_or_create_project(conn, project_path)

        assert result1["display_name"] == result2["display_name"]
        assert result1["project_slug"] == result2["project_slug"]

        count = conn.execute(
            "SELECT COUNT(*) FROM projects WHERE project_path = ?", (project_path,)
        ).fetchone()[0]
        assert count == 1

    def test_junk_path_returns_empty_display_name(self):
        """Junk paths (/tmp/, task folders) return early with empty display_name and no DB row."""
        conn = _make_conn()
        junk_path = "/tmp/pytest-123/test_0"

        result = _get_or_create_project(conn, junk_path)

        assert result["display_name"] == ""

        # No row should be created in the DB
        row = conn.execute(
            "SELECT * FROM projects WHERE project_path = ?", (junk_path,)
        ).fetchone()
        assert row is None

    def test_user_edited_prevents_overwrite(self, tmp_path: Path, monkeypatch):
        """When user_edited=1, display_name is never overwritten, even when stale."""
        conn = _make_conn()
        # Bypass junk-path filter so tmp_path is treated as a real project path
        monkeypatch.setattr("supercharge.metrics._is_junk_project_path", lambda p: False)

        # Insert a pyproject.toml so resolution would give a different name
        (tmp_path / "pyproject.toml").write_text("[project]\nname = \"resolved-pkg\"\n")

        stale_time = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        conn.execute(
            """\
            INSERT INTO projects (project_path, project_slug, display_name, user_edited, last_updated)
            VALUES (?, ?, ?, 1, ?)
            """,
            (str(tmp_path), str(tmp_path).replace("/", "-"), "Custom Name", stale_time),
        )
        conn.commit()

        result = _get_or_create_project(conn, str(tmp_path))

        assert result["display_name"] == "Custom Name"

        # Verify DB row was also left unchanged
        row = conn.execute(
            "SELECT display_name FROM projects WHERE project_path = ?", (str(tmp_path),)
        ).fetchone()
        assert row["display_name"] == "Custom Name"

    def test_stale_cache_reresolves(self, tmp_path: Path, monkeypatch):
        """When user_edited=0 and last_updated >24h ago, display_name is re-resolved."""
        conn = _make_conn()
        # Bypass junk-path filter so tmp_path is treated as a real project path
        monkeypatch.setattr("supercharge.metrics._is_junk_project_path", lambda p: False)

        # Create a metadata file so re-resolution gives a known name
        (tmp_path / "pyproject.toml").write_text("[project]\nname = \"fresh-name\"\n")

        stale_time = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        conn.execute(
            """\
            INSERT INTO projects (project_path, project_slug, display_name, user_edited, last_updated)
            VALUES (?, ?, ?, 0, ?)
            """,
            (str(tmp_path), str(tmp_path).replace("/", "-"), "Old Display Name", stale_time),
        )
        conn.commit()

        result = _get_or_create_project(conn, str(tmp_path))

        assert result["display_name"] == "fresh-name"

        # Verify DB row was updated
        row = conn.execute(
            "SELECT display_name FROM projects WHERE project_path = ?", (str(tmp_path),)
        ).fetchone()
        assert row["display_name"] == "fresh-name"

    def test_fresh_cache_no_reresolve(self, tmp_path: Path, monkeypatch):
        """When user_edited=0 and last_updated <24h ago, display_name is NOT re-resolved."""
        conn = _make_conn()
        # Bypass junk-path filter so tmp_path is treated as a real project path
        monkeypatch.setattr("supercharge.metrics._is_junk_project_path", lambda p: False)

        # Create a metadata file so re-resolution would give a different name
        (tmp_path / "pyproject.toml").write_text("[project]\nname = \"fresh-name\"\n")

        recent_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        conn.execute(
            """\
            INSERT INTO projects (project_path, project_slug, display_name, user_edited, last_updated)
            VALUES (?, ?, ?, 0, ?)
            """,
            (str(tmp_path), str(tmp_path).replace("/", "-"), "Cached Name", recent_time),
        )
        conn.commit()

        result = _get_or_create_project(conn, str(tmp_path))

        assert result["display_name"] == "Cached Name"

        # Verify DB row was NOT changed
        row = conn.execute(
            "SELECT display_name FROM projects WHERE project_path = ?", (str(tmp_path),)
        ).fetchone()
        assert row["display_name"] == "Cached Name"
