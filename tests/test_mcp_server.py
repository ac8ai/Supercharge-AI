"""Tests for the MCP server review_contributions tool."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from supercharge.mcp_server import _check_mcp_available, _create_server

# ── Helpers ──────────────────────────────────────────────────────────────


def _make_elicitation_result(action: str = "accept", data: dict | None = None):
    """Create a mock elicitation result with .action and .data attributes."""
    result = MagicMock()
    result.action = action
    result.data = data
    return result


def _make_ctx(
    return_value=None,
    side_effect=None,
):
    """Create a mock MCP context with ctx.session.send_elicitation as AsyncMock."""
    ctx = MagicMock()
    ctx.session = MagicMock()
    ctx.session.send_elicitation = AsyncMock(
        return_value=return_value,
        side_effect=side_effect,
    )
    return ctx


def _make_candidate(
    tmp_path: Path,
    name: str = "pattern.md",
    title: str = "Test Pattern",
    keywords: list[str] | None = None,
    category: str = "behavior",
    content: str = "# Content\n\nMemory content here.\n",
) -> dict:
    """Create a candidate dict matching contribute.list_candidates output."""
    if keywords is None:
        keywords = ["testing", "automation"]
    filepath = tmp_path / category / name
    filepath.parent.mkdir(parents=True, exist_ok=True)
    filepath.touch()
    return {
        "path": filepath,
        "title": title,
        "keywords": keywords,
        "category": category,
        "content": content,
    }


# ── Fixture: extract tool from server ────────────────────────────────────


@pytest.fixture
def _get_tool():
    """Extract the review_contributions Tool object from a FastMCP server."""
    if not _check_mcp_available():
        pytest.skip("mcp package not installed")
    server = _create_server()
    tool = server._tool_manager.get_tool("review_contributions")
    assert tool is not None, "review_contributions tool not registered"
    return tool


# ── _check_mcp_available ─────────────────────────────────────────────────


class TestCheckMcpAvailable:
    """Test MCP package availability check."""

    def test_returns_true_when_mcp_importable(self):
        with patch.dict("sys.modules", {"mcp": MagicMock()}):
            assert _check_mcp_available() is True

    def test_returns_false_when_mcp_not_importable(self):
        with patch.dict("sys.modules", {"mcp": None}):
            # When a module is in sys.modules as None, import raises ImportError
            assert _check_mcp_available() is False


# ── _create_server ───────────────────────────────────────────────────────


class TestCreateServer:
    """Test MCP server creation."""

    def test_raises_import_error_when_mcp_missing(self):
        with patch.dict("sys.modules", {"mcp": None, "mcp.server.fastmcp": None}):
            with pytest.raises(ImportError, match="mcp package"):
                _create_server()

    @pytest.mark.skipif(not _check_mcp_available(), reason="mcp package not installed")
    def test_server_has_review_contributions_tool(self):
        server = _create_server()
        assert server is not None


# ── review_contributions tool ────────────────────────────────────────────


class TestReviewContributions:
    """Integration tests that invoke the review_contributions tool via FastMCP."""

    @pytest.mark.anyio
    async def test_no_candidates(self, _get_tool, tmp_path: Path):
        """No candidates -> returns {"status": "no_candidates", ...}."""
        tool = _get_tool
        ctx = _make_ctx()

        with (
            patch("supercharge.contribute.list_candidates", return_value=[]),
            patch("supercharge.paths._user_methodology_dir", return_value=tmp_path),
        ):
            raw = await tool.run({}, context=ctx)
            result = json.loads(raw)
            assert result["status"] == "no_candidates"

    @pytest.mark.anyio
    async def test_single_candidate_approve(self, _get_tool, tmp_path: Path):
        """Approve single candidate -> submit_contribution + mark_submitted called."""
        tool = _get_tool
        candidate = _make_candidate(tmp_path, title="Good Pattern")
        elicitation_result = _make_elicitation_result(
            action="accept", data={"action": "approve"}
        )
        ctx = _make_ctx(return_value=elicitation_result)

        submission = {
            "action": "created",
            "issue_url": "https://github.com/ac8ai/Supercharge-AI/issues/42",
            "issue_number": 42,
        }

        with (
            patch("supercharge.contribute.list_candidates", return_value=[candidate]),
            patch("supercharge.paths._user_methodology_dir", return_value=tmp_path),
            patch("supercharge.contribute.strip_context", return_value="stripped"),
            patch(
                "supercharge.contribute.submit_contribution", return_value=submission
            ) as mock_submit,
            patch("supercharge.contribute.mark_submitted") as mock_mark,
        ):
            raw = await tool.run({}, context=ctx)
            result = json.loads(raw)

            assert result["approved"] == 1
            assert result["rejected"] == 0
            assert result["skipped"] == 0
            assert len(result["issues"]) == 1
            assert result["issues"][0]["url"] == submission["issue_url"]

            mock_submit.assert_called_once_with(candidate["path"])
            mock_mark.assert_called_once_with(
                candidate["path"], submission["issue_url"]
            )

    @pytest.mark.anyio
    async def test_single_candidate_reject(self, _get_tool, tmp_path: Path):
        """Reject single candidate -> mark_rejected called."""
        tool = _get_tool
        candidate = _make_candidate(tmp_path, title="Bad Pattern")
        elicitation_result = _make_elicitation_result(
            action="accept", data={"action": "reject"}
        )
        ctx = _make_ctx(return_value=elicitation_result)

        with (
            patch("supercharge.contribute.list_candidates", return_value=[candidate]),
            patch("supercharge.paths._user_methodology_dir", return_value=tmp_path),
            patch("supercharge.contribute.strip_context", return_value="stripped"),
            patch("supercharge.contribute.mark_rejected") as mock_reject,
        ):
            raw = await tool.run({}, context=ctx)
            result = json.loads(raw)

            assert result["approved"] == 0
            assert result["rejected"] == 1
            assert result["skipped"] == 0
            mock_reject.assert_called_once_with(candidate["path"])

    @pytest.mark.anyio
    async def test_single_candidate_skip(self, _get_tool, tmp_path: Path):
        """Skip single candidate -> no mutations, skipped=1."""
        tool = _get_tool
        candidate = _make_candidate(tmp_path, title="Maybe Pattern")
        elicitation_result = _make_elicitation_result(
            action="accept", data={"action": "skip"}
        )
        ctx = _make_ctx(return_value=elicitation_result)

        with (
            patch("supercharge.contribute.list_candidates", return_value=[candidate]),
            patch("supercharge.paths._user_methodology_dir", return_value=tmp_path),
            patch("supercharge.contribute.strip_context", return_value="stripped"),
            patch("supercharge.contribute.submit_contribution") as mock_submit,
            patch("supercharge.contribute.mark_submitted") as mock_mark,
            patch("supercharge.contribute.mark_rejected") as mock_reject,
        ):
            raw = await tool.run({}, context=ctx)
            result = json.loads(raw)

            assert result["approved"] == 0
            assert result["rejected"] == 0
            assert result["skipped"] == 1
            mock_submit.assert_not_called()
            mock_mark.assert_not_called()
            mock_reject.assert_not_called()

    @pytest.mark.anyio
    async def test_multiple_candidates_accept_all(self, _get_tool, tmp_path: Path):
        """Multiple candidates + accept_all -> all submitted."""
        tool = _get_tool
        candidates = [
            _make_candidate(tmp_path, name="a.md", title="Pattern A"),
            _make_candidate(tmp_path, name="b.md", title="Pattern B"),
        ]
        # First elicitation: accept_all
        accept_all_result = _make_elicitation_result(
            action="accept", data={"action": "accept_all"}
        )
        ctx = _make_ctx(return_value=accept_all_result)

        submission_a = {
            "action": "created",
            "issue_url": "https://github.com/ac8ai/Supercharge-AI/issues/10",
            "issue_number": 10,
        }
        submission_b = {
            "action": "created",
            "issue_url": "https://github.com/ac8ai/Supercharge-AI/issues/11",
            "issue_number": 11,
        }

        with (
            patch("supercharge.contribute.list_candidates", return_value=candidates),
            patch("supercharge.paths._user_methodology_dir", return_value=tmp_path),
            patch(
                "supercharge.contribute.submit_contribution",
                side_effect=[submission_a, submission_b],
            ) as mock_submit,
            patch("supercharge.contribute.mark_submitted") as mock_mark,
        ):
            raw = await tool.run({}, context=ctx)
            result = json.loads(raw)

            assert result["approved"] == 2
            assert result["rejected"] == 0
            assert result["skipped"] == 0
            assert len(result["issues"]) == 2
            assert mock_submit.call_count == 2
            assert mock_mark.call_count == 2

    @pytest.mark.anyio
    async def test_multiple_candidates_review_individually(
        self, _get_tool, tmp_path: Path
    ):
        """Multiple candidates + review_individually -> each reviewed separately."""
        tool = _get_tool
        candidates = [
            _make_candidate(tmp_path, name="a.md", title="Pattern A"),
            _make_candidate(tmp_path, name="b.md", title="Pattern B"),
        ]

        # First elicitation: review_individually
        # Then: approve first, reject second
        review_individually = _make_elicitation_result(
            action="accept", data={"action": "review_individually"}
        )
        approve_result = _make_elicitation_result(
            action="accept", data={"action": "approve"}
        )
        reject_result = _make_elicitation_result(
            action="accept", data={"action": "reject"}
        )
        ctx = _make_ctx(
            side_effect=[review_individually, approve_result, reject_result]
        )

        submission = {
            "action": "created",
            "issue_url": "https://github.com/ac8ai/Supercharge-AI/issues/20",
            "issue_number": 20,
        }

        with (
            patch("supercharge.contribute.list_candidates", return_value=candidates),
            patch("supercharge.paths._user_methodology_dir", return_value=tmp_path),
            patch("supercharge.contribute.strip_context", return_value="stripped"),
            patch(
                "supercharge.contribute.submit_contribution", return_value=submission
            ) as mock_submit,
            patch("supercharge.contribute.mark_submitted") as mock_mark,
            patch("supercharge.contribute.mark_rejected") as mock_reject,
        ):
            raw = await tool.run({}, context=ctx)
            result = json.loads(raw)

            assert result["approved"] == 1
            assert result["rejected"] == 1
            assert result["skipped"] == 0
            mock_submit.assert_called_once_with(candidates[0]["path"])
            mock_mark.assert_called_once()
            mock_reject.assert_called_once_with(candidates[1]["path"])

    @pytest.mark.anyio
    async def test_elicitation_raises_exception_skips_candidate(
        self, _get_tool, tmp_path: Path
    ):
        """Elicitation raises exception -> candidate is skipped gracefully."""
        tool = _get_tool
        candidate = _make_candidate(tmp_path, title="Error Pattern")
        ctx = _make_ctx(side_effect=RuntimeError("elicitation failed"))

        with (
            patch("supercharge.contribute.list_candidates", return_value=[candidate]),
            patch("supercharge.paths._user_methodology_dir", return_value=tmp_path),
            patch("supercharge.contribute.strip_context", return_value="stripped"),
            patch("supercharge.contribute.submit_contribution") as mock_submit,
        ):
            raw = await tool.run({}, context=ctx)
            result = json.loads(raw)

            assert result["skipped"] == 1
            assert result["approved"] == 0
            mock_submit.assert_not_called()

    @pytest.mark.anyio
    async def test_submit_raises_exception_skips_that_candidate(
        self, _get_tool, tmp_path: Path
    ):
        """submit_contribution raises -> that candidate skipped, others continue."""
        tool = _get_tool
        candidates = [
            _make_candidate(tmp_path, name="a.md", title="Failing Pattern"),
            _make_candidate(tmp_path, name="b.md", title="Good Pattern"),
        ]

        # First: review_individually, then approve both
        review_individually = _make_elicitation_result(
            action="accept", data={"action": "review_individually"}
        )
        approve_result = _make_elicitation_result(
            action="accept", data={"action": "approve"}
        )
        ctx = _make_ctx(
            side_effect=[review_individually, approve_result, approve_result]
        )

        submission_ok = {
            "action": "created",
            "issue_url": "https://github.com/ac8ai/Supercharge-AI/issues/30",
            "issue_number": 30,
        }

        with (
            patch("supercharge.contribute.list_candidates", return_value=candidates),
            patch("supercharge.paths._user_methodology_dir", return_value=tmp_path),
            patch("supercharge.contribute.strip_context", return_value="stripped"),
            patch(
                "supercharge.contribute.submit_contribution",
                side_effect=[RuntimeError("submit failed"), submission_ok],
            ),
            patch("supercharge.contribute.mark_submitted") as mock_mark,
        ):
            raw = await tool.run({}, context=ctx)
            result = json.loads(raw)

            # First candidate fails submission -> skipped
            # Second candidate succeeds -> approved
            assert result["approved"] == 1
            assert result["skipped"] == 1
            mock_mark.assert_called_once()

    @pytest.mark.anyio
    async def test_user_declines_elicitation_treated_as_skip(
        self, _get_tool, tmp_path: Path
    ):
        """result.action == 'decline' -> treated as skip."""
        tool = _get_tool
        candidate = _make_candidate(tmp_path, title="Declined Pattern")
        decline_result = _make_elicitation_result(action="decline", data=None)
        ctx = _make_ctx(return_value=decline_result)

        with (
            patch("supercharge.contribute.list_candidates", return_value=[candidate]),
            patch("supercharge.paths._user_methodology_dir", return_value=tmp_path),
            patch("supercharge.contribute.strip_context", return_value="stripped"),
            patch("supercharge.contribute.submit_contribution") as mock_submit,
            patch("supercharge.contribute.mark_rejected") as mock_reject,
        ):
            raw = await tool.run({}, context=ctx)
            result = json.loads(raw)

            assert result["skipped"] == 1
            assert result["approved"] == 0
            assert result["rejected"] == 0
            mock_submit.assert_not_called()
            mock_reject.assert_not_called()


# ── Elicitation schema ───────────────────────────────────────────────────


class TestElicitationSchema:
    """Test that the elicitation schema matches the spec."""

    def test_review_schema_has_required_fields(self):
        """The review elicitation schema has the correct structure."""
        schema = {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["approve", "reject", "skip"],
                    "description": "What to do with this contribution",
                }
            },
            "required": ["action"],
        }

        assert schema["type"] == "object"
        assert "action" in schema["properties"]
        assert schema["properties"]["action"]["type"] == "string"
        assert set(schema["properties"]["action"]["enum"]) == {"approve", "reject", "skip"}
        assert "action" in schema["required"]

    def test_accept_all_schema_has_required_fields(self):
        """The accept-all elicitation schema has the correct structure."""
        schema = {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["accept_all", "review_individually"],
                    "description": "Accept all contributions or review each one",
                }
            },
            "required": ["action"],
        }

        assert schema["type"] == "object"
        assert "action" in schema["properties"]
        assert set(schema["properties"]["action"]["enum"]) == {
            "accept_all",
            "review_individually",
        }


# ── CLI integration ─────────────────────────────────────────────────────


class TestMcpCliGroup:
    """Test the mcp CLI group and serve command."""

    def test_mcp_group_exists(self):
        """The mcp group is registered on the supercharge CLI."""
        from supercharge.cli import supercharge as cli

        # Find the mcp command in the registered commands
        commands = cli.commands if hasattr(cli, "commands") else {}
        assert "mcp" in commands

    def test_serve_subcommand_exists(self):
        """The serve subcommand is registered under mcp."""
        from supercharge.cli import mcp

        commands = mcp.commands if hasattr(mcp, "commands") else {}
        assert "serve" in commands

    def test_serve_errors_when_mcp_not_installed(self):
        """serve command raises ClickException when mcp is not installed."""
        from click.testing import CliRunner

        from supercharge.cli import supercharge as cli

        runner = CliRunner()

        with patch(
            "supercharge.mcp_server._check_mcp_available",
            return_value=False,
        ):
            result = runner.invoke(cli, ["mcp", "serve"])
            assert result.exit_code != 0
            assert "mcp package" in result.output or "mcp package" in str(result.exception)
