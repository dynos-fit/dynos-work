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

Before EVERY planner receipt write, you MUST first write a per-phase injected-prompt sidecar by piping the planner prompt body into `hooks/router.py planner-inject-prompt`. Capture the printed sha256 digest and pass it back through as the `injected_prompt_sha256=<digest>` kwarg on the receipt call. The receipt will raise `ValueError` if the sidecar is missing or its contents do not match — that is the proof-of-injection gate.

```bash
# Discovery planner — write sidecar, capture digest:
DISCOVERY_DIGEST=$(printf '%s' "$DISCOVERY_PROMPT" | python3 "${PLUGIN_HOOKS}/router.py" planner-inject-prompt --task-id {id} --phase discovery)

# Architectural Design Doc planner (high/critical-risk only) — write sidecar, capture digest:
ARCH_DESIGN_DIGEST=$(printf '%s' "$ARCH_DESIGN_PROMPT" | python3 "${PLUGIN_HOOKS}/router.py" planner-inject-prompt --task-id {id} --phase arch-design)

# Spec planner — write sidecar, capture digest:
SPEC_DIGEST=$(printf '%s' "$SPEC_PROMPT" | python3 "${PLUGIN_HOOKS}/router.py" planner-inject-prompt --task-id {id} --phase spec)

# Plan planner — write sidecar, capture digest:
PLAN_DIGEST=$(printf '%s' "$PLAN_PROMPT" | python3 "${PLUGIN_HOOKS}/router.py" planner-inject-prompt --task-id {id} --phase plan)
```

Then, after each planner subagent returns, write the matching deterministic receipt:

```bash
# After planner spawn (discovery/design/classification):
python3 hooks/ctl.py planner-receipt .dynos/task-{id} discovery \
  --tokens-used {TOTAL_TOKENS} \
  --model {MODEL_USED} \
  --agent-name planning \
  --injected-prompt-sha256 "${DISCOVERY_DIGEST}"

# After planner spawn (architectural design doc — high/critical-risk only):
python3 hooks/ctl.py planner-receipt .dynos/task-{id} arch-design \
  --tokens-used {TOTAL_TOKENS} \
  --model {MODEL_USED} \
  --agent-name planning \
  --injected-prompt-sha256 "${ARCH_DESIGN_DIGEST}"

# After planner spawn (spec normalization):
python3 hooks/ctl.py planner-receipt .dynos/task-{id} spec \
  --tokens-used {TOTAL_TOKENS} \
  --model {MODEL_USED} \
  --agent-name planning \
  --injected-prompt-sha256 "${SPEC_DIGEST}"

# After planner spawn (plan generation OR combined Spec + Plan):
python3 hooks/ctl.py planner-receipt .dynos/task-{id} plan \
  --tokens-used {TOTAL_TOKENS} \
  --model {MODEL_USED} \
  --agent-name planning \
  --injected-prompt-sha256 "${PLAN_DIGEST}"

# After validate_task_artifacts passes — REQUIRED before execute skill can run.
python3 hooks/ctl.py plan-validated-receipt .dynos/task-{id}

# After spec-completion auditor on high/critical-risk tasks only:
python3 hooks/ctl.py plan-audit-receipt .dynos/task-{id} \
  --tokens-used {TOTAL_TOKENS} \
  --model {MODEL_USED}
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
7. Deterministically verify that `manifest.json` parses as valid JSON and that `task_id`, `created_at`, `raw_input`, and `stage` are present before continuing. Run:

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

## MANDATORY: Role Stamping Before Every Agent Spawn

Every Agent tool spawn in this skill (planner, spec-completion-auditor, testing-executor) requires a stamped `active-segment-role` file. The subagent's `pre_tool_use.py` reads this file to resolve its write role; without it the subagent runs as `execute-inline` and `write_policy.decide_write` denies the writes the subagent is trying to make:

- `planning` is the only role that may write `discovery-notes.md`, `design-decisions.md`, `spec.md`, and `plan.md`. Without the stamp, the planner falls to `execute-inline` and **every spec/plan write is denied**.
- `audit-*` roles are the only roles that may write `audit-reports/`. Without the stamp, the spec-completion-auditor falls to `execute-inline` and its audit report write is denied.
- `testing-executor` is the only executor role permitted to write `evidence/tdd-tests.md` plus the test files in this stage; without it the spawn falls to `execute-inline` (works for repo files but not for the executor-scoped invariants downstream).

Direct writes to `active-segment-role` are denied by `write_policy.py` — the file is wrapper-required. Always go through ctl:

```bash
python3 "${PLUGIN_HOOKS}/ctl.py" stamp-role .dynos/task-{id} --role "{role}"
```

The wrapper enforces `_STAMP_ROLE_ALLOWLIST` (`hooks/ctl.py`), which gates the allowed values. Forgery defense for `audit-*` claims is enforced downstream by `receipt_audit_done`, which cross-checks against `spawn-log.jsonl` — stamping a role that no real Agent spawn matches produces an unforgeable audit-trail mismatch at receipt time.

The stamp file is overwritten by each new stamp, so successive phases do not need explicit cleanup between spawns. Step 9 cleans it up before handing off to `/dynos-work:execute`.

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

There is no direct-classify shortcut here. Always use the planner for Discovery + Design + Classification. Do NOT skip the planner spawn and do NOT infer classification directly from the task input in prompt logic.

**Learned Planning Skill Injection (skip when `learning_enabled=false`):** Before spawning the planner, check if a learned planning skill exists for this task type:

```bash
PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 -c "from pathlib import Path; from router import resolve_route; r = resolve_route(Path('.'), 'plan-skill', '{task_type}'); print(r['agent_path'] or '')" 
```

If a non-empty path is returned AND the file exists, read it, strip frontmatter, and append its contents to the planner's instruction below under a `## Learned Planning Rules` heading. This injects project-specific planning patterns (e.g., tighter acceptance criteria, better segment sizing) derived from past task retrospectives. Log: `{timestamp} [ROUTE] plan-skill route={mode} agent={agent_name}`.

**Stamp role BEFORE the spawn (MANDATORY):**

```bash
python3 "${PLUGIN_HOOKS}/ctl.py" stamp-role .dynos/task-{id} --role "planning"
```

Without this stamp the planner subagent resolves to `execute-inline` and its writes to `discovery-notes.md` and `design-decisions.md` are denied by `write_policy`.

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

Write the returned classification object to `/tmp/classification-{id}.json`, then run `python3 hooks/ctl.py write-classification .dynos/task-{id} --from /tmp/classification-{id}.json`.

Deterministic validation before proceeding:
1. The wrapper enforces `classification.type`.
2. The wrapper enforces `classification.risk_level`.
3. The wrapper enforces `classification.domains`.
4. If `write-classification` fails, stop and correct the payload before moving on.

Transition the stage by running:
Finalize classification through the deterministic control-plane entrypoint:

```text
python3 hooks/ctl.py run-start-classification .dynos/task-{id}
```

`run-start-classification` validates the classification payload, applies fast-track + `tdd_required`, and advances the manifest to `SPEC_NORMALIZATION` when the task is ready to continue. If it exits non-zero, the JSON payload names the exact classification defects.

**If the output contains `"tdd_required": true`:** Step 8 (TDD-First Gate) is **mandatory** for this task. Do not override this with your own risk assessment — `tdd_required` is set deterministically by the system for `high` and `critical` risk tasks. The state machine will block `PLAN_AUDIT → PRE_EXECUTION_SNAPSHOT` if Step 8 is skipped. Note this now and plan accordingly.

---

## Step 2b — Fast-Track Gate (conditional)

Fast-track is determined by `run-start-classification`. Do not recompute it in prompt logic or by hand.

When fast-tracked (`fast_track: true`), apply these simplifications throughout the remaining steps:
- **Spec (Step 3):** The planner should produce a concise spec. The `Implicit Requirements Surfaced` and `Risk Notes` sections can contain a single line each if no significant risks exist.
- **Planning (Step 5):** The execution graph should contain a **single segment** (no multi-segment decomposition). A single executor handles the entire change.
- **Audit (handled by audit skill):** When `fast_track: true` in the manifest, spawn only `spec-completion-auditor` and `security-auditor`. Skip all other auditors regardless of streak or domain.

If any condition is not met, proceed normally (no fast-track). Do not ask the user — this is a deterministic gate.

---

## Step 2c — External Solution Gate (always runs)

Before inventing a solution from scratch, run the deterministic gate:

```text
python3 hooks/ctl.py run-external-solution-gate .dynos/task-{id}
```

This command writes `.dynos/task-{id}/external-solution-gate.json` and prints the same decision payload to stdout. The gate owns the decision artifact. Do NOT hand-write or rewrite this JSON in prompt logic.

The artifact shape is:

```json
{
  "search_recommended": true,
  "search_used": false,
  "query_reason": "One sentence explaining the recommendation",
  "candidates": [],
  "recommended_choice": null,
  "decision_basis": {
    "task_type": "feature|bugfix|refactor|migration|ml|full-stack",
    "risk_level": "low|medium|high|critical",
    "domains": ["backend"],
    "trigger_matches": ["stripe"],
    "local_bug_matches": [],
    "file_scoped": false
  }
}
```

Rules:
- If `search_recommended` is `false`, proceed with local repo evidence only.
- If `search_recommended` is `true`, you MUST conduct external research before proceeding. Use `query_reason` and `decision_basis` to form the search query, then call:

  ```text
  python3 hooks/ctl.py write-search-receipt .dynos/task-{id} \
    --query "<your search query>" \
    --urls-consulted "<url1>,<url2>" \
    --findings-summary "<one-sentence summary of what the research found>"
  ```

  Both `--urls-consulted` and `--findings-summary` are required when `search_recommended` is `true`; they record the research evidence in the receipt so the audit chain can verify that real external research was performed.

  `run-spec-ready` (Step 3 exit) checks for this receipt and exits non-zero if it is missing. There is no rationalization that bypasses this — if `search_recommended` is `true` and the receipt is absent, the spec cannot advance to SPEC_REVIEW.
- The planner still owns the final design choice. Research findings inform the plan; they do not automatically authorize adopting any external library or pattern.
- Do not mutate the gate artifact by hand to claim search happened or to inject candidates.

Logging: append exactly one line to the execution log:

`{timestamp} [GATE] external-solution — recommended: {true|false}`

Proceed to Step 2d.

---

## Step 2d — Architectural Design Doc Gate (conditional)

**Only runs when `classification.risk_level` is `high` or `critical`.** Read `.dynos/task-{id}/classification.json`. If `risk_level` is `low` or `medium`, append `{timestamp} [SKIP] arch-design — risk_level={risk_level}` to the execution log and proceed to Step 3.

For high/critical tasks, spawn the planner with the **Architectural Design Doc** phase to produce `.dynos/task-{id}/design-doc.md` — a §1–§13 design document that walks the higher-level architectural questions before spec normalization. §10 Open Questions is the human handoff.

**Stamp role BEFORE the spawn (MANDATORY):** write the per-phase injected-prompt sidecar and capture the digest:

```bash
ARCH_DESIGN_DIGEST=$(printf '%s' "$ARCH_DESIGN_PROMPT" | python3 "${PLUGIN_HOOKS}/router.py" planner-inject-prompt --task-id {id} --phase arch-design)
```

Without this stamp the planner subagent resolves to `execute-inline` and its write to `design-doc.md` is denied by `write_policy`.

Spawn the planner subagent (`dynos-work:planning`) with instruction:

> Architectural Design Doc phase. Read `raw-input.md`, `discovery-notes.md`, `design-decisions.md`, and `classification.json`. Produce `.dynos/task-{id}/design-doc.md` following the §1–§13 structure defined in your agent prompt. Cite `file:line` for every claim about the current codebase. Surface unresolved decisions in §10 Open questions. Do NOT write to `docs/`. Do NOT advance any lifecycle stage.

After the spawn returns, write the receipt:

```bash
python3 hooks/ctl.py planner-receipt .dynos/task-{id} arch-design \
  --tokens-used {TOTAL_TOKENS} \
  --model {MODEL_USED} \
  --agent-name planning \
  --injected-prompt-sha256 "${ARCH_DESIGN_DIGEST}"
```

Read `.dynos/task-{id}/design-doc.md` and locate the `## 10. Open questions` section. If it contains only the literal `None — design fully resolved.`, no human input is needed; log `{timestamp} [GATE] arch-design — no open questions` and proceed to Step 3.

Otherwise, parse each open question and present it to the user via **AskUserQuestion**. Append the user's resolutions back to `design-doc.md` immediately under the `## 10. Open questions` section as a `### Resolutions` sub-section, one line per resolved question: `Q{n}: {chosen-option} — {one-line rationale}`.

Logging: append exactly one line to the execution log:
`{timestamp} [GATE] arch-design — open-questions: {n} (resolved)`

Proceed to Step 3.

---

---

# Phase 3: Specification

## Step 3 — Spec Normalization

**Fast-track combined spawn:** If `manifest.json` has `"fast_track": true`, skip the spawn and the spec validation. Spec is produced in Step 5 by the combined Spec + Plan planner spawn. **Do NOT advance the manifest stage here** — leave it at `SPEC_NORMALIZATION`. Walking the stage forward before `spec.md` exists breaks the artifact invariant in `hooks/lib_validate.py` (`_SPEC_REQUIRED_AFTER` requires `spec.md` once stage is `SPEC_REVIEW` or beyond), and any `/dynos-work:status` or `/dynos-work:resume` invocation in the window between Step 3 and Step 5 completing would observe `stage=PLANNING` with no spec on disk. The stage walk happens in Step 5 after `spec.md` is written. Log: `{timestamp} [SKIP] spec-normalization-spawn — fast_track combined planner (stage walk deferred to Step 5)`. Skip the rest of this step and proceed to Step 4.

**Normal path:** **Stamp role BEFORE the spawn (MANDATORY):**

```bash
python3 "${PLUGIN_HOOKS}/ctl.py" stamp-role .dynos/task-{id} --role "planning"
```

Without this stamp the planner falls to `execute-inline` and the `spec.md` write is denied by `write_policy.decide_write` (the spec.md guard at `hooks/write_policy.py:290-296` denies executor roles outright). This is the failure mode reported in the SPEC_NORMALIZATION block incident.

Spawn the Planner subagent with instruction:

```text
Phase: Spec Normalization.
Read raw-input.md, discovery-notes.md, and design-decisions.md.
Also read the actual implementation files referenced in the task (e.g., the files that will be modified). Verify runtime semantics directly from the code — do not assume template engines, escaping conventions, or generation mechanisms without reading the relevant functions. Include specific function signatures, data flow paths, and module boundaries in the spec.
Write a spec that leaves executors zero room to hand-wave. Name the exact behavior, exact boundaries, exact failure modes, and exact evidence needed to prove completion.
Write spec.md.
See docs/spec-writing-rules.md for known spec-writing anti-patterns.
```

If the spec still contains vague adjectives, missing states, or unstated boundary behavior after normalization, send it back again.

After `spec.md` is written, run deterministic spec validation:
1. The file must contain the headings `Task Summary`, `User Context`, `Acceptance Criteria`, `Implicit Requirements Surfaced`, `Out of Scope`, `Assumptions`, and `Risk Notes`.
2. Acceptance criteria must be a numbered list starting at `1` and incrementing by `1` with no gaps.
3. Every acceptance criterion must be concrete and independently testable.
4. Any assumption that affects behavior must be labeled `needs confirmation` or `safe assumption`.

If any rule fails, send the Planner back to fix `spec.md` before presenting it.

Finalize spec readiness through the deterministic control-plane entrypoint:

```text
python3 hooks/ctl.py run-spec-ready .dynos/task-{id}
```

`run-spec-ready` validates `spec.md`, writes the `spec-validated` receipt, and advances `SPEC_NORMALIZATION -> SPEC_REVIEW` when the artifact is sound. If it exits non-zero, the JSON payload tells you exactly why the spec must be regenerated.

---

## Step 4 — Spec Review

<!-- scheduler-owned: SPEC_REVIEW -> PLANNING -->

**Fast-track skip:** If `manifest.json` has `"fast_track": true`, skip this step. Spec is reviewed together with the plan in Step 6 (combined approval gate). Log: `{timestamp} [SKIP] spec-review — fast_track combined gate`.

**Auto-approve path (precedes the human path):** If `manifest.json` has `"auto_approve_gates": true`, do NOT present `spec.md` to the user. Run the auto-approved variant of the approve-stage ctl command instead. This is the only sanctioned bypass — it still hashes the live `spec.md`, writes a `human-approval-SPEC_REVIEW` receipt with `approver_type="residual-auto"`, and advances SPEC_REVIEW → PLANNING through the same atomic gate that the human path uses. The receipt is forensically distinguishable from a human approval (the `approver_type` field), so the audit chain remains intact.

```text
python3 hooks/ctl.py approve-stage .dynos/task-{id} SPEC_REVIEW --auto-approved
```

- Exit code 0: receipt was written and the stage advanced. Skip the rest of this step.
- Exit code 1: the gate refused — the most common cause is `auto_approve_gates is not true` (the manifest flag was flipped off mid-flight) or a hash mismatch. Log the stderr message and fall through to the human-approval path below. Do NOT retry with `--auto-approved` if the manifest flag is no longer true; the gate is correct to refuse.

In the auto path, the "if changes requested" and "if rejected" branches of the human path below are unreachable — the residual queue's classification has already filtered out tasks where human review is required. If you find yourself in those branches with `auto_approve_gates=true`, that is a bug in the pick-time ceilings, not a runtime decision to make here.

**Normal path:** Present `spec.md` to the user and ask for approval.

- If approved: run the `approve-stage` ctl command below. It hashes the current `spec.md`, writes the `human-approval-SPEC_REVIEW` receipt with that hash, the scheduler then observes the receipt write and advances the task to PLANNING asynchronously. Do NOT write a manual `[HUMAN]` log line — `approve-stage` is the only path that satisfies the receipt-gate in `transition_task` (which compares the receipt's `artifact_sha256` against the live `spec.md` at transition time and refuses with `human-approval-SPEC_REVIEW` / `hash mismatch` substrings on drift).
- If changes are requested: append the feedback, respawn the Planner in Spec Normalization mode, re-run deterministic spec validation, write a new `receipt_spec_validated`, and present the updated spec again. Do NOT call `approve-stage` until the user re-approves the regenerated spec.
- If rejected outright: run `python3 hooks/ctl.py transition .dynos/task-{id} FAILED`, append `[FAILED] Spec rejected by user`, and stop. Do not edit `manifest.json` directly.

When approved:

```text
python3 hooks/ctl.py approve-stage .dynos/task-{id} SPEC_REVIEW
```

Exit code 0 means the receipt was written and the scheduler queued the advance to PLANNING (verify via manifest.stage after the in-process event dispatch completes). Exit code 1 means the gate refused — the stderr text identifies the cause (missing artifact, hash drift, illegal transition). Do not retry without addressing the reported cause; in particular, do not call `python3 hooks/ctl.py transition ... --force` to bypass — that would advance the stage without a receipt and break the audit chain.

---

---

# Phase 4: Planning

## Step 5 — Generate Plan + Execution Graph

(`transition_task` auto-appends the `[STAGE] → PLANNING` log line; do not write it manually.)

Choose planning mode through ctl:

```bash
python3 "${PLUGIN_HOOKS}/ctl.py" run-planning-mode .dynos/task-{id}
```

Use the JSON output as authoritative:
- `planning_mode == "fast_track_combined"`: use the fast-track combined flow
- `planning_mode == "hierarchical"`: use hierarchical planning
- `planning_mode == "standard"`: use standard planning

Do NOT re-derive fast-track, risk-based escalation, or acceptance-criteria thresholds in prompt logic.

**Stamp role BEFORE every planner spawn in this step (MANDATORY — applies to all three flows below):**

```bash
python3 "${PLUGIN_HOOKS}/ctl.py" stamp-role .dynos/task-{id} --role "planning"
```

For hierarchical flow, stamp once before the Master Planner spawn AND again before each Worker Planner spawn (each spawn reads the file fresh at its first tool call — successive stamps with the same role are idempotent and overwrite cleanly). Without these stamps the planner falls to `execute-inline` and `plan.md` / `execution-graph.json` writes are denied by `write_policy`.

Hierarchical flow:
1. Spawn Master Planner using the default planning model for this repo.
2. Spawn Worker Planners in parallel for non-overlapping subsystems.
3. Merge outputs into final `plan.md` and an execution-graph payload, then persist the final graph ONLY via `python3 hooks/ctl.py write-execution-graph .dynos/task-{id} --from /tmp/execution-graph-{id}.json`.

Fast-track combined flow (when `fast_track: true`):
1. **Stage precondition:** the manifest is still at `SPEC_NORMALIZATION` (Step 3 deferred the walk). Do NOT advance yet.
2. Spawn Planner (Opus) ONCE with phase `Spec + Plan` to produce `spec.md`, `plan.md`, and an execution-graph payload in `/tmp/execution-graph-{id}.json`, then persist the final graph ONLY via `python3 hooks/ctl.py write-execution-graph .dynos/task-{id} --from /tmp/execution-graph-{id}.json`. This replaces both Step 3 (Spec Normalization) and Step 5's normal planner spawn.
3. After the spawn returns AND `validate_task_artifacts` passes (see below), walk the stage forward through `SPEC_NORMALIZATION → SPEC_REVIEW → PLANNING` (each transition is legal per `ALLOWED_STAGE_TRANSITIONS` in `hooks/lib_core.py`). Only advance once the artifacts that justify each stage exist on disk. Log each transition. Then continue with the post-validation flow below (which advances to `PLAN_REVIEW`).

Standard flow:
1. Spawn Planner (Opus) with instruction to generate `plan.md` and an execution-graph payload, then persist the final graph ONLY via `python3 hooks/ctl.py write-execution-graph .dynos/task-{id} --from /tmp/execution-graph-{id}.json`.

After generation, run deterministic artifact validation before any human review. If available in this repo, run:

```text
python3 hooks/validate_task_artifacts.py .dynos/task-{id} --no-gap
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

```bash
python3 hooks/ctl.py plan-validated-receipt .dynos/task-{id}
```

Append to the execution log (transition_task auto-appends the `[STAGE] → PLAN_REVIEW` line — only the `[DONE]` line is the skill's responsibility):

```text
{timestamp} [DONE] planning — final plan.md and execution-graph.json written (mode: {hierarchical|standard})
```

Transition the stage by running:

```text
python3 hooks/ctl.py transition .dynos/task-{id} PLAN_REVIEW
```

---

## Step 6 — Plan Review

This gate always runs. For fast-track tasks it acts as the combined Spec + Plan approval (since Step 4 was skipped) — present BOTH `spec.md` AND `plan.md` together.

**Auto-approve path (precedes the human path):** If `manifest.json` has `"auto_approve_gates": true`, do NOT present `plan.md` (or the combined `spec.md` + `plan.md`) to the user. Use the `--auto-approved` variant of approve-stage instead. Two cases:

- Normal path (Step 4 already wrote the SPEC_REVIEW receipt — auto or human):

  ```text
  python3 hooks/ctl.py approve-stage .dynos/task-{id} PLAN_REVIEW --auto-approved
  ```

- Fast-track combined gate (Step 4 was skipped; the manifest is at SPEC_REVIEW after Step 5's stage walk and needs both receipts in order):

  ```text
  python3 hooks/ctl.py approve-stage .dynos/task-{id} SPEC_REVIEW --auto-approved
  python3 hooks/ctl.py approve-stage .dynos/task-{id} PLAN_REVIEW --auto-approved
  ```

  The state machine requires the SPEC_REVIEW receipt before the PLAN_REVIEW receipt — this ordering is unchanged by the auto-approval feature. Both calls must return exit 0; if either returns exit 1, log the stderr message and fall through to the human-approval path for that specific gate.

Either auto path:

- Exit code 0 on every call: receipts written, stages advanced, skip the rest of this step.
- Exit code 1 on any call: the gate refused (most common: `auto_approve_gates is not true`, hash mismatch, or illegal transition). Log the stderr message and fall through to the human path below for the refused gate. Do not bypass with `transition --force`.

In the auto path, "if changes requested" and "if rejected outright" branches below are unreachable — see the same note in Step 4.

Present the artifact(s) to the user and ask for approval.

- If approved (normal path): run `python3 hooks/ctl.py approve-stage .dynos/task-{id} PLAN_REVIEW`. This hashes the current `plan.md`, writes the `human-approval-PLAN_REVIEW` receipt with that hash, and atomically advances PLAN_REVIEW → PLAN_AUDIT. Exit code 0 means success; exit code 1 means the gate refused (stderr identifies the cause). Do not bypass with `transition --force`.
- If approved (fast-track combined gate): the manifest is currently at SPEC_REVIEW (Step 5 walked it through `SPEC_NORMALIZATION → SPEC_REVIEW → PLANNING → PLAN_REVIEW`). Run `approve-stage` twice in order — first for the spec, then for the plan:

  ```text
  python3 hooks/ctl.py approve-stage .dynos/task-{id} SPEC_REVIEW
  python3 hooks/ctl.py approve-stage .dynos/task-{id} PLAN_REVIEW
  ```

  Each call hashes the live artifact, writes the matching receipt, and advances one stage. Both must succeed; if either returns exit 1, address the reported cause before retrying.
- If changes are requested: append the feedback, respawn planning (combined Spec + Plan phase for fast-track, otherwise standard planning), re-run deterministic artifact validation, and present the updated artifact(s) again. Do NOT call `approve-stage` until the user re-approves the regenerated artifact(s) — the gate compares the receipt hash to the live file at transition time, so an approval against an out-of-date hash will be refused with `hash mismatch`.
- If rejected outright: run `python3 hooks/ctl.py transition .dynos/task-{id} FAILED`, append `[FAILED] Plan rejected by user`, and stop. Do not edit `manifest.json` directly.

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

   **Stamp role BEFORE the spawn (MANDATORY — only when the conditional fires):**

   ```bash
   python3 "${PLUGIN_HOOKS}/ctl.py" stamp-role .dynos/task-{id} --role "audit-spec-completion"
   ```

   Without this stamp the auditor falls to `execute-inline` and its `audit-reports/spec-completion.json` write is denied by `write_policy.decide_write` (which restricts `audit-reports/` to `audit-*` roles). Forgery defense: `receipt_audit_done` cross-checks the orchestrator-claimed spawn against `spawn-log.jsonl`, so stamping without a real Agent spawn produces an unforgeable mismatch at receipt time.

   Additionally, surface any segment where `len(segment.files_expected) >= 10` (computed budget ≥ 35, the `TOOL_BUDGET_ADVISORY` threshold from `hooks/lib_tool_budget.py`) as a non-blocking advisory finding `near-budget-ceiling`. The advisory is informational and does NOT block plan approval; it warns the operator that the segment is approaching the 11-file overflow ceiling and may want decomposition.

3. If gap analysis finds gaps, or (when invoked) the auditor finds gaps, route back to planning, repair, and rerun deterministic artifact validation.
4. Create a git branch safety net: `dynos/task-{id}-snapshot`.

---

## Step 8 — TDD-First Gate

This gate is **mandatory** when `manifest.classification.tdd_required` is `true` (auto-derived by the system for `high` and `critical` risk tasks; also set for explicit opt-in). **Do not skip this step based on your own risk judgment.** The `run-start-classification` output surfaces `tdd_required`; if it is `true`, this step is required and the state machine will block `PLAN_AUDIT → PRE_EXECUTION_SNAPSHOT` without it.

When `tdd_required` is `false`: tests are written by `testing-executor` after production code, in the execute skill (Step 4 of execute), where the implementation context is already known. This avoids ~1.5–2M tokens of pre-code context loading per task.

When `tdd_required` is `true`:

1. **Stamp role BEFORE the spawn (MANDATORY):**

   ```bash
   python3 "${PLUGIN_HOOKS}/ctl.py" stamp-role .dynos/task-{id} --role "testing-executor"
   ```

   Without this stamp the testing-executor falls to `execute-inline`. Repo-file writes still work because `write_policy` permits both `execute-inline` and `*-executor` for repo artifacts, but the role file is what every other dynos-work skill in this codebase stamps before an executor spawn — keeping the convention here ensures the spawn-log entry's claimed role matches the runtime role and that downstream receipts identify the spawn correctly.

   Spawn `testing-executor` with instruction:

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
5. When validation passes (and before asking for human approval), write the TDD receipt:

   ```python
   from pathlib import Path
   from lib_receipts import receipt_tdd_tests, hash_file

   evidence_path = Path(".dynos/task-{id}/evidence/tdd-tests.md")
   receipt_tdd_tests(
       task_dir=Path(".dynos/task-{id}"),
       test_file_paths=[...],                       # list of test file paths from this run
       tests_evidence_sha256=hash_file(evidence_path),
       tokens_used=TOTAL_TOKENS,                    # from testing-executor spawn usage
       model_used="opus",                           # or whichever model was used
   )
   ```

6. **Auto-approve path (precedes the human path):** If `manifest.json` has `"auto_approve_gates": true` AND `tdd_required: true` (which is the only condition under which Step 8 fires at all), do NOT present the test suite to the user. After the testing-executor spawn and the deterministic validation in step 2 above have both passed, run the auto-approved variant of approve-stage:

   ```text
   python3 hooks/ctl.py approve-stage .dynos/task-{id} TDD_REVIEW --auto-approved
   ```

   - Exit code 0: receipt written, stage advanced TDD_REVIEW → PRE_EXECUTION_SNAPSHOT. Proceed to step 7 (commit tests).
   - Exit code 1: the gate refused. Log the stderr message and fall through to the human-approval path (step 7 below) for this gate. Do not bypass with `transition --force`.

   This conditional does NOT change the `tdd_required == false` behavior. When `tdd_required` is `false`, Step 8 does not fire at all (existing condition unchanged); the auto-approval flag has no effect on a step that is not executed.

7. When the user approves the test suite (or after step 6's auto-approval has run), transition out of TDD_REVIEW via the `approve-stage` ctl command. This hashes `evidence/tdd-tests.md`, writes the `human-approval-TDD_REVIEW` receipt with that hash, and advances TDD_REVIEW → PRE_EXECUTION_SNAPSHOT in one atomic step. Do NOT append a manual `[HUMAN]` log line — the receipt + approve-stage path is the only one the state machine accepts (the gate refuses with `human-approval-TDD_REVIEW` / `hash mismatch` substrings on drift):

   ```text
   python3 hooks/ctl.py approve-stage .dynos/task-{id} TDD_REVIEW
   ```

   Exit code 0 means the receipt was written and the stage advanced. Exit code 1 means the gate refused — the stderr text identifies the cause. Do not bypass with `transition --force`. (If step 6's auto-approval already advanced the stage, this human-path call is unreachable.)

8. Commit the approved tests to the snapshot branch before any production code is written. **The commit message MUST start with `tdd:`** (e.g. `tdd: PRO-XYZ test suite (RED)`). This is a load-bearing convention, not just a style hint: `ctl record-snapshot` in the execute skill detects HEAD's commit message and rewinds the recorded snapshot SHA to `HEAD^` when the message starts with `tdd:`. Without that rewind, the TDD-committed test files end up AT the snapshot SHA, never appear in `git diff <snapshot>`, and break `run-execution-segment-done`'s coverage check for the test segment. Other commit-message prefixes (`feat:`, `fix:`, `refactor:`, etc.) suppress the rewind.

---

---

# Phase 6: Handoff

## Step 9 — Done

**Role file cleanup (MANDATORY — BEFORE the handoff transition):** Delete `.dynos/task-{id}/active-segment-role` so `/dynos-work:execute` starts each segment with a clean slate. Deletion is permitted by `write_policy` (only the *write* path is wrapper-required); leaving the file in place is benign because every executor stamp overwrites it, but cleaning up keeps the spawn-log → role file relationship one-to-one for the next phase.

```bash
rm -f .dynos/task-{id}/active-segment-role
```

Transition the stage by running:

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
