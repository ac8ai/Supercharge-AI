"""Tests that agent definition files are consistent with the permission system.

Every agent must be able to write result.md and notes.md — both as a
Task-tool agent (via `tools:` frontmatter) and as a worker (via
_AGENT_PERMISSIONS deep_tools). This test catches mismatches like a
definition saying `tools: Read, Glob, Grep` while `_AGENT_PERMISSIONS`
includes Write in deep_tools.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from supercharge.permissions import _AGENT_PERMISSIONS

AGENTS_DIR = Path(__file__).resolve().parent.parent / "agents"


def _parse_tools_from_frontmatter(agent_file: Path) -> list[str]:
    """Extract the tools list from YAML frontmatter of an agent definition."""
    content = agent_file.read_text()
    # Frontmatter is between --- markers
    match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    assert match, f"No YAML frontmatter found in {agent_file.name}"
    frontmatter = match.group(1)
    for line in frontmatter.splitlines():
        if line.startswith("tools:"):
            tools_str = line.split(":", 1)[1].strip()
            return [t.strip() for t in tools_str.split(",")]
    pytest.fail(f"No 'tools:' field in frontmatter of {agent_file.name}")


def _all_agent_names() -> list[str]:
    """Return all agent type names from definition files."""
    return [f.stem for f in sorted(AGENTS_DIR.glob("*.md"))]


class TestAgentDefinitionTools:
    """Verify agent definitions include tools needed for core operations."""

    @pytest.mark.parametrize("agent_name", _all_agent_names())
    def test_agent_has_write_in_tools_frontmatter(self, agent_name: str):
        """Every agent must have Write in its tools: frontmatter.

        All agents write result.md and notes.md as Task-tool subagents.
        The tools: field in the agent definition controls which tools are
        available to Task-tool agents (first-level agents spawned by the
        orchestrator). Without Write, the agent cannot produce deliverables.
        """
        agent_file = AGENTS_DIR / f"{agent_name}.md"
        tools = _parse_tools_from_frontmatter(agent_file)
        assert "Write" in tools, (
            f"Agent '{agent_name}' is missing Write in tools: frontmatter. "
            f"Current tools: {tools}. "
            f"All agents must have Write to produce result.md and notes.md."
        )

    @pytest.mark.parametrize("agent_name", _all_agent_names())
    def test_agent_has_write_in_worker_permissions(self, agent_name: str):
        """Every agent's deep workers must have Write in allowed_tools.

        Workers also need to write to their context files and (for some
        scopes) to result.md/notes.md.
        """
        if agent_name not in _AGENT_PERMISSIONS:
            pytest.skip(f"No _AGENT_PERMISSIONS entry for '{agent_name}'")
        perms = _AGENT_PERMISSIONS[agent_name]
        deep_tools = perms["deep_tools"]
        assert "Write" in deep_tools, (
            f"Agent '{agent_name}' worker is missing Write in deep_tools. "
            f"Current deep_tools: {deep_tools}."
        )

    @pytest.mark.parametrize("agent_name", _all_agent_names())
    def test_definition_tools_subset_consistency(self, agent_name: str):
        """Agent definition tools should be a superset of worker deep_tools
        for tools that both systems understand (Read, Write, Edit, Glob, Grep, Bash).

        If a worker can use Write, the Task-tool agent should also be able to.
        """
        if agent_name not in _AGENT_PERMISSIONS:
            pytest.skip(f"No _AGENT_PERMISSIONS entry for '{agent_name}'")

        agent_file = AGENTS_DIR / f"{agent_name}.md"
        definition_tools = set(_parse_tools_from_frontmatter(agent_file))
        worker_tools = set(_AGENT_PERMISSIONS[agent_name]["deep_tools"])

        # Only check tools that both systems recognize (file-level tools)
        file_tools = {"Read", "Write", "Edit", "Glob", "Grep"}
        worker_file_tools = worker_tools & file_tools
        definition_file_tools = definition_tools & file_tools

        missing = worker_file_tools - definition_file_tools
        assert not missing, (
            f"Agent '{agent_name}' workers have {missing} but the agent definition "
            f"does not. Workers: {sorted(worker_tools)}, "
            f"Definition: {sorted(definition_tools)}. "
            f"If workers need these tools, the Task-tool agent likely does too."
        )
