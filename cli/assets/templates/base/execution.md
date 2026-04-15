---
name: execution
description: "Container for executor sub-agents (backend, db, integration, ml, refactor, testing, ui). Each executor implements a segment from the execution graph."
---

# dynos-work: Execution Container

The `execution` skill is a container for the following executor sub-agents, each invoked during `/dynos-work:execute` based on the segment's declared executor type:

- `backend-executor` — backend code segments
- `db-executor` — database-only segments
- `integration-executor` — cross-system wiring
- `ml-executor` — ML/data-pipeline segments
- `refactor-executor` — restructuring with no behavior change
- `testing-executor` — test-suite additions and TDD
- `ui-executor` — UI-only segments

Inputs and outputs are defined in this skill's `contract.json`. Each executor reads:
- the segment (from `execution-graph.json`)
- matched acceptance criteria text (from `spec.md`)
- dependency evidence (previously generated `evidence/{dep}.md` files)
- prevention rules filtered from `dynos_patterns.md`

Each executor writes `evidence/{segment-id}.md` documenting modified files, integration points, failure cases handled, required config, and the acceptance criteria it satisfied.
