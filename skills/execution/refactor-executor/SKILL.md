---
name: execution/refactor-executor
description: "Internal: Refactor Executor. Restructures code without changing behavior. No new features. Write evidence on completion."
---

# dynos-work: execution/refactor-executor

Spawn the `refactor-executor` agent with the user's prompt as the instruction.

## Ruthlessness Standard

- Do not let "refactor" become cover for behavior drift, ambiguity, or renamed confusion.
- The executor must preserve semantics exactly unless the instruction explicitly authorizes behavioral change.
- If the refactor increases indirection without reducing risk or complexity, it is not an improvement.

## What to pass

Pass the user's full prompt verbatim. Do not summarize or sanitize it.
Prepend a short hard wrapper that tells the agent to preserve behavior, verify that meaning and call flow did not drift, and only then write evidence.
