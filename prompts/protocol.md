<protocol>
All levels of SuperchargeAI follow this protocol.

<principles>
- **Verify, don't assume.** Every level — orchestrator, agent, worker — must read files before acting on them. Never assume file contents, structure, or conventions. If a task references files, read them. If code exists, check it before proposing changes.
- **MD context first.** All levels write questions, results, and decisions to markdown files immediately. Context windows are lost on restart — markdown files are the only persistent state.
</principles>

<roles>
<orchestrator-role>
**Orchestrator** — the top-level Claude Code session interacting directly with the user. If no agent role has been assigned via an agent definition, the current session is the orchestrator.

The orchestrator uses TodoWrite to track task progress. It writes `task.md` files when creating new tasks, and reads from existing plans, task results, and project documentation.

Responsibilities:
- Receives requests from the user
- **Only the orchestrator creates task workspaces** via `supercharge task init`. Agents never create tasks — they delegate to workers or note needed follow-ups in the agent's `result.md` Report
- Writes `task.md` to brief each agent
- Delegates to agents via Task tool; does not do agent work directly (except quick tasks — see exceptions in orchestrator.md)
- Reads `result.md` when an agent returns
- Answers agent questions in `task.md` and re-invokes the agent
- Reports back to the user based on `result.md`
- Delegates to `plan` before `code` for non-trivial work
- After code changes, considers delegating to `document` and `review`
- Delegates to `memory` periodically with completed results
</orchestrator-role>

<agent-role>
**Agents** — specialized subagents invoked via the Task tool. Each agent receives its role and workflow from its agent definition file.

Responsibilities:
- Reads task from `.claude/SuperchargeAI/<agent>/<uuid>/task.md`
- Reads every file referenced in task.md — never assumes contents
- Maintains working memory in `notes.md` (the agent's only persistent context)
- Delegates low-level work to workers via `supercharge subtask init` (see `<workers>` section)
- **Agents do not create task workspaces** (`supercharge task init`). If work by another agent type is needed, the agent notes it in `result.md` Report — the orchestrator creates the task
- Writes deliverable to `result.md` before returning
- Adds `## Questions` to `task.md` if clarification is needed, then returns. Questions go through the orchestrator to the user
- Updates `notes.md` before every return (next invocation depends on it)
</agent-role>

<worker-role>
**Workers** — Agent SDK subprocesses spawned by agents to execute specific assignments. See the full worker protocol in worker.md.

Two modes based on model:
- **Deep** (opus/sonnet): context file, session resume, can spawn sub-workers. Deep workers are MD-context-first — they maintain the context file throughout execution, just as agents maintain `notes.md`. The context file is the worker's persistent state across resumes.
- **Fast** (haiku): fire-and-forget, no context file, no resume

Responsibilities:
- Executes a specific, scoped assignment from the calling agent
- Stays within assignment scope — does exactly what was asked
- Can read any project file for context (including task.md, notes.md, result.md of the parent task) but only writes to the worker's own context file and project files within scope
- Does not write or edit the agent's `result.md` or `notes.md`
- Returns results via context file Result section (deep) or direct output (fast)
</worker-role>

<agent-types>
| Agent | Purpose |
|-------|---------|
| `plan` | Decompose requests into structured task lists |
| `code` | Implement features, fix bugs, write tests (deep coding / prototyping / bug fixing) |
| `document` | Update documentation to reflect changes or fix inconsistencies |
| `research` | Search the web, gather external context |
| `review` | Code review of completed work |
| `consistency` | Check for contradictions, broken references, duplication |
| `memory` | Maintain project and methodology memory from task results |
</agent-types>
</roles>

<task-protocol>
Every task uses a folder: `.claude/SuperchargeAI/<agent>/<uuid>/`

<task-md>
Created by the orchestrator.

```
# Task

Description of what needs to be done.

## Requirements

Definition of done.

## Context

Relevant codebase context.

## References

Relevant file paths and resources.
```
</task-md>

<result-md>
Created by the executing agent. Structure:

```
# Result

## Report

Final deliverable. The caller will not know anything about task execution that is not in this report. If changes affect project scripts or memory relevance, note it here. If follow-up work by other agent types is needed, note it here too.

## Memory

### Code

Patterns noted: what failed, how it was solved, best practices for this repo.

### Instructions

#### Content

What was left undefined in the task instructions.

#### Structure

What is conceptually missing from the task structure.
```
</result-md>

<notes-md>
Maintained by the executing agent. This is the agent's persistent context across restarts.

```
# Open tasks

- [ ] Subtask name - description
- [ ] Subtask with note - see ## Note title below

# Notes

## Note title

Working notes, searchable by title. Keep brief, correct, and up to date.
```

Notes can use subfolders for complex topics. Each subfolder must have an `index.md` with a `# Context` section (linking to parent) and a `# Content` section (listing all files in the subfolder).
</notes-md>
</task-protocol>

<memory>
Maintained by the `memory` agent in `.claude/SuperchargeAI/memory/`:

```
memory/
├── project/          # Project-specific gotchas and patterns
└── methodology/
    ├── behavior/     # Agent behavior instructions
    └── flows/        # Workflow adjustments
```

Each memory file has `# Content` (read by all agents) and `# Notes` (for memory agent only).
</memory>

<workers>
Agents delegate low-level work to workers via the `supercharge` CLI.

**Spawning a worker:**
```
supercharge subtask init <task_uuid> <agent_type> "<prompt>" --model <model>
```
Returns JSON: `{"worker_id": "...", "result": "..."}` or `{"worker_id": "...", "error": "..."}`.

**Resuming a worker (when it stopped with Questions):**
```
supercharge subtask resume <worker_id> "<answer>"
```

**Model determines worker mode:**

| Model | Mode | Context file | Resume | Recursion |
|-------|------|-------------|--------|-----------|
| opus, sonnet | Deep | Yes | Yes | Yes |
| haiku | Fast | No | No | No |

Choose the model based on task complexity:
- **Deep** (opus/sonnet): multi-step work, may need clarification, may delegate further
- **Fast** (haiku): simple self-contained tasks — grep, compute, check a single file

**MD context first:** The calling agent logs every worker question and result in `notes.md` immediately. Context windows are lost on restart — markdown is the only persistent state. This applies to both deep and fast worker results.

Workers can spawn sub-workers if the recursion budget allows (tracked via `SUPERCHARGE_RECURSION_REMAINING` env var, default 5 levels). Workers at budget 0 cannot spawn.
</workers>

<scripts>
Multi-line scripts go in `.claude/SuperchargeAI/scripts/` with `lowercase_name.ext` naming. Each script starts with a docstring explaining its purpose and usage context.
</scripts>

<tips>
- If WebSearch or WebFetch fails or is unavailable, fall back to CLI tools (`curl`, `wget`) via Bash. Do not abandon web retrieval on the first failure.
</tips>
</protocol>
