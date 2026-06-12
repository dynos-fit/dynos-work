---
name: execution
description: "Internal dynos-work executor skill group. Routes execution graph segments to the specialized executor skills."
---

# dynos-work: Execution Skill Group

This directory is the container for specialized executor skills:

- `backend-executor`
- `db-executor`
- `integration-executor`
- `ml-executor`
- `refactor-executor`
- `testing-executor`
- `ui-executor`

Do not invoke this grouping skill directly for implementation work. The `execute` skill owns routing, prompt injection, role stamping, spawn-budget checks, and receipt validation, then dispatches each execution graph segment to the matching specialized executor.

If this skill is selected directly, stop and run the `execute` skill instead.
