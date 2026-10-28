---
name: start
description: "Start a new dynos-work task: handles discovery, spec, plan, execution, and audit. Runs only when the user explicitly types /dynos-work:start; never auto-triggered from conversation or by task-shaped requests."
---

# dynos-work: Unified Foundry Start

You are the entry point for dynos-work. You own all human-in-the-loop gates before execution. When done, the task is ready for `/dynos-work:execute`.

There is one pipeline for all tasks. There are no shortcuts. Historical memory may inform discovery and design review, but it is advisory only. Human approval and deterministic artifact checks decide readiness.

## Command Funnel (applies to every command in this skill)

Every deterministic step below runs through the plugin CLI. Resolve it once at the start of the skill and substitute the ABSOLUTE path literally into each command you run (permission prefix-matching operates on literal command text):

```bash
PLUGIN_ROOT="${CODEX_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}"
if [ -z "$PLUGIN_ROOT" ]; then
  # Some sessions leave both unset. The plugin root is two levels above this
  # skill's base directory (printed when the skill loads); substitute it here.
  PLUGIN_ROOT="$(cd "{skill_base_dir}/../.." && pwd)"
fi
if [ ! -x "$PLUGIN_ROOT/bin/dynos" ]; then
  echo "Cannot locate the dynos-work plugin root; set CODEX_PLUGIN_ROOT or CLAUDE_PLUGIN_ROOT." >&2
  exit 2
fi
DYNOS="${PLUGIN_ROOT}/bin/dynos"   # resolve once; use the absolute path in every command
```

`"$DYNOS" ctl <subcommand>` wraps `hooks/ctl.py`; `"$DYNOS" hook <script> ...` wraps helper scripts (router, lib_tokens, build_prompt_context, ...) with PYTHONPATH handled internally. A permissions-ON user can allow the single `<plugin-root>/bin/dynos` prefix once instead of approving every call.

Token usage is recorded automatically by the SubagentStop hook; do not record it by hand.
Immediately after each planner spawn, make exactly one call to `"$DYNOS" ctl planner-receipt .dynos/task-{id} <phase>`; omit token counts, model labels, and prompt digests. Use `discovery`, `arch-design`, `spec`, or `plan`; a combined Spec + Plan spawn uses `plan` so the plan-stage gate remains satisfied.

### On validation failure

For a deterministic validation failure, run `ctl planner-retry <task_dir> --phase <discovery|spec|plan|combined|arch-design>`. If `allowed` is true, set planner phase to `repair`:

```text
"$DYNOS" ctl set-planner-phase .dynos/task-{id} repair
```

Spawn the planner in **Repair** phase with the structured `--json` errors, note whether this was a fresh spawn or session resume in the log, write one planner receipt for the original phase, then rerun the same validator. The repair edits only the named artifact and error location. If `allowed` is false, present the remaining errors with **provide guidance** (one final Repair pass seeded with that guidance), **accept with justification** (run `ctl validation-override-receipt <task_dir> --artifact <name> --justification "<reason>"`, only if policy allows it), and **fail task**. Human requests for changes are distinct from validator retries; judgment feedback gets at most one Repair pass before the artifact is shown again.

JSON payloads (classification, execution-graph, repair-log) are piped to the ctl wrapper over stdin with `--from -` and a heredoc — NEVER staged at `/tmp` or any raw filesystem path (the write policy denies those, by design). Temp files, when genuinely needed, belong in `.dynos/task-{id}/_scratch/`.

## Scope Standard

- Do not let ambiguity leak downstream; resolve it or name it.
- Vague criteria and unbounded scope do not survive a phase gate — but neither does bloat. The validator enforces ceilings of 20 acceptance criteria, 2,500 spec words, and 4,000 plan words. A task that needs more is two tasks.
- Missing validation, error handling, auth, loading, empty, retry, and rollback behaviour belongs inside the criteria it affects, not as one extra criterion per state.
- For judgment feedback, allow at most one Repair pass before showing the artifact again. Each post-approval amendment is counted; the default window is one per artifact.
- Prefer a blocked gate over a rotten artifact, and a short artifact a human can actually read over a long one they rubber-stamp.

---

---

# Phase 1: Intake

## Step 0 — Metadata & Initialization

1. Before initializing, inspect the user input for:
   - A file path ending in `.prd.md`, `.pdf`, or `.txt` — treat it as a primary spec source (`--input-type prd`).
   - A URL to a Figma link, screenshot, or wireframe — note it as a design artifact (`--input-type wireframe`).
   - An attached screenshot or image — note it as a visual spec artifact (`--input-type mixed`).
2. Initialize the task through the single deterministic entrypoint. It creates `.dynos/`, registers the project + ensures the daemon (silent, idempotent), generates the `task-YYYYMMDD-NNN` id, and writes `raw-input.md`, the initial `manifest.json`, and `execution-log.md` — all under ctl ownership. Pipe the FULL task description exactly as given via stdin:

```bash
"$DYNOS" ctl run-start-init --root . --input-type {text|prd|wireframe|mixed} --description - <<'TASK_INPUT'
{full task description exactly as provided by the user}
TASK_INPUT
```

3. Read the printed JSON for `task_id` and `task_dir`; use them in every later step. Verify with:

```text
"$DYNOS" ctl validate-task .dynos/task-{id}
```
4. Print: `dynos-work: Foundry Task Initialized: {task_id}`

---

---

---

## Step 1 — Assemble Discovery Context

Read `raw-input.md`, the files it names, and optional trajectory memory. Do not ask the user questions in this step. The planner's questions and any hard/critical design choices are presented together in Step 2.

---

## Step 2 — Discovery + Design + Classification

There is no direct-classify shortcut here. Always use the planner for Discovery + Design + Classification. Do NOT skip the planner spawn and do NOT infer classification directly from the task input in prompt logic.

**Learned Planning Skill Injection (skip when `learning_enabled=false`):** Before spawning the planner, check if a learned planning skill exists for this task type:

```bash
"$DYNOS" hook router resolve plan-skill {task_type} --root .
```

Read `agent_path` from the printed JSON.

If a non-empty path is returned AND the file exists, read it, strip frontmatter, and append its contents to the planner's instruction below under a `## Learned Planning Rules` heading. This injects project-specific planning patterns (e.g., tighter acceptance criteria, better segment sizing) derived from past task retrospectives. Log: `{timestamp} [ROUTE] plan-skill route={mode} agent={agent_name}`.

Before the spawn, set the read-policy phase: `"$DYNOS" ctl set-planner-phase .dynos/task-{id} discovery`.

Spawn the Planner subagent (`dynos-work:planning`) with instruction:

```prompt
Phase: Discovery + Design + Classification (combined).
Read raw-input.md and discovery-notes.md if present. Also read any attached trajectory context as advisory prior history only.

Be ruthless. Surface ambiguity, hidden requirements, failure modes, and soft assumptions. Do not produce generic questions or generic design options.

Perform three things in one pass:
1. Discovery: return at most 4 unresolved questions. Format each as `Qn: <question> — changes: implementation|risk|acceptance — default: <recommended answer>`. Pre-fill a default for every question.
2. Design Options: for hard/critical subtasks only, give options and one recommended option. Resolve other choices directly.
3. Assumptions: list decisions you resolved, each tagged `safe assumption` or `needs confirmation`.
4. Classification: return type, domains, risk_level, and notes as JSON.

Return four sections named `Questions`, `Design Options`, `Assumptions`, and `Classification (JSON)`.
```

Validate the response against the required format. Present up to 4 highest-impact questions and design choices together in one `AskUserQuestion` call. If there are more than 4 total, append the remainder to `discovery-notes.md` under `## Deferred (needs confirmation)` and apply each supplied default. Write `discovery-notes.md` once with Q&A, assumptions, and deferred items; write `design-decisions.md` once with the selected options and rationale.

If no high-risk design options were returned, write `design-decisions.md` with the autonomous design choices and rationale.

Persist the returned classification object by piping it to the ctl wrapper over stdin (never stage it at a raw path):

```bash
"$DYNOS" ctl write-classification .dynos/task-{id} --from - <<'JSON'
{the planner's returned classification object, verbatim}
JSON
```

Deterministic validation before proceeding:
1. The wrapper enforces `classification.type`.
2. The wrapper enforces `classification.risk_level`.
3. The wrapper enforces `classification.domains`.
4. If `write-classification` fails, stop and correct the payload before moving on.

Transition the stage by running:
Finalize classification through the deterministic control-plane entrypoint:

```text
"$DYNOS" ctl run-start-classification .dynos/task-{id}
```

`run-start-classification` validates the classification payload, applies fast-track, `tdd_required`, `planning_path`, and `arch_design_required`, and advances the manifest to `SPEC_NORMALIZATION` when the task is ready to continue. If it exits non-zero, the JSON payload names the exact classification defects. If `risk_floor_applied` is true, print `planner_risk_level` and `risk_floor_signals` once so the user can see why risk was raised. Print `planning_path`, `tdd_required`, and `arch_design_required` once so the user can see which gates this task will run.

**If the output contains `"tdd_required": true`:** Step 8 (TDD-First Gate) is **mandatory** for this task. The flag is opt-in — it is true only when the user asked for tests before code or the project policy (`tdd_default_risk_levels`) lists this risk level — so do not set it from your own risk assessment. The state machine will block `PLAN_AUDIT → PRE_EXECUTION_SNAPSHOT` if Step 8 is skipped while the flag is true. When it is `false`, Step 8 does not run and `testing-executor` writes tests during execute.

---

## Step 2b — Planning Path (deterministic)

Read `planning_path` and `fast_track` from `run-start-classification`. `planning_path` chooses combined or staged planning; `fast_track` continues to control audit reduction only. Do not recompute either value. Combined is the default; staged applies only to migrations, critical-risk tasks, and high-risk tasks touching `db` or `security`. The `testing` and `docs` domains never influence the choice.

When `planning_path == "combined"`, combine spec and plan generation and review in Steps 5–6. When `fast_track: true`, apply the concise-spec guidance and audit reduction below:
- **Spec (Step 3):** The planner should produce a concise spec. The `Implicit Requirements Surfaced` and `Risk Notes` sections can contain a single line each if no significant risks exist.
- **Planning (Step 5):** The execution graph should contain a **single segment** (no multi-segment decomposition). A single executor handles the entire change.
- **Audit (handled by audit skill):** When `fast_track: true` in the manifest, spawn only `spec-completion-auditor` and `security-auditor`. Skip all other auditors regardless of streak or domain.

If any condition is not met, proceed normally (no fast-track). Do not ask the user — this is a deterministic gate.

---

## Step 2c — External Solution Gate (always runs)

Before inventing a solution from scratch, run the deterministic gate:

```text
"$DYNOS" ctl run-external-solution-gate .dynos/task-{id}
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
  "$DYNOS" ctl write-search-receipt .dynos/task-{id} \
    --query "<your search query>" \
    --urls-consulted "<url1>,<url2>" \
    --findings-summary "<one-sentence summary of what the research found>"
  ```

  Both `--urls-consulted` and `--findings-summary` are required when `search_recommended` is `true`; they record the research evidence in the receipt so the audit chain can verify that real external research was performed.

  `run-spec-ready` (Step 3 exit) checks for this receipt and exits non-zero if it is missing. There is no rationalization that bypasses this — if `search_recommended` is `true` and the receipt is absent, the spec cannot advance to SPEC_REVIEW.
- The planner still owns the final design choice. Research findings inform the plan; they do not automatically authorize adopting any external library or pattern.
- Do not mutate the gate artifact by hand to claim search happened or to inject candidates.

Logging: `run-external-solution-gate` appends its own `[GATE] external-solution — recommended: {true|false}` line; do not write one by hand.

Proceed to Step 2d.

---

## Step 2d — Architectural Design Doc Gate (conditional)

**Only runs when `arch_design_required` is true** in the `run-start-classification` output (also persisted on `manifest.json`). The default is critical risk only; projects widen it through `policy.arch_design_min_risk` (`high` or `always`). For every other task, record the skip and proceed to Step 3:

```bash
"$DYNOS" ctl log-append .dynos/task-{id} --tag SKIP --message "arch-design — arch_design_required=false risk_level={risk_level}"
```

All execution-log lines in this skill go through `log-append`. Never append to `execution-log.md` directly: once a role is stamped for a subagent, the orchestrator resolves as that role and the direct write is denied.

When `arch_design_required` is true, spawn the planner with the **Architectural Design Doc** phase to produce `.dynos/task-{id}/design-doc.md` — a §1–§13 design document that walks the higher-level architectural questions before spec normalization. §10 Open Questions is the human handoff.


Spawn the planner subagent (`dynos-work:planning`) with instruction:

> Architectural Design Doc phase. Read `raw-input.md`, `discovery-notes.md`, `design-decisions.md`, and `classification.json`. Produce `.dynos/task-{id}/design-doc.md` following the §1–§13 structure defined in your agent prompt. Cite `file:line` for every claim about the current codebase. Surface unresolved decisions in §10 Open questions. Do NOT write to `docs/`. Do NOT advance any lifecycle stage.

After the spawn returns, write the receipt:

```bash
"$DYNOS" ctl planner-receipt .dynos/task-{id} arch-design
```

Read `.dynos/task-{id}/design-doc.md` and locate the `## 10. Open questions` section. If it contains only the literal `None — design fully resolved.`, no human input is needed. Otherwise, parse each open question and present it to the user via **AskUserQuestion**. Append the user's resolutions back to `design-doc.md` immediately under the `## 10. Open questions` section as a `### Resolutions` sub-section, one line per resolved question: `Q{n}: {chosen-option} — {one-line rationale}`.

Logging (one line either way; message is `no open questions` or `open-questions: {n} (resolved)`):

```bash
"$DYNOS" ctl log-append .dynos/task-{id} --tag GATE --message "arch-design — {no open questions | open-questions: {n} (resolved)}"
```

Proceed to Step 3.

---

---

# Phase 3: Specification

## Step 3 — Spec Normalization

**Combined path:** If `planning_path == "combined"`, skip the spec-only planner spawn and validation here. Step 5 creates both artifacts and performs the stage walk after checking that `spec.md` exists. Do NOT advance the manifest stage here.

**Normal path:** Set the phase with `"$DYNOS" ctl set-planner-phase .dynos/task-{id} spec`, then spawn the Planner subagent with instruction:

```text
Phase: Spec Normalization.
Read raw-input.md, discovery-notes.md, and design-decisions.md.
Also read the actual implementation files referenced in the task (e.g., the files that will be modified). Verify runtime semantics directly from the code — do not assume template engines, escaping conventions, or generation mechanisms without reading the relevant functions. Include specific function signatures, data flow paths, and module boundaries in the spec.
Name the exact behavior, boundaries, failure modes, and the evidence that proves each criterion. Write the minimum falsifiable set: the validator rejects more than 20 acceptance criteria or 2,500 words, and a human must be able to review it in one sitting. Fold loading/empty/error/retry behaviour into the criteria it affects instead of adding one criterion per state.
Write spec.md once, then edit sparingly. Do not run validators; the orchestrator validates after you return.
See docs/spec-writing-rules.md for known spec-writing anti-patterns.
```

If deterministic validation fails, follow **On validation failure** with phase `spec`. Judgment feedback gets at most one Repair pass.

After `spec.md` is written, run deterministic spec validation:
1. The file must contain the headings `Task Summary`, `User Context`, `Acceptance Criteria`, `Implicit Requirements Surfaced`, `Out of Scope`, `Assumptions`, and `Risk Notes`.
2. Acceptance criteria must be a numbered list starting at `1` and incrementing by `1` with no gaps.
3. Every acceptance criterion must be concrete and independently testable.
4. Any assumption that affects behavior must be labeled `needs confirmation` or `safe assumption`.

If any rule fails, follow **On validation failure** with phase `spec` and the structured errors.

Finalize spec readiness through the deterministic control-plane entrypoint:

```text
"$DYNOS" ctl run-spec-ready .dynos/task-{id}
```

`run-spec-ready` validates `spec.md`, writes the `spec-validated` receipt, and advances `SPEC_NORMALIZATION -> SPEC_REVIEW` when the artifact is sound. If it exits non-zero, the JSON payload tells you exactly why the spec must be regenerated.

---

## Step 4 — Spec Review

<!-- scheduler-owned: SPEC_REVIEW -> PLANNING -->

**Combined path skip:** If `planning_path == "combined"`, skip this step and review both artifacts in Step 6. Log: `{timestamp} [SKIP] spec-review — combined planning path`.

**Auto-approve path (precedes the human path):** If `manifest.json` has `"auto_approve_gates": true`, do NOT present `spec.md` to the user. Run the auto-approved variant of the approve-stage ctl command instead. This is the only sanctioned bypass — it still hashes the live `spec.md`, writes a `human-approval-SPEC_REVIEW` receipt with `approver_type="residual-auto"`, and advances SPEC_REVIEW → PLANNING through the same atomic gate that the human path uses. The receipt is forensically distinguishable from a human approval (the `approver_type` field), so the audit chain remains intact.

```text
"$DYNOS" ctl approve-stage .dynos/task-{id} SPEC_REVIEW --auto-approved
```

- Exit code 0: receipt was written and the stage advanced. Skip the rest of this step.
- Exit code 1: the gate refused — the most common cause is `auto_approve_gates is not true` (the manifest flag was flipped off mid-flight) or a hash mismatch. Log the stderr message and fall through to the human-approval path below. Do NOT retry with `--auto-approved` if the manifest flag is no longer true; the gate is correct to refuse.

In the auto path, the "if changes requested" and "if rejected" branches of the human path below are unreachable — the residual queue's classification has already filtered out tasks where human review is required. If you find yourself in those branches with `auto_approve_gates=true`, that is a bug in the pick-time ceilings, not a runtime decision to make here.

**Normal path:** Present `spec.md` to the user and ask for approval.

- If approved: run the `approve-stage` ctl command below. It hashes the current `spec.md`, writes the `human-approval-SPEC_REVIEW` receipt with that hash, the scheduler then observes the receipt write and advances the task to PLANNING asynchronously. Do NOT write a manual `[HUMAN]` log line — `approve-stage` is the only path that satisfies the receipt-gate in `transition_task` (which compares the receipt's `artifact_sha256` against the live `spec.md` at transition time and refuses with `human-approval-SPEC_REVIEW` / `hash mismatch` substrings on drift).
- If changes are requested: append the feedback, use one Repair pass on `spec.md`, rerun deterministic validation, and present the updated spec. Do NOT call `approve-stage` until the user approves the live spec.
- If rejected outright: run `"$DYNOS" ctl transition .dynos/task-{id} FAILED`, append `[FAILED] Spec rejected by user`, and stop. Do not edit `manifest.json` directly.

When approved:

```text
"$DYNOS" ctl approve-stage .dynos/task-{id} SPEC_REVIEW
```

Exit code 0 means the receipt was written and the scheduler queued the advance to PLANNING (verify via manifest.stage after the in-process event dispatch completes). Exit code 1 means the gate refused — the stderr text identifies the cause (missing artifact, hash drift, illegal transition). Do not retry without addressing the reported cause; in particular, do not call `"$DYNOS" ctl transition ... --force` to bypass — that would advance the stage without a receipt and break the audit chain.

---

---

# Phase 4: Planning

## Step 5 — Generate Plan + Execution Graph

(`transition_task` auto-appends the `[STAGE] → PLANNING` log line; do not write it manually.)

Choose planning mode through ctl:

```bash
"$DYNOS" ctl run-planning-mode .dynos/task-{id}
```

Use the JSON output as authoritative:
- `planning_mode == "combined"`: use the combined flow (including when the fast-track audit flag is false)
- `planning_mode == "hierarchical"`: use hierarchical planning
- `planning_mode == "standard"`: use standard planning

Do NOT re-derive fast-track, risk-based escalation, or acceptance-criteria thresholds in prompt logic. Set the read-policy phase once before planning by running `"$DYNOS" ctl set-planner-phase .dynos/task-{id} combined` when `planning_path == combined`, otherwise use `plan`.

Hierarchical flow:
1. Spawn Master Planner using the default planning model for this repo.
2. Spawn Worker Planners in parallel for non-overlapping subsystems.
3. Merge outputs into final `plan.md` and an execution-graph payload, then persist the final graph ONLY via `"$DYNOS" ctl write-execution-graph .dynos/task-{id} --from -` (pipe the payload over stdin with a heredoc — never stage it at /tmp or any raw path).

Combined flow (when `planning_path == "combined"`):
1. **Stage precondition:** the manifest is still at `SPEC_NORMALIZATION` (Step 3 deferred the walk). Do NOT advance yet.
2. Spawn Planner ONCE with phase `Spec + Plan` to produce `spec.md`, `plan.md`, and an execution-graph payload returned in its final message; then persist the final graph ONLY via `"$DYNOS" ctl write-execution-graph .dynos/task-{id} --from -` (pipe the payload over stdin with a heredoc — never stage it at /tmp or any raw path). This replaces the separate spec and plan spawns. Tell the planner the ceilings (20 criteria, 2,500 spec words, 4,000 plan words) and that it writes each artifact once and does not run validators.
3. Run `validate_task_artifacts --no-gap`, then `"$DYNOS" ctl run-combined-stage-walk .dynos/task-{id}`. It verifies spec, plan, and graph exist before the stage walk, validates strictly, writes the plan-validated receipt, and advances to PLAN_REVIEW.

Standard flow:
1. Spawn Planner (Opus) with instruction to generate `plan.md` and an execution-graph payload, then persist the final graph ONLY via `"$DYNOS" ctl write-execution-graph .dynos/task-{id} --from -` (pipe the payload over stdin with a heredoc — never stage it at /tmp or any raw path).

After generation, run deterministic artifact validation before any human review. If available in this repo, run:

```text
"$DYNOS" hook validate_task_artifacts .dynos/task-{id} --no-gap --json
```

The command is the source of truth for artifact validation. Use the rules below to explain and repair failures:

For `plan.md`:
1. It must contain `Technical Approach`, `Reference Code`, `Components / Modules`, `Data Flow`, `Error Handling Strategy`, `Test Strategy`, `Dependency Graph`, and `Open Questions`.
2. When domains include backend, ui, or security: `API Contracts` section is required. When domains include db: `Data Model` section is required.
3. Every component or module section must list exact files.
4. `Reference Code` paths must exist in the repo unless explicitly marked as to-be-created.
5. **Gap analysis (deterministic):** if the plan contains `API Contracts` or `Data Model` sections, their claims are verified against the codebase. Endpoints listed in the API Contracts table must correspond to actual route definitions. Tables listed in the Data Model table must correspond to actual model/schema/migration definitions. Claimed-but-not-found entries are validation errors — the planner must either fix the table or mark new entries as to-be-created. For a new API Contracts or Data Model row, include `to-be-created` in any cell of that row; this explicitly exempts it from the existing-code check until its execution segment creates it.

For `execution-graph.json`:
1. It must parse as valid JSON.
2. Every segment must have unique `id`.
3. Every segment must declare exactly one valid executor.
4. No file may appear in more than one segment's `files_expected`.
5. Every `depends_on` reference must point to an existing segment.
6. The dependency graph must be acyclic.
7. Every `criteria_id` must map to a real acceptance criterion in `spec.md`.
8. Every acceptance criterion in `spec.md` must be covered by at least one segment.

If any validation fails, follow **On validation failure** with the matching phase and structured JSON errors.

**Receipt: plan-validated (MANDATORY).** On the combined path, `run-combined-stage-walk` writes this receipt. On the staged path, once `validate_task_artifacts` passes, write the receipt. Without this receipt the eventual transition to `EXECUTION` (in the execute skill) will be blocked by the state machine:

```bash
"$DYNOS" ctl plan-validated-receipt .dynos/task-{id}
```

Record completion (transition_task auto-appends the `[STAGE] → PLAN_REVIEW` line — only the `[DONE]` line is the skill's responsibility):

```bash
"$DYNOS" ctl log-append .dynos/task-{id} --tag DONE --message "planning — final plan.md and execution-graph.json written (mode: {combined|hierarchical|standard})"
```

Transition the stage by running:

```text
"$DYNOS" ctl transition .dynos/task-{id} PLAN_REVIEW
```

---

## Step 6 — Plan Review

This gate always runs. On the combined path, present BOTH `spec.md` AND `plan.md` together and use `approve-stage ... PLAN_REVIEW --combined` to write both approvals in one transition.

**Auto-approve path (precedes the human path):** If `manifest.json` has `"auto_approve_gates": true`, do NOT present `plan.md` (or the combined `spec.md` + `plan.md`) to the user. Use the `--auto-approved` variant of approve-stage instead. Two cases:

- Normal path (Step 4 already wrote the SPEC_REVIEW receipt — auto or human):

  ```text
  "$DYNOS" ctl approve-stage .dynos/task-{id} PLAN_REVIEW --auto-approved
  ```

- Combined gate:

  ```text
  "$DYNOS" ctl approve-stage .dynos/task-{id} PLAN_REVIEW --combined --auto-approved
  ```

  The combined command hashes both artifacts and writes both approval receipts atomically.

Either auto path:

- Exit code 0 on every call: receipts written, stages advanced, skip the rest of this step.
- Exit code 1 on any call: the gate refused (most common: `auto_approve_gates is not true`, hash mismatch, or illegal transition). Log the stderr message and fall through to the human path below for the refused gate. Do not bypass with `transition --force`.

In the auto path, "if changes requested" and "if rejected outright" branches below are unreachable — see the same note in Step 4.

Present the artifact(s) to the user and ask for approval.

- If approved (normal path): run `"$DYNOS" ctl approve-stage .dynos/task-{id} PLAN_REVIEW`. This hashes the current `plan.md`, writes the `human-approval-PLAN_REVIEW` receipt with that hash, and atomically advances PLAN_REVIEW → PLAN_AUDIT. Exit code 0 means success; exit code 1 means the gate refused (stderr identifies the cause). Do not bypass with `transition --force`.
- If approved (combined path): run `"$DYNOS" ctl approve-stage .dynos/task-{id} PLAN_REVIEW --combined`. This hashes both live artifacts, writes both approval receipts, and advances to PLAN_AUDIT.
- If changes are requested: append the feedback and use one Repair pass for the named artifact(s), then re-run deterministic validation and present the updated artifact(s). Do NOT call `approve-stage` until the user re-approves the updated artifact(s) — the gate compares the receipt hash to the live file at transition time.
- If rejected outright: run `"$DYNOS" ctl transition .dynos/task-{id} FAILED`, append `[FAILED] Plan rejected by user`, and stop. Do not edit `manifest.json` directly.

---

---

# Phase 5: Verification

## Step 7 — Plan Audit

The plan audit is deterministic at every risk level. `validate_task_artifacts` already proves criteria coverage; `run-plan-audit` adds gap analysis (API Contracts / Data Model claims against real code), the signature check, and the intermediate-state check, then writes the hash-bound `plan-audit-check` receipt itself. The LLM `spec-completion-auditor` pass (~300k tokens and ~13 minutes per run, observed running twice per task) is opt-in through `policy.plan_audit_llm`.

1. **Run the deterministic plan audit (mandatory, always runs, idempotent):**
   ```bash
   "$DYNOS" ctl run-plan-audit .dynos/task-{id}
   ```
   - `status == "passed"`: proceed. `receipt_cached: true` means a fresh receipt already covered these exact artifacts and nothing was redone. Do NOT spawn an auditor.
   - `status == "replan_required"`: a claimed endpoint or table does not exist. The planner must fix the table or mark the row `to-be-created` (any cell of an API Contracts or Data Model row). The human already approved this plan, so a repaired plan needs approving again: ctl has moved the task back to `PLANNING` (`transitioned_to`). Follow **On validation failure** with phase `plan`, then repeat the end of Step 5 (validation, `plan-validated-receipt`, transition to `PLAN_REVIEW`) and Step 6 for re-approval, and only then rerun this command. Never edit `plan.md` while the task is at `PLAN_AUDIT`: `run-execute-setup` refuses a plan changed after approval.
   - `status == "llm_audit_required"`: only when the project opted in. Spawn `spec-completion-auditor`, then finalize with `run-plan-audit --report-path ... --tokens-used ... --model ...`.

2. Surface any segment where `len(segment.files_expected) >= 10` (computed budget ≥ 35, the `TOOL_BUDGET_ADVISORY` threshold from `hooks/lib_tool_budget.py`) as a non-blocking advisory finding `near-budget-ceiling`. It warns the operator that the segment is approaching the 11-file overflow ceiling and may want decomposition.

3. Create a git branch safety net: `dynos/task-{id}-snapshot`.

---

## Step 8 — TDD-First Gate

This gate is **mandatory** when `manifest.classification.tdd_required` is `true`. The flag is opt-in: the planner sets it only when the user explicitly asked for tests before code, and the project policy may default it per risk level (`tdd_default_risk_levels`). **Do not set or skip it based on your own risk judgment.** The `run-start-classification` output surfaces `tdd_required`; if it is `true`, this step is required and the state machine will block `PLAN_AUDIT → PRE_EXECUTION_SNAPSHOT` without it.

When `tdd_required` is `false`: tests are written by `testing-executor` after production code, in the execute skill (Step 4 of execute), where the implementation context is already known. This avoids ~1.5–2M tokens of pre-code context loading per task.

When `tdd_required` is `true`:

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
5. When validation passes (and before asking for human approval), write the TDD receipt through ctl (the evidence hash is computed from the on-disk file — do not supply hashes yourself):

   ```bash
   "$DYNOS" ctl tdd-receipt .dynos/task-{id} \
     --test-file {path/to/test_one.py} --test-file {path/to/test_two.py} \
     --tokens-used {TOTAL_TOKENS} \
     --model {MODEL_USED}
   ```

6. **Auto-approve path (precedes the human path):** If `manifest.json` has `"auto_approve_gates": true` AND `tdd_required: true` (which is the only condition under which Step 8 fires at all), do NOT present the test suite to the user. After the testing-executor spawn and the deterministic validation in step 2 above have both passed, run the auto-approved variant of approve-stage:

   ```text
   "$DYNOS" ctl approve-stage .dynos/task-{id} TDD_REVIEW --auto-approved
   ```

   - Exit code 0: receipt written, stage advanced TDD_REVIEW → PRE_EXECUTION_SNAPSHOT. Proceed to step 7 (commit tests).
   - Exit code 1: the gate refused. Log the stderr message and fall through to the human-approval path (step 7 below) for this gate. Do not bypass with `transition --force`.

   This conditional does NOT change the `tdd_required == false` behavior. When `tdd_required` is `false`, Step 8 does not fire at all (existing condition unchanged); the auto-approval flag has no effect on a step that is not executed.

7. When the user approves the test suite (or after step 6's auto-approval has run), transition out of TDD_REVIEW via the `approve-stage` ctl command. This hashes `evidence/tdd-tests.md`, writes the `human-approval-TDD_REVIEW` receipt with that hash, and advances TDD_REVIEW → PRE_EXECUTION_SNAPSHOT in one atomic step. Do NOT append a manual `[HUMAN]` log line — the receipt + approve-stage path is the only one the state machine accepts (the gate refuses with `human-approval-TDD_REVIEW` / `hash mismatch` substrings on drift):

   ```text
   "$DYNOS" ctl approve-stage .dynos/task-{id} TDD_REVIEW
   ```

   Exit code 0 means the receipt was written and the stage advanced. Exit code 1 means the gate refused — the stderr text identifies the cause. Do not bypass with `transition --force`. (If step 6's auto-approval already advanced the stage, this human-path call is unreachable.)

8. Commit the approved tests to the snapshot branch before any production code is written. **The commit message MUST start with `tdd:`** (e.g. `tdd: PRO-XYZ test suite (RED)`). This is a load-bearing convention, not just a style hint: `ctl record-snapshot` in the execute skill detects HEAD's commit message and rewinds the recorded snapshot SHA to `HEAD^` when the message starts with `tdd:`. Without that rewind, the TDD-committed test files end up AT the snapshot SHA, never appear in `git diff <snapshot>`, and break `run-execution-segment-done`'s coverage check for the test segment. Other commit-message prefixes (`feat:`, `fix:`, `refactor:`, etc.) suppress the rewind.

---

---

# Phase 6: Handoff

## Step 9 — Done

**Role cleanup (MANDATORY — BEFORE the handoff transition):** Clear unconsumed role grants and the legacy role file so `/dynos-work:execute` starts each segment with a clean slate. Do NOT `rm` the role file by hand — `active-segment-role` is wrapper-required and write_policy denies the deletion; `clear-role` is the sanctioned (privilege-reducing) path:

```bash
"$DYNOS" ctl clear-role .dynos/task-{id}
```

Transition the stage by running:

```text
"$DYNOS" ctl transition .dynos/task-{id} PRE_EXECUTION_SNAPSHOT
```

(`transition_task` auto-appends the `[STAGE] → PRE_EXECUTION_SNAPSHOT` line; do not write an `[ADVANCE]` line by hand.)

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
