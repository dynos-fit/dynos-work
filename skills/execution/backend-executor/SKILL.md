---
name: execution/backend-executor
description: "Internal: Backend Executor. Implements API routes, services, business logic, auth. Read spec and segment. Write evidence on completion."
---

# dynos-work: execution/backend-executor

Spawn the `backend-executor` agent with the user's prompt as the instruction.

## What to pass

Pass the user's full prompt verbatim as the instruction to the agent. Do not summarize or reformat it.
