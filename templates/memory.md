---
title: <Brief title>
keywords: [keyword1, keyword2, keyword3]
type: <memory|skill-proposal — default is "memory", use "skill-proposal" for skill candidates>
created: <YYYY-MM-DD>
updated: <YYYY-MM-DD>
contribution_candidate: <true|false, optional — set by memory agent for universal methodology memories>
---

# Content

<For project memory: actionable knowledge for agents working in this codebase. "Do X when Y" not "we noticed Z".>
<For methodology memory: document what went wrong and the correction. These are improvement candidates for SuperchargeAI's prompts, tools, or code — not instructions for other agents.>

<For skill proposals (type: skill-proposal), use this structure instead:>

## Trigger

<When should this skill be used? Describe the situation or condition.>

## Steps

<The sequence of actions to execute. Be specific enough that an agent can follow them.>

## Evidence

<Redacted summary of where this pattern was observed. Reference session/task IDs, not project-specific code or business logic. Exception: concrete references are acceptable if the project is public or the pattern is about SuperchargeAI itself.>

# Notes

<Memory agent only: usage tracking, stability assessment, promotion candidates for CLAUDE.md or project config.>
