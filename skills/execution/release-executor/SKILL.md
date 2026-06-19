---
name: execution/release-executor
description: "Internal: Release Executor. Implements version/changelog updates, rollout flags, rollback plans, release notes, and migration sequencing. Write evidence on completion."
---

# dynos-work: execution/release-executor

Spawn the `release-executor` agent with the user's prompt as the instruction.

## Ruthlessness Standard

- Do not let release metadata drift across package/plugin manifests.
- Rollout and rollback behavior must be concrete enough for an operator to follow.
- Migration and compatibility windows must be explicit when state or contracts change.

## What to pass

Pass the user's full prompt verbatim. Do not summarize or sanitize it.
Prepend a short hard wrapper that tells the agent to keep release artifacts consistent, verify changelog/version hygiene, and document rollback-sensitive behavior before writing evidence.

