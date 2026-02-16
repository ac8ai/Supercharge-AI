<agent>
<notes-discipline>
`notes.md` is your only persistent state. Update it **continuously as you work**, never in a batch at the end.

**When to update notes.md:**
1. **Immediately after reading task.md** — write your `# Open tasks` plan of attack
2. **Before each worker spawn** — mark the subtask `[~]` in progress
3. **After each worker returns** — mark `[x]`, log key findings under `# Notes`
4. **On every significant discovery or decision** — add a `## Note` entry
5. **Before returning** — final update with any loose ends

A notes.md written in one dump at the end is useless — it captures nothing that result.md doesn't already have. Write as if your context window could be lost after every tool call.
</notes-discipline>

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
