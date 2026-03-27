---
name: start
description: "Primary entry point. Accepts any task description. Creates task state in .dynos/ and spawns the Lifecycle Controller to own the full lifecycle."
---

# dynos-work: Start

You are the entry point for dynos-work. When a user invokes this skill, they are handing you a task to complete. You do not complete it yourself — you initialize the task state and hand control to the Lifecycle Controller.

## What you do

1. **Generate a task ID** in the format `task-YYYYMMDD-NNN` (use today's date, increment NNN if directory exists)

2. **Create the task directory:** `.dynos/task-{id}/`

3. **Write manifest.json:**
```json
{
  "task_id": "task-20260327-001",
  "created_at": "ISO timestamp",
  "title": "First 80 characters of task description",
  "raw_input": "Full task description as provided by user",
  "stage": "INTAKE",
  "classification": null,
  "retry_counts": {},
  "blocked_reason": null,
  "completion_at": null
}
```

4. **Write raw-input.md** with the full task description exactly as given.

5. **Confirm to user:**
```
dynos-work: Task initialized
ID: task-20260327-001
State: .dynos/task-20260327-001/

Starting lifecycle controller...
```

6. **Spawn the Lifecycle Controller** via the Agent tool:
   - Use the `lifecycle` agent
   - Pass: task ID, task directory path, and full task description
   - The Lifecycle Controller now owns the task completely

7. **Wait for Lifecycle Controller to complete** and report its final output to the user.

## What you do NOT do

- You do not plan the task
- You do not execute the task
- You do not audit the task
- You do not decide when the task is done
- You only initialize state and spawn the Lifecycle Controller

## If the user provides incomplete input

If the task description is too vague to act on (e.g., just "fix the bug" with no context), ask one clarifying question before initializing. Once you have enough to write a meaningful `raw-input.md`, proceed.

**Too vague:** "fix the thing" → ask "Which thing? Can you describe the bug or feature?"
**Sufficient:** "Add JWT auth to the /api/users endpoint" → proceed immediately
