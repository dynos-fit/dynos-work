---
name: execution/data-executor
description: "Internal: Data Executor. Implements ETL, analytics, backfills, data quality, reconciliation, and data pipeline changes. Write evidence on completion."
---

# dynos-work: execution/data-executor

Spawn the `data-executor` agent with the user's prompt as the instruction.

## Ruthlessness Standard

- Do not guess about data shape, historical rows, or retry behavior.
- Every pipeline change must account for idempotency, partial failure, and reconciliation.
- If a backfill can corrupt data twice, it is incomplete.

## What to pass

Pass the user's full prompt verbatim. Do not summarize or sanitize it.
Prepend a short hard wrapper that tells the agent to verify data contracts, idempotency, and failure recovery before writing evidence.

