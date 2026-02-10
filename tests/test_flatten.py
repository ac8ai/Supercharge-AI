"""Tests for the flatten module."""

from pathlib import Path

from supercharge.flatten import flatten_imports, is_inside_code_block, resolve_path


class TestResolvePath:
    def test_relative_path(self, tmp_path: Path) -> None:
        result = resolve_path("foo/bar.md", tmp_path)
        assert result == tmp_path / "foo/bar.md"

    def test_home_path(self) -> None:
        result = resolve_path("~/test.md", Path("/some/dir"))
        assert result == Path.home() / "test.md"

    def test_absolute_path(self) -> None:
        result = resolve_path("/absolute/path.md", Path("/some/dir"))
        assert result == Path("/absolute/path.md")


class TestIsInsideCodeBlock:
    def test_normal_text(self) -> None:
        content = "Hello @world"
        assert is_inside_code_block(content, 6) is False

    def test_inside_backticks(self) -> None:
        content = "Hello `@world` there"
        assert is_inside_code_block(content, 7) is True

    def test_inside_fenced_block(self) -> None:
        content = "Before\n```\n@inside\n```\nAfter"
        match_pos = content.index("@inside")
        assert is_inside_code_block(content, match_pos) is True

    def test_after_fenced_block(self) -> None:
        content = "Before\n```\ncode\n```\n@after"
        match_pos = content.index("@after")
        assert is_inside_code_block(content, match_pos) is False

    def test_inside_fenced_with_language(self) -> None:
        content = "Before\n```python\n@inside\n```\nAfter"
        match_pos = content.index("@inside")
        assert is_inside_code_block(content, match_pos) is True


class TestFlattenImports:
    def test_no_imports(self) -> None:
        content = "Just plain text"
        result = flatten_imports(content, Path("/tmp"))
        assert result == content

    def test_missing_file(self) -> None:
        content = "Import @nonexistent"
        result = flatten_imports(content, Path("/tmp"))
        assert "<!-- NOT FOUND: nonexistent.md -->" in result

    def test_simple_import(self, tmp_path: Path) -> None:
        child = tmp_path / "child.md"
        child.write_text("Child content")

        content = "Before @child after"
        result = flatten_imports(content, tmp_path)

        assert "<!-- BEGIN: child.md -->" in result
        assert "Child content" in result
        assert "<!-- END: child.md -->" in result

    def test_recursive_import(self, tmp_path: Path) -> None:
        grandchild = tmp_path / "grandchild.md"
        grandchild.write_text("Grandchild content")

        child = tmp_path / "child.md"
        child.write_text("Child imports @grandchild")

        content = "Root imports @child"
        result = flatten_imports(content, tmp_path)

        assert "Child imports" in result
        assert "Grandchild content" in result

    def test_circular_import(self, tmp_path: Path) -> None:
        a = tmp_path / "a.md"
        b = tmp_path / "b.md"
        a.write_text("A imports @b")
        b.write_text("B imports @a")

        content = "Start @a"
        result = flatten_imports(content, tmp_path)

        assert "<!-- CIRCULAR: a.md -->" in result

    def test_max_depth(self, tmp_path: Path) -> None:
        content = "Test"
        result = flatten_imports(content, tmp_path, depth=5, max_depth=5)
        assert result == content

    def test_preserves_code_span(self) -> None:
        content = "Keep `@this` intact"
        result = flatten_imports(content, Path("/tmp"))
        assert result == content

    def test_preserves_code_block(self) -> None:
        content = "```\n@inside\n```"
        result = flatten_imports(content, Path("/tmp"))
        assert result == content

    def test_subfolder_import(self, tmp_path: Path) -> None:
        nested = tmp_path / "nested"
        nested.mkdir()
        child = nested / "child.md"
        child.write_text("Nested child")

        content = "Import @nested/child"
        result = flatten_imports(content, tmp_path)

        assert "Nested child" in result
