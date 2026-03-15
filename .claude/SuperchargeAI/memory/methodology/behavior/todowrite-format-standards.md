---
title: TodoWrite items must follow SuperchargeAI naming format
keywords: [todowrite, format, naming, orchestrator, convention, standards]
created: 2026-03-11
updated: 2026-03-15
---

# Content

**Problem Context:** The orchestrator's TodoWrite items did not follow SuperchargeAI naming conventions. The user asked: "Why your todo list doesn't follow supercharge standards?"

**Solution:** TodoWrite items created by the orchestrator must use the format:
```
[agent_type:short_uuid] Imperative verb phrase
```

Examples:
- `[plan:a1b2c3d4] Design write scope fix approach`
- `[code:d4e5f6a1] Implement context scope redefinition`
- `[review:b7c8d9e2] Verify permissions test coverage`

**Key Insights:**
- The prefix `[agent_type:short_uuid]` ties each todo to the specific agent task being tracked
- `short_uuid` is the first 8 hex characters of the task UUID (what `supercharge task init` prints)
- This makes it easy to correlate TodoWrite items with task directories in `.claude/SuperchargeAI/tasks/<agent>/<short_id>/`
- The orchestrator manages multiple agent tasks simultaneously; consistent naming prevents confusion

# Notes

Source: transcript ef6e6dff — user corrected TodoWrite format mid-session.
