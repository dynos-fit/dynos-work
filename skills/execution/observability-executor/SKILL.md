---
name: execution/observability-executor
description: "Internal: Observability Executor. Implements logs, metrics, traces, alerts, dashboards, and reliability instrumentation. Write evidence on completion."
---

# dynos-work: execution/observability-executor

Spawn the `observability-executor` agent with the user's prompt as the instruction.

## Ruthlessness Standard

- Do not add noisy instrumentation that cannot answer an operational question.
- Every critical failure mode should be visible without adding emergency debug code.
- Telemetry must not leak secrets or sensitive user data.

## What to pass

Pass the user's full prompt verbatim. Do not summarize or sanitize it.
Prepend a short hard wrapper that tells the agent to instrument the real runtime path, preserve redaction, and verify the signal is actionable before writing evidence.

