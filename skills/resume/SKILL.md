---
name: resume
description: "Power user: Resume an interrupted task from .dynos/ state. Use after session restart or context compression."
---

# dynos-work: Resume

Resume a dynos-work task that was interrupted.

## What you do

1. List all tasks in `.dynos/` that are not DONE or FAILED (read each manifest.json)
2. If one active task: resume it automatically
3. If multiple active tasks: show list and ask which to resume
4. Read `manifest.json` to determine current stage
5. Spawn the `lifecycle` agent via the Agent tool with the existing task ID and state
6. The Lifecycle Controller picks up from the current stage

## Output on resume

```
dynos-work: Resuming task-20260327-001
Title: [task title]
Resuming from stage: CHECKPOINT_AUDIT

Starting lifecycle controller...
```

## Edge cases

- If manifest.json is corrupt or unreadable: report the error and suggest starting fresh with `dynos-work:start`
- If audit-reports exist from before interruption: the Lifecycle Controller will read them and determine if re-audit is needed
- If execution-graph has segments with no evidence files: the Lifecycle Controller will re-run those segments

## When to use

- After a session restart during a long task
- After Claude Code context was compressed mid-task
- After a network interruption
- After manually investigating the task state with `/dynos-work:status`
