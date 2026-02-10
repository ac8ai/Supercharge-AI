#!/usr/bin/env python3
"""Flatten markdown files by resolving @path imports (Claude Code syntax)."""

import re
from pathlib import Path

import click


def resolve_path(import_path: str, base_dir: Path) -> Path:
    """Resolve import path relative to base directory or home."""
    path = Path(import_path).expanduser()
    return path if path.is_absolute() else base_dir / import_path


def is_inside_code_block(content: str, match_start: int) -> bool:
    """Check if position is inside a code block or code span."""
    before = content[:match_start]

    # Count backticks for code spans (odd = inside)
    backtick_count = before.count("`") - before.count("\\`")
    if backtick_count % 2 == 1:
        return True

    # Check for fenced code blocks
    lines_before = before.split("\n")
    fence_count = sum(1 for line in lines_before if re.match(r"^```", line.strip()))
    return fence_count % 2 == 1


def flatten_imports(
    content: str,
    base_dir: Path,
    resolved: set[str] | None = None,
    depth: int = 0,
    max_depth: int = 5,
) -> str:
    """Recursively resolve @import syntax in markdown content."""
    if resolved is None:
        resolved = set()

    if depth >= max_depth:
        return content

    pattern = r"(?<!`)@([^\s`\[\]]+)"

    def replace_import(match: re.Match) -> str:
        if is_inside_code_block(content, match.start()):
            return match.group(0)

        import_path = match.group(1)

        # Add .md extension if not present and no extension
        if "." not in Path(import_path).name:
            import_path = f"{import_path}.md"

        full_path = resolve_path(import_path, base_dir)
        path_str = str(full_path.resolve())

        if path_str in resolved:
            return f"<!-- CIRCULAR: {import_path} -->"

        if not full_path.exists():
            return f"<!-- NOT FOUND: {import_path} -->"

        resolved.add(path_str)

        imported_content = full_path.read_text()
        imported_content = flatten_imports(
            imported_content,
            full_path.parent,
            resolved,
            depth + 1,
            max_depth,
        )

        return f"<!-- BEGIN: {import_path} -->\n{imported_content}\n<!-- END: {import_path} -->"

    return re.sub(pattern, replace_import, content)


def flatten_file(
    input_path: Path, output_path: Path | None = None, max_depth: int = 5,
) -> str:
    """Flatten a markdown file with resolved @imports."""
    content = input_path.read_text()
    flattened = flatten_imports(content, input_path.parent, max_depth=max_depth)

    if output_path:
        output_path.write_text(flattened)

    return flattened


@click.command()
@click.argument("input_file", type=click.Path(exists=True, path_type=Path))
@click.argument("output_file", type=click.Path(path_type=Path), required=False)
@click.option("--max-depth", default=5, help="Maximum import recursion depth")
def main(input_file: Path, output_file: Path | None, max_depth: int) -> None:
    """Resolve @path imports in markdown files into a single document."""
    result = flatten_file(input_file, output_file, max_depth=max_depth)

    if not output_file:
        click.echo(result)


if __name__ == "__main__":
    main()
