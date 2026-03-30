---
name: execution/integration-executor
description: "Internal: Integration Executor. Wires components together, connects external APIs, handles plumbing. Write evidence on completion."
---

# dynos-work: execution/integration-executor

Spawn the `integration-executor` agent with the user's prompt as the instruction.

## What to pass

Pass the user's full prompt verbatim as the instruction to the agent. Do not summarize or reformat it.
