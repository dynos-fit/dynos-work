---
name: execution/ml-executor
description: "Internal: ML Executor. Implements ML models, training pipelines, inference code, data processing. Read spec and segment. Write evidence on completion."
---

# dynos-work: execution/ml-executor

Spawn the `ml-executor` agent with the user's prompt as the instruction.

## Ruthlessness Standard

- Do not let the executor hide weak assumptions behind model language or data-science jargon.
- If data shape, training flow, or inference contracts are unclear, the agent must inspect them before editing.
- Do not accept a change that improves one metric while making reproducibility, safety, or serving behavior ambiguous.

## What to pass

Pass the user's full prompt verbatim. Do not summarize or sanitize it.
Prepend a short hard wrapper that tells the agent to verify dataset/model contracts, fix the real mechanism, and only claim success with concrete evidence.
