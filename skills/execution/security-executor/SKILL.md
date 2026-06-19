---
name: execution/security-executor
description: "Internal: Security Executor. Implements auth, authorization, validation, crypto, secret-handling, and vulnerability remediations. Write evidence on completion."
---

# dynos-work: execution/security-executor

Spawn the `security-executor` agent with the user's prompt as the instruction.

## Ruthlessness Standard

- Do not patch symptoms while leaving the attack path alive.
- Every security fix must name the trusted boundary and enforce it in code.
- If a mitigation is only convention, it is not a mitigation.

## What to pass

Pass the user's full prompt verbatim. Do not summarize or sanitize it.
Prepend a short hard wrapper that tells the agent to close the exploit mechanism, verify negative paths, and write evidence only after the vulnerable route is no longer viable.

