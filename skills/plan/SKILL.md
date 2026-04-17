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

## Ruthlessness Standard

- Treat a vague plan as a delayed execution failure.
- Do not accept coverage by implication. Every acceptance criterion needs an explicit owner and mechanism.
- If the plan does not name failure modes, rollback impact, and verification strategy, it is incomplete.

## What you do

### Step 1 — Find active task

Find the most recent active task in `.dynos/` (manifest.json with stage not DONE/FAILED). If none, print "No active task. Start one with /dynos-work:start" and stop.

Read `manifest.json`, `spec.md`, and `design-decisions.md` (if it exists).

Verify `spec.md` exists. If not, print "No spec found. Run /dynos-work:start to generate one." and stop.

### Step 2 — Generate plan (PLANNING)

Update `manifest.json` stage to `PLANNING`. If available in this repo, use:

```text
python3 hooks/ctl.py transition .dynos/task-{id} PLANNING
```

Append to execution log:
```
{timestamp} [STAGE] → PLANNING
{timestamp} [SPAWN] planning — generate implementation plan
```

Spawn the `planning` agent with instruction: "Generate the implementation plan and execution graph. Read `spec.md` and `design-decisions.md` (if it exists). Human design choices are binding. Write to `.dynos/task-{id}/plan.md` and `.dynos/task-{id}/execution-graph.json`. Include: technical approach, module/component breakdown, data flow, error handling, failure modes, rollback or migration risk where relevant, test strategy, and explicit file ownership per segment. Do not leave any acceptance criterion covered only by implication."

Wait for completion. Run deterministic artifact validation before human review. If available in this repo, use:

```text
python3 hooks/ctl.py validate-task .dynos/task-{id} --strict
```

Append to log:
```
{timestamp} [DONE] planning — plan.md written
{timestamp} [STAGE] → PLAN_REVIEW
```

Update `manifest.json` stage to `PLAN_REVIEW`. If available in this repo, use:

```text
python3 hooks/ctl.py transition .dynos/task-{id} PLAN_REVIEW
```

### Step 3 — Human review (PLAN_REVIEW)

Read `plan.md`. Present to the user using **AskUserQuestion**:

```
=== Plan Review ===

[contents of plan.md]

---
Approve this plan? (yes / no + what to change)
```

- If **approved**: append `{timestamp} [HUMAN] PLAN_REVIEW — approved` to log. Proceed to Step 4.
- If **changes requested**: append `{timestamp} [HUMAN] PLAN_REVIEW — changes requested: {summary}` to log. Spawn planning agent again with the feedback. Re-present the updated plan. Repeat until approved.
- If **rejected**: set `manifest.json` stage to `FAILED`, append `[FAILED] Plan rejected by user`. Stop.

### Step 4 — Spec coverage audit (PLAN_AUDIT)

Update `manifest.json` stage to `PLAN_AUDIT`. If available in this repo, use:

```text
python3 hooks/ctl.py transition .dynos/task-{id} PLAN_AUDIT
```

Append to log:
```
{timestamp} [STAGE] → PLAN_AUDIT
{timestamp} [SPAWN] spec-completion-auditor — verify plan covers all acceptance criteria
```

Spawn the `spec-completion-auditor` agent with instruction: "Audit the plan against the spec BEFORE execution. Read `spec.md` and `plan.md`. Verify every acceptance criterion in `spec.md` is explicitly addressed in `plan.md`. Flag criteria with no corresponding component, module, task, verification step, or owner. Treat implied coverage as missing. Write report to `.dynos/task-{id}/audit-reports/plan-audit-{timestamp}.json`."

Wait for completion. Read the report.

- If all criteria covered: append `{timestamp} [DONE] spec-completion-auditor — all criteria covered` to log. Proceed to Step 5.
- If gaps found: append `{timestamp} [DECISION] plan gaps found — respawning planner to fill: {list}` to log. Spawn planning agent with instruction: "The plan is missing or weak on: [{uncovered criteria}]. Update `plan.md` with explicit implementation ownership, mechanism, failure handling, and verification for each gap. Remove ambiguity instead of adding filler." Re-run the audit. Repeat until all covered.

### Step 5 — Done

Update `manifest.json` stage to `PRE_EXECUTION_SNAPSHOT`. If available in this repo, use:

```text
python3 hooks/ctl.py transition .dynos/task-{id} PRE_EXECUTION_SNAPSHOT
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
