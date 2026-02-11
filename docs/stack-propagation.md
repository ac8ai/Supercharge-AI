# Stack Propagation

How context, env vars, and identifiers flow through the SuperchargeAI stack.

## Layers

```
Orchestrator  (Claude Code session, user-facing)
    |
    | Task tool (native subagent)
    v
Agent         (native subagent, gets protocol via SubagentStart hook)
    |
    | supercharge subtask init <agent_type> "<prompt>" --task-uuid <uuid> --model <model>
    v
Worker        (Agent SDK process, deep or fast)
    |
    | supercharge subtask init <agent_type> "<prompt>" --model <model>
    v
Sub-worker    (Agent SDK process, recursion depth - 1)
```

## Environment Variables

| Variable | Orchestrator | Agent | Worker | Sub-worker |
|----------|-------------|-------|--------|------------|
| `CLAUDE_PROJECT_DIR` | Not set | Not set | Set (resolved*) | Set (inherited) |
| `SUPERCHARGE_RECURSION_REMAINING` | Not set | Not set | Set (default - 1) | Set (parent - 1) |
| `SUPERCHARGE_TASK_UUID` | Not set | Not set | Set (task UUID) | Set (same UUID) |
| `SUPERCHARGE_MAX_RECURSION_DEPTH` | Optional (settings.json) | Inherited | Inherited | Inherited |
| `CLAUDE_PLUGIN_ROOT` | Set by Claude Code | Set by Claude Code | Not set | Not set |

\* `_build_options()` resolves `CLAUDE_PROJECT_DIR` via env var → `git rev-parse --show-toplevel` → cwd, then propagates the resolved value to all child workers via env dict.

### Recursion Depth Flow

1. Agent runs `supercharge subtask init`. `_get_remaining_depth()` checks env:
   - `SUPERCHARGE_RECURSION_REMAINING` set? Use it.
   - `SUPERCHARGE_MAX_RECURSION_DEPTH` set? Use it.
   - Neither? Default to 5.
2. `_build_options()` sets `env={SUPERCHARGE_RECURSION_REMAINING: remaining - 1, SUPERCHARGE_TASK_UUID: <uuid>, CLAUDE_PROJECT_DIR: <resolved>}`.
3. Agent SDK merges: `{**os.environ, **options.env}` (confirmed from SDK source).
4. Worker process inherits merged env. When it runs `supercharge subtask init`, step 1 repeats with the decremented value.

Example: default depth 5 -> worker gets 4 -> sub-worker gets 3 -> ... -> budget 0 = cannot spawn.

Fast (haiku) workers always get budget 0 and cannot spawn.

### Task UUID Flow

Task UUID propagates through two mechanisms:

1. **Env var (deterministic):** `_build_options()` sets `SUPERCHARGE_TASK_UUID` in env. All child processes inherit it automatically via SDK env merge. The `--task-uuid` CLI option uses Click's `envvar` parameter to read from this env var when the flag is omitted.

2. **Prompt (informational):** Workers receive `Parent task UUID: <uuid>` in their initial prompt for visibility. This is redundant with the env var but makes the UUID explicit in the worker's context.

| Level | How it gets the UUID | How it propagates |
|-------|---------------------|-------------------|
| Orchestrator | Runs `supercharge task init <agent_type>`, captures UUID | Writes to agent prompt |
| Agent | Extracts UUID from its task path | Passes `--task-uuid <uuid>` flag to `supercharge subtask init` |
| Worker | Env var `SUPERCHARGE_TASK_UUID` (set by `_build_options()`) + prompt | Env var auto-propagates; CLI resolves from env if flag omitted |
| Sub-worker | Same as worker | Same as worker (if budget allows) |

All workers under a task share the same parent task UUID. Sub-workers are stored at `<task_root>/<agent_type>/<uuid>/workers/<worker_id>.md`.

## Prompt Injection

| Level | Source | Content |
|-------|--------|---------|
| Orchestrator | SessionStart hook | directive + protocol + orchestrator |
| Orchestrator | CLAUDE.md `@path` include | claude-md.md (priority reinforcement) |
| Agent | SubagentStart hook | directive + protocol |
| Worker | `system_prompt` param | protocol + worker role |
| Sub-worker | `system_prompt` param | protocol + worker role |

On session resume, the SessionStart hook re-injects the full protocol (no early return).

## Data Directory Resolution

Two functions, used by different callers:

| Function | Used by | Priority |
|----------|---------|----------|
| `_hook_data_dir()` | SessionStart, SubagentStart hooks | `CLAUDE_PLUGIN_ROOT` -> `SUPERCHARGE_ROOT` -> plugin cache -> `_cli_data_dir()` |
| `_cli_data_dir()` | CLI commands, worker system prompts | `SUPERCHARGE_ROOT` -> installed package data -> dev source tree |

Hooks have `CLAUDE_PLUGIN_ROOT` set by Claude Code. CLI commands (run in Bash) do not.