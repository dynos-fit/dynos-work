---
name: execution/db-executor
description: "Internal: DB Executor. Implements schema changes, migrations, ORM models, queries. Read spec and segment. Write evidence on completion."
---

# dynos-work: execution/db-executor

Spawn the `db-executor` agent with the user's prompt as the instruction.

## What to pass

Pass the user's full prompt verbatim as the instruction to the agent. Do not summarize or reformat it.
