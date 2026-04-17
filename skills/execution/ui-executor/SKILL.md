---
name: execution/ui-executor
description: "Internal: UI Executor. Implements UI components, pages, interactions, and styles. Read spec and segment. Write evidence on completion."
---

# dynos-work: execution/ui-executor

Spawn the `ui-executor` agent with the user's prompt as the instruction.

## Ruthlessness Standard

- Do not let the executor ship a pretty surface that leaves broken states, missing feedback, or dead interactions underneath.
- If the task affects state, loading, empty, error, or success behavior, the agent must check each visible outcome.
- Do not accept screenshot-friendly work that fails keyboard, layout, or data-driven behavior.

## What to pass

Pass the user's full prompt verbatim. Do not summarize or sanitize it.
Prepend a short hard wrapper that tells the agent to verify actual rendered behavior and state transitions before writing evidence.
