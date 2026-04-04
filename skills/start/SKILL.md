---
name: start
description: "Unified Foundry entry point. Drives discovery, design review, spec normalization, plan generation, deterministic artifact validation, and mandatory human approval before execution."
---

# dynos-work: Unified Foundry Start

You are the entry point for dynos-work. You own all human-in-the-loop gates before execution. When done, the task is ready for `/dynos-work:execute`.

There is one pipeline for all tasks. There are no shortcuts. Historical memory may inform discovery and design review, but it is advisory only. Human approval and deterministic artifact checks decide readiness.

---

## Step 0 — Metadata & Initialization

1. Ensure `.dynos/` exists: `mkdir -p .dynos`. Then auto-register this project with the global registry (silent, idempotent): run `python3 "${PLUGIN_HOOKS}/dynoregistry.py" register "$(pwd)" 2>/dev/null || true`. This creates `~/.dynos/projects/{slug}/` and adds the project to `~/.dynos/registry.json` if not already registered. No user action needed. Then ensure the local maintenance daemon is running (silent, idempotent): run `PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 "${PLUGIN_HOOKS}/dynomaintain.py" start --root "$(pwd)" 2>/dev/null || true`. This starts the daemon if not already running. If already running, it is a no-op.
2. Generate a task ID in the format `task-YYYYMMDD-NNN`.
3. Create the task directory: `.dynos/task-{id}/`.
3. Write `raw-input.md` with the full task description exactly as given.
4. Initialize `manifest.json` with at least:

```json
{
  "task_id": "task-20260403-001",
  "created_at": "ISO timestamp",
  "title": "First 80 characters of task description",
  "raw_input": "Full task description as provided by user",
  "input_type": "text | prd | wireframe | mixed",
  "stage": "FOUNDRY_INITIALIZED",
  "classification": null,
  "retry_counts": {},
  "blocked_reason": null,
  "completed_at": null
}
```

5. Initialize `.dynos/task-{id}/execution-log.md`.
6. Before writing `raw-input.md`, inspect the user input for:
   - A file path ending in `.prd.md`, `.pdf`, or `.txt` and treat it as a primary spec source.
   - A URL to a Figma link, screenshot, or wireframe and note it as a design artifact.
   - An attached screenshot or image and note it as a visual spec artifact.
7. Deterministically verify that `manifest.json` parses as valid JSON and that `task_id`, `created_at`, `raw_input`, and `stage` are present before continuing. If available in this repo, use:

```text
python3 hooks/dynosctl.py validate-task .dynos/task-{id}
```
8. Print: `dynos-work: Foundry Task Initialized: task-YYYYMMDD-NNN`

---

## Step 1 — Discovery Intake

1. Build discovery context from:
   - `raw-input.md`
   - relevant existing code in the repo
   - optional trajectory memory from `.dynos/trajectories.json` if available
2. If trajectory memory exists, use it only to surface likely ambiguity or failure patterns. Do not treat it as ground truth and do not copy prior solutions blindly.
3. Generate up to 5 targeted questions that materially reduce implementation risk.
4. Present the questions to the user using `AskUserQuestion`.
5. Write `discovery-notes.md` with the Q&A.

---

## Step 2 — Discovery + Design + Classification

**Fast-track discovery skip:** Before spawning the planner, check if the task input is already well-scoped. A task is well-scoped when ALL of:
- It names a specific file or narrow set of files
- It states explicit constraints (e.g., "do not change X", "UI only", "single-file")
- It describes a concrete, bounded change (not open-ended like "improve performance")

When well-scoped: skip the planner spawn entirely. Instead, write `discovery-notes.md` with "No discovery needed — task is well-scoped." Write `design-decisions.md` with "No hard/critical design options — autonomous decisions only." Classify directly (infer type, domains, risk_level from the input). Then proceed to Step 2b (fast-track gate) and Step 3.

When NOT well-scoped: spawn the planner as normal below.

Spawn the Planner subagent (`dynos-work:planning`) with instruction:

```text
Phase: Discovery + Design + Classification (combined).
Read raw-input.md and discovery-notes.md if present. Also read any attached trajectory context as advisory prior history only.

Perform three things in one pass:
1. Discovery: generate only the highest-value unresolved questions.
2. Design Options: break the task into subtasks. For any subtask rated hard complexity or critical value, generate 2-3 design options with pros and cons. For easy or medium subtasks, decide directly.
3. Classification: produce type, domains, risk_level, and notes.

Return three sections:
- Questions
- Design Options
- Classification (JSON)
```

Present any remaining discovery questions to the user and append answers to `discovery-notes.md`.

If hard or critical design options were returned:
1. Run the Founder skill in simulation mode for each high-risk design option.
2. Treat simulation output as advisory evidence, not automatic approval.
3. Present each option to the user with the simulation evidence and record the chosen design in `design-decisions.md`.

If no high-risk design options were returned, write `design-decisions.md` with the autonomous design choices and rationale.

Write the returned classification object to `manifest.json`.

Deterministic validation before proceeding:
1. `classification.type` must be one of `feature | bugfix | refactor | migration | ml | full-stack`.
2. `classification.risk_level` must be one of `low | medium | high | critical`.
3. `classification.domains` must be an array of known domains only.
4. If validation fails, stop and correct the classification before moving on.

Update `manifest.json` stage to `SPEC_NORMALIZATION`. If available in this repo, use:

```text
python3 hooks/dynosctl.py transition .dynos/task-{id} SPEC_NORMALIZATION
```

---

## Step 2b — Fast-Track Gate (conditional)

After classification, determine fast-track eligibility **deterministically** by running:

```text
python3 -c "from dynoslib import apply_fast_track; from pathlib import Path; print(apply_fast_track(Path('.dynos/task-{id}')))"
```

This checks: `risk_level == "low"` AND exactly 1 domain. It writes `"fast_track": true` or `"fast_track": false` to `manifest.json`. If the command is not available, manually check the conditions and write the field.

When fast-tracked (`fast_track: true`), apply these simplifications throughout the remaining steps:
- **Spec (Step 3):** The planner should produce a concise spec. The `Implicit Requirements Surfaced` and `Risk Notes` sections can contain a single line each if no significant risks exist.
- **Planning (Step 5):** The execution graph should contain a **single segment** (no multi-segment decomposition). A single executor handles the entire change.
- **Audit (handled by audit skill):** When `fast_track: true` in the manifest, spawn only `spec-completion-auditor` and `security-auditor`. Skip all other auditors regardless of streak or domain.

If any condition is not met, proceed normally (no fast-track). Do not ask the user — this is a deterministic gate.

---

## Step 3 — Spec Normalization

Spawn the Planner subagent with instruction:

```text
Phase: Spec Normalization.
Read raw-input.md, discovery-notes.md, and design-decisions.md.
Also read the actual implementation files referenced in the task (e.g., the files that will be modified). Verify runtime semantics directly from the code — do not assume template engines, escaping conventions, or generation mechanisms without reading the relevant functions. Include specific function signatures, data flow paths, and module boundaries in the spec.
Write spec.md.
```

After `spec.md` is written, run deterministic spec validation:
1. The file must contain the headings `Task Summary`, `User Context`, `Acceptance Criteria`, `Implicit Requirements Surfaced`, `Out of Scope`, `Assumptions`, and `Risk Notes`.
2. Acceptance criteria must be a numbered list starting at `1` and incrementing by `1` with no gaps.
3. Every acceptance criterion must be concrete and independently testable.
4. Any assumption that affects behavior must be labeled `needs confirmation` or `safe assumption`.

If any rule fails, send the Planner back to fix `spec.md` before presenting it.

Update `manifest.json` stage to `SPEC_REVIEW`. If available in this repo, use:

```text
python3 hooks/dynosctl.py transition .dynos/task-{id} SPEC_REVIEW
```

---

## Step 4 — Spec Review

This gate always runs. There is no skip path.

Present `spec.md` to the user and ask for approval.

- If approved: append `{timestamp} [HUMAN] SPEC_REVIEW — approved` to the execution log.
- If changes are requested: append the feedback, respawn the Planner in Spec Normalization mode, re-run deterministic spec validation, and present the updated spec again.
- If rejected outright: set `manifest.json` stage to `FAILED`, append `[FAILED] Spec rejected by user`, and stop.

When approved, update `manifest.json` stage to `PLANNING`. If available in this repo, use:

```text
python3 hooks/dynosctl.py transition .dynos/task-{id} PLANNING
```

---

## Step 5 — Generate Plan + Execution Graph

Append to the execution log:

```text
{timestamp} [STAGE] → PLANNING
```

Choose planning mode deterministically:
- Use hierarchical planning if `risk_level` is `high` or `critical`, or if `spec.md` contains more than 10 acceptance criteria.
- Otherwise use standard planning.

Hierarchical flow:
1. Spawn Master Planner (Opus) for strategic boundaries.
2. Spawn Worker Planners in parallel for non-overlapping subsystems.
3. Merge outputs into final `plan.md` and `execution-graph.json`.

Standard flow:
1. Spawn Planner (Opus) with instruction to generate `plan.md` and `execution-graph.json`.

After generation, run deterministic artifact validation before any human review. If available in this repo, run:

```text
python3 hooks/validate_task_artifacts.py .dynos/task-{id}
```

The command is the source of truth for artifact validation. Use the rules below to explain and repair failures:

For `plan.md`:
1. It must contain `Technical Approach`, `Reference Code`, `Components / Modules`, `Data Flow`, `Error Handling Strategy`, `Test Strategy`, `Dependency Graph`, and `Open Questions`.
2. Every component or module section must list exact files.
3. `Reference Code` paths must exist in the repo unless explicitly marked as to-be-created.

For `execution-graph.json`:
1. It must parse as valid JSON.
2. Every segment must have unique `id`.
3. Every segment must declare exactly one valid executor.
4. No file may appear in more than one segment's `files_expected`.
5. Every `depends_on` reference must point to an existing segment.
6. The dependency graph must be acyclic.
7. Every `criteria_id` must map to a real acceptance criterion in `spec.md`.
8. Every acceptance criterion in `spec.md` must be covered by at least one segment.

If any validation fails, respawn planning and fix the artifacts before continuing.

Append to the execution log:

```text
{timestamp} [DONE] planning — final plan.md and execution-graph.json written (mode: {hierarchical|standard})
{timestamp} [STAGE] → PLAN_REVIEW
```

Update `manifest.json` stage to `PLAN_REVIEW`. If available in this repo, use:

```text
python3 hooks/dynosctl.py transition .dynos/task-{id} PLAN_REVIEW
```

---

## Step 6 — Plan Review

This gate always runs. There is no skip path.

Present `plan.md` to the user and ask for approval.

- If approved: append `{timestamp} [HUMAN] PLAN_REVIEW — approved` to the execution log.
- If changes are requested: append the feedback, respawn planning, re-run deterministic artifact validation, and present the updated plan again.
- If rejected outright: set `manifest.json` stage to `FAILED`, append `[FAILED] Plan rejected by user`, and stop.

---

## Step 7 — Plan Audit

**Fast-track skip:** If `manifest.json` has `"fast_track": true`, check the project policy at `~/.dynos/projects/{slug}/policy.json` for `"fast_track_skip_plan_audit": true`. If both are true, skip the plan audit entirely and proceed to Step 8. Log: `{timestamp} [SKIP] plan audit — fast_track_skip_plan_audit policy`. This policy is set automatically by the improvement engine when low-risk tasks consistently pass without repair.

**Normal path:**

1. Spawn `spec-completion-auditor` to verify that `plan.md` and `execution-graph.json` cover all acceptance criteria in `spec.md`.
2. If the auditor finds gaps, route back to planning, repair the gaps, and rerun both deterministic artifact validation and the plan audit.
3. Create a git branch safety net: `dynos/task-{id}-snapshot`.

---

## Step 8 — TDD-First Gate

This gate always runs. There is no skip path.

1. Spawn `testing-executor` with instruction:

```text
TDD-First Mode.
Read spec.md and plan.md.
Write a complete test suite covering every acceptance criterion.
Do not implement production code.
Write only test files and evidence to .dynos/task-{id}/evidence/tdd-tests.md.
```

2. Deterministically validate the generated tests before user review:
   - Every acceptance criterion from `spec.md` must be mapped to at least one test case in the evidence summary.
   - Test file paths must be under the repository root.
   - No production source files may be modified in this step.
3. Present the test file paths and summary to the user.
4. If changes are requested, rerun the testing executor and the same deterministic validation.
5. When approved, append `{timestamp} [HUMAN] TDD_REVIEW — approved` to the execution log.
6. Commit the approved tests to the snapshot branch before any production code is written.

---

## Step 9 — Done

Update `manifest.json` stage to `PRE_EXECUTION_SNAPSHOT`. If available in this repo, use:

```text
python3 hooks/dynosctl.py transition .dynos/task-{id} PRE_EXECUTION_SNAPSHOT
```

Append to the execution log:

```text
{timestamp} [ADVANCE] PLAN_AUDIT → PRE_EXECUTION_SNAPSHOT
```

Print:

```text
Foundry Ready to Execute.

Task:   {task_id}
Spec:   {N} acceptance criteria (human approved)
Plan:   approved, validated, and audited (mode: {standard|hierarchical})
Memory: advisory only

Next: /dynos-work:execute
```

---

## What you do NOT do

- You do not execute production code.
- You do not audit production code.
- You do not decide when the task is done.
- You do not skip discovery, spec review, plan review, plan audit, or TDD review.
- You do not let historical memory or the Founder override human approval or deterministic validation.

---

## Hard Rules

- **No stealth paths:** all tasks follow Steps 0-9.
- **Memory is advisory:** trajectory retrieval may inform questions or design review, but it never overrides the current repo or user instructions.
- **Founder is a consultant:** simulation output informs design decisions but never replaces review gates.
- **Validation before trust:** do not present `spec.md`, `plan.md`, `execution-graph.json`, or the generated test suite for approval until deterministic validation passes.
- **Stop on ambiguity:** if a blocking ambiguity remains unresolved after discovery, flag it explicitly instead of silently choosing.
