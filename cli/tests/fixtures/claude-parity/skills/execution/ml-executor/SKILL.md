---
name: execution/ml-executor
description: "Internal: ML Executor. Implements ML models, training pipelines, inference code, data processing. Read spec and segment. Write evidence on completion."
---

# dynos-work: execution/ml-executor

Spawn the `ml-executor` agent with the user's prompt as the instruction.

## What to pass

Pass the user's full prompt verbatim as the instruction to the agent. Do not summarize or reformat it.
