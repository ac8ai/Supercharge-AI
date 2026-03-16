"""Community methodology contribution pipeline.

Handles context stripping, similarity matching, and GitHub Issue submission
for sharing anonymised methodology memory with the SuperchargeAI community.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from supercharge import __version__
from supercharge.paths import _read_frontmatter

_DEFAULT_REPO = "ac8ai/Supercharge-AI"

# ── Regex patterns ──────────────────────────────────────────────────────────

_PATH_RE = re.compile(r"(?:/[a-zA-Z0-9_.-]+){2,}|~/\S+")
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_REFERENCE_RE = re.compile(r"(?:task|worker):[a-zA-Z0-9]+")
_KEYWORDS_BODY_RE = re.compile(r"\*\*Keywords:\*\*\s*(.+)")


# ── Internal helpers ────────────────────────────────────────────────────────


def _parse_keywords(raw: str) -> list[str]:
    """Parse a keyword string like '[a, b, c]' into a list."""
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return [k.strip() for k in raw.split(",") if k.strip()]


def _extract_content_after_frontmatter(text: str) -> str:
    """Return everything after the closing '---' of frontmatter."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return text
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "".join(lines[i + 1 :])
    return text


# ── Context stripping ───────────────────────────────────────────────────────


def strip_context(content: str) -> str:
    """Remove project-specific details from a methodology memory's content.

    - Removes the entire '# Notes' section (everything from '# Notes' to EOF)
    - Removes lines starting with 'Source:'
    - Replaces absolute file paths with '<path>'
    - Replaces UUID patterns (8-4-4-4-12 hex) with '<uuid>'
    - Replaces task/worker references (task:abc or worker:def) with '<reference>'
    - Strips leading/trailing whitespace from result
    """
    # Remove everything from '# Notes' to end of file
    notes_match = re.search(r"^# Notes\b", content, re.MULTILINE)
    if notes_match:
        content = content[: notes_match.start()]

    # Remove lines starting with 'Source:'
    lines = content.splitlines()
    lines = [line for line in lines if not line.startswith("Source:")]
    content = "\n".join(lines)

    # Replace absolute file paths
    content = _PATH_RE.sub("<path>", content)

    # Replace UUID patterns
    content = _UUID_RE.sub("<uuid>", content)

    # Replace task/worker references
    content = _REFERENCE_RE.sub("<reference>", content)

    return content.strip()


# ── Similarity matching ─────────────────────────────────────────────────────


def keyword_similarity(keywords_a: list[str], keywords_b: list[str]) -> float:
    """Compute Jaccard similarity between two keyword lists.

    Returns |intersection| / |union|. Returns 0.0 if both lists are empty.
    """
    set_a = {k.lower() for k in keywords_a}
    set_b = {k.lower() for k in keywords_b}
    if not set_a and not set_b:
        return 0.0
    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / len(union)


def find_similar_issues(
    keywords: list[str],
    repo: str = _DEFAULT_REPO,
) -> list[dict]:
    """Search GitHub Issues with 'community-memory' label for similar keywords.

    Runs ``gh issue list`` and compares each issue's keywords (parsed from
    the ``**Keywords:** ...`` line in the body) against the given keywords
    using Jaccard similarity.

    Returns issues with similarity >= 0.5, sorted by similarity descending.
    Each result dict has: number, title, url, body, similarity, keywords.
    """
    result = subprocess.run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--label",
            "community-memory",
            "--json",
            "number,title,url,body",
            "--limit",
            "100",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []

    try:
        issues = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return []

    similar: list[dict] = []
    for issue in issues:
        body = issue.get("body", "") or ""
        m = _KEYWORDS_BODY_RE.search(body)
        issue_keywords: list[str] = _parse_keywords(m.group(1)) if m else []

        score = keyword_similarity(keywords, issue_keywords)
        if score >= 0.5:
            similar.append(
                {
                    "number": issue["number"],
                    "title": issue["title"],
                    "url": issue["url"],
                    "body": body,
                    "similarity": score,
                    "keywords": issue_keywords,
                }
            )

    similar.sort(key=lambda x: x["similarity"], reverse=True)
    return similar


# ── Issue formatting ────────────────────────────────────────────────────────


def _format_new_issue_body(
    title: str,
    keywords: list[str],
    category: str,
    content: str,
    version: str | None = None,
) -> str:
    """Format the body for a new community memory issue."""
    if version is None:
        version = __version__
    kw_str = ", ".join(keywords)
    return (
        f"## Memory Contribution\n\n"
        f"**Title:** {title}\n"
        f"**Keywords:** {kw_str}\n"
        f"**Category:** {category}\n\n"
        f"### Content\n"
        f"{content}\n\n"
        f"---\n"
        f"_Submitted via SuperchargeAI v{version}_\n"
    )


def _format_comment_body(
    title: str,
    keywords: list[str],
    content: str,
    version: str | None = None,
) -> str:
    """Format the body for a comment on an existing community memory issue."""
    if version is None:
        version = __version__
    kw_str = ", ".join(keywords)
    return (
        f"Another user encountered this pattern:\n\n"
        f"**Title:** {title}\n"
        f"**Keywords:** {kw_str}\n\n"
        f"### Content\n"
        f"{content}\n\n"
        f"---\n"
        f"_Submitted via SuperchargeAI v{version}_\n"
    )


# ── Submission ──────────────────────────────────────────────────────────────


def submit_contribution(
    memory_path: Path,
    repo: str = _DEFAULT_REPO,
) -> dict:
    """Submit a methodology memory file as a GitHub Issue contribution.

    Full flow:
    1. Read memory file, parse frontmatter (title, keywords) and content.
    2. Determine category from parent directory name.
    3. Strip context from content.
    4. Search for similar issues via find_similar_issues().
    5. If similar issue found (score >= 0.7): add comment on existing issue.
    6. If no similar issue: create new issue with label 'community-memory'.

    Returns {'action': 'created'|'commented', 'issue_url': str, 'issue_number': int}.
    """
    text = memory_path.read_text(encoding="utf-8")
    fm = _read_frontmatter(memory_path)

    title = fm.get("title", memory_path.stem)
    raw_keywords = fm.get("keywords", "")
    keywords = _parse_keywords(raw_keywords)
    category = memory_path.parent.name

    content = _extract_content_after_frontmatter(text)
    stripped = strip_context(content)

    similar = find_similar_issues(keywords, repo)
    # Pick the best match with similarity >= 0.7
    best = next((issue for issue in similar if issue["similarity"] >= 0.7), None)

    if best is not None:
        body = _format_comment_body(title, keywords, stripped)
        result = subprocess.run(
            [
                "gh",
                "issue",
                "comment",
                str(best["number"]),
                "--repo",
                repo,
                "--body",
                body,
            ],
            capture_output=True,
            text=True,
        )
        result.check_returncode()
        return {
            "action": "commented",
            "issue_url": best["url"],
            "issue_number": best["number"],
        }
    else:
        issue_title = f"[Community Memory] {title}"
        body = _format_new_issue_body(title, keywords, category, stripped)
        result = subprocess.run(
            [
                "gh",
                "issue",
                "create",
                "--repo",
                repo,
                "--title",
                issue_title,
                "--label",
                "community-memory",
                "--body",
                body,
            ],
            capture_output=True,
            text=True,
        )
        result.check_returncode()
        issue_url = result.stdout.strip()
        m = re.search(r"/issues/(\d+)$", issue_url)
        issue_number = int(m.group(1)) if m else 0
        return {
            "action": "created",
            "issue_url": issue_url,
            "issue_number": issue_number,
        }


# ── Scanning ────────────────────────────────────────────────────────────────


def list_candidates(methodology_dir: Path) -> list[dict]:
    """Scan methodology directory for contribution candidates.

    Returns files with 'contribution_candidate: true' in frontmatter,
    skipping files with 'contribution_status: submitted'.

    Each result dict has: path, title, keywords, category, content.
    Category is the parent directory name (e.g. 'behavior' or 'flows').
    Content is everything after the frontmatter block.
    """
    candidates: list[dict] = []
    for md_file in methodology_dir.rglob("*.md"):
        fm = _read_frontmatter(md_file)
        if fm.get("contribution_candidate", "").lower() != "true":
            continue
        if fm.get("contribution_status", "").lower() == "submitted":
            continue

        title = fm.get("title", md_file.stem)
        raw_keywords = fm.get("keywords", "")
        keywords = _parse_keywords(raw_keywords)
        category = md_file.parent.name

        text = md_file.read_text(encoding="utf-8")
        content = _extract_content_after_frontmatter(text)

        candidates.append(
            {
                "path": md_file,
                "title": title,
                "keywords": keywords,
                "category": category,
                "content": content,
            }
        )

    return candidates


def mark_submitted(memory_path: Path, issue_url: str) -> None:
    """Update frontmatter: set contribution_status=submitted and contribution_url."""
    text = memory_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    if not lines or lines[0].strip() != "---":
        return

    # Find the closing '---'
    closing_idx: int | None = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            closing_idx = i
            break

    if closing_idx is None:
        return

    # Remove existing contribution_status and contribution_url lines
    new_lines: list[str] = []
    for i, line in enumerate(lines):
        if i == 0 or i >= closing_idx:
            new_lines.append(line)
            continue
        stripped = line.lstrip()
        if stripped.startswith("contribution_status:") or stripped.startswith(
            "contribution_url:"
        ):
            continue
        new_lines.append(line)

    # Find the new closing '---' index after removals
    new_closing_idx: int | None = None
    for i, line in enumerate(new_lines[1:], start=1):
        if line.strip() == "---":
            new_closing_idx = i
            break

    if new_closing_idx is None:
        return

    # Insert contribution_status and contribution_url before closing '---'
    new_lines.insert(new_closing_idx, f"contribution_url: {issue_url}\n")
    new_lines.insert(new_closing_idx, "contribution_status: submitted\n")

    memory_path.write_text("".join(new_lines), encoding="utf-8")


def mark_rejected(memory_path: Path) -> None:
    """Update frontmatter: replace contribution_candidate: true with false."""
    text = memory_path.read_text(encoding="utf-8")
    new_text = re.sub(
        r"^(contribution_candidate:\s*)true\b",
        r"\g<1>false",
        text,
        flags=re.MULTILINE,
    )
    memory_path.write_text(new_text, encoding="utf-8")


# ── Prerequisite checking ───────────────────────────────────────────────────


def check_gh_available() -> tuple[bool, str]:
    """Check if the gh CLI is installed and authenticated.

    Returns (True, '') if available and authenticated.
    Returns (False, error_message) otherwise.
    """
    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
        )
    except FileNotFoundError:
        return (False, "gh CLI is not installed. Install from https://cli.github.com/")

    if result.returncode != 0:
        return (False, "gh CLI is not authenticated. Run: gh auth login")

    return (True, "")
