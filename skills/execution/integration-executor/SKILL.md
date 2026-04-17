---
name: execution/integration-executor
description: "Internal: Integration Executor. Wires components together, connects external APIs, handles plumbing. Write evidence on completion."
---

# dynos-work: execution/integration-executor

Spawn the `integration-executor` agent with the user's prompt as the instruction.

## Ruthlessness Standard

- Do not accept hand-wavy plumbing work. The executor must prove exactly what talks to what.
- A fix that changes one boundary while ignoring retries, errors, or timeouts is incomplete.
- If the integration behavior depends on config, payload shape, or ordering, the agent must verify those facts directly.

## What to pass

Pass the user's full prompt verbatim. Do not summarize or sanitize it.
Prepend a short hard wrapper that tells the agent to trace the full integration boundary, close the real failure mode, and verify error-path behavior before writing evidence.
