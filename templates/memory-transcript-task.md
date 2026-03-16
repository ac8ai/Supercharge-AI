# Task

Harvest learnings from completed session transcripts. You are running in the background -- there is no orchestrator to interact with.

## Requirements

1. Read each transcript file listed below. Some files include a line offset. For those, ONLY process content from that line onward -- earlier content was already reviewed in a previous pass
2. Extract patterns worth remembering:
   - Corrections the user made to agent behavior
   - Negative feedback patterns (what the user rejected or asked to redo)
   - Methodology learnings (workflow adjustments, missing steps)
   - Project-specific patterns (gotchas, best practices)
3. Write project-specific patterns to `{memory_dir}/project/` and methodology patterns to `{methodology_dir}`
4. Follow the memory file format: YAML frontmatter + `# Content` + `# Notes`
5. For **methodology** memories only: evaluate whether the learning is universally applicable — not tied to this project's tech stack, naming conventions, file paths, or codebase quirks. If universally applicable, add `contribution_candidate: true` to the YAML frontmatter. Examples of universal learnings: "always read files before writing," "plan before code for non-trivial work," "recover interrupted edits by re-reading." Examples of non-universal: anything referencing specific frameworks, file paths, or project structure.
5. After processing each transcript, stamp it as reviewed:
   ```
   supercharge memory stamp <transcript_path>
   ```
6. Do NOT delete transcript files -- only stamp them

## Transcript Files

{transcript_list}

## Context

You are a memory agent running autonomously in the background. There is no orchestrator. Work through all transcripts systematically.

## References

- Project memory directory: {memory_dir}
- Methodology memory directory: {methodology_dir}
