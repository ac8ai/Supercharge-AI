# Supercharge-AI

Python tools and hooks for Claude Code.

## Prerequisites

- [uv](https://docs.astral.sh/uv/)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## Installation

### Option A: As submodule (recommended)

```bash
cd ~/.claude
git submodule add git@github.com:ac8ai/Supercharge-AI.git Supercharge-AI
git config submodule.recurse true
```

### Option B: Standalone clone

```bash
git clone git@github.com:ac8ai/Supercharge-AI.git ~/.claude/Supercharge-AI
```

## Configuration

Add to `~/.claude/CLAUDE.md`:

```markdown
@Supercharge-AI/CLAUDE.md
```

## Usage

All tools are exposed as entry points. Run from any project:

```bash
UV_PROJECT_ENVIRONMENT=$PWD/.claude/.venv uv run --project ~/.claude/Supercharge-AI <command>
```

## Supported Commands

*In development*

## Updating

*TBD - workflow to be defined*
