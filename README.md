# Supercharge-AI

Python tools and hooks for Claude Code.

Inspired by [Recursive Language Models (RLM)](https://arxiv.org/abs/2512.24601) and [Confucius Code Agent (CCA)](https://arxiv.org/abs/2512.10398) — recursive self-delegation from RLM, persistent note-taking and hierarchical orchestration from CCA.

## Installation

```bash
claude plugin install supercharge-ai --plugin-dir ~/.claude/Supercharge-AI
```

On first session start, the plugin auto-installs `uv` and the `supercharge` CLI if missing. Prompts are injected automatically via SessionStart and SubagentStart hooks.

## Architecture

**Three-layer system:**
1. **Orchestrator** - Top-level Claude Code session with Task tool for native subagents
2. **Agents** - Native `.claude/agents/*.md` for high-level coordination
3. **Workers** - Claude Agent SDK (Python) for low-level execution

Workers use Agent SDK instead of `claude -p` for:
- Direct API calls (no subprocess overhead)
- Session resumption across context resets
- Custom tool configurations per agent type
- Recursion depth tracking via environment variables

## Commands

### `supercharge task init <agent_type>`

Create a new task workspace. Prints the UUID.

### `supercharge subtask init <task_uuid> <agent_type> <prompt>`

Spawn a new Agent SDK worker on a task. Returns JSON `{worker_id, result}`.

Options:
- `--max-turns N` — cap on agentic turns
- `--model MODEL` — model override (sonnet, opus, haiku)

### `supercharge subtask resume <worker_id> <prompt>`

Resume a worker that stopped with Questions. Looks up session from worker file.

### `supercharge flatten <input_file> [output_file]`

Resolve `@path` imports in markdown files into a single document.

## Recursion Depth

Workers can spawn sub-workers recursively (RLM-style). A countdown
mechanism prevents infinite recursion.

### How it works

Two environment variables control depth:

| Variable | Set by | Purpose |
|----------|--------|---------|
| `SUPERCHARGE_MAX_RECURSION_DEPTH` | User (settings.json) | Initial budget for the first `subtask init` call |
| `SUPERCHARGE_RECURSION_REMAINING` | CLI (internal) | Countdown passed to each child worker |

**Heuristic in `subtask init`:**

1. `SUPERCHARGE_RECURSION_REMAINING` is set → we're inside a worker → use it
2. Not set → we're at the first call (orchestrator/agent level):
   - `SUPERCHARGE_MAX_RECURSION_DEPTH` is set → use it as initial budget
   - Not set → default to **5**

Each `subtask init` call decrements the remaining count by 1 and passes
it to the child via `ClaudeAgentOptions(env=...)`. When remaining reaches
0, the CLI refuses to spawn and returns an error.

**Example chain (default 5):**

```
Agent calls subtask init          → remaining=5, child gets 4
  Worker calls subtask init       → remaining=4, child gets 3
    Sub-worker calls subtask init → remaining=3, child gets 2
      ...
        Worker at remaining=0     → CLI refuses: "Max recursion depth reached"
```

The worker's initial prompt includes its recursion budget so it knows
whether it can delegate.

### Configuration

Set `SUPERCHARGE_MAX_RECURSION_DEPTH` in your project's Claude Code
settings to override the default:

**`.claude/settings.json`** (checked into repo, shared with team):

```json
{
  "env": {
    "SUPERCHARGE_MAX_RECURSION_DEPTH": "3"
  }
}
```

**`.claude/settings.local.json`** (gitignored, personal override):

```json
{
  "env": {
    "SUPERCHARGE_MAX_RECURSION_DEPTH": "8"
  },
  "permissions": {
    "allow": ["..."]
  }
}
```

The `env` field in Claude Code settings sets environment variables for
the entire session. These propagate through Bash commands and Agent SDK
subprocesses automatically.

**Precedence** (highest wins):

1. `SUPERCHARGE_RECURSION_REMAINING` (already inside a worker)
2. `SUPERCHARGE_MAX_RECURSION_DEPTH` (user-configured in settings.json)
3. Default: 5

### Fast Model Configuration

`SUPERCHARGE_FAST_MODELS` controls which models use fast (fire-and-forget)
mode. Comma-separated, case-insensitive. When set, replaces the default
entirely.

| Variable | Default | Example |
|----------|---------|---------|
| `SUPERCHARGE_FAST_MODELS` | `haiku` | `haiku,sonnet` |

```json
{
  "env": {
    "SUPERCHARGE_FAST_MODELS": "haiku"
  }
}
```

Set to empty string to disable fast mode for all models.

## Agent Types

| Agent | Purpose |
|-------|---------|
| `plan` | Decompose requests into structured task lists |
| `code` | Implement features, fix bugs, write tests |
| `document` | Update documentation to reflect changes |
| `research` | Search the web, gather external context |
| `review` | Code review of completed work |
| `consistency` | Check for contradictions, broken references, duplication |
| `memory` | Maintain project and methodology memory |

## Worker Modes

Model determines mode: opus/sonnet → deep (context file, resume,
recursion), haiku → fast (fire-and-forget). See protocol.md for
agent-facing details.

## Worker Context File

Deep workers get a context file at `workers/<worker_id>.md` with these
sections:

| Section | Purpose |
|---------|---------|
| Assignment | Pre-filled by CLI. Do not modify. |
| Progress | Updated as worker works. |
| Result | Final deliverable. Agent reads this. |
| Files | Every file created or modified. |
| Questions | Blocks progress. Agent answers and resumes. |
| Errors | Problems encountered (not all require stopping). |
| Memory | Optional. Patterns, gotchas, instruction gaps for the memory agent. |

## Per-Agent Tool Permissions

Workers get tool access scoped by agent type. Two mechanisms:

1. **`allowed_tools`** — coarse filter: which tools a worker can see at all
2. **`can_use_tool` callback** — fine-grained: path-based Write/Edit scoping
   (deep workers only; fast workers don't support callbacks)

### Tool allowlists

| Agent | Deep worker tools | Fast worker tools |
|-------|-------------------|-------------------|
| `code` | Read, Write, Edit, Bash, Glob, Grep | Read, Glob, Grep |
| `plan` | Read, Write, Glob, Grep | Read, Glob, Grep |
| `review` | Read, Write, Bash, Glob, Grep | Read, Glob, Grep |
| `document` | Read, Write, Edit, Glob, Grep | Read, Glob, Grep |
| `research` | Read, Write, Glob, Grep, WebSearch, WebFetch | Read, Glob, Grep, WebSearch, WebFetch |
| `consistency` | Read, Write, Glob, Grep | Read, Glob, Grep |
| `memory` | Read, Write, Glob, Grep | Read, Glob, Grep |

All deep workers get Write for their context file. The `can_use_tool`
callback then scopes *where* they can write.

### Write scopes (deep workers)

| Scope | Agents | Write/Edit allowed |
|-------|--------|--------------------|
| `project` | code, document | Anywhere in project root |
| `memory` | memory | Memory dir + context file |
| `context` | plan, review, research, consistency | Context file only |

### Architectural enforcement

The `can_use_tool` callback also blocks `supercharge task init` in Bash
for all workers — only the orchestrator creates task workspaces.

## TODO

- [ ] End-to-end integration test
- [ ] CLI test suite