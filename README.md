# Supercharge-AI

Multi-agent framework for Claude Code. Moves context to markdown, delegates recursively to workers, and learns from its mistakes through persistent memory.

Inspired by [Recursive Language Models (RLM)](https://arxiv.org/abs/2512.24601) and [Confucius Code Agent (CCA)](https://arxiv.org/abs/2512.10398) — recursive self-delegation from RLM, persistent note-taking and hierarchical orchestration from CCA.

## Installation

### From marketplace (stable)

```bash
claude plugin marketplace add ac8ai/Supercharge-AI
claude plugin install supercharge-ai
supercharge init --add-permissions
```

### From local clone (beta)

```bash
git clone https://github.com/ac8ai/Supercharge-AI.git
cd Supercharge-AI && git checkout beta
supercharge init --add-permissions
```

Local installs auto-pull the latest beta and install editable on each session start — no manual updating needed.

### What happens

On first session start, the plugin auto-installs `uv` and the `supercharge` CLI if missing. Prompts are injected automatically via hooks. `supercharge init` adds the SuperchargeAI include to your project's CLAUDE.md. The `--add-permissions` flag adds permission entries so you don't get constant approval dialogs.

Linux and macOS only.

## How it works

Once installed, SuperchargeAI takes over Claude Code's delegation. Instead of doing everything in one context window, it:

1. **Plans** — decomposes your request into scoped tasks
2. **Delegates** — sends each task to a specialized agent (code, review, research, etc.)
3. **Recurses** — agents can spawn workers, workers can spawn sub-workers (up to 5 levels deep)
4. **Remembers** — harvests learnings into persistent memory after each task

Everything flows through markdown files in `.claude/SuperchargeAI/` — task briefs, notes, results, memory. Context windows are temporary; the markdown is permanent.

### Agent types

| Agent | Purpose |
|-------|---------|
| `plan` | Decompose requests into structured task lists |
| `code` | Implement features, fix bugs, write tests |
| `document` | Update documentation to reflect changes |
| `research` | Search the web, gather external context |
| `review` | Code review of completed work |
| `consistency` | Check for contradictions, broken references, duplication |
| `memory` | Maintain project and methodology memory from task results |

## Key commands

```bash
supercharge init              # Set up SuperchargeAI in your project
supercharge deinit            # Remove it
supercharge dashboard         # Web UI for metrics and session traces
supercharge version           # Check installed version
```

Most other commands (`task init`, `subtask init`, `memory run`, etc.) are used by the agents themselves — you don't need to run them manually.

## Configuration

Override in `.claude/settings.json` or `.claude/settings.local.json`:

```json
{
  "env": {
    "SUPERCHARGE_MAX_RECURSION_DEPTH": "3",
    "SUPERCHARGE_MAX_TURNS": "50"
  }
}
```

| Variable | Default | Purpose |
|----------|---------|---------|
| `SUPERCHARGE_MAX_RECURSION_DEPTH` | `5` | How many levels deep workers can spawn sub-workers |
| `SUPERCHARGE_MAX_TURNS` | (none) | Limit worker turns per invocation |
| `SUPERCHARGE_FAST_MODELS` | `haiku` | Models that use fast (fire-and-forget) worker mode |

## Branching and releases

- `main` — stable releases, what marketplace users get
- `beta` — pre-release testing via local clone
- Feature branches merge into `beta`, then `beta` into `main`

Version format follows PEP 440: `0.4.0` (stable), `0.4.0b1` (beta). See `.claude/CLAUDE.md` for details.

## Developer docs

- [docs/stack-propagation.md](docs/stack-propagation.md) — env var, context, and identifier flow through the orchestrator/agent/worker stack
