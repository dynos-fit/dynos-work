---
name: plan
description: "Power user: Generate implementation plan, review with human, audit spec coverage. Runs PLANNING → PLAN_REVIEW → PLAN_AUDIT in one shot. Use after /dynos-work:start approves the spec."
---

# dynos-work: Plan

Generates the implementation plan, presents it for your approval, then audits it for spec coverage — all in one command. When done, the task is ready for `/dynos-work:execute`.

## What you do

### Step 1 — Find active task

Find the most recent active task in `.dynos/` (manifest.json with stage not DONE/FAILED). If none, print "No active task. Start one with /dynos-work:start" and stop.

Read `manifest.json`, `spec.md`, and `design-decisions.md` (if it exists).

### Step 2 — Generate plan (PLANNING)

Update `manifest.json` stage to `PLANNING`. Append to execution log:
```
{timestamp} [STAGE] → PLANNING
{timestamp} [SPAWN] planning — generate implementation plan
```

Spawn the `planning` agent with instruction: "Generate the implementation plan. Read `spec.md` and `design-decisions.md` (if it exists). Human design choices are binding. Write to `.dynos/task-{id}/plan.md`. Include: technical approach, module/component breakdown, data flow, error handling, test strategy."

Wait for completion. Append to log:
```
{timestamp} [DONE] planning — plan.md written
```

### Step 3 — Human review (PLAN_REVIEW)

Update `manifest.json` stage to `PLAN_REVIEW`. Append to log:
```
{timestamp} [STAGE] → PLAN_REVIEW
{timestamp} [GATE] PLAN_REVIEW — waiting for human approval
```

Present to the user:
```
=== Plan Review ===

[contents of plan.md]

---
Approve this plan? (yes / no + what to change)
```

Use AskUserQuestion to collect response.

- If **approved**: append `{timestamp} [HUMAN] PLAN_REVIEW — approved` to log. Proceed to Step 4.
- If **changes requested**: append `{timestamp} [HUMAN] PLAN_REVIEW — changes requested: {summary}` to log. Spawn planning agent again with the feedback. Re-present the updated plan. Repeat until approved.
- If **rejected**: set `manifest.json` stage to `FAILED`, append `[FAILED] Plan rejected by user`. Stop.

### Step 4 — Spec coverage audit (PLAN_AUDIT)

Update `manifest.json` stage to `PLAN_AUDIT`. Append to log:
```
{timestamp} [STAGE] → PLAN_AUDIT
{timestamp} [SPAWN] spec-completion-auditor — verify plan covers all acceptance criteria
```

Spawn the `spec-completion-auditor` agent with instruction: "Audit the plan against the spec BEFORE execution. Read `spec.md` and `plan.md`. Verify every acceptance criterion in `spec.md` is explicitly addressed in `plan.md`. Flag criteria with no corresponding component, module, or task. Write report to `.dynos/task-{id}/audit-reports/plan-audit-{timestamp}.json`."

Wait for completion. Read the report.

- If all criteria covered: append `{timestamp} [DONE] spec-completion-auditor — all criteria covered` to log. Proceed to Step 5.
- If gaps found: append `{timestamp} [DECISION] plan gaps found — respawning planner to fill: {list}` to log. Spawn planning agent with instruction: "The plan is missing coverage for: [{uncovered criteria}]. Update `plan.md` to address them." Re-run the audit. Repeat until all covered.

### Step 5 — Done

Update `manifest.json` stage to `EXECUTION_GRAPH_BUILD`. Append to log:
```
{timestamp} [ADVANCE] PLAN_AUDIT → EXECUTION_GRAPH_BUILD
```

Print:
```
Plan approved and audited. All acceptance criteria covered.

Next: /dynos-work:execute
```
