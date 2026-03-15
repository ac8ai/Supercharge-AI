"""Tests for CLAUDE_CONFIG_DIR support.

Verifies that _user_config_dir() and all callers respect the env var.
"""

from __future__ import annotations

import json
from pathlib import Path

from supercharge.hooks import _ensure_project_dir
from supercharge.paths import _hook_data_dir, _user_config_dir
from supercharge.permissions import (
    _add_user_permissions,
    _remove_user_permissions,
    _user_settings_path,
)

# ── _user_config_dir() ──────────────────────────────────────────────────────


class TestUserConfigDir:
    """Core helper: _user_config_dir()."""

    def test_default_no_env_var(self, monkeypatch):
        """Without CLAUDE_CONFIG_DIR, returns ~/.claude."""
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        result = _user_config_dir()
        assert result == Path.home() / ".claude"

    def test_custom_dir(self, monkeypatch):
        """With CLAUDE_CONFIG_DIR set, returns that path."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/custom/config")
        result = _user_config_dir()
        assert result == Path("/custom/config")

    def test_empty_string_falls_back(self, monkeypatch):
        """Empty string is treated as unset -> falls back to ~/.claude."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "")
        result = _user_config_dir()
        assert result == Path.home() / ".claude"

    def test_relative_path(self, monkeypatch):
        """Relative path is accepted as-is (Path handles it)."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "relative/config")
        result = _user_config_dir()
        assert result == Path("relative/config")

    def test_trailing_slash_normalized(self, monkeypatch):
        """Trailing slash is normalized by Path."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/custom/config/")
        result = _user_config_dir()
        assert result == Path("/custom/config")


# ── _user_settings_path() ───────────────────────────────────────────────────


class TestUserSettingsPath:
    """_user_settings_path() uses _user_config_dir()."""

    def test_default(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        result = _user_settings_path()
        assert result == Path.home() / ".claude" / "settings.json"

    def test_custom_dir(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/custom/dir")
        result = _user_settings_path()
        assert result == Path("/custom/dir") / "settings.json"


# ── _hook_data_dir() plugin cache fallback ───────────────────────────────────


class TestHookDataDirPluginCache:
    """_hook_data_dir() plugin cache path uses _user_config_dir()."""

    def test_plugin_cache_uses_config_dir(self, monkeypatch, tmp_path):
        """When CLAUDE_CONFIG_DIR is set, plugin cache is looked up there."""
        # Remove the higher-priority env vars so we fall through to plugin cache
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        monkeypatch.delenv("SUPERCHARGE_ROOT", raising=False)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))

        # Create fake plugin cache structure
        cache_dir = tmp_path / "plugins" / "cache" / "marketplace-x" / "supercharge-ai" / "1.0.0"
        prompts_dir = cache_dir / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "protocol.md").write_text("test")

        result = _hook_data_dir()
        assert result == cache_dir

    def test_plugin_cache_default(self, monkeypatch, tmp_path):
        """Without CLAUDE_CONFIG_DIR, plugin cache is in ~/.claude."""
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        monkeypatch.delenv("SUPERCHARGE_ROOT", raising=False)
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

        # The actual ~/.claude/plugins/cache likely doesn't exist in test,
        # so it should fall through to CLI data dir. Just verify no crash.
        result = _hook_data_dir()
        assert isinstance(result, Path)

    def test_env_var_takes_priority_over_cache(self, monkeypatch, tmp_path):
        """CLAUDE_PLUGIN_ROOT still takes priority over plugin cache."""
        plugin_root = tmp_path / "plugin_root"
        plugin_root.mkdir()
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/should/not/be/used")

        result = _hook_data_dir()
        assert result == plugin_root


# ── _ensure_project_dir() ───────────────────────────────────────────────────


class TestEnsureProjectDir:
    """_ensure_project_dir() creates project dirs under _user_config_dir()."""

    def test_default_path(self, monkeypatch, tmp_path):
        """Default: creates ~/.claude/projects/<slug>/."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        _ensure_project_dir({"cwd": "/workspace/myproject"})

        expected = fake_home / ".claude" / "projects" / "-workspace-myproject"
        assert expected.is_dir()

    def test_custom_config_dir(self, monkeypatch, tmp_path):
        """With CLAUDE_CONFIG_DIR, creates <config>/projects/<slug>/."""
        config_dir = tmp_path / "custom_config"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))

        _ensure_project_dir({"cwd": "/workspace/myproject"})

        expected = config_dir / "projects" / "-workspace-myproject"
        assert expected.is_dir()

    def test_no_cwd_does_nothing(self, monkeypatch, tmp_path):
        """Empty cwd -> no directory created."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "config"))
        _ensure_project_dir({"cwd": ""})
        assert not (tmp_path / "config" / "projects").exists()

    def test_nonexistent_config_dir_created(self, monkeypatch, tmp_path):
        """CLAUDE_CONFIG_DIR pointing to non-existent dir: mkdir(parents=True) handles it."""
        config_dir = tmp_path / "does" / "not" / "exist"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))

        _ensure_project_dir({"cwd": "/myproject"})

        expected = config_dir / "projects" / "-myproject"
        assert expected.is_dir()


# ── Regression: permissions with custom CLAUDE_CONFIG_DIR ────────────────────


class TestPermissionsWithConfigDir:
    """Ensure add/remove permissions work correctly with custom config dir."""

    def test_add_permissions_custom_dir(self, monkeypatch, tmp_path):
        """_add_user_permissions writes to the custom config dir."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        settings_path = _user_settings_path()
        assert settings_path == tmp_path / "settings.json"

        added = _add_user_permissions(settings_path)
        assert len(added) > 0
        assert settings_path.exists()

        settings = json.loads(settings_path.read_text())
        assert "Bash(supercharge *)" in settings["permissions"]["allow"]

    def test_remove_permissions_custom_dir(self, monkeypatch, tmp_path):
        """_remove_user_permissions reads/writes from the custom config dir."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        settings_path = _user_settings_path()

        # First add, then remove
        _add_user_permissions(settings_path)
        removed = _remove_user_permissions(settings_path)
        assert removed > 0

        settings = json.loads(settings_path.read_text())
        assert "Bash(supercharge *)" not in settings["permissions"]["allow"]

    def test_end_to_end_settings_path_consistency(self, monkeypatch, tmp_path):
        """_user_settings_path always returns consistent path for same env."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "config"))
        path1 = _user_settings_path()
        path2 = _user_settings_path()
        assert path1 == path2
        assert path1.parent == tmp_path / "config"
