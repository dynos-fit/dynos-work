---
name: start
description: "Start a new task. Give it a description and it handles discovery, spec, plan, execution, and audit."
---

# dynos-work: Unified Foundry Start

You are the entry point for dynos-work. You own all human-in-the-loop gates before execution. When done, the task is ready for `/dynos-work:execute`.

There is one pipeline for all tasks. There are no shortcuts. Historical memory may inform discovery and design review, but it is advisory only. Human approval and deterministic artifact checks decide readiness.

## Ruthlessness Standard

- Do not let ambiguity leak downstream.
- Do not allow vague specs, soft assumptions, or unbounded scope to survive a phase gate.
- Missing validation, error handling, auth, loading, empty, retry, and rollback requirements are spec defects.
- If an artifact is weak, send it back. Do not carry weakness forward.
- Prefer a blocked gate over a rotten artifact.
- If the work can fail in an obvious way, encode that failure mode before execution starts.

---

## MANDATORY: Token Recording After Every Agent Spawn

After EVERY Agent tool call in this skill (planner, spec-completion auditor, testing-executor), you MUST write a receipt that records token usage. Read `total_tokens` from the Agent tool result's usage summary and run:

```python
from lib_receipts import receipt_planner_spawn, receipt_plan_audit, receipt_plan_validated

# After planner spawn (discovery/design/classification):
receipt_planner_spawn(task_dir, "discovery", tokens_used=TOTAL_TOKENS)

# After planner spawn (spec normalization):
receipt_planner_spawn(task_dir, "spec", tokens_used=TOTAL_TOKENS)

# After planner spawn (plan generation OR combined Spec + Plan):
receipt_planner_spawn(task_dir, "plan", tokens_used=TOTAL_TOKENS)

# After validate_task_artifacts passes — REQUIRED before execute skill can run:
receipt_plan_validated(
    task_dir,
    segment_count=N,                # from execution-graph.json segments length
    criteria_coverage=[1, 2, ...],  # sorted unique criteria_ids covered
    validation_passed=True,
)

# After spec-completion auditor (plan audit; only invoked for high/critical risk):
receipt_plan_audit(task_dir, tokens_used=TOTAL_TOKENS, finding_count=N)

```

Each receipt auto-records tokens to `token-usage.json`. If you skip this, the retrospective will show 0 tokens and the effectiveness scores will be wrong. This is the same enforcement pattern as the execute skill's receipts.

---

---

# Phase 1: Intake

## Step 0 — Metadata & Initialization

1. Ensure `.dynos/` exists: `mkdir -p .dynos`. Then auto-register this project with the global registry (silent, idempotent): run `python3 "${PLUGIN_HOOKS}/registry.py" register "$(pwd)" 2>/dev/null || true`. This creates `~/.dynos/projects/{slug}/` and adds the project to `~/.dynos/registry.json` if not already registered. No user action needed. Then ensure the local maintenance daemon is running (silent, idempotent): run `PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 "${PLUGIN_HOOKS}/daemon.py" start --root "$(pwd)" 2>/dev/null || true`. If already running, it is a no-op.
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
python3 hooks/ctl.py validate-task .dynos/task-{id}
```
8. Print: `dynos-work: Foundry Task Initialized: task-YYYYMMDD-NNN`

---

## Token & Event Capture (applies to ALL events in this skill)

After every subagent spawn AND every deterministic validation, record the event:

**For LLM subagent spawns** (planner, testing-executor, spec-completion-auditor):
```bash
PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 "${PLUGIN_HOOKS}/lib_tokens.py" record \
  --task-dir .dynos/task-{id} \
  --agent "{agent_name}" \
  --model "{model_name}" \
  --input-tokens {input_tokens} \
  --output-tokens {output_tokens} \
  --phase planning \
  --stage "{current_manifest_stage}" \
  --type spawn \
  --detail "{what the agent did}"
```

**For deterministic Python validations** (validate_task_artifacts, ctl validate-task, spec heading check, etc.):
```bash
PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 "${PLUGIN_HOOKS}/lib_tokens.py" record \
  --task-dir .dynos/task-{id} \
  --agent "{validation_tool_name}" \
  --model "none" \
  --input-tokens 0 \
  --output-tokens 0 \
  --phase planning \
  --stage "{current_manifest_stage}" \
  --type deterministic \
  --detail "{validation result summary}"
```

Where:
- `input_tokens`/`output_tokens` come from the Agent tool result's usage summary (pass `0` if unavailable)
- `model_name` is the model used (e.g. "opus", "sonnet", "haiku")
- `current_manifest_stage` is the current stage from manifest.json (e.g. DISCOVERY, SPEC_NORMALIZATION, PLANNING)
- `detail` is a short description of what happened (e.g. "Discovery + Design + Classification", "Validated spec.md headings — 0 errors")

This writes to `.dynos/task-{id}/token-usage.json` with a chronological event log. The same hook is used by execute and audit skills — events accumulate across all phases.

**Specific events to record in this skill:**
- Step 2: Planner spawn (phase=planning, stage=DISCOVERY)
- Step 2: Classification validation (type=deterministic, stage=DISCOVERY)
- Step 2b: Fast-track gate check (type=deterministic, stage=DISCOVERY)
- Step 3: Planner spec normalization spawn (phase=planning, stage=SPEC_NORMALIZATION)
- Step 3: Spec heading/criteria validation (type=deterministic, stage=SPEC_NORMALIZATION)
- Step 5: Planner plan generation spawn (phase=planning, stage=PLANNING)
- Step 5: validate_task_artifacts run (type=deterministic, stage=PLANNING)
- Step 7: spec-completion-auditor plan audit spawn (phase=audit, stage=PLAN_AUDIT)
- Step 8: testing-executor TDD spawn (phase=tdd, stage=PLAN_AUDIT)
- Step 8: TDD criteria coverage validation (type=deterministic, stage=PLAN_AUDIT)

---

## Step 1 — Discovery Intake

1. Build discovery context from:
   - `raw-input.md`
   - relevant existing code in the repo
   - optional trajectory memory from `.dynos/trajectories.json` if available (**skip when `learning_enabled=false` in project policy.json**)
2. If trajectory memory exists and learning is enabled, use it only to surface likely ambiguity or failure patterns. Do not treat it as ground truth and do not copy prior solutions blindly.
3. Generate up to 5 targeted questions that materially reduce implementation risk.
4. Present the questions to the user using `AskUserQuestion`.
5. Write `discovery-notes.md` with the Q&A.

---

---

# Phase 2: Design

## Step 2 — Discovery + Design + Classification

**Fast-track discovery skip:** Before spawning the planner, check if the task input is already well-scoped. A task is well-scoped when ALL of:
- It names a specific file or narrow set of files
- It states explicit constraints (e.g., "do not change X", "UI only", "single-file")
- It describes a concrete, bounded change (not open-ended like "improve performance")

When well-scoped: skip the planner spawn entirely. Instead, write `discovery-notes.md` with "No discovery needed — task is well-scoped." Write `design-decisions.md` with "No hard/critical design options — autonomous decisions only." Classify directly (infer type, domains, risk_level from the input). Then proceed to Step 2b (fast-track gate) and Step 3.

When NOT well-scoped: spawn the planner as normal below.

**Learned Planning Skill Injection (skip when `learning_enabled=false`):** Before spawning the planner, check if a learned planning skill exists for this task type:

```bash
PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 -c "from pathlib import Path; from router import resolve_route; r = resolve_route(Path('.'), 'plan-skill', '{task_type}'); print(r['agent_path'] or '')" 
```

If a non-empty path is returned AND the file exists, read it, strip frontmatter, and append its contents to the planner's instruction below under a `## Learned Planning Rules` heading. This injects project-specific planning patterns (e.g., tighter acceptance criteria, better segment sizing) derived from past task retrospectives. Log: `{timestamp} [ROUTE] plan-skill route={mode} agent={agent_name}`.

Spawn the Planner subagent (`dynos-work:planning`) with instruction:

```text
Phase: Discovery + Design + Classification (combined).
Read raw-input.md and discovery-notes.md if present. Also read any attached trajectory context as advisory prior history only.

Be ruthless. Surface ambiguity, hidden requirements, failure modes, and soft assumptions. Do not produce generic questions or generic design options.

Perform three things in one pass:
1. Discovery: generate only the highest-value unresolved questions.
2. Design Options: break the task into subtasks. For any subtask rated hard complexity or critical value, generate 2-3 design options with pros and cons. For easy or medium subtasks, decide directly.
3. Classification: produce type, domains, risk_level, and notes.

Return three sections:
- Questions
- Design Options
- Classification (JSON)
```

Reject lazy output mentally before accepting it. If the planner returns generic questions, generic design options, or a mushy classification, send it back.

Present any remaining discovery questions to the user and append answers to `discovery-notes.md`.

If hard or critical design options were returned, present each option to the user with pros/cons and record the chosen design in `design-decisions.md`.

If no high-risk design options were returned, write `design-decisions.md` with the autonomous design choices and rationale.

Write the returned classification object to `manifest.json`.

Deterministic validation before proceeding:
1. `classification.type` must be one of `feature | bugfix | refactor | migration | ml | full-stack`.
2. `classification.risk_level` must be one of `low | medium | high | critical`.
3. `classification.domains` must be an array of known domains only.
4. If validation fails, stop and correct the classification before moving on.

Update `manifest.json` stage to `SPEC_NORMALIZATION`. If available in this repo, use:

```text
python3 hooks/ctl.py transition .dynos/task-{id} SPEC_NORMALIZATION
```

---

## Step 2b — Fast-Track Gate (conditional)

After classification, determine fast-track eligibility **deterministically** by running:

```text
PYTHONPATH="hooks:${PYTHONPATH:-}" python3 -c "from lib import apply_fast_track; from pathlib import Path; print(apply_fast_track(Path('.dynos/task-{id}')))"
```

This checks: `risk_level == "low"` AND exactly 1 domain. It writes `"fast_track": true` or `"fast_track": false` to `manifest.json`. If the command is not available, manually check the conditions and write the field.

When fast-tracked (`fast_track: true`), apply these simplifications throughout the remaining steps:
- **Spec (Step 3):** The planner should produce a concise spec. The `Implicit Requirements Surfaced` and `Risk Notes` sections can contain a single line each if no significant risks exist.
- **Planning (Step 5):** The execution graph should contain a **single segment** (no multi-segment decomposition). A single executor handles the entire change.
- **Audit (handled by audit skill):** When `fast_track: true` in the manifest, spawn only `spec-completion-auditor` and `security-auditor`. Skip all other auditors regardless of streak or domain.

If any condition is not met, proceed normally (no fast-track). Do not ask the user — this is a deterministic gate.

---

---

# Phase 3: Specification

## Step 3 — Spec Normalization

**Fast-track combined spawn:** If `manifest.json` has `"fast_track": true`, skip the spawn and the spec validation. Spec is produced in Step 5 by the combined Spec + Plan planner spawn. Walk the manifest stage straight through `SPEC_NORMALIZATION → SPEC_REVIEW → PLANNING` (no work between transitions; this preserves the state-machine invariants while collapsing the LLM work into a single spawn). Log: `{timestamp} [SKIP] spec-normalization-spawn — fast_track combined planner`. Skip the rest of this step and proceed to Step 4.

**Normal path:** Spawn the Planner subagent with instruction:

```text
Phase: Spec Normalization.
Read raw-input.md, discovery-notes.md, and design-decisions.md.
Also read the actual implementation files referenced in the task (e.g., the files that will be modified). Verify runtime semantics directly from the code — do not assume template engines, escaping conventions, or generation mechanisms without reading the relevant functions. Include specific function signatures, data flow paths, and module boundaries in the spec.
Write a spec that leaves executors zero room to hand-wave. Name the exact behavior, exact boundaries, exact failure modes, and exact evidence needed to prove completion.
Write spec.md.
```

If the spec still contains vague adjectives, missing states, or unstated boundary behavior after normalization, send it back again.

After `spec.md` is written, run deterministic spec validation:
1. The file must contain the headings `Task Summary`, `User Context`, `Acceptance Criteria`, `Implicit Requirements Surfaced`, `Out of Scope`, `Assumptions`, and `Risk Notes`.
2. Acceptance criteria must be a numbered list starting at `1` and incrementing by `1` with no gaps.
3. Every acceptance criterion must be concrete and independently testable.
4. Any assumption that affects behavior must be labeled `needs confirmation` or `safe assumption`.

If any rule fails, send the Planner back to fix `spec.md` before presenting it.

Update `manifest.json` stage to `SPEC_REVIEW`. If available in this repo, use:

```text
python3 hooks/ctl.py transition .dynos/task-{id} SPEC_REVIEW
```

---

## Step 4 — Spec Review

**Fast-track skip:** If `manifest.json` has `"fast_track": true`, skip this step. Spec is reviewed together with the plan in Step 6 (combined approval gate). Log: `{timestamp} [SKIP] spec-review — fast_track combined gate`.

**Normal path:** Present `spec.md` to the user and ask for approval.

- If approved: append `{timestamp} [HUMAN] SPEC_REVIEW — approved` to the execution log.
- If changes are requested: append the feedback, respawn the Planner in Spec Normalization mode, re-run deterministic spec validation, and present the updated spec again.
- If rejected outright: set `manifest.json` stage to `FAILED`, append `[FAILED] Spec rejected by user`, and stop.

When approved, update `manifest.json` stage to `PLANNING`. If available in this repo, use:

```text
python3 hooks/ctl.py transition .dynos/task-{id} PLANNING
```

---

---

# Phase 4: Planning

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

Fast-track combined flow (when `fast_track: true`):
1. Spawn Planner (Opus) ONCE with phase `Spec + Plan` to produce `spec.md`, `plan.md`, and `execution-graph.json` together. This replaces both Step 3 (Spec Normalization) and Step 5's normal planner spawn.

Standard flow:
1. Spawn Planner (Opus) with instruction to generate `plan.md` and `execution-graph.json`.

After generation, run deterministic artifact validation before any human review. If available in this repo, run:

```text
python3 hooks/validate_task_artifacts.py .dynos/task-{id}
```

The command is the source of truth for artifact validation. Use the rules below to explain and repair failures:

For `plan.md`:
1. It must contain `Technical Approach`, `Reference Code`, `Components / Modules`, `Data Flow`, `Error Handling Strategy`, `Test Strategy`, `Dependency Graph`, and `Open Questions`.
2. When domains include backend, ui, or security: `API Contracts` section is required. When domains include db: `Data Model` section is required.
3. Every component or module section must list exact files.
4. `Reference Code` paths must exist in the repo unless explicitly marked as to-be-created.
5. **Gap analysis (deterministic):** if the plan contains `API Contracts` or `Data Model` sections, their claims are verified against the codebase. Endpoints listed in the API Contracts table must correspond to actual route definitions. Tables listed in the Data Model table must correspond to actual model/schema/migration definitions. Claimed-but-not-found entries are validation errors — the planner must either fix the table or mark new entries as to-be-created.

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

**Receipt: plan-validated (MANDATORY).** Once `validate_task_artifacts` passes, write the plan-validated receipt. Without this receipt the eventual transition to `EXECUTION` (in the execute skill) will be blocked by the state machine:

```python
from pathlib import Path
from lib_receipts import receipt_plan_validated

# Read segment count and criteria coverage from execution-graph.json
import json
graph = json.loads(Path(".dynos/task-{id}/execution-graph.json").read_text())
segments = graph["segments"]
criteria = sorted({c for s in segments for c in s.get("criteria_ids", [])})

receipt_plan_validated(
    task_dir=Path(".dynos/task-{id}"),
    segment_count=len(segments),
    criteria_coverage=criteria,
    validation_passed=True,
)
```

Append to the execution log:

```text
{timestamp} [DONE] planning — final plan.md and execution-graph.json written (mode: {hierarchical|standard})
{timestamp} [STAGE] → PLAN_REVIEW
```

Update `manifest.json` stage to `PLAN_REVIEW`. If available in this repo, use:

```text
python3 hooks/ctl.py transition .dynos/task-{id} PLAN_REVIEW
```

---

## Step 6 — Plan Review

This gate always runs. For fast-track tasks it acts as the combined Spec + Plan approval (since Step 4 was skipped) — present BOTH `spec.md` AND `plan.md` together.

Present the artifact(s) to the user and ask for approval.

- If approved: append `{timestamp} [HUMAN] PLAN_REVIEW — approved` to the execution log (for fast-track also append `[HUMAN] SPEC_REVIEW — approved (combined gate)`).
- If changes are requested: append the feedback, respawn planning (combined Spec + Plan phase for fast-track, otherwise standard planning), re-run deterministic artifact validation, and present the updated artifact(s) again.
- If rejected outright: set `manifest.json` stage to `FAILED`, append `[FAILED] Plan rejected by user`, and stop.

---

---

# Phase 5: Verification

## Step 7 — Plan Audit

The deterministic gap analysis ALWAYS runs. The LLM auditor only runs for high/critical-risk tasks (the deterministic check covers low/medium because validate_task_artifacts already enforces criteria coverage). This avoids 1.5–3M tokens per task on auditor work that duplicates the deterministic checks.

1. **Deterministic gap analysis (mandatory, always runs):**
   ```bash
   python3 hooks/plan_gap_analysis.py --root . --task-dir .dynos/task-{id}
   ```
   This verifies that claims in `## API Contracts` and `## Data Model` sections correspond to real code. If the plan claims an endpoint or table exists that the codebase doesn't have, the planner must either fix the table or explicitly mark the entry as to-be-created. Gap analysis failures block — repair before continuing.

2. **LLM plan auditor (conditional):** Only spawn `spec-completion-auditor` when `risk_level` is `high` or `critical`. For low/medium risk, the deterministic checks (`validate_task_artifacts` for criteria coverage + gap analysis for code/plan alignment) are authoritative — skip the LLM spawn. Log: `{timestamp} [SKIP] plan-audit-llm — risk_level={risk}`.

3. If gap analysis finds gaps, or (when invoked) the auditor finds gaps, route back to planning, repair, and rerun deterministic artifact validation.
4. Create a git branch safety net: `dynos/task-{id}-snapshot`.

---

## Step 8 — TDD-First Gate (opt-in only)

This gate is **off by default**. It is only invoked when `risk_level` is `critical` OR the spec explicitly requests TDD-first.

When NOT invoked: tests are written by `testing-executor` after production code, in the execute skill (Step 4 of execute), where the implementation context is already known. This avoids ~1.5–2M tokens of pre-code context loading per task.

When invoked (critical risk or explicit opt-in):

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

---

# Phase 6: Handoff

## Step 9 — Done

Update `manifest.json` stage to `PRE_EXECUTION_SNAPSHOT`. If available in this repo, use:

```text
python3 hooks/ctl.py transition .dynos/task-{id} PRE_EXECUTION_SNAPSHOT
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
- You do not skip discovery, plan review, or plan audit (when applicable to the risk level).
- You do not let historical memory override human approval or deterministic validation.

---

## Hard Rules

- **No stealth paths:** all tasks follow Steps 0-9.
- **Memory is advisory:** trajectory retrieval may inform questions or design review, but it never overrides the current repo or user instructions.
- **Validation before trust:** do not present `spec.md`, `plan.md`, `execution-graph.json`, or the generated test suite for approval until deterministic validation passes.
- **Stop on ambiguity:** if a blocking ambiguity remains unresolved after discovery, flag it explicitly instead of silently choosing.
