#!/usr/bin/env python3
"""SuperchargeAI CLI — thin entry point delegating to submodules."""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import click

from supercharge.hooks import hook_pre_tool_use, hook_session_start, hook_subagent_start
from supercharge.metrics import _emit
from supercharge.paths import (
    AmbiguousPrefixError,
    _archive_root,
    _cli_data_dir,
    _copy_template,
    _find_task_dir,
    _read_frontmatter,
    _resolve_prefix,
    _task_root,
)
from supercharge.permissions import (
    _ENV_TASK_UUID,
    _ENV_WORKER_ID,
    _add_user_permissions,
    _find_worker_file,
    _get_remaining_depth,
    _is_fast_mode,
    _remove_user_permissions,
    _user_settings_path,
)
from supercharge.workers import (
    _deep_worker_init,
    _deep_worker_resume,
    _fast_worker_init,
    _memory_agent_run,
    _prepare_worker_file,
)

# ── Main group ───────────────────────────────────────────────────────────────


@click.group()
def supercharge():
    """SuperchargeAI - multi-agent framework for Claude Code."""


@supercharge.command("version")
def version_cmd():
    """Print installed version."""
    from supercharge import __version__

    click.echo(__version__)


# ── Hook commands (defined in hooks.py, registered here) ─────────────────────

supercharge.add_command(hook_session_start)
supercharge.add_command(hook_subagent_start)
supercharge.add_command(hook_pre_tool_use)

# ── init / deinit ────────────────────────────────────────────────────────────

_SUPERCHARGE_MARKER = "supercharge-ai"


def _find_claude_md(project_dir: str | None = None) -> Path:
    """Locate CLAUDE.md in the project's .claude/ directory."""
    root = Path(project_dir) if project_dir else Path.cwd()
    return root / ".claude" / "CLAUDE.md"


@supercharge.command()
@click.option("--project-dir", default=None, help="Project root (default: cwd)")
@click.option(
    "--add-permissions",
    is_flag=True,
    default=False,
    help="Add tool permissions to ~/.claude/settings.json (needed for VS Code).",
)
def init(project_dir: str | None, add_permissions: bool):
    """Add SuperchargeAI include line to the project's CLAUDE.md."""
    claude_md = _find_claude_md(project_dir)

    if claude_md.exists() and _SUPERCHARGE_MARKER in claude_md.read_text():
        click.echo("Already configured — CLAUDE.md contains supercharge-ai reference.")
    else:
        # Resolve the absolute path to claude-md.md template
        data_dir = _cli_data_dir()
        template_path = data_dir / "prompts" / "claude-md.md"
        if not template_path.exists():
            raise click.ClickException(f"Template not found: {template_path}")

        include_line = f"\nSupercharge-AI: @{template_path}\n"

        claude_md.parent.mkdir(parents=True, exist_ok=True)
        with claude_md.open("a") as f:
            f.write(include_line)

        click.echo(f"Added to {claude_md}:")
        click.echo(f"  Supercharge-AI: @{template_path}")

    if add_permissions:
        settings_path = _user_settings_path()
        added = _add_user_permissions(settings_path)
        if added:
            click.echo(f"\nAdded to {settings_path}:")
            for entry in added:
                click.echo(f"  {entry}")
            click.echo(
                "\nThese permissions are needed for VS Code where plugin hooks "
                "don't fire yet (https://github.com/anthropics/claude-code/issues/18547)."
            )
            click.echo("Remove with: supercharge permissions remove")
        else:
            click.echo("\nPermissions already present in settings.json.")


@supercharge.command()
@click.option("--project-dir", default=None, help="Project root (default: cwd)")
def deinit(project_dir: str | None):
    """Remove SuperchargeAI include line from the project's CLAUDE.md."""
    claude_md = _find_claude_md(project_dir)

    if not claude_md.exists():
        click.echo("No CLAUDE.md found.")
        return

    lines = claude_md.read_text().splitlines(keepends=True)
    filtered = [line for line in lines if _SUPERCHARGE_MARKER not in line]

    if len(filtered) == len(lines):
        click.echo("No supercharge-ai reference found in CLAUDE.md.")
        return

    claude_md.write_text("".join(filtered))
    click.echo(f"Removed supercharge-ai reference from {claude_md}.")


# ── permissions ──────────────────────────────────────────────────────────────


@supercharge.group()
def permissions():
    """Manage SuperchargeAI tool permissions in ~/.claude/settings.json."""


@permissions.command("remove")
def permissions_remove():
    """Remove SuperchargeAI permission entries from ~/.claude/settings.json."""
    settings_path = _user_settings_path()
    removed = _remove_user_permissions(settings_path)
    if removed:
        click.echo(f"Removed {removed} SuperchargeAI permission(s) from {settings_path}.")
    else:
        click.echo("No SuperchargeAI permissions found in settings.json.")


# ── slug generation ──────────────────────────────────────────────────────────

def _name_to_slug(name: str) -> str:
    """Convert a human-readable task name to a kebab-case slug.

    ``"Implement Auth Middleware"`` -> ``"implement-auth-middleware"``

    Strips non-alphanumeric chars (except spaces/hyphens), lowercases,
    replaces spaces with hyphens, and collapses multiple hyphens.
    """
    # Strip non-alphanumeric except spaces and hyphens
    slug = re.sub(r"[^a-zA-Z0-9 \-]", "", name)
    slug = slug.lower().strip()
    # Replace spaces with hyphens
    slug = slug.replace(" ", "-")
    # Collapse multiple hyphens
    slug = re.sub(r"-{2,}", "-", slug)
    # Strip leading/trailing hyphens
    slug = slug.strip("-")
    return slug


# ── author validation ────────────────────────────────────────────────────────


def _validate_author(author: str) -> str:
    """Validate author signature format: <type>:<id>.

    Validates:
    - orchestrator:<id> -- format check only (non-empty id)
    - task:<uuid> -- looks up via _find_task_dir(), rejects if not found
    - worker:<id> -- looks up via _find_worker_file(), rejects if not found

    Returns the validated author string.
    Raises click.ClickException on invalid format or failed lookup.
    """
    if ":" not in author:
        raise click.ClickException(
            f"Invalid author format: '{author}'. Expected <type>:<id>."
        )

    author_type, _, author_id = author.partition(":")

    if not author_id:
        raise click.ClickException(
            f"Invalid author format: '{author}'. ID must not be empty."
        )

    if author_type == "orchestrator":
        return author
    elif author_type == "task":
        try:
            if not _find_task_dir(author_id):
                raise click.ClickException(f"Task '{author_id}' not found.")
        except AmbiguousPrefixError as e:
            raise click.ClickException(
                f"Ambiguous task prefix '{e.prefix}' — matches: {', '.join(e.matches)}"
            )
        return author
    elif author_type == "worker":
        try:
            if not _find_worker_file(author_id):
                raise click.ClickException(f"Worker '{author_id}' not found.")
        except AmbiguousPrefixError as e:
            raise click.ClickException(
                f"Ambiguous worker prefix '{e.prefix}' — matches: {', '.join(e.matches)}"
            )
        return author
    else:
        raise click.ClickException(
            f"Unknown author type: '{author_type}'. Expected orchestrator, task, or worker."
        )


# ── task ─────────────────────────────────────────────────────────────────────


@supercharge.group()
def task():
    """Manage task workspaces for native subagents."""


@task.command("init")
@click.argument("agent_type")
@click.option(
    "--author", default=None,
    help="Author: orchestrator:<session_id>, task:<uuid>, or worker:<id>",
)
@click.option(
    "--name", required=True,
    help="Human-readable task name (e.g. 'Implement Auth Middleware').",
)
@click.option(
    "--full", "print_full", is_flag=True, default=False,
    help="Print full UUID instead of 8-char short ID.",
)
def task_init(agent_type: str, author: str | None, name: str, print_full: bool):
    """Create a new task workspace. Prints the short ID (or full UUID with --full)."""
    root = _task_root()

    # Generate UUID, check for 8-char prefix collision, regenerate if needed
    for _ in range(10):
        task_id = str(uuid.uuid4())
        prefix = task_id[:8]
        collision = False
        if root.exists():
            for agent_dir in root.iterdir():
                if agent_dir.is_dir():
                    for d in agent_dir.iterdir():
                        if d.is_dir() and d.name.startswith(prefix):
                            collision = True
                            break
                if collision:
                    break
        if not collision:
            break
    else:
        raise click.ClickException("Failed to generate unique short ID after 10 attempts")

    # Determine folder name
    slug = _name_to_slug(name)
    folder_name = f"{prefix}-{slug}" if slug else prefix

    task_dir = root / agent_type / folder_name

    task_dir.mkdir(parents=True, exist_ok=True)

    _copy_template("task.md", task_dir / "task.md")
    _copy_template("result.md", task_dir / "result.md")
    _copy_template("notes.md", task_dir / "notes.md")

    # Inject YAML frontmatter into task.md
    task_md = task_dir / "task.md"
    frontmatter_fields = [
        f"task_uuid: {task_id}",
        f"task_name: {name}",
    ]
    frontmatter_fields.extend([
        f"agent_type: {agent_type}",
        f"created_at: {datetime.now(timezone.utc).isoformat()}",
    ])
    if author:
        _validate_author(author)
        frontmatter_fields.append(f"created_by: {author}")
    frontmatter = "---\n" + "\n".join(frontmatter_fields) + "\n---\n\n"

    # Build content: name header + original template
    original = task_md.read_text()
    header = f"# {name}\n\n"
    task_md.write_text(frontmatter + header + original)

    _emit(
        "task_init",
        task_uuid=task_id,
        agent_type=agent_type,
        parent_id=author or "",
    )

    click.echo(task_id if print_full else task_id[:8])


@task.command("cleanup")
@click.argument("task_uuids", nargs=-1, required=True)
def task_cleanup(task_uuids: tuple[str, ...]):
    """Safely delete task folders after memory harvesting.

    Accepts full UUIDs, short prefixes, or folder names. Confirms the path
    is inside .claude/SuperchargeAI/tasks/ before removing. Accepts multiple IDs.
    """
    import shutil

    for task_uuid in task_uuids:
        try:
            task_dir = _find_task_dir(task_uuid)
            if not task_dir:
                click.echo(f"Error: Task {task_uuid} not found", err=True)
                continue

            # Resolve to full UUID for metrics/logging
            fm = _read_frontmatter(task_dir / "task.md")
            resolved_uuid = fm.get("task_uuid", task_uuid)

            # Verify the resolved path is actually inside the task root
            task_root = _task_root()
            try:
                task_dir.resolve().relative_to(task_root.resolve())
            except ValueError:
                click.echo(
                    f"Error: Task directory {task_dir} is outside task root {task_root}",
                    err=True,
                )
                continue

            shutil.rmtree(task_dir)
            _emit("task_cleanup", task_uuid=resolved_uuid)
            click.echo(f"Removed {task_dir}")
        except AmbiguousPrefixError as e:
            click.echo(
                f"Error: Ambiguous prefix '{e.prefix}' — matches: {', '.join(e.matches)}",
                err=True,
            )
        except Exception as e:
            click.echo(f"Error processing {task_uuid}: {e}", err=True)


@task.command("archive")
@click.argument("task_uuids", nargs=-1, required=True)
@click.option("--title", default=None, help="Archive title (default: from task.md)")
@click.option("--force", is_flag=True, default=False, help="Archive even if result.md is missing")
def task_archive(task_uuids: tuple[str, ...], title: str | None, force: bool):
    """Archive research/plan task folders to .claude/SuperchargeAI/archive/.

    Extracts the Report section from result.md, writes an archive file with
    YAML frontmatter, and removes the original task directory.
    Accepts full UUIDs, short prefixes, or folder names for batch operation.
    """
    import shutil

    for task_uuid in task_uuids:
        try:
            # 1. Find task dir (supports prefixes)
            task_dir = _find_task_dir(task_uuid)
            if not task_dir:
                click.echo(f"Error: Task {task_uuid} not found", err=True)
                continue

            # 2. Resolve to full UUID for frontmatter/metrics
            fm = _read_frontmatter(task_dir / "task.md")
            resolved_uuid = fm.get("task_uuid", task_uuid)

            # 3. Verify inside task root
            task_root = _task_root()
            try:
                task_dir.resolve().relative_to(task_root.resolve())
            except ValueError:
                click.echo(
                    f"Error: Task directory {task_dir} is outside task root {task_root}",
                    err=True,
                )
                continue

            # 4. Check agent type
            agent_type = task_dir.parent.name
            if agent_type not in ("research", "plan"):
                click.echo(
                    f"Error: Archive is only for research/plan tasks. "
                    f"Use 'supercharge task cleanup' instead. (got: {agent_type})",
                    err=True,
                )
                continue

            # 5. Read task.md
            task_md_path = task_dir / "task.md"
            task_md_content = task_md_path.read_text() if task_md_path.exists() else ""

            # 6. Read result.md
            result_md_path = task_dir / "result.md"
            report_section = ""
            if not result_md_path.exists():
                if not force:
                    click.echo(
                        f"Error: {task_uuid} has no result.md. Use --force to archive anyway.",
                        err=True,
                    )
                    continue
                msg = f"Warning: {task_uuid} has no result.md, archiving task.md only."
                click.echo(msg, err=True)
            else:
                result_content = result_md_path.read_text()
                # Extract ## Report section (to next ## heading or EOF)
                report_match = re.search(
                    r"(## Report\s*\n)(.*?)(?=\n## |\Z)",
                    result_content,
                    re.DOTALL,
                )
                if report_match:
                    report_section = report_match.group(1) + report_match.group(2).rstrip()
                else:
                    report_section = result_content.rstrip()

            # 7. Determine title
            archive_title = title
            if not archive_title:
                heading_match = re.search(r"^#\s+(.+)$", task_md_content, re.MULTILINE)
                if heading_match:
                    raw_title = heading_match.group(1).strip()
                    # Slugify: lowercase, replace non-alnum with hyphens, truncate
                    slug = re.sub(r"[^a-z0-9]+", "-", raw_title.lower()).strip("-")[:50]
                    archive_title = slug if slug else "untitled"
                else:
                    archive_title = "untitled"

            # 8. Build filename
            now = datetime.now(timezone.utc)
            timestamp = now.strftime("%Y-%m-%dT%H%M")
            filename = f"{timestamp}_{agent_type}_{archive_title}.md"

            # 9. Create archive dir
            archive_dir = _archive_root()
            archive_dir.mkdir(parents=True, exist_ok=True)

            # 10. Write archive file
            archive_path = archive_dir / filename
            frontmatter = (
                "---\n"
                f"task_uuid: {resolved_uuid}\n"
                f"agent_type: {agent_type}\n"
                f"archived_at: {now.isoformat()}\n"
                "---\n\n"
            )
            parts = [frontmatter, task_md_content.rstrip()]
            if report_section:
                parts.append("\n\n---\n\n" + report_section)
            archive_path.write_text("\n".join(parts) + "\n")

            # 11. Remove original
            shutil.rmtree(task_dir)

            _emit("task_archive", task_uuid=resolved_uuid, agent_type=agent_type)

            # 12. Print archive path
            click.echo(str(archive_path))

        except AmbiguousPrefixError as e:
            click.echo(
                f"Error: Ambiguous prefix '{e.prefix}' — matches: {', '.join(e.matches)}",
                err=True,
            )
        except Exception as e:
            click.echo(f"Error processing {task_uuid}: {e}", err=True)


@task.command("resolve")
@click.argument("prefix")
def task_resolve(prefix: str):
    """Resolve a short prefix to a full task UUID. Prints the full UUID."""
    try:
        result = _resolve_prefix(prefix)
    except AmbiguousPrefixError as e:
        click.echo(
            f"Ambiguous prefix '{e.prefix}' — matches: {', '.join(e.matches)}",
            err=True,
        )
        raise SystemExit(1)
    except ValueError as e:
        raise click.ClickException(str(e))

    if result is None:
        raise click.ClickException(f"No task found matching prefix '{prefix}'.")

    full_uuid, _folder_name = result
    click.echo(full_uuid)


# ── subtask ──────────────────────────────────────────────────────────────────


@supercharge.group()
def subtask():
    """Manage Agent SDK workers within a task."""


@subtask.command("init")
@click.argument("agent_type")
@click.argument("prompt")
@click.option(
    "--task-uuid",
    envvar=_ENV_TASK_UUID,
    default=None,
    help="Parent task UUID (agents pass this; workers get it from env).",
)
@click.option(
    "--model",
    default=None,
    help="Model override (sonnet, opus, haiku)",
)
@click.option(
    "--author", default=None,
    help="Author: task:<uuid> or worker:<id>",
)
@click.option(
    "--short", "print_short", is_flag=True, default=False,
    help="Return worker_id[:8] in JSON instead of full UUID.",
)
def subtask_init(
    agent_type: str,
    prompt: str,
    task_uuid: str | None,
    model: str | None,
    author: str | None,
    print_short: bool,
):
    """Spawn a new Agent SDK worker on a task. Prints JSON {worker_id, result}."""
    from supercharge.signals import setup_signal_handlers

    setup_signal_handlers()

    # Resolve task UUID: --task-uuid flag > SUPERCHARGE_TASK_UUID env var
    # Click's envvar= already handles the fallback, so task_uuid may come
    # from either source. Validate consistency if both are present.
    env_uuid = os.environ.get(_ENV_TASK_UUID)
    if task_uuid and env_uuid and task_uuid != env_uuid:
        # Resolve the flag value to full UUID before comparing with env (always full UUID)
        resolved = _resolve_prefix(task_uuid)
        resolved_full = resolved[0] if resolved else task_uuid
        if resolved_full != env_uuid:
            raise click.ClickException(
                f"--task-uuid ({task_uuid}) conflicts with {_ENV_TASK_UUID} env var ({env_uuid})."
            )
    if not task_uuid:
        raise click.ClickException(f"Task UUID required. Pass --task-uuid or set {_ENV_TASK_UUID}.")

    # Resolve max_turns from env (optional)
    max_turns_str = os.environ.get("SUPERCHARGE_MAX_TURNS")
    max_turns = int(max_turns_str) if max_turns_str else None

    fast = _is_fast_mode(model)

    if not fast:
        remaining = _get_remaining_depth()
        if remaining <= 0:
            raise click.ClickException(
                "Max recursion depth reached (0 remaining). "
                "Set SUPERCHARGE_MAX_RECURSION_DEPTH in "
                "settings.json env to increase the limit."
            )
    else:
        remaining = 1  # fast workers always get budget=0 (1-1)

    task_dir = _find_task_dir(task_uuid)
    if not task_dir:
        raise click.ClickException(f"Task {task_uuid} not found")

    # Resolve author: explicit --author > auto-infer from env
    if author:
        _validate_author(author)
    else:
        env_worker_id = os.environ.get(_ENV_WORKER_ID)
        if env_worker_id:
            author = f"worker:{env_worker_id}"
        elif env_uuid:
            author = f"task:{env_uuid}"

    worker_id = str(uuid.uuid4())

    if fast:
        result = asyncio.run(
            _fast_worker_init(
                task_dir,
                agent_type,
                prompt,
                worker_id,
                max_turns,
                model,
            )
        )
    else:
        worker_file = _prepare_worker_file(task_dir, worker_id, prompt, author=author)
        result = asyncio.run(
            _deep_worker_init(
                task_dir,
                agent_type,
                prompt,
                worker_id,
                worker_file,
                remaining,
                max_turns,
                model,
            )
        )

    _emit(
        "subtask_init",
        task_uuid=task_uuid,
        worker_id=worker_id,
        agent_type=agent_type,
        parent_id=author or "",
        detail=json.dumps({"model": model or "default", "fast": fast}),
    )

    if print_short and "worker_id" in result:
        result["worker_id"] = result["worker_id"][:8]

    click.echo(json.dumps(result))


@subtask.command("resume")
@click.argument("worker_id")
@click.argument("prompt")
def subtask_resume(worker_id: str, prompt: str):
    """Resume a deep worker by worker_id. worker_id is the session_id."""
    from supercharge.signals import setup_signal_handlers

    setup_signal_handlers()

    # Verify the worker exists (has a context file)
    worker_file = _find_worker_file(worker_id)
    if not worker_file:
        raise click.ClickException(
            f"Worker {worker_id} not found. Only deep workers (opus/sonnet) can be resumed."
        )

    # Derive task_dir and agent_type from worker file path:
    # <task_root>/<agent_type>/<uuid>/workers/<worker_id>.md
    task_dir = worker_file.parent.parent
    agent_type = task_dir.parent.name

    result = asyncio.run(_deep_worker_resume(worker_id, prompt, task_dir, agent_type))

    _emit("subtask_resume", worker_id=worker_id)

    click.echo(json.dumps(result))


# ── memory ──────────────────────────────────────────────────────────────────


@supercharge.group()
def memory():
    """Background memory harvesting commands."""


@memory.command("run")
@click.argument("task_uuid")
def memory_run(task_uuid: str):
    """Run the memory agent on a task workspace (background process)."""
    from supercharge.signals import setup_signal_handlers

    setup_signal_handlers()
    asyncio.run(_memory_agent_run(task_uuid))


@memory.command("stamp")
@click.argument("transcript_path", type=click.Path(exists=True))
def memory_stamp(transcript_path: str):
    """Mark a transcript as reviewed by appending a stamp entry."""
    from supercharge.memory import _stamp_transcript

    _stamp_transcript(Path(transcript_path))
    click.echo(f"Stamped {transcript_path}")


# ── dashboard ────────────────────────────────────────────────────────────────


@supercharge.command()
@click.option("--port", default=None, type=int, help="Port to listen on (default: 9333)")
@click.option("--host", default="127.0.0.1", help="Host to bind to")
@click.option("--open", "open_browser", is_flag=True, help="Open browser on start")
def dashboard(port, host, open_browser):
    """Start the SuperchargeAI dashboard web server."""
    from supercharge.dashboard import _run_server

    _run_server(host=host, port=port, open_browser=open_browser)
