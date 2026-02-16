# Obsoleting Markdown References

## Idea

Detect when file references in markdown (notes.md, result.md, memory files, documentation) become stale — i.e., the referenced file has been modified after the reference was created.

## Approaches Under Investigation

### File-level: mtime-based detection
SessionStart hook records timestamp, PreToolUse Read hook compares file mtime to session start. Simple (~100 LOC), <10ms/check, catches 70-80% of cases. Misses cross-session staleness.

### Reference-level: "parallel git" tracking
Maintain a separate git repo (stored outside the project tree) that tracks agent-written markdown files. Use `git blame` to determine when each reference line was written, compare to referenced file's mtime. Catches cross-session staleness at reference granularity.

### Sub-file granularity
Track not just "file X was referenced" but "function Y in file X" or "section Z in file X". Could use tree-sitter for code, heading detection for markdown. Significantly more complex.

## Key Constraint

Solution must not interfere with project's regular git or pollute the project tree with tool files (.git directories, databases, etc.).

## Open Questions

- Can a separate `GIT_DIR` pointed outside the project work reliably for tracking markdown changes?
- Is reference-level tracking worth the complexity over simple file-level mtime?
- How to handle references created by human edits (not tracked by hooks)?
- What false positive rate is acceptable before warnings become noise?