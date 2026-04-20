---
name: plan
description: "Internal dynos-work skill. Re-run planning on an existing task. Use when you need to regenerate the plan after spec changes, or if the task was started externally. Runs PLANNING → PLAN_REVIEW → PLAN_AUDIT with deterministic artifact validation."
---

# dynos-work: Plan

Re-runs the planning phase on an existing task. Use this when:
- You need to regenerate the plan after manual spec changes
- The task was initialized externally and skipped planning
- You want to replan after a major scope change

When done, the task is ready for `/dynos-work:execute`.

## What you do

### Step 1 — Find active task

Find the most recent active task in `.dynos/` (manifest.json with stage not DONE/FAILED). If none, print "No active task. Start one with /dynos-work:start" and stop.

Read `manifest.json`, `spec.md`, and `design-decisions.md` (if it exists).

Verify `spec.md` exists. If not, print "No spec found. Run /dynos-work:start to generate one." and stop.

### Step 2 — Generate plan (PLANNING)

Transition the stage to `PLANNING` (transition_task auto-writes the `[STAGE] → PLANNING` log line):

```text
python3 hooks/dynosctl.py transition .dynos/task-{id} PLANNING
```

Append the spawn line to the execution log:
```
{timestamp} [SPAWN] planning — generate implementation plan
```

Spawn the `planning` agent with instruction: "Generate the implementation plan and execution graph. Read `spec.md` and `design-decisions.md` (if it exists). Human design choices are binding. Write to `.dynos/task-{id}/plan.md` and `.dynos/task-{id}/execution-graph.json`. Include: technical approach, module/component breakdown, data flow, error handling, test strategy, and explicit file ownership per segment."

Wait for completion. Run deterministic artifact validation before human review:

```text
python3 hooks/dynosctl.py validate-task .dynos/task-{id} --strict
```

Append to log:
```
{timestamp} [DONE] planning — plan.md written
```

Transition the stage to `PLAN_REVIEW` (transition_task auto-writes the `[STAGE] → PLAN_REVIEW` log line):

```text
python3 hooks/dynosctl.py transition .dynos/task-{id} PLAN_REVIEW
```

### Step 3 — Human review (PLAN_REVIEW)

Read `plan.md`. Present to the user using **AskUserQuestion**:

```
=== Plan Review ===

[contents of plan.md]

---
Approve this plan? (yes / no + what to change)
```

- If **approved**: run the `approve-stage` ctl command below. It hashes the current `plan.md`, writes the `human-approval-PLAN_REVIEW` receipt with that hash, then transitions PLAN_REVIEW → PLAN_AUDIT in one atomic step. The hash is computed from the CURRENT `plan.md` content **at transition time** (the gate re-hashes the file and compares it to `receipt.artifact_sha256`), so an approval that races against a manual edit will be refused with the literal substrings `human-approval-PLAN_REVIEW` and `hash mismatch`. Do NOT add a manual `[HUMAN]` log line — the receipt is the audit trail. Then proceed to Step 4.

  ```text
  python3 hooks/dynosctl.py approve-stage .dynos/task-{id} PLAN_REVIEW
  ```

  Exit code 0 means success; exit code 1 means the gate refused (stderr identifies the cause). Do not bypass with `transition --force`.
- If **changes requested**: append `{timestamp} [HUMAN] PLAN_REVIEW — changes requested: {summary}` to log. Spawn planning agent again with the feedback. Re-present the updated plan. Repeat until approved. Do NOT call `approve-stage` against a stale plan.
- If **rejected**: set `manifest.json` stage to `FAILED`, append `[FAILED] Plan rejected by user`. Stop.

### Step 4 — Spec coverage audit (PLAN_AUDIT)

The `approve-stage` call in Step 3 has already advanced the manifest to `PLAN_AUDIT` — do NOT call `transition .dynos/task-{id} PLAN_AUDIT` here (the state machine would refuse it as `PLAN_AUDIT → PLAN_AUDIT`).

Append to log:
```
{timestamp} [SPAWN] spec-completion-auditor — verify plan covers all acceptance criteria
```

Spawn the `spec-completion-auditor` agent with instruction: "Audit the plan against the spec BEFORE execution. Read `spec.md` and `plan.md`. Verify every acceptance criterion in `spec.md` is explicitly addressed in `plan.md`. Flag criteria with no corresponding component, module, or task. Write report to `.dynos/task-{id}/audit-reports/plan-audit-{timestamp}.json`."

Wait for completion. Read the report.

- If all criteria covered: append `{timestamp} [DONE] spec-completion-auditor — all criteria covered` to log. Proceed to Step 5.
- If gaps found: append `{timestamp} [DECISION] plan gaps found — respawning planner to fill: {list}` to log. Spawn planning agent with instruction: "The plan is missing coverage for: [{uncovered criteria}]. Update `plan.md` to address them." Re-run the audit. Repeat until all covered.

### Step 5 — Done

Transition the stage to `PRE_EXECUTION_SNAPSHOT`:

```text
python3 hooks/dynosctl.py transition .dynos/task-{id} PRE_EXECUTION_SNAPSHOT
```

Append to log:
```
{timestamp} [ADVANCE] PLAN_AUDIT → PRE_EXECUTION_SNAPSHOT
```

Print:
```
Plan approved and audited. All acceptance criteria covered.

Next: /dynos-work:execute
```
