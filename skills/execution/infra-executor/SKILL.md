---
name: execution/infra-executor
description: "Internal: Infrastructure Executor. Implements deployment, CI/CD, container, IaC, and environment configuration changes. Write evidence on completion."
---

# dynos-work: execution/infra-executor

Spawn the `infra-executor` agent with the user's prompt as the instruction.

## Ruthlessness Standard

- Do not accept deployment config that only works locally.
- Every environment, secret, permission, and rollout assumption must be explicit.
- If rollback order matters, the executor must state and verify it.

## What to pass

Pass the user's full prompt verbatim. Do not summarize or sanitize it.
Prepend a short hard wrapper that tells the agent to inspect the deployment surface, preserve least privilege, and verify rollback or compatibility behavior before writing evidence.

