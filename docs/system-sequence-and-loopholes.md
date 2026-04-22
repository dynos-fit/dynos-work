# dynos-work Sequence Diagram And Loophole Map

This document is the compact companion to [system-workflow-trace.md](/Users/hassam/Documents/dynos-work/docs/system-workflow-trace.md).

It has two jobs:

1. show the live system as a **Mermaid sequence diagram**
2. show the live system's **"trust me bro" loopholes** by stage, with concrete file/function references

Scope:
- this traces the **live repo at `/Users/hassam/Documents/dynos-work`**
- not the cost-reduction worktree
- where the live branch is still prompt-driven, this document says so directly

## 1. Sequence Diagram

```mermaid
sequenceDiagram
    autonumber

    actor User
    participant Start as skills/start/SKILL.md
    participant Plan as skills/plan/SKILL.md
    participant Execute as skills/execute/SKILL.md
    participant Audit as skills/audit/SKILL.md
    participant Router as hooks/router.py
    participant Ctl as hooks/ctl.py
    participant Core as hooks/lib_core.py
    participant Receipts as hooks/lib_receipts.py
    participant Scheduler as hooks/scheduler.py
    participant Validate as hooks/lib_validate.py
    participant QLearn as memory/lib_qlearn.py
    participant Eventbus as hooks/eventbus.py

    User->>Start: /dynos-work:start
    Start->>Ctl: validate-task
    Ctl->>Validate: validate_task_artifacts(task_dir)
    Validate-->>Ctl: pass/fail
    Ctl-->>Start: validation result

    Start->>Router: planner-inject-prompt (discovery/spec/plan)
    Router->>Receipts: write planner prompt sidecars
    Router-->>Start: planner prompt digest
    Start->>Receipts: receipt_planner_spawn(...)
    Receipts->>Scheduler: handle_receipt_written(...)

    Start->>User: ask discovery questions
    User-->>Start: answers
    Start->>Start: write discovery-notes.md
    Start->>Start: write design-decisions.md
    Start->>Start: write classification into manifest.json
    Start->>Ctl: transition SPEC_NORMALIZATION
    Ctl->>Core: transition_task(task_dir, "SPEC_NORMALIZATION")
    Core-->>Ctl: manifest updated or refusal

    Start->>Start: write spec.md
    Start->>Validate: validate_task_artifacts(task_dir)
    Validate-->>Start: spec valid/invalid
    Start->>Ctl: approve-stage SPEC_REVIEW
    Ctl->>Receipts: receipt_human_approval(...)
    Receipts->>Scheduler: handle_receipt_written(...)
    Scheduler->>Core: compute_next_stage() then transition_task(..., "PLANNING")

    User->>Plan: /dynos-work:plan
    Plan->>Plan: write plan.md + execution-graph.json
    Plan->>Receipts: receipt_plan_validated(...)
    Plan->>Ctl: approve-stage PLAN_REVIEW
    Ctl->>Receipts: receipt_human_approval(...)
    Receipts->>Scheduler: handle_receipt_written(...)
    Note over Scheduler: Live branch scheduler scope is narrow;<br/>most later sequencing is still skill-driven.

    User->>Execute: /dynos-work:execute
    Execute->>Router: executor-plan
    Router-->>Execute: segment routing plan
    Execute->>Receipts: receipt_executor_routing(plan['segments'])
    loop each segment
        Execute->>Router: inject-prompt
        Router->>Receipts: write executor prompt sidecar
        Router-->>Execute: final executor prompt
        Execute->>Execute: spawn executor agent
        Execute->>Receipts: receipt_executor_done(...)
    end
    Execute->>Ctl: transition TEST_EXECUTION
    Ctl->>Core: transition_task(task_dir, "TEST_EXECUTION")
    Core-->>Ctl: pass/fail

    User->>Audit: /dynos-work:audit
    Audit->>Router: build_audit_plan(...)
    Router-->>Audit: audit-plan.json
    Audit->>Receipts: receipt_audit_routing(...)
    loop each spawned auditor
        Audit->>Router: audit-inject-prompt
        Router->>Receipts: write auditor prompt sidecar
        Router-->>Audit: final auditor prompt
        Audit->>Audit: spawn auditor
        Audit->>Ctl: audit-receipt
        Ctl->>Receipts: receipt_audit_done(...)
    end

    alt blocking findings exist
        Audit->>Ctl: repair-plan
        Ctl->>QLearn: build_repair_plan(...)
        QLearn-->>Ctl: assignments
        Audit->>Audit: spawn repair-coordinator / write repair-log.json
        Audit->>Audit: spawn repair executors
        Audit->>Ctl: repair-update
        Ctl->>QLearn: update_from_outcomes(...)
        QLearn-->>Ctl: q-table updates
        Audit->>Ctl: transition REPAIR_PLANNING / REPAIR_EXECUTION / CHECKPOINT_AUDIT
        Ctl->>Core: transition_task(...)
    else no blocking findings
        Audit->>Ctl: compute-reward --write
        Ctl->>Validate: compute_reward(task_dir)
        Validate-->>Ctl: retrospective payload
        Ctl->>Receipts: receipt_retrospective(...)
        Audit->>Core: transition_task(task_dir, "DONE")
    end

    Core-->>Eventbus: DONE task-completed side effects
    Eventbus->>Eventbus: learning/calibration handlers
    Eventbus->>Core: transition_task(task_dir, "CALIBRATED")
```

## 2. Control Surface Summary

The live system has four control layers:

1. **Prompt / skill layer**
   - `skills/start/SKILL.md`
   - `skills/plan/SKILL.md`
   - `skills/execute/SKILL.md`
   - `skills/audit/SKILL.md`

2. **Receipt layer**
   - `hooks/lib_receipts.py`
   - this is where sidecars and proof objects become durable

3. **State machine / transition gates**
   - `hooks/lib_core.py`
   - especially `transition_task()`

4. **Routing / validation / learning**
   - `hooks/router.py`
   - `hooks/lib_validate.py`
   - `memory/lib_qlearn.py`
   - `hooks/eventbus.py`

The loopholes live mostly in layer 1. The strong defenses live mostly in layers 2 and 3.

## 3. Loophole Map

This section is intentionally blunt.

## 3.1 Start Phase

### Loophole: direct classification shortcut in prompt logic

File:
- [skills/start/SKILL.md](/Users/hassam/Documents/dynos-work/skills/start/SKILL.md:204)

Live text:

```text
When well-scoped: skip the planner spawn entirely. Instead, write `discovery-notes.md` with "No discovery needed — task is well-scoped." Write `design-decisions.md` with "No hard/critical design options — autonomous decisions only." Classify directly (infer type, domains, risk_level from the input). Then proceed to Step 2b (fast-track gate) and Step 3.
```

Why this is a loophole:
- the model is deciding whether the task is "well-scoped"
- the model is then allowed to skip the planner
- the model is then allowed to infer classification fields directly
- there is validation later, but the **decision to skip the planner is prompt-owned**

Failure mode:
- Claude convinces itself the task is simple
- planner never runs
- discovery and design compression happens silently
- classification is still syntactically valid, so downstream code may not catch the strategic mistake

### Loophole: external-solution gate is hand-authored by the model

File:
- [skills/start/SKILL.md](/Users/hassam/Documents/dynos-work/skills/start/SKILL.md:283)

Live behavior:
- the prompt tells the model to decide whether search should happen
- the prompt tells the model to write `external-solution-gate.json`
- the prompt tells the model what JSON shape to emit

Why this is a loophole:
- the gate artifact is supposed to be a control-plane decision
- but in the live branch it is still produced by the LLM
- the model can under-trigger search, over-trigger search, or fake a strong recommendation trail

Failure mode:
- local-only invention when external docs would have saved time
- or dependency-chasing because the model got excited
- either way the gate is not code-owned

### Loophole: transition to `SPEC_NORMALIZATION` is skill-driven

Files:
- [skills/start/SKILL.md](/Users/hassam/Documents/dynos-work/skills/start/SKILL.md:256)
- [hooks/lib_core.py](/Users/hassam/Documents/dynos-work/hooks/lib_core.py:1375)

Why this is only partially safe:
- `transition_task()` still refuses illegal moves
- but the skill still tells the model when to make the move
- the live scheduler does not own this edge

## 3.2 Spec Review

### Strength: approval is hash-bound and scheduler-driven

Files:
- [hooks/ctl.py](/Users/hassam/Documents/dynos-work/hooks/ctl.py:140)
- [hooks/scheduler.py](/Users/hassam/Documents/dynos-work/hooks/scheduler.py:58)
- [hooks/lib_core.py](/Users/hassam/Documents/dynos-work/hooks/lib_core.py:1392)

Why this is good:
- approval receipt contains artifact hash
- scheduler computes whether `SPEC_REVIEW -> PLANNING` is legal
- this edge is one of the few places where sequencing is actually inverted into code

Remaining weakness:
- only this narrow edge is scheduler-owned in the live branch

## 3.3 Planning

### Loophole: plan generation and plan-review choreography remain prompt-heavy

Files:
- [skills/plan/SKILL.md](/Users/hassam/Documents/dynos-work/skills/plan/SKILL.md)
- [hooks/lib_validate.py](/Users/hassam/Documents/dynos-work/hooks/lib_validate.py:240)

What is safe:
- `validate_task_artifacts()` validates the plan and graph
- `plan-validated` freshness is later enforced by `transition_task()`

What is still loose:
- the skill still decides when to respawn planning
- the skill still decides how to react to soft plan quality issues
- the skill still determines most of the human review choreography

This is not a fake defense issue. It is a real split-brain:
- artifact structure is deterministic
- planning flow is still mostly prose

## 3.4 Execute Phase

### Loophole: critical path, caching, and progressive audit logic are described in prompt prose

File:
- [skills/execute/SKILL.md](/Users/hassam/Documents/dynos-work/skills/execute/SKILL.md:116)

Live text includes:
- calculate dependency depth
- prioritize highest-depth segments
- decide cache reuse from evidence + timestamps
- trigger background audits for high-risk domains

Why this is a loophole:
- these are orchestration decisions
- in the live branch they are not computed by ctl
- the model is being trusted to do scheduling math and caching policy correctly

Failure mode:
- wrong batch ordering
- bogus cache hits
- lost evidence reuse
- unnecessary respawns

### Loophole: manual fallback if `inject-prompt` is unavailable

File:
- [skills/execute/SKILL.md](/Users/hassam/Documents/dynos-work/skills/execute/SKILL.md:192)

Live text:

```text
If `inject-prompt` is not available (command not found), fall back to manually reading the `agent_path` file from the executor plan and appending its contents to the prompt. But this should never happen in this repo.
```

Why this is a loophole:
- it creates an explicit bypass around deterministic prompt construction
- once the model is told a manual path exists, it can rationalize using it

### Loophole: execution completion still depends on skill prose

File:
- [skills/execute/SKILL.md](/Users/hassam/Documents/dynos-work/skills/execute/SKILL.md:288)

Why this is a loophole:
- the skill says “repeat until all segments have evidence files”
- then call transition to `TEST_EXECUTION`
- in the live branch there is no dedicated ctl command that owns segment-state aggregation and final execution completion

What protects you:
- `transition_task()` still enforces legal edges
- `CHECKPOINT_AUDIT` later enforces executor receipts

What does not protect you:
- unnecessary executor churn before that point

## 3.5 Audit Phase

### Strength: audit routing and prompt injection are deterministic

Files:
- [hooks/router.py](/Users/hassam/Documents/dynos-work/hooks/router.py:805)
- [hooks/router.py](/Users/hassam/Documents/dynos-work/hooks/router.py:1482)
- [hooks/ctl.py](/Users/hassam/Documents/dynos-work/hooks/ctl.py:223)

What is safe:
- eligible auditors are built deterministically
- learned auditor prompt injection is deterministic
- `audit-receipt` writes through `receipt_audit_done(...)`

### Loophole: repair-loop planning is still half prompt-owned

Files:
- [skills/audit/SKILL.md](/Users/hassam/Documents/dynos-work/skills/audit/SKILL.md:194)
- [skills/audit/SKILL.md](/Users/hassam/Documents/dynos-work/skills/audit/SKILL.md:203)
- [skills/audit/SKILL.md](/Users/hassam/Documents/dynos-work/skills/audit/SKILL.md:231)

Live flow:
- model feeds findings into `ctl repair-plan`
- then a `repair-coordinator` agent is spawned to write `repair-log.json`
- later the model builds `outcomes` and feeds them into `ctl repair-update`

Why this is a loophole:
- `build_repair_plan()` chooses assignments deterministically
- but `repair-log.json` batching and concrete task shaping are still produced by an LLM
- `outcomes` assembly is also model-authored in the live branch

Failure mode:
- wrong files in `repair-log.json`
- retry counts drift
- bad batching on shared files
- Q-learning updated from a hand-assembled, possibly wrong outcome payload

### Loophole: transition to `DONE` is still explicitly called from audit skill

File:
- [skills/audit/SKILL.md](/Users/hassam/Documents/dynos-work/skills/audit/SKILL.md:415)

Live text:

```text
Write `completion.json`. Transition the task to `DONE` by calling `transition_task(task_dir, "DONE")` from `lib.py`
```

Why this is a loophole:
- the `DONE` gate is strong
- but the decision to invoke it is still in prompt logic
- there is no dedicated “finish audit” ctl command in the live branch

This is better than no gate, but worse than deterministic completion ownership.

## 3.6 Repair Skill

### Loophole: repair skill still tells the model to update `repair-log.json`

File:
- [skills/repair/SKILL.md](/Users/hassam/Documents/dynos-work/skills/repair/SKILL.md:47)

Live text:

```text
- Update `repair-log.json` with the result
```

Why this is a loophole:
- `repair-log.json` is control-plane state
- telling the model to mutate it directly is an invitation to drift

## 3.7 Scheduler Coverage

### Loophole: scheduler scope is intentionally narrow

File:
- [hooks/scheduler.py](/Users/hassam/Documents/dynos-work/hooks/scheduler.py:23)

Live text:

```text
Scope ceiling (task-20260419-008 POC): ``compute_next_stage`` handles
only ``SPEC_REVIEW -> PLANNING``.
```

Why this matters:
- most later stage transitions are still not owned by scheduler predicates
- receipt writes are durable and auditable, but the **sequencing brain** still mostly lives in skill prose

## 3.8 Learning / Calibration

### Strength: retrospective scoring is deterministic

Files:
- [hooks/ctl.py](/Users/hassam/Documents/dynos-work/hooks/ctl.py:267)
- [hooks/lib_validate.py](/Users/hassam/Documents/dynos-work/hooks/lib_validate.py:526)

What is safe:
- retrospective is computed from files on disk
- `receipt_retrospective(...)` is self-computing
- eventbus later attempts `DONE -> CALIBRATED`

### Remaining weakness

Even here, the live branch still depends on the audit skill to reach the right pre-DONE artifact set before the eventbus ever sees the task.

## 4. Diagram Legend: What Is Actually Hard vs Soft

### Hard, code-owned

- `transition_task()` legality and gate checks
- receipt sidecar verification
- human approval hash checks
- `plan-validated` freshness checks
- executor receipt enforcement before checkpoint audit
- retrospective scoring
- `DONE -> CALIBRATED`

### Soft, prompt-owned

- discovery skip / direct classify
- external-solution gate writing
- execution critical-path ordering
- cache reuse decisions in execute skill
- progressive background auditing
- repair-log batching and file lists
- repair outcome payload assembly
- final audit completion decision

## 5. Highest-Risk Loopholes

If you only care about the most dangerous ones in the live branch, it is these five:

1. **Start can skip planner and classify directly**
   - [skills/start/SKILL.md](/Users/hassam/Documents/dynos-work/skills/start/SKILL.md:204)

2. **External-solution gate is hand-authored**
   - [skills/start/SKILL.md](/Users/hassam/Documents/dynos-work/skills/start/SKILL.md:283)

3. **Execute skill owns scheduling/caching logic**
   - [skills/execute/SKILL.md](/Users/hassam/Documents/dynos-work/skills/execute/SKILL.md:116)

4. **Audit skill still uses repair-coordinator to build `repair-log.json`**
   - [skills/audit/SKILL.md](/Users/hassam/Documents/dynos-work/skills/audit/SKILL.md:203)

5. **Audit skill hand-builds repair outcomes for Q-learning**
   - [skills/audit/SKILL.md](/Users/hassam/Documents/dynos-work/skills/audit/SKILL.md:231)

## 6. Blunt Bottom Line

The live branch already has a strong deterministic substrate.

But the system is still not “LLM only writes content, code owns orchestration.”

The live reality is:
- code owns **proofs, hashes, gates, and some routing**
- prompts still own **a lot of sequencing, batching, skipping, and control artifact assembly**

That is the real loophole map for the branch you are on right now.
