"""MCP server exposing a review_contributions tool with elicitation.

Allows users to review and approve/reject methodology memory contributions
from within a Claude Code conversation via the MCP protocol.
"""

import json
import logging

logger = logging.getLogger(__name__)


def _check_mcp_available() -> bool:
    """Check if the mcp package is importable."""
    try:
        import mcp  # noqa: F401

        return True
    except ImportError:
        return False


def _create_server():  # noqa: C901
    """Create and configure the MCP server with the review_contributions tool.

    Returns the configured ``mcp.server.fastmcp.FastMCP`` instance.

    Raises ImportError if the ``mcp`` package is not installed.
    """
    try:
        from mcp.server.fastmcp import Context, FastMCP
    except ImportError:
        raise ImportError(
            "MCP support requires the mcp package. "
            "Install with: uv pip install 'supercharge-ai[mcp]'"
        )

    mcp_server = FastMCP("supercharge-contribute")

    @mcp_server.tool()
    async def review_contributions(ctx: Context) -> str:
        """Review pending methodology memory contributions.

        Presents each contribution candidate for user review via elicitation.
        Users can approve, reject, or skip each candidate.
        """
        from supercharge.contribute import (
            list_candidates,
            mark_rejected,
            mark_submitted,
            strip_context,
            submit_contribution,
        )
        from supercharge.paths import _user_methodology_dir

        methodology_dir = _user_methodology_dir()
        candidates = list_candidates(methodology_dir)

        if not candidates:
            return json.dumps({
                "status": "no_candidates",
                "message": "No contribution candidates found.",
            })

        approved = 0
        rejected = 0
        skipped = 0
        issues: list[dict] = []

        # Offer "Accept All" if multiple candidates
        if len(candidates) > 1:
            accept_all = False
            try:
                result = await ctx.session.send_elicitation(
                    message=(
                        f"Found {len(candidates)} contribution candidate(s). "
                        "Would you like to accept all, or review individually?"
                    ),
                    requested_schema={
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["accept_all", "review_individually"],
                                "description": "Accept all contributions or review each one",
                            }
                        },
                        "required": ["action"],
                    },
                )
                if result and hasattr(result, "action") and result.action == "accept":
                    if result.data and result.data.get("action") == "accept_all":
                        accept_all = True
            except Exception:
                # Elicitation not supported or failed — fall through to individual review
                pass

            if accept_all:
                for candidate in candidates:
                    try:
                        submission = submit_contribution(candidate["path"])
                        mark_submitted(candidate["path"], submission["issue_url"])
                        approved += 1
                        issues.append({
                            "title": candidate["title"],
                            "url": submission["issue_url"],
                            "action": submission["action"],
                        })
                    except Exception as e:
                        logger.warning("Failed to submit %s: %s", candidate["title"], e)
                        skipped += 1

                return json.dumps({
                    "approved": approved,
                    "rejected": rejected,
                    "skipped": skipped,
                    "issues": issues,
                })

        # Individual review for each candidate
        for candidate in candidates:
            stripped = strip_context(candidate["content"])
            keywords_str = ", ".join(candidate["keywords"])

            try:
                result = await ctx.session.send_elicitation(
                    message=(
                        f"Review contribution: {candidate['title']}\n\n"
                        f"Keywords: {keywords_str}\n\n"
                        f"{stripped}"
                    ),
                    requested_schema={
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["approve", "reject", "skip"],
                                "description": "What to do with this contribution",
                            }
                        },
                        "required": ["action"],
                    },
                )
            except Exception:
                # Elicitation not supported — skip this candidate
                skipped += 1
                continue

            # Parse action from elicitation result
            action = "skip"
            if result and hasattr(result, "action"):
                if result.action == "accept" and result.data:
                    action = result.data.get("action", "skip")
                elif result.action == "decline":
                    action = "skip"

            if action == "approve":
                try:
                    submission = submit_contribution(candidate["path"])
                    mark_submitted(candidate["path"], submission["issue_url"])
                    approved += 1
                    issues.append({
                        "title": candidate["title"],
                        "url": submission["issue_url"],
                        "action": submission["action"],
                    })
                except Exception as e:
                    logger.warning("Failed to submit %s: %s", candidate["title"], e)
                    skipped += 1
            elif action == "reject":
                mark_rejected(candidate["path"])
                rejected += 1
            else:
                skipped += 1

        return json.dumps({
            "approved": approved,
            "rejected": rejected,
            "skipped": skipped,
            "issues": issues,
        })

    return mcp_server


def run_server() -> None:
    """Start the MCP server with stdio transport.

    Called by ``supercharge mcp serve``.
    """
    server = _create_server()
    server.run(transport="stdio")
