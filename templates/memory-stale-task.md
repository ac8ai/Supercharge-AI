# Task

Harvest learnings from stale task folders and clean them up. You are running in the background -- there is no orchestrator to interact with.

## Requirements

1. For each stale folder listed below, read all result.md and worker context files
2. Extract learnings from `## Memory` sections into shared memory
3. Look for **skill candidates** — if multiple task results show the same multi-step workaround converging, or a pattern that was attempted and failed before succeeding:
   - For project-specific skills: write a skill proposal to `{memory_dir}/project/` with `type: skill-proposal` in frontmatter
   - For universal methodology skills: write to `{methodology_dir}/skills/` with `type: skill-proposal` and `contribution_candidate: true`
   - **Evidence redaction**: do not include project-specific code, business logic, or proprietary details in skill proposals. Describe the pattern shape and reference task IDs only. Exception: if the project is public or the pattern is about SuperchargeAI itself, concrete references are acceptable
4. Write project-specific patterns to `{memory_dir}/project/` and methodology patterns to `{methodology_dir}`
5. Follow the memory file format: YAML frontmatter + `# Content` + `# Notes`
6. For **methodology** memories only: evaluate whether the learning is universally applicable — not tied to this project's tech stack, naming conventions, file paths, or codebase quirks. If universally applicable, add `contribution_candidate: true` to the YAML frontmatter. Examples of universal learnings: "always read files before writing," "plan before code for non-trivial work," "recover interrupted edits by re-reading." Examples of non-universal: anything referencing specific frameworks, file paths, or project structure.
7. After harvesting each folder:
   - For **research** and **plan** task folders: archive with `supercharge task archive --agent-type memory <uuid1> [uuid2] ...`
   - For all other agent types: delete with `supercharge task cleanup --agent-type memory <uuid1> [uuid2] ...`

## Stale Task Folders

{folder_list}

## Context

You are a memory agent running autonomously in the background. There is no orchestrator. Work through all folders systematically.

## References

- Project memory directory: {memory_dir}
- Methodology memory directory: {methodology_dir}
