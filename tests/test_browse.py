"""Tests for the browse module — filesystem tree walker for .claude/SuperchargeAI/."""

from __future__ import annotations

from pathlib import Path

from supercharge.browse import (
    _build_browse_response,
    _build_memories_response,
    _build_tasks_response,
    _read_memory_content,
    _read_task_summary,
    _read_worker_summary,
    _walk_tree,
)


def _write(path: Path, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


# ── _walk_tree ───────────────────────────────────────────────────────────


class TestWalkTree:
    """Test _walk_tree on various directory structures."""

    def test_nested_dirs_and_files(self, tmp_path: Path):
        _write(tmp_path / "a" / "b.txt", "hello")
        _write(tmp_path / "a" / "c.txt", "world")
        _write(tmp_path / "d.txt", "top")

        tree = _walk_tree(tmp_path)
        assert tree["name"] == tmp_path.name
        assert tree["type"] == "dir"
        assert "children" in tree

        # Dirs come first, then files — both sorted alphabetically
        names = [c["name"] for c in tree["children"]]
        assert names == ["a", "d.txt"]

        subdir = tree["children"][0]
        assert subdir["type"] == "dir"
        sub_names = [c["name"] for c in subdir["children"]]
        assert sub_names == ["b.txt", "c.txt"]

    def test_hidden_files_skipped(self, tmp_path: Path):
        _write(tmp_path / ".hidden", "secret")
        _write(tmp_path / "visible.txt", "hello")

        tree = _walk_tree(tmp_path)
        names = [c["name"] for c in tree["children"]]
        assert ".hidden" not in names
        assert "visible.txt" in names

    def test_hidden_dirs_skipped(self, tmp_path: Path):
        _write(tmp_path / ".git" / "config", "x")
        _write(tmp_path / "src" / "main.py", "x")

        tree = _walk_tree(tmp_path)
        names = [c["name"] for c in tree["children"]]
        assert ".git" not in names
        assert "src" in names

    def test_empty_directory(self, tmp_path: Path):
        tree = _walk_tree(tmp_path)
        assert tree["type"] == "dir"
        assert tree["children"] == []

    def test_file_has_size_and_mtime(self, tmp_path: Path):
        _write(tmp_path / "f.txt", "12345")

        tree = _walk_tree(tmp_path)
        file_node = tree["children"][0]
        assert file_node["type"] == "file"
        assert file_node["size"] == 5
        assert "mtime" in file_node
        assert "children" not in file_node

    def test_dir_has_no_size(self, tmp_path: Path):
        (tmp_path / "subdir").mkdir()

        tree = _walk_tree(tmp_path)
        dir_node = tree["children"][0]
        assert dir_node["type"] == "dir"
        assert "children" in dir_node

    def test_dirs_before_files_alphabetical(self, tmp_path: Path):
        _write(tmp_path / "z_file.txt", "x")
        (tmp_path / "a_dir").mkdir()
        _write(tmp_path / "a_file.txt", "x")
        (tmp_path / "m_dir").mkdir()

        tree = _walk_tree(tmp_path)
        names = [c["name"] for c in tree["children"]]
        assert names == ["a_dir", "m_dir", "a_file.txt", "z_file.txt"]


# ── _read_task_summary ───────────────────────────────────────────────────


class TestReadTaskSummary:
    """Test _read_task_summary with various task.md files."""

    def test_valid_frontmatter(self, tmp_path: Path):
        task_dir = tmp_path / "my-task"
        task_dir.mkdir()
        (task_dir / "task.md").write_text(
            "---\n"
            "task_uuid: abc-123\n"
            "agent_type: code\n"
            "created_at: 2026-01-01T00:00:00+00:00\n"
            "created_by: orchestrator:s1\n"
            "task_name: implement auth\n"
            "---\n\n# Task\n"
        )

        result = _read_task_summary(task_dir)
        assert result is not None
        assert result["task_uuid"] == "abc-123"
        assert result["agent_type"] == "code"
        assert result["created_at"] == "2026-01-01T00:00:00+00:00"
        assert result["created_by"] == "orchestrator:s1"
        assert result["task_name"] == "implement auth"

    def test_missing_task_md(self, tmp_path: Path):
        task_dir = tmp_path / "no-task"
        task_dir.mkdir()
        assert _read_task_summary(task_dir) is None

    def test_malformed_frontmatter(self, tmp_path: Path):
        task_dir = tmp_path / "bad-task"
        task_dir.mkdir()
        (task_dir / "task.md").write_text("No frontmatter here.\n")

        result = _read_task_summary(task_dir)
        # Empty frontmatter -> None (no task_uuid)
        assert result is None

    def test_missing_optional_fields(self, tmp_path: Path):
        task_dir = tmp_path / "minimal-task"
        task_dir.mkdir()
        (task_dir / "task.md").write_text(
            "---\n"
            "task_uuid: xyz-456\n"
            "agent_type: plan\n"
            "created_at: 2026-02-01T00:00:00+00:00\n"
            "created_by: user\n"
            "---\n"
        )

        result = _read_task_summary(task_dir)
        assert result is not None
        assert result["task_uuid"] == "xyz-456"
        assert "task_name" not in result


# ── _read_worker_summary ─────────────────────────────────────────────────


class TestReadWorkerSummary:
    """Test _read_worker_summary with various worker .md files."""

    def test_valid_worker(self, tmp_path: Path):
        wf = tmp_path / "w1.md"
        wf.write_text(
            "---\n"
            "worker_id: w1\n"
            "agent_type: code\n"
            "spawned_at: 2026-01-01T01:00:00+00:00\n"
            "created_by: task:abc-123\n"
            "model: opus\n"
            "---\n\n# Worker\n"
        )

        result = _read_worker_summary(wf)
        assert result is not None
        assert result["worker_id"] == "w1"
        assert result["agent_type"] == "code"
        assert result["spawned_at"] == "2026-01-01T01:00:00+00:00"
        assert result["created_by"] == "task:abc-123"
        assert result["model"] == "opus"

    def test_invalid_worker_file(self, tmp_path: Path):
        wf = tmp_path / "bad.md"
        wf.write_text("not frontmatter\n")

        result = _read_worker_summary(wf)
        assert result is None

    def test_nonexistent_worker_file(self, tmp_path: Path):
        result = _read_worker_summary(tmp_path / "missing.md")
        assert result is None

    def test_minimal_worker(self, tmp_path: Path):
        wf = tmp_path / "w2.md"
        wf.write_text(
            "---\n"
            "worker_id: w2\n"
            "agent_type: code\n"
            "spawned_at: 2026-01-01T02:00:00+00:00\n"
            "created_by: task:def-789\n"
            "---\n"
        )

        result = _read_worker_summary(wf)
        assert result is not None
        assert result["worker_id"] == "w2"
        assert "model" not in result


# ── _build_browse_response ───────────────────────────────────────────────


class TestBuildBrowseResponse:
    """Test _build_browse_response with a realistic folder structure."""

    def _make_structure(self, root: Path) -> None:
        """Build a realistic .claude/SuperchargeAI layout."""
        sa = root / ".claude" / "SuperchargeAI"

        # tasks/plan/uuid1/
        plan_dir = sa / "tasks" / "plan" / "uuid-plan-1"
        plan_dir.mkdir(parents=True)
        (plan_dir / "task.md").write_text(
            "---\n"
            "task_uuid: uuid-plan-1\n"
            "agent_type: plan\n"
            "created_at: 2026-01-01T00:00:00+00:00\n"
            "created_by: user\n"
            "---\n"
        )

        # tasks/code/uuid2/
        code_dir = sa / "tasks" / "code" / "uuid-code-1"
        code_dir.mkdir(parents=True)
        (code_dir / "task.md").write_text(
            "---\n"
            "task_uuid: uuid-code-1\n"
            "agent_type: code\n"
            "created_at: 2026-01-02T00:00:00+00:00\n"
            "created_by: orchestrator:s1\n"
            "---\n"
        )
        workers_dir = code_dir / "workers"
        workers_dir.mkdir()
        (workers_dir / "w1.md").write_text(
            "---\n"
            "worker_id: w1\n"
            "agent_type: code\n"
            "spawned_at: 2026-01-02T01:00:00+00:00\n"
            "created_by: task:uuid-code-1\n"
            "---\n"
        )

        # archive/
        archive_dir = sa / "archive"
        archive_dir.mkdir(parents=True)
        (archive_dir / "old-task").mkdir()
        (archive_dir / "old-task" / "task.md").write_text(
            "---\n"
            "task_uuid: old-task\n"
            "agent_type: code\n"
            "created_at: 2025-12-01T00:00:00+00:00\n"
            "created_by: user\n"
            "---\n"
        )

    def test_response_has_required_keys(self, tmp_path: Path):
        self._make_structure(tmp_path)
        sa_root = tmp_path / ".claude" / "SuperchargeAI"

        resp = _build_browse_response(project_dir=tmp_path)
        assert "root" in resp
        assert "tasks" in resp
        assert "archive" in resp
        assert "tree" in resp

    def test_root_path(self, tmp_path: Path):
        self._make_structure(tmp_path)

        resp = _build_browse_response(project_dir=tmp_path)
        assert resp["root"] == str(tmp_path / ".claude" / "SuperchargeAI")

    def test_tasks_grouped_by_agent_type(self, tmp_path: Path):
        self._make_structure(tmp_path)

        resp = _build_browse_response(project_dir=tmp_path)
        assert "plan" in resp["tasks"]
        assert "code" in resp["tasks"]
        assert len(resp["tasks"]["plan"]) == 1
        assert len(resp["tasks"]["code"]) == 1
        assert resp["tasks"]["plan"][0]["uuid"] == "uuid-plan-1"
        assert resp["tasks"]["code"][0]["uuid"] == "uuid-code-1"

    def test_task_entry_has_expected_fields(self, tmp_path: Path):
        self._make_structure(tmp_path)

        resp = _build_browse_response(project_dir=tmp_path)
        entry = resp["tasks"]["code"][0]
        assert "uuid" in entry
        assert "folder" in entry
        assert "agent_type" in entry
        assert "created_at" in entry

    def test_archive_populated(self, tmp_path: Path):
        self._make_structure(tmp_path)

        resp = _build_browse_response(project_dir=tmp_path)
        assert len(resp["archive"]) == 1
        assert resp["archive"][0]["uuid"] == "old-task"

    def test_tree_present(self, tmp_path: Path):
        self._make_structure(tmp_path)

        resp = _build_browse_response(project_dir=tmp_path)
        assert resp["tree"]["type"] == "dir"
        assert resp["tree"]["name"] == "SuperchargeAI"

    def test_empty_project(self, tmp_path: Path):
        sa = tmp_path / ".claude" / "SuperchargeAI"
        sa.mkdir(parents=True)

        resp = _build_browse_response(project_dir=tmp_path)
        assert resp["tasks"] == {}
        assert resp["archive"] == []
        assert resp["tree"]["type"] == "dir"

    def test_nonexistent_project_dir(self, tmp_path: Path):
        resp = _build_browse_response(project_dir=tmp_path)
        assert resp["tasks"] == {}
        assert resp["archive"] == []
        assert resp["tree"] is None


# ── _build_memories_response ─────────────────────────────────────────────


class TestBuildMemoriesResponse:
    """Test _build_memories_response with various memory directory layouts."""

    def _make_memory(self, root: Path, rel_path: str, content: str) -> None:
        """Create a memory file at the given relative path under memory/."""
        mem_root = root / ".claude" / "SuperchargeAI" / "memory"
        full = mem_root / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)

    def test_realistic_memory_dir(self, tmp_path: Path):
        self._make_memory(tmp_path, "project/db-gotchas.md", (
            "---\n"
            "title: Database gotchas\n"
            "keywords: [postgres, migrations]\n"
            "created: 2026-01-01\n"
            "updated: 2026-02-15\n"
            "---\n\n"
            "# Content\n\n"
            "Always run migrations in a transaction.\n"
        ))
        self._make_memory(tmp_path, "project/api-patterns.md", (
            "---\n"
            "title: API patterns\n"
            "keywords: [rest, validation]\n"
            "created: 2026-01-10\n"
            "updated: 2026-01-10\n"
            "---\n\n"
            "# Content\n\n"
            "Use Pydantic for request validation.\n"
        ))
        self._make_memory(tmp_path, "methodology/behavior/no-force-push.md", (
            "---\n"
            "title: No force push\n"
            "keywords: [git, safety]\n"
            "created: 2026-01-05\n"
            "updated: 2026-01-05\n"
            "---\n\n"
            "# Content\n\n"
            "Never force push to main.\n"
        ))

        resp = _build_memories_response(project_dir=tmp_path)
        cats = resp["categories"]
        assert "project" in cats
        assert "methodology" in cats
        assert len(cats["project"]) == 2
        assert len(cats["methodology"]) == 1

        # Verify entry fields
        entry = cats["project"][0]  # api-patterns.md (sorted)
        assert entry["title"] == "API patterns"
        assert entry["keywords"] == ["rest", "validation"]
        assert entry["created"] == "2026-01-10"
        assert "path" in entry
        assert "preview" in entry

    def test_empty_memory_dir(self, tmp_path: Path):
        mem = tmp_path / ".claude" / "SuperchargeAI" / "memory"
        mem.mkdir(parents=True)

        resp = _build_memories_response(project_dir=tmp_path)
        assert resp["categories"] == {}

    def test_no_memory_dir(self, tmp_path: Path):
        resp = _build_memories_response(project_dir=tmp_path)
        assert resp["categories"] == {}

    def test_malformed_frontmatter(self, tmp_path: Path):
        self._make_memory(tmp_path, "project/broken.md",
                          "No frontmatter here, just content.\n")

        resp = _build_memories_response(project_dir=tmp_path)
        cats = resp["categories"]
        assert "project" in cats
        entry = cats["project"][0]
        # Falls back to stem for title
        assert entry["title"] == "broken"
        assert entry["keywords"] == []

    def test_category_grouping_top_level(self, tmp_path: Path):
        """Top-level files (not in a subdirectory) go into 'root' category."""
        self._make_memory(tmp_path, "index.md", (
            "---\n"
            "title: Memory Index\n"
            "keywords: [index]\n"
            "created: 2026-01-01\n"
            "updated: 2026-01-01\n"
            "---\n\n"
            "Top-level memory file.\n"
        ))

        resp = _build_memories_response(project_dir=tmp_path)
        assert "root" in resp["categories"]
        assert resp["categories"]["root"][0]["title"] == "Memory Index"


# ── _build_tasks_response ────────────────────────────────────────────────


class TestBuildTasksResponse:
    """Test _build_tasks_response with active and archived tasks."""

    def _make_task(self, root: Path, agent_type: str, uuid: str,
                   title: str = "Do something", has_result: bool = False,
                   archived: bool = False) -> None:
        """Create a task folder with task.md and optionally result.md."""
        sa = root / ".claude" / "SuperchargeAI"
        if archived:
            task_dir = sa / "archive" / uuid
        else:
            task_dir = sa / "tasks" / agent_type / uuid
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "task.md").write_text(
            "---\n"
            f"task_uuid: {uuid}\n"
            f"agent_type: {agent_type}\n"
            "created_at: 2026-01-01T00:00:00+00:00\n"
            "created_by: user\n"
            "---\n\n"
            "# Task\n\n"
            f"{title}\n"
        )
        if has_result:
            (task_dir / "result.md").write_text("# Result\n\nDone.\n")

    def test_active_and_archived_grouping(self, tmp_path: Path):
        self._make_task(tmp_path, "plan", "p1", "Plan it")
        self._make_task(tmp_path, "code", "c1", "Code it", has_result=True)
        self._make_task(tmp_path, "code", "c-old", "Old code", archived=True, has_result=True)

        resp = _build_tasks_response(project_dir=tmp_path)
        assert "plan" in resp["active"]
        assert "code" in resp["active"]
        assert len(resp["archived"]) == 1
        assert resp["archived"][0]["uuid"] == "c-old"

    def test_status_detection(self, tmp_path: Path):
        self._make_task(tmp_path, "code", "done1", "Done task", has_result=True)
        self._make_task(tmp_path, "code", "pend1", "Pending task", has_result=False)

        resp = _build_tasks_response(project_dir=tmp_path)
        entries = resp["active"]["code"]
        by_uuid = {e["uuid"]: e for e in entries}
        assert by_uuid["done1"]["status"] == "completed"
        assert by_uuid["done1"]["has_result"] is True
        assert by_uuid["pend1"]["status"] == "pending"
        assert by_uuid["pend1"]["has_result"] is False

    def test_title_extraction(self, tmp_path: Path):
        self._make_task(tmp_path, "plan", "t1", "Implement user authentication")

        resp = _build_tasks_response(project_dir=tmp_path)
        entry = resp["active"]["plan"][0]
        assert entry["title"] == "Implement user authentication"

    def test_empty_project(self, tmp_path: Path):
        resp = _build_tasks_response(project_dir=tmp_path)
        assert resp["active"] == {}
        assert resp["archived"] == []

    def test_entry_has_expected_fields(self, tmp_path: Path):
        self._make_task(tmp_path, "code", "f1", "Check fields")

        resp = _build_tasks_response(project_dir=tmp_path)
        entry = resp["active"]["code"][0]
        for key in ("uuid", "agent_type", "created_at", "title", "status", "has_result"):
            assert key in entry, f"Missing key: {key}"


# ── _read_memory_content ─────────────────────────────────────────────────


class TestReadMemoryContent:
    """Test _read_memory_content with various file states."""

    def _make_memory(self, root: Path, rel_path: str, content: str) -> None:
        mem_root = root / ".claude" / "SuperchargeAI" / "memory"
        full = mem_root / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)

    def test_valid_file(self, tmp_path: Path):
        self._make_memory(tmp_path, "project/patterns.md", (
            "---\n"
            "title: Patterns\n"
            "keywords: [design, arch]\n"
            "created: 2026-01-01\n"
            "updated: 2026-01-01\n"
            "---\n\n"
            "# Content\n\n"
            "Use dependency injection.\n"
        ))

        result = _read_memory_content("project/patterns.md", project_dir=tmp_path)
        assert result is not None
        assert result["path"] == "project/patterns.md"
        assert result["title"] == "Patterns"
        assert result["keywords"] == ["design", "arch"]
        assert "Use dependency injection." in result["content"]

    def test_nonexistent_file(self, tmp_path: Path):
        # Ensure memory dir exists but file doesn't
        mem = tmp_path / ".claude" / "SuperchargeAI" / "memory"
        mem.mkdir(parents=True)

        result = _read_memory_content("project/missing.md", project_dir=tmp_path)
        assert result is None

    def test_file_without_frontmatter(self, tmp_path: Path):
        self._make_memory(tmp_path, "project/plain.md",
                          "Just plain content, no frontmatter.\n")

        result = _read_memory_content("project/plain.md", project_dir=tmp_path)
        assert result is not None
        assert result["title"] == "plain"  # falls back to stem
        assert result["keywords"] == []
        assert "Just plain content" in result["content"]

    def test_path_traversal_prevention(self, tmp_path: Path):
        # Create the memory dir so the root resolves
        mem = tmp_path / ".claude" / "SuperchargeAI" / "memory"
        mem.mkdir(parents=True)

        # Try to escape via ../
        result = _read_memory_content("../../etc/passwd", project_dir=tmp_path)
        assert result is None

    def test_path_traversal_with_embedded_dotdot(self, tmp_path: Path):
        mem = tmp_path / ".claude" / "SuperchargeAI" / "memory"
        mem.mkdir(parents=True)

        result = _read_memory_content("project/../../../etc/passwd", project_dir=tmp_path)
        assert result is None
