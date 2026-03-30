---
name: start
description: "Primary entry point. Runs discovery+design+classification, spec normalization, spec review, plan+execution-graph generation, plan review, and spec coverage audit. When done, run /dynos-work:execute."
---

# dynos-work: Start

You are the entry point for dynos-work. You own all human-in-the-loop gates before execution. When done, the task is ready for `/dynos-work:execute`.

## What you do

### Step 1 — Initialize

1. Generate a task ID in the format `task-YYYYMMDD-NNN` (use today's date, increment NNN if directory exists)
2. Create the task directory: `.dynos/task-{id}/`
3. Write `raw-input.md` with the full task description exactly as given
4. Write `manifest.json`:
```json
{
  "task_id": "task-20260327-001",
  "created_at": "ISO timestamp",
  "title": "First 80 characters of task description",
  "raw_input": "Full task description as provided by user",
  "stage": "DISCOVERY",
  "classification": null,
  "retry_counts": {},
  "blocked_reason": null,
  "completion_at": null
}
```
5. Create execution log at `.dynos/task-{id}/execution-log.md`:
```
# Execution Log — task-{id}
Plugin: dynos-work vX.X.X
Started: {ISO timestamp}

---
{timestamp} [STAGE] → DISCOVERY
```
6. Print:
```
dynos-work: Task initialized
ID: task-20260327-001
```

### Step 2 — Discovery + Design + Classification (single planner spawn)

**Spawn the Planner subagent** (dynos-work:planning) with instruction: "Phase: Discovery + Design + Classification (combined). Read `raw-input.md`. Perform all three phases in one pass:

1. **Discovery:** Generate up to 5 targeted questions that would meaningfully improve understanding of the spec — gaps, ambiguities, trade-offs, unstated constraints. Do not ask obvious or trivial questions.
2. **Design Options:** Break the task into subtasks. Rate each: complexity (easy/medium/hard) and value (low/medium/high/critical). For any subtask rated hard complexity OR critical value, generate 2-3 design options with pros and cons. Decide easy/medium subtasks autonomously.
3. **Classification:** Classify the task (type, domains, risk_level).

Return a structured response with three sections: Questions (numbered list), Design Options (only hard/critical subtasks with options; note autonomous decisions), and Classification (JSON object)."

**Present discovery questions** to the user using **AskUserQuestion** (all questions in one prompt). Wait for their answers.

Write `.dynos/task-{id}/discovery-notes.md` with Q&A.

**If hard/critical design options were returned**, present them to the user **one at a time** using **AskUserQuestion**. If no critical/hard subtasks, note autonomous decisions.

Write `.dynos/task-{id}/design-decisions.md` with decisions.

Write classification to `manifest.json` under the `classification` key.

Append to log:
```
{timestamp} [DONE] discovery + design + classification — notes, decisions, and classification written
{timestamp} [STAGE] → CLASSIFY_AND_SPEC
```

Update `manifest.json` stage to `CLASSIFY_AND_SPEC`.

### Step 3 — Spec Normalization

**Spawn the Planner subagent** with instruction: "Phase: Spec Normalization. Read `raw-input.md`, `discovery-notes.md`, and `design-decisions.md`. Human design choices are binding. Write normalized spec with numbered acceptance criteria to `spec.md`."

Wait for it to complete. The Planner writes `spec.md` per its own format. Verify `spec.md` exists.

Append to log:
```
{timestamp} [DONE] spec normalization — spec.md written
{timestamp} [STAGE] → SPEC_REVIEW
```

Update `manifest.json` stage to `SPEC_REVIEW`.

### Step 4 — Spec Review (you run this, not a subagent)

**This gate always runs. There is no skip path.**

Read `spec.md` and `design-decisions.md`. Present to the user using **AskUserQuestion**:

```
=== Spec Review ===

[contents of spec.md]

---
=== Design Decisions ===

[contents of design-decisions.md, or "No design decisions recorded."]

---
Does this spec accurately capture what you want built?
(yes / no + what to change)
```

- If **approved**: append `{timestamp} [HUMAN] SPEC_REVIEW — approved` to log. Proceed to Step 5.
- If **changes requested**: append `{timestamp} [HUMAN] SPEC_REVIEW — changes requested: {summary}` to log. Spawn Planner with instruction: "Phase: Spec Normalization. Human requested changes: [{feedback}]. Re-normalize spec.md incorporating the feedback. Design decisions remain binding unless explicitly overridden." Then re-present the updated spec. Repeat until approved.

Update `manifest.json` stage to `PLANNING`.

### Step 5 — Generate Plan + Execution Graph (PLANNING)

Append to log:
```
{timestamp} [STAGE] → PLANNING
{timestamp} [SPAWN] planning — generate implementation plan and execution graph
```

**Spawn the Planner subagent** with instruction: "Generate the implementation plan AND execution graph. Read `spec.md` and `design-decisions.md`. Human design choices are binding.

1. Write the implementation plan to `.dynos/task-{id}/plan.md`. Include: technical approach, module/component breakdown, data flow, error handling, test strategy.
2. Write the execution graph to `.dynos/task-{id}/execution-graph.json`. Each segment must declare: id, executor, description, files_expected, depends_on, parallelizable, criteria_ids (list of acceptance criterion numbers this segment satisfies). Executor types: ui-executor, backend-executor, ml-executor, db-executor, refactor-executor, testing-executor, integration-executor."

Wait for completion. The Planner writes `plan.md` and `execution-graph.json` per its own format. Append to log:
```
{timestamp} [DONE] planning — plan.md and execution-graph.json written
{timestamp} [STAGE] → PLAN_REVIEW
```

Update `manifest.json` stage to `PLAN_REVIEW`.

### Step 6 — Plan Review (you run this, not a subagent)

Read `plan.md`. Present to the user using **AskUserQuestion**:

```
=== Plan Review ===

[contents of plan.md]

---
Approve this plan? (yes / no + what to change)
```

- If **approved**: append `{timestamp} [HUMAN] PLAN_REVIEW — approved` to log. Proceed to Step 7.
- If **changes requested**: append `{timestamp} [HUMAN] PLAN_REVIEW — changes requested: {summary}` to log. Spawn Planner again with the feedback. Re-present the updated plan. Repeat until approved.
- If **rejected**: set `manifest.json` stage to `FAILED`. Append `[FAILED] Plan rejected by user`. Stop.

### Step 7 — Spec Coverage Audit (PLAN_AUDIT)

Update `manifest.json` stage to `PLAN_AUDIT`. Append to log:
```
{timestamp} [STAGE] → PLAN_AUDIT
{timestamp} [SPAWN] spec-completion-auditor — verify plan covers all acceptance criteria
```

Spawn the `spec-completion-auditor` agent with instruction: "Audit the plan against the spec BEFORE execution. Read `spec.md` and `plan.md`. Verify every acceptance criterion in `spec.md` is explicitly addressed in `plan.md`. Flag criteria with no corresponding component, module, or task. Write report to `.dynos/task-{id}/audit-reports/plan-audit-{timestamp}.json`."

Wait for completion. Read the report.

- If all criteria covered: append `{timestamp} [DONE] spec-completion-auditor — all criteria covered` to log. Proceed to Step 8.
- If gaps found: append `{timestamp} [DECISION] plan gaps found — respawning planner to fill: {list}` to log. Spawn planning agent with instruction: "The plan is missing coverage for: [{uncovered criteria}]. Update `plan.md` and `execution-graph.json` to address them." Re-run the audit. Repeat until all covered.

### Step 8 — Done

Update `manifest.json` stage to `PRE_EXECUTION_SNAPSHOT`. Append to log:
```
{timestamp} [ADVANCE] PLAN_AUDIT → PRE_EXECUTION_SNAPSHOT
```

Print:
```
Ready to execute.

Task:  {task_id}
Spec:  {N} acceptance criteria
Plan:  approved and audited

Next: /dynos-work:execute
```

## What you do NOT do

- You do not execute code
- You do not audit code
- You do not decide when the task is done
- You do not skip discovery, spec review, or plan review — ever

## If the user provides incomplete input

If the task description is too vague (e.g. "fix the bug" with no context), ask one clarifying question before initializing.

**Too vague:** "fix the thing" → ask "Which thing?"
**Sufficient:** "Add JWT auth to the /api/users endpoint" → proceed immediately
