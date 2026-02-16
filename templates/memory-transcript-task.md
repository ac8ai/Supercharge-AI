# Task

Harvest learnings from completed session transcripts. You are running in the background -- there is no orchestrator to interact with.

## Requirements

1. Read each transcript file listed below
2. Extract patterns worth remembering:
   - Corrections the user made to agent behavior
   - Negative feedback patterns (what the user rejected or asked to redo)
   - Methodology learnings (workflow adjustments, missing steps)
   - Project-specific patterns (gotchas, best practices)
3. Write learnings to `memory/methodology/` and `memory/project/` as appropriate
4. Follow the memory file format: YAML frontmatter + `# Content` + `# Notes`
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

- Memory directory: {memory_dir}
