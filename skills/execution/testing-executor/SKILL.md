---
name: execution/testing-executor
description: "Internal: Testing Executor. Writes unit, integration, and e2e tests. Read spec and segment. Write evidence on completion."
---

# dynos-work: execution/testing-executor

Spawn the `testing-executor` agent with the user's prompt as the instruction.

## What to pass

Pass the user's full prompt verbatim as the instruction to the agent. Do not summarize or reformat it.
