# Task

Harvest learnings from stale task folders and clean them up. You are running in the background -- there is no orchestrator to interact with.

## Requirements

1. For each stale folder listed below, read all result.md and worker context files
2. Extract learnings from `## Memory` sections into shared memory
3. Write learnings to `memory/methodology/` and `memory/project/` as appropriate
4. Follow the memory file format: YAML frontmatter + `# Content` + `# Notes`
5. After harvesting each folder, delete it with `supercharge task cleanup <uuid>` (extract the UUID from the folder path)

## Stale Task Folders

{folder_list}

## Context

You are a memory agent running autonomously in the background. There is no orchestrator. Work through all folders systematically.

## References

- Memory directory: {memory_dir}
