---
name: execution/db-executor
description: "Internal: DB Executor. Implements schema changes, migrations, ORM models, queries. Read spec and segment. Write evidence on completion."
---

# dynos-work: execution/db-executor

Spawn the `db-executor` agent with the user's prompt as the instruction.

## Ruthlessness Standard

- Do not allow the executor to guess about schema semantics, migration safety, or query behavior.
- Every database change must account for real read/write paths, rollback risk, and data integrity impact.
- Do not accept a fix that patches one query while leaving the surrounding invariant broken.

## What to pass

Pass the user's full prompt verbatim. Do not summarize or sanitize it.
Prepend a short hard wrapper that tells the agent to inspect the actual schema/query path first, state safety assumptions, and verify the changed behavior before writing evidence.
