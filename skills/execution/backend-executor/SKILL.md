---
name: execution/backend-executor
description: "Internal: Backend Executor. Implements API routes, services, business logic, auth. Read spec and segment. Write evidence on completion."
---

# dynos-work: execution/backend-executor

Spawn the `backend-executor` agent with the user's prompt as the instruction.

## Ruthlessness Standard

- Do not soften the task. The executor must fix the real mechanism, not the visible symptom.
- Do not let the executor assume backend behavior from naming alone. It must read the actual code paths first.
- Do not accept "implemented" without evidence that the changed flow, edge cases, and failure path were checked.

## What to pass

Pass the user's full prompt verbatim. Do not summarize or sanitize it.
Prepend a short hard wrapper that tells the agent to read the relevant code first, solve the underlying backend failure, and only write completion evidence after verification.
