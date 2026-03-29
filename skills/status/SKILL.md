---
name: status
description: "Power user: Show current task state, lifecycle stage, audit results, and open gaps."
---

# dynos-work: Status

Show the current state of the active dynos-work task.

## What you do

1. Find the most recent active task in `.dynos/` (manifest.json with stage not DONE/FAILED)
2. If no active task, report "No active dynos-work task found. Start one with /dynos-work:start"
3. Read: manifest.json, spec.md, execution-graph.json, latest audit-reports, repair-log.json, test-results.json, execution-log.md
4. Print a human-readable status report

## Output format

```
dynos-work Status Report
========================
Task: task-20260327-001
Title: [First 80 chars of task]
Stage: CHECKPOINT_AUDIT
Risk: medium
Snapshot: dynos/task-20260327-001-snapshot

Lifecycle Progress:
  ✓ INTAKE
  ✓ DISCOVERY
  ✓ DESIGN_OPTIONS
  ✓ CLASSIFY_AND_SPEC
  ✓ PLANNING
  ✓ SPEC_REVIEW (user-approved)
  ✓ PLAN_REVIEW (auto-approved | user-approved)
  ✓ EXECUTION_GRAPH_BUILD
  ✓ PRE_EXECUTION_SNAPSHOT
  ✓ EXECUTION (3/3 segments complete)
  ✓ TEST_EXECUTION (all tests passed)
  → CHECKPOINT_AUDIT (in progress)
  ○ FINAL_AUDIT
  ○ COMPLETION_REVIEW

Execution Progress:
  Segments: [N]/[total] complete
  Current batch: [seg-003-ui, seg-004-tests]
  Completed: [seg-001-db, seg-002-backend]

Acceptance Criteria: [N]/[total] covered

Latest Audit Results:
  spec-completion: PASS | FAIL | not yet run
  security: PASS | FAIL | not yet run
  ui: PASS | FAIL | SKIPPED | SKIPPED_REUSE | not yet run
  code-quality: PASS | FAIL | SKIPPED | SKIPPED_REUSE | not yet run
  db-schema: PASS | FAIL | SKIPPED | SKIPPED_REUSE | not yet run

Open Blocking Findings:
  [finding-id] [auditor]: [description] ([file:line])

Test Results: PASS | FAIL ([N] failing tests) | not yet run

Repair Cycle: [N] ([N] findings resolved, [N] remaining)

Next command: [one of the below based on current stage]

Recent Activity (last 10 lines of execution-log.md):
  [last 10 log entries, or "No execution log yet" if file doesn't exist]
```

## Next command mapping

Always end the status report with a "Next:" line based on current stage:

| Stage | Next command |
|---|---|
| PLANNING | `/dynos-work:plan` |
| PLAN_REVIEW | `/dynos-work:plan` |
| PLAN_AUDIT | `/dynos-work:plan` |
| EXECUTION_GRAPH_BUILD | `/dynos-work:execute` |
| PRE_EXECUTION_SNAPSHOT | `/dynos-work:execute` |
| EXECUTION | `/dynos-work:execute` |
| TEST_EXECUTION | `/dynos-work:test` |
| CHECKPOINT_AUDIT | `/dynos-work:audit` |
| REPAIR_PLANNING | `/dynos-work:repair` |
| REPAIR_EXECUTION | `/dynos-work:repair` |
| FINAL_AUDIT | `/dynos-work:final-audit` |
| COMPLETION_REVIEW | `/dynos-work:final-audit` |
| DONE | Task complete |
| FAILED | Review failure report, consider `/dynos-work:repair` or rollback |
