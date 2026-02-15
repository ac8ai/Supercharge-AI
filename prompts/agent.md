<agent>
<task-tracking>
Track subtasks in the `# Open tasks` section of `notes.md` using structured entries:

```
# Open tasks

- [ ] [worker:a1b2c3] Set up database schema
- [~] [worker:d4e5f6] Implement login endpoint (after: worker:a1b2c3)
- [x] [worker:j0k1l2] Create migration script
- [ ] Write integration tests (after: worker:d4e5f6)
```

**Status:** `[ ]` pending, `[~]` in progress, `[x]` completed.

**Labels:** `[worker:short_id]` for worker-backed tasks (first 6 chars of worker_id). Omit the label for tasks you handle directly.

**Dependencies:** `(after: worker:short_id)` when ordering matters between parallel subtasks. Omit when ordering is obvious or sequential.

**Update immediately:** mark in progress before spawning a worker, log the result in notes.md after the worker returns. Context windows are lost on restart — notes.md is the only persistent state.
</task-tracking>
</agent>
