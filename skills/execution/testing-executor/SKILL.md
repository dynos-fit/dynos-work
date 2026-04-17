---
name: execution/testing-executor
description: "Internal: Testing Executor. Writes unit, integration, and e2e tests. Read spec and segment. Write evidence on completion."
---

# dynos-work: execution/testing-executor

Spawn the `testing-executor` agent with the user's prompt as the instruction.

## Ruthlessness Standard

- Do not allow decorative tests that only confirm the happy path.
- The executor must target the actual regression mechanism, boundary condition, and failure mode.
- A test that would pass before the bug fix is wasted output.

## What to pass

Pass the user's full prompt verbatim. Do not summarize or sanitize it.
Prepend a short hard wrapper that tells the agent to write tests that fail for the real bug, cover the edge behavior, and prove the regression is closed.
