# plan.md — Pre-Execution Latency Remediation for dynos-work

This file is a work order for an executing agent. It is gitignored and never committed. Each numbered PR below is one branch and one pull request. Do not bundle PRs.

**Execution contract for unattended runs.** You are invoked once per PR. The prompt names the PR (`PR-NN`); implement only that one. Start from a fresh `origin/main` checkout unless the prompt names a base branch. Never ask the user a question: make the reasonable choice, record it in the PR body under "Decisions made without input", and continue. The run is complete when you have printed, in your final output: the `pytest` summary line with exit code 0, the branch name you pushed, and the URL of the pull request you opened with `gh pr create`. If a step is blocked (push denied, tool unavailable), print `BLOCKED: <reason>` and stop; do not work around a permission denial.

Repo baseline: `main` at version 7.5.19. Test suite: `python3 -m pytest -q` (3,221 collected: 3,202 passed, 19 skipped, ~100 s on `main`). Python 3, no external test deps beyond `pytest` (`pip install pytest` if missing).

---

## 0. Context you need before touching anything

### 0.1 The complaint

Users report that `/dynos-work:start` takes hours before `/dynos-work:execute` can run. Measured on eight saved task histories (June 2026): median 81 minutes from first planner event to `PRE_EXECUTION_SNAPSHOT`, five of eight over an hour, one planner spawn consumed about 7.3M input tokens. Hook and CLI process overhead is **not** the cause (0.06–0.33 s per invocation, measured). The time is model spawns, human approval waits, validation-failure respawns, and orchestrator round trips.

### 0.2 Root causes, each mapped to code

| ID | Problem | Where |
|---|---|---|
| P1 | Token ledger is wrong. SubagentStop hook resolves `hooks/` from the session cwd, so it only records when the project *is* the plugin repo. Unknown model strings are relabelled `opus`. Skills also mandate manual `lib_tokens record` calls that duplicate or replace the hook. Receipt-side recorder is a no-op shim. | `hooks/subagent-tokens:25`, `hooks/lib_tokens.py:192-216`, `hooks/receipts/core.py::_record_tokens`, `skills/start/SKILL.md` "Token & Event Capture" + "MANDATORY: Token Recording" sections |
| P2 | No planning-phase timing. `compute_pipeline_budget` is audit-only. Nothing separates model time from approval wait from idle. | `hooks/lib_validate.py:1109` |
| P3 | Per-spawn ceremony: 6 manual `stamp-role` calls, a prompt sidecar (2 extra calls per spawn) that is never compared with the prompt actually sent to the Agent tool, manual token records, hand-written log lines. Step 2d prose shows the inject command where it says "stamp role". | `skills/start/SKILL.md` Steps 2, 2d, 3, 5, 7, 8; `hooks/router.py:2406 cmd_planner_inject_prompt`; `hooks/receipts/planner.py:29 receipt_planner_spawn` |
| P4 | Discovery asks the user twice: Step 1 `AskUserQuestion` before the planner, Step 2 again from the planner's own list, design options a third time. | `skills/start/SKILL.md:205-296` |
| P5 | Risk cliff and dead fast path. `compute_fast_track` requires `risk_level == "low"` and exactly one domain. `_compute_risk_floor` forces `high` when domains include `db`, `security`, or `migration`, or when there are 3+ domains; forces `medium` when raw input or spec contains the bare words `auth` or `migration` (matches "author", "migrate the tests"). Planner prompt tells it a simple change "may be high" and to always add the `security` domain when auth is touched. One `high` turns on design doc + hierarchical planning + LLM plan audit + TDD gate + extra human round. | `hooks/lib_validate.py:566`, `hooks/ctl.py:567-572, 607-700`, `agents/planning.md` "Classification rules" |
| P6 | Three serial planner spawns (discovery, spec, plan) for every non-fast-track task. The combined spec+plan phase exists but is reachable only via fast-track. State machine demands a `planner-spec` receipt at `SPEC_NORMALIZATION → SPEC_REVIEW` unless `fast_track`. | `hooks/lib_core.py` ~1011-1030 and ~1696-1720 (two copies of the gate), `hooks/ctl.py:4107 cmd_run_planning_mode` |
| P7 | Hierarchical planning (master + N workers + merge) triggers on `criteria_count > 10`. The spec template makes loading/empty/error/disabled states implicit criteria, so UI tasks routinely cross it. | `hooks/ctl.py:4126` |
| P8 | Any deterministic validation failure → "respawn planning". No retry cap in the start phase (only audit repair cycles have one). Judgment rejections ("vague adjectives", "lazy output") are also uncapped. | `skills/start/SKILL.md` Steps 2, 3, 5, 6; `hooks/lib_validate.py:627 validate_task_artifacts` (~40 rules) |
| P9 | Planner context unbounded. Read budget is prose only; agent has Read/Grep/Glob/Bash. `maxTurns: 30` while the arch-design phase permits 30 file reads. 100 KB prompt guard logs and allows. | `agents/planning.md:1-40`, `hooks/router.py:185`, `hooks/read_policy.py:44` (non-audit roles fast-exit allow) |
| P10 | External-solution gate over-fires: triggers include `setup`, `configure`, `cache`, `retry`, `queue`, `metrics`, `integration`, `migration`, `pagination`, `tracing`, `cloud`; task type `full-stack` forces research. A hit makes a web search + receipt mandatory before the spec can advance. | `hooks/ctl.py:166-196, 231-260` |
| P11 | Gates accrete unmeasured. The retire-or-condition rule in `PIPELINES.md` covers only the audit phase. | `PIPELINES.md` "Retire-or-Condition Rule" |

### 0.2b Audit-phase defects (second bug report, verified on 7.5.19)

A high-risk task with 16 auditors (15 ensemble) and 95 changed files reached `run-audit-findings-gate` → `repair_required` with 18 blocking findings, then `run-repair-log-build` produced a repair-log that fails its own validator ("finding_id not found in live audit reports", "duplicate finding_id", "severity mismatch"). The repair loop cannot start and there is no sanctioned way forward.

| ID | Problem | Where |
|---|---|---|
| A1 | Gate, cycle plan, and repair-log builder read **every** report via `select_eligible_reports(task_dir, stage=None)` (permissive: all tiers, all statuses, no dedup). The repair-log **validator** reads via `_collect_audit_findings`, which adds a secondary dedup on `{file}:{line}:{category}`. Findings with no `file`/`line` in the same category collapse to one id, so an id the gate queued is "not found" at validation. | `hooks/ctl.py:4330, 4391, 4631, 4897`; `hooks/lib_validate.py:478-519` (dedup at 506-515), `:985-987` |
| A2 | Ensemble verdict is not consumed. `lib_ensemble.evaluate_cascade` returns `verdict: escalated` (deep tier binding) but nothing marks fast-tier reports superseded, so haiku-only blocking findings stay live forever and can never clear. | `hooks/lib_ensemble.py:160-215`; no consumer in `ctl.py` gate/plan paths |
| A3 | Finding ids are not namespaced per report or tier. Two tiers of one auditor can both emit `F-02` (→ "duplicate finding_id") or `F-01` with different severities (→ "severity mismatch"). | `hooks/lib_validate.py:981-982`, severity check near `:990+` |
| A4 | `findings_by_auditor[auditor_name] = {...}` is keyed by auditor name only; both tier reports share it, so the file that sorts last (haiku after fable) overwrites the binding counts. | `hooks/ctl.py:4348` |
| A5 | Report filename contract disagrees: `skills/audit/SKILL.md:202` says `{auditor}-checkpoint-{timestamp}.json`; the strict gate regex accepts only `{auditor}-{model}-attempt-{n}.json`. | `hooks/lib_validate.py:372-374`, `skills/audit/SKILL.md:133, 202` |
| A6 | No sanctioned way to append to `execution-log.md`. The orchestrator role may write it, but on Claude Code the orchestrator adopts the last stamped role (`_adoptable_stamped_role`), and `planning` / executor roles are denied. Skills still require `[GATE]` lines. PR-03 (auto-stamp) makes this strictly worse unless a ctl append command exists first. | `hooks/write_policy.py:193-213, 787-800`; `hooks/pre_tool_use.py:96, 655` |
| A7 | `pre_tool_use` checks Bash write destinations literally; `$T/_scratch/x` with `T=` set in the same command is denied as "path escapes task boundary". | `hooks/pre_tool_use.py:364-455` (`_resolve_path` does no expansion) |
| A8 | `plan_gap_analysis` has no to-be-created exemption for API endpoints **or** Data Model tables, although `plan.md` prose and the start skill tell the planner to mark new entries to-be-created. Greenfield repos get `replan_required` for every planned endpoint. | `hooks/plan_gap_analysis.py:309-431, 432+` (no `to-be-created` string anywhere in the file) |
| A9 | `claude-md-auditor` runs `python3 "${PLUGIN_HOOKS}/lib_claude_md.py"`; with the variable unset it executes `/lib_claude_md.py` and files a **blocking** `tool-error` finding instead of failing the spawn. | `agents/claude-md-auditor.md:60` |
| A10 | Ownership check runs `git diff --name-only` without `-c core.quotepath=false`; non-ASCII paths come back escaped and `files_expected` fails to match. | `hooks/ctl.py:2768, 4203, 4806` |
| A11 | `next-continuation` reported a segment with an evidence file but no executor receipt as complete. The incompleteness check does require a receipt; the likely leak is the `cached_segments` path in `_compute_execution_batch_payload` treating evidence presence as done. Verify before fixing. | `hooks/ctl.py:5437-5495` |
| A12 | After `amend-artifact plan`, `run-execute-setup` fails with "plan was never validated" and no pointer to `plan-validated-receipt`. | `hooks/lib_core.py:1075, 1797` |

### 0.3 Invariants you must not break

- **Stage transitions are owned by `hooks/ctl.py` / `lib_core.transition_task`.** Skills call ctl commands; never add stage writes to agents.
- **Receipt chain and hash binding stay.** `approve-stage` hashes the live artifact; `transition_task` compares. Do not add bypasses.
- **Spawn-log forgery defence stays.** `receipt_audit_done` cross-checks claimed auditor spawns against `spawn-log.jsonl`. PR-04 *strengthens* this pattern for planners; it does not weaken it.
- **`_SPEC_REQUIRED_AFTER` invariant:** the manifest stage must not pass `SPEC_NORMALIZATION` until `spec.md` exists on disk (`hooks/lib_validate.py` ~616). Any combined-path stage walk must check artifact existence first.
- **`fast_track` keeps its audit meaning.** The audit skill and `build_audit_plan` reduce auditors when `fast_track` is true. PR-07 introduces a separate `planning_path` field so widening the planning path does not weaken audit.
- **write_policy roles:** `planning` may write `discovery-notes.md`, `design-decisions.md`, `design-doc.md`, `spec.md`, `plan.md`. `orchestrator` may write `execution-log.md`, `discovery-notes.md`, `design-decisions.md`. Executors may not write `spec.md`/`plan.md`. Keep these.
- **Prose tests exist.** `tests/test_skill_prose_*.py`, `tests/test_skill_stage_references.py`, `tests/test_skill_start_planner_inject.py`, `tests/test_skill_md_step_2c.py`, `tests/test_execute_skill_inline_decision_table.py` pin skill text. When you change skill prose, update the pin in the same PR rather than weakening the assertion.
- **Templates mirror:** `cli/assets/templates/base/agents/*.md` and `cli/assets/templates/base/*.md` mirror `agents/` and skills. When you edit `agents/planning.md`, apply the same edit to `cli/assets/templates/base/agents/planning.md`. Check `tests/test_lib_templates.py` for what is asserted.

### 0.4 Release hygiene (from CLAUDE.md, mandatory per PR)

- Do **not** edit the version fields in `package.json`, `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `.codex-plugin/plugin.json`. CI bumps them and pushes a `chore: update release metadata` commit to your branch; rebase after it lands before pushing again.
- Write a concrete `CHANGELOG.md` entry under `## [Unreleased]` for every PR: what changed in behaviour and why. One entry per PR.
- Commit style: `type: short description` (`feat`, `fix`, `docs`, `chore`).
- Before opening each PR: `python3 -m pytest -q` must be green. Add tests for every behaviour change.
- Do not add a new mandatory start-phase step in any PR here. The point is removal.

### 0.5 Definitions

- **Combined path:** one planner spawn produces `spec.md`, `plan.md`, and the execution graph; one human review covers spec and plan.
- **Staged path:** today's flow: separate spec spawn, spec review, plan spawn, plan review.
- **Repair pass:** a planner spawn (or resume) given only the existing artifact(s) and a JSON error list, instructed to edit in place.

---

## 1. PR list (one PR each)

Landing order: **01, 02, 13, 14, 16, 15, 03, 05, 11, 06, 04, 09, 07, 08, 10, 17, 18, 19, 20, 21, 22, 12.**

PR-01…12 are the pre-execution latency fixes (§0.2). PR-13…22 are the audit-phase defects (§0.2b). 13 and 14 block real tasks today and land right after the measurement PRs.

Dependencies: 07 needs 06. 08 needs 05. 10 needs 03. **16 must land before 03** (auto-stamp removes the orchestrator's ability to append the log; 16 provides the ctl path). 14 needs 13. 12 needs 02 and should land last. Others are independent.

---

### PR-01 — fix: make token attribution correct and single-sourced

**Fixes P1.** Branch `fix/token-attribution`.

Changes:
1. `hooks/subagent-tokens`: replace `HOOKS_DIR="${CWD}/hooks"` with the script-relative resolution the other hooks use (`SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"; HOOKS_DIR="$SCRIPT_DIR"`). Keep `--root "$CWD"`.
2. `hooks/lib_tokens.py::_resolve_model`: return the given model string unchanged when it is non-empty and not `none`/`n/a`; return `"unknown"` for empty/null. Delete the fallback to `_DEFAULT_PARENT_MODEL` and the constant if nothing else uses it (grep first). Add `model_resolution: "exact" | "unknown" | "deterministic"` to each recorded event.
3. `hooks/lib_tokens_hook.py`: pass the model detected from the transcript verbatim; if none detected, pass `"unknown"`.
4. `hooks/receipts/core.py`: delete `_record_tokens` shim and every call site in `hooks/receipts/*.py`. Receipts keep `tokens_used` in their payload for backward compatibility but no longer write to `token-usage.json`.
5. `hooks/ctl.py` receipt subcommands (`planner-receipt`, `plan-audit-receipt`, `tdd-receipt`, and the execute/audit equivalents): make `--tokens-used` optional, default `0`. Update receipt writers so `tokens_used == 0` no longer emits `planner_spawn_zero_tokens`; that event is now meaningless. Remove it and its test if one exists.
6. Skill prose: delete the "MANDATORY: Token Recording After Every Agent Spawn" section, the "Token & Event Capture" section, and the "Specific events to record in this skill" list from `skills/start/SKILL.md`. Remove equivalent manual `lib_tokens record` instructions from `skills/execute/SKILL.md` and `skills/audit/SKILL.md`. Leave one sentence in each skill's Command Funnel: "Token usage is recorded automatically by the SubagentStop hook; do not record it by hand." Deterministic validators already self-record (`hooks/validate_task_artifacts.py:main`).
7. Any rollup/summary in `lib_tokens.py`, `hooks/report.py`, `telemetry/*` that infers model from role tier: remove the inference; group by the recorded `model` only.

Tests:
- New `tests/test_subagent_tokens_hook_cwd.py`: invoke `hooks/subagent-tokens` with a payload whose `cwd` is a temp project dir (not the plugin repo) and a fake transcript; assert an event lands in that project's active task `token-usage.json`.
- Update `tests/test_token_attribution.py`, `tests/test_orphan_token_attribution.py`, `tests/test_hostmodel_tokens.py`: `fable`, `sonnet`, `opus`, and an arbitrary string round-trip unchanged; empty → `unknown`.
- `tests/test_skill_prose_regressions.py`: assert `lib_tokens record` does not appear in the three skills.
- `tests/test_receipts_exports.py` / `tests/test_lib_receipts_shim.py`: update for removed shim.

Acceptance: from a non-plugin project directory, a `dynos-work:planning` spawn running as `fable` produces exactly one spawn event in `token-usage.json` with `model == "fable"`.

CHANGELOG: "Token attribution now comes from the SubagentStop hook only. The hook previously resolved its helper path from the session cwd and silently did nothing outside the plugin repo; unknown models were relabelled `opus`; skills mandated manual duplicate records. All three fixed."

---

### PR-02 — feat: planning-phase timeline KPI and `ctl timeline`

**Fixes P2.** Branch `feat/planning-timeline`. Independent of PR-01 but more useful after it.

Data sources already on disk per task: `spawn-log.jsonl` (`pre`/`post` entries with `timestamp`, `subagent_type`), `execution-log.md` (`[STAGE] → X` lines), `receipts/human-approval-*.json` and `receipts/auto-approval-*.json` (`written_at`), `manifest.json::created_at`.

Changes:
1. `hooks/lib_core.py::transition_task`: in addition to the prose log line, append `{"from": ..., "to": ..., "at": now_iso()}` to `.dynos/task-{id}/stage-transitions.jsonl` through the ctl write path. Add the filename to the control-plane/ctl-owned list in `write_policy.py` so agents cannot write it.
2. `hooks/lib_validate.py`: add `compute_planning_timeline(task_dir) -> dict` with keys:
   - `pre_execution_seconds` (`created_at` → first transition into `PRE_EXECUTION_SNAPSHOT`; `null` if not reached)
   - `planner_spawns`: `[{phase_guess, subagent_type, start, end, seconds}]` from paired pre/post spawn-log entries where `subagent_type` ends with `planning`, `spec-completion-auditor`, or `testing-executor` and timestamp precedes pre-execution
   - `model_seconds`: sum of spawn durations
   - `approval_waits`: for each of `SPEC_REVIEW`, `PLAN_REVIEW`, `TDD_REVIEW`: `requested_at` = transition into that stage, `granted_at` = matching approval receipt `written_at`; `seconds`
   - `approval_seconds`: sum
   - `idle_gaps`: intervals > 1800 s with no spawn, receipt, or transition; `idle_seconds` = sum
   - `orchestrator_seconds` = `pre_execution_seconds − model_seconds − approval_seconds − idle_seconds` (floor 0)
   - `planner_respawns`: planner spawn count minus distinct phases observed (≥ 0)
   - `human_rounds`: count of approval receipts + count of `[HUMAN]`/`AskUserQuestion` markers if logged (best effort; document as approximate)
   Degrade to zeros/nulls on missing files, same contract as `compute_pipeline_budget`.
3. Write the dict into `task-retrospective.json` as `planning_timeline` wherever `pipeline_budget` is written (grep `compute_pipeline_budget(` for the call site).
4. `hooks/ctl.py`: new subcommand `timeline <task_dir> [--json]`. Prints a fixed-width table of the buckets and the spawn list; `--json` prints the dict. Read-only.
5. `telemetry/dashboard.py` / `hooks/dashboard.py`: add per-task columns model / approval / orchestrator / idle seconds if the data layer makes that cheap; otherwise leave for follow-up and say so in the PR body.
6. `PIPELINES.md`: document `planning_timeline` next to `pipeline_budget`.

Tests:
- New `tests/test_planning_timeline.py`: build a temp task dir with synthetic spawn-log, receipts, transitions; assert each bucket to the second; idle-gap detection; respawn count; missing-file degradation.
- `tests/test_telemetry_tolerates_new_stages.py`, `tests/test_record_snapshot.py` still pass.
- `tests/test_write_policy*.py`: `stage-transitions.jsonl` denied to agent roles.

Acceptance: `"$PLUGIN_ROOT/bin/dynos" ctl timeline .dynos/task-X` prints model, approval, orchestrator, and idle seconds for any existing task directory.

---

### PR-03 — feat: stamp the active role automatically from the Agent spawn hook

**Fixes P3 (stamps).** Branch `feat/auto-role-stamp`.

Changes:
1. `hooks/lib_core.py`: add `SUBAGENT_ROLE_MAP` and `role_for_subagent_type(subagent_type: str) -> str | None`:
   - `dynos-work:planning` → `planning`
   - `dynos-work:<x>-executor` → `<x>-executor` (only if in `_EXECUTOR_ROLE_ALLOWLIST`)
   - `dynos-work:<x>-auditor` → `audit-<x>`
   - `dynos-work:repair-coordinator` → `repair-coordinator`; `dynos-work:investigator` → `investigator`
   - anything else → `None`
   Accept both the `dynos-work:` prefixed and bare forms.
2. `hooks/agent_spawn_log.py` pre phase (`_build_pre_entry` / `main`): after writing the spawn-log entry, resolve the task dir (reuse the pre_tool_use helpers or the same ancestor walk), map the role, and stamp `active-segment-role` using the same function `cmd_stamp_role` uses (factor the stamping body out of `hooks/ctl.py:2411` into `lib_core.stamp_role(task_dir, role)` so both call it). If the role maps to `None`, do not touch the file and emit `spawn_role_unmapped`. Record `stamped_role` in the spawn-log entry.
3. Keep `ctl stamp-role` and `ctl clear-role` as manual recovery; `_STAMP_ROLE_ALLOWLIST` unchanged.
4. `skills/start/SKILL.md`: remove the section "MANDATORY: Role Stamping Before Every Agent Spawn" and every "Stamp role BEFORE the spawn (MANDATORY)" block (Steps 2, 2d, 3, 5, 7, 8). Fix Step 2d where the "stamp role" paragraph currently shows the `planner-inject-prompt` command. Add one sentence to the Command Funnel: "The Agent spawn hook stamps `active-segment-role` from the agent type; you do not stamp roles by hand. `ctl clear-role` in Step 9 is unchanged."
5. `skills/execute/SKILL.md`, `skills/audit/SKILL.md`: remove per-spawn stamp instructions where the agent type fully determines the role. If execute relies on segment-specific state beyond the role name, keep that part and say why in the PR body.
6. Mirror prose changes into `cli/assets/templates/base/*.md` if those templates carry the same sections (check `tests/test_lib_templates.py`).

Tests:
- `tests/test_agent_spawn_log_hook.py`: for each agent type, a pre payload leaves the expected role in `active-segment-role`; unknown type leaves the file untouched and emits the event.
- `tests/test_active_segment_role_lock.py`, `tests/test_ctl_write_boundary.py`: still pass; stamping path is still `ctl`-owned.
- `tests/test_receipt_audit_spawn_log_crosscheck.py`: forgery check unchanged; add a case where a role is stamped by the hook but the receipt claims a different auditor → still refused.
- Prose tests: no `stamp-role` instruction precedes an Agent spawn in the three skills.

Acceptance: with no manual stamp, a `dynos-work:planning` spawn writes `spec.md` and `write_policy` allows it.

---

### PR-04 — refactor: bind planner receipts to the spawn log instead of a prompt sidecar

**Fixes P3 (sidecar).** Branch `refactor/planner-receipt-spawn-log`. Land after PR-03.

Rationale: `planner-inject-prompt` writes a sidecar of whatever text the orchestrator pipes; nothing compares it with the prompt the Agent tool actually received. The PreToolUse Agent hook already records `prompt_sha256` of the real prompt. Binding the receipt to that is cheaper (one call instead of three) and stronger.

Changes:
1. `hooks/receipts/planner.py::receipt_planner_spawn`: remove `injected_prompt_sha256`. New behaviour: find the latest `spawn-log.jsonl` `post` entry whose `subagent_type` maps to `planning` (use `role_for_subagent_type` from PR-03) and whose timestamp is later than the previous `planner-*` receipt for this task (or task creation). Record `spawn_prompt_sha256`, `spawn_result_sha256`, `spawn_started_at`, `spawn_finished_at` in the receipt. Refuse with `ValueError("receipt_planner_spawn: no planner spawn recorded since <ts>")` if none. If `spawn-log.jsonl` is absent, degrade: write the receipt with `spawn_evidence: "degraded"` and emit `planner_spawn_evidence_degraded`, following `hooks/receipts/stage.py:992 _assert_spawn_log_evidence`.
2. `hooks/ctl.py::cmd_planner_receipt` (2555): drop `--injected-prompt-sha256`.
3. `hooks/router.py::cmd_planner_inject_prompt` (2406): keep the subcommand as an optional prompt builder (learned-rule injection) but stop requiring or asserting the sidecar. Remove `INJECTED_PLANNER_PROMPTS_DIR` usage from receipts; keep the constant only if the audit/executor sidecars share it (they do: leave executor/auditor sidecars alone, this PR is planner-only).
4. `skills/start/SKILL.md`: delete the `*_DIGEST=$(printf '%s' "$X_PROMPT" | ... planner-inject-prompt ...)` blocks, the "proof-of-injection gate" paragraph, and `--injected-prompt-sha256` from every `planner-receipt` example. After each planner spawn the orchestrator runs exactly one command: `planner-receipt`.
5. `docs/write-boundary-spec.md`: update the SEC-004 section: planner proof moves from "orchestrator piped text" to "hook observed the spawn".

Tests:
- Rename `tests/test_receipt_planner_spawn_sidecar.py` → `tests/test_receipt_planner_spawn_crosscheck.py`: binds to latest spawn; refuses when none; refuses when the only spawn predates the previous receipt; degraded host emits event.
- `tests/test_planner_inject_prompt.py`: remove sidecar-required cases; keep prompt-building cases.
- `tests/test_skill_start_planner_inject.py`, `tests/test_inject_prompt_sidecar.py` (planner parts): update.
- `tests/test_gate_planner_receipts.py`: unchanged expectations for receipt presence at stage gates.

Acceptance: per planner spawn, orchestrator deterministic calls = 1 (`planner-receipt`).

---

### PR-05 — feat: one-pass discovery

**Fixes P4.** Branch `feat/one-pass-discovery`.

Changes:
1. `skills/start/SKILL.md` Step 1: delete items 3–5 (generate questions, `AskUserQuestion`, write notes). Step 1 becomes "assemble discovery context": read `raw-input.md`, the files it names, optional trajectory memory. No user interaction.
2. Step 2 planner instruction: require the response to contain
   - `Questions`: at most 4, each formatted `Qn: <question> — changes: implementation|risk|acceptance — default: <recommended answer>`;
   - `Design Options`: only for hard/critical subtasks, each with a recommended option;
   - `Assumptions`: everything the planner resolved itself, each tagged `safe assumption` or `needs confirmation`;
   - `Classification (JSON)`.
3. Orchestrator presents questions **and** design options in a single `AskUserQuestion` call (max 4 questions per call; if the planner returns more than 4 items total, take the 4 highest impact and append the rest to `discovery-notes.md` under `## Deferred (needs confirmation)` with their defaults applied).
4. Write `discovery-notes.md` (Q&A + assumptions + deferred) and `design-decisions.md` once, after that single round.
5. `agents/planning.md` and `cli/assets/templates/base/agents/planning.md`: Discovery phase gains the 4-question cap, the tag format, and "pre-fill a default for every question".
6. Step 2d (arch-design open questions) stays as the only second round, high/critical only.

Tests:
- `tests/test_skill_prose_regressions.py`: between the `## Step 1` and `## Step 3` headings exactly one `AskUserQuestion` occurrence on the normal path (Step 2d's is inside a "high/critical only" block; assert by section).
- New prose assertion that `agents/planning.md` Discovery phase states "at most 4".

Acceptance: normal path has one human round before spec normalization.

---

### PR-06 — fix: calibrate risk classification and the deterministic risk floor

**Fixes P5 (classification).** Branch `fix/risk-calibration`. Security-relevant: keep floors for real signals, remove the over-broad ones.

Changes in `hooks/ctl.py`:
1. `_HIGH_RISK_DOMAINS` (572): replace with two sets: `_FLOOR_HIGH_DOMAINS = {"migration"}` and `_FLOOR_MEDIUM_DOMAINS = {"db", "security"}`. `security` floors to `high` only when the keyword scan also matches an auth/secrets term (see 3).
2. `_compute_risk_floor` (607): `len(domains) >= 3` → `medium` (was `high`). `len(files_expected) >= 10` → `high` (unchanged).
3. `_RISK_KEYWORD_PATTERN` (567): split into
   - `_KEYWORD_HIGH = r"\b(payment|drop (table|column)|irreversible|data loss)\b"` → `high`
   - `_KEYWORD_MEDIUM = r"\b(auth(entication|orization|z)|oauth|session token|secret|hmac|signing|encrypt(ion|ed)?|schema migration|data migration)\b"` → `medium`
   - bare `auth` and bare `migration` no longer match.
   All case-insensitive, word-bounded; keep the "no ReDoS" comment accurate.
4. Token-count floor on `spec.md` > 1500 words → `medium` (unchanged).
5. `cmd_run_start_classification` JSON output: include `risk_floor_applied: bool`, `risk_floor_signals`, and `planner_risk_level` so the orchestrator can tell the user why risk was raised. Skill Step 2: print that one line when `risk_floor_applied` is true.

Changes in `agents/planning.md` (+ template mirror), "Classification rules":
6. Replace the narrative risk paragraph with a rubric:
   - `critical`: possible data loss; changes auth/authz semantics; production schema migration; breaking a public API contract.
   - `high`: cross-service contract change; new trust boundary; payment or PII handling; more than 10 files.
   - `medium`: default for a feature or bugfix touching 2–10 files or any persisted state.
   - `low`: single-file, or docs/tests-only, with no persisted-state effect.
7. `domains: security` is added only when the change itself modifies auth, secrets, permissions, or crypto code paths, not because a touched module calls them.
8. `tdd_required` derivation (`high`/`critical`) unchanged.

Tests:
- `tests/test_risk_level_upgrade.py`, `tests/test_claude_md_risk_gate.py`: update; add cases: `["backend","db"]` + no keywords → `medium`; raw input "the author wants…" → no floor; "schema migration" → `medium`; `["security"]` + "oauth" → `high`; 3 domains → `medium`; 10 files → `high`.
- Prose test: rubric headings present in `agents/planning.md`.

Acceptance: a two-domain `backend`+`db` CRUD feature with no auth keywords classifies `medium`.

CHANGELOG must say explicitly which floors were lowered and which stayed.

---

### PR-07 — feat: combined spec+plan planning path for low/medium risk with one review

**Fixes P5 (fast path) and P6.** Branch `feat/combined-planning-path`. Land after PR-06.

Design: new manifest field `planning_path ∈ {"combined","staged"}`, computed at classification time, independent of `fast_track`. `fast_track` keeps its current definition and its audit-reduction effect.

Changes:
1. `hooks/lib_validate.py`: `compute_planning_path(manifest) -> str`: `"combined"` when `risk_level in {"low","medium"}` and `len(domains) <= 2` and `type != "migration"`; else `"staged"`. Honour `policy.json::planning_path_override` (`"staged"` or `"combined"`). `apply_fast_track` persists both `fast_track` and `planning_path` in the same write (rename to `apply_planning_flags`, keep old name as alias).
2. `hooks/lib_core.py`: in **both** gate copies (~1011 and ~1696), the `planner-spec` receipt requirement at `SPEC_NORMALIZATION → SPEC_REVIEW` is skipped when `planning_path == "combined"` **or** `fast_track` is true. `planner-plan` at `PLANNING → PLAN_REVIEW` stays required. Factor the flag read into one helper `_combined_planning(manifest)`.
3. `hooks/ctl.py::cmd_run_planning_mode` (4107): return `"combined"` when `planning_path == "combined"`; keep `"fast_track_combined"` as an alias value in the JSON for one release (`planning_mode_alias`). Otherwise defer to PR-08 logic (or current logic if PR-08 not yet landed).
4. `hooks/ctl.py`: new `run-combined-stage-walk <task_dir>`: preconditions `spec.md`, `plan.md`, `execution-graph.json` exist and `validate_task_artifacts(strict=True, run_gap=False)` is clean; then transitions `SPEC_NORMALIZATION → SPEC_REVIEW → PLANNING → PLAN_REVIEW` via `transition_task`, writes the `plan-validated` receipt, prints JSON. Refuses (exit 1, JSON errors) otherwise. Replaces the hand-run walk in Step 5.
5. `hooks/ctl.py::cmd_approve_stage` (935): add `--combined`. Only legal when stage is `SPEC_REVIEW` after a combined walk: hashes `spec.md` and `plan.md`, writes `human-approval-SPEC_REVIEW` and `human-approval-PLAN_REVIEW` (or the `auto-approval-*` variants with `--auto-approved`) in one atomic step, advances to `PLAN_AUDIT`. Refuse if either file is missing. Reuse `receipt_human_approval` (`hooks/receipts/approval.py:50`).
6. `skills/start/SKILL.md`:
   - Step 2b: rename to "Planning Path (deterministic)"; read `planning_path` from the `run-start-classification` output; keep the fast-track audit note.
   - Step 3 and Step 4: "Skip when `planning_path == combined`" (replace the fast-track-only wording).
   - Step 5: on `combined`, one planner spawn in `Spec + Plan` phase, then `validate_task_artifacts --no-gap`, then `run-combined-stage-walk`.
   - Step 6: on `combined`, present `spec.md` and `plan.md` together and run `approve-stage ... --combined` (or `--combined --auto-approved` on the residual path). Changes requested → PR-09 repair pass on the named artifact only.
   - Staged path text unchanged.
7. `agents/planning.md` `Spec + Plan` phase (+ mirror): remove "fast-track only" and the single-segment rule; allow up to 4 segments; keep concise-section allowance for `low` only.
8. `skills/audit/SKILL.md`, `hooks/lib_core.py::build_audit_plan`, `skills/execute/SKILL.md` inline fast-track: **unchanged**, still keyed on `fast_track`.
9. `PIPELINES.md`: stage table gains the two paths; `docs/system-sequence-and-loopholes.md` sequence diagram updated.

Tests:
- New `tests/test_planning_path.py`: eligibility matrix; override; `run-combined-stage-walk` refuses without `spec.md` (invariant); `approve-stage --combined` writes both receipts, refuses on missing artifact, advances to `PLAN_AUDIT`; hash mismatch refused.
- `tests/test_gate_planner_receipts.py`, `tests/test_scheduler_spec_review_to_planning.py`, `tests/test_scheduler_compute_next_stage.py`: combined path advances without `planner-spec`; staged still refuses.
- `tests/test_stage_order.py`, `tests/test_skill_stage_references.py`, `tests/test_conditional_plan_sections.py`: update.
- `tests/test_auto_approve_gates.py`: `--combined --auto-approved` path.

Acceptance: medium two-domain task = discovery spawn → combined spawn → one review → gap analysis → ready. Two planner spawns, two human rounds.

---

### PR-08 — feat: choose hierarchical planning from independent subsystems, not criteria count

**Fixes P7.** Branch `feat/hierarchical-by-subsystem`. Land after PR-05.

Changes:
1. `agents/planning.md` Discovery phase (+ mirror): classification JSON gains optional `"subsystems": [{"name": str, "paths": [str], "independent": bool}]`, derived from the subtask breakdown already required. Rules: paths repo-relative, no path in two subsystems, `independent` true only when the subsystem can be planned with no shared interface decisions.
2. `hooks/ctl.py::_normalize_classification_payload` (~690): validate and persist `subsystems`; drop malformed entries with a warning in the JSON output; refuse overlapping paths.
3. `hooks/lib_validate.py` classification validator (~534-562): schema for `subsystems`.
4. `hooks/ctl.py::cmd_run_planning_mode` (4107): after the combined check (PR-07), `hierarchical` iff (`risk_level in {high, critical}` and independent subsystems ≥ `policy.hierarchical_min_subsystems_high` (default 2)) or (independent subsystems ≥ `policy.hierarchical_min_subsystems` (default 3)). Else `standard`. Delete the `criteria_count > 10` clause; keep `criteria_count` in the JSON for telemetry.
5. `hooks/planner.py planning-mode` subcommand: same rule (it mirrors ctl today; make it call the same function).

Tests:
- New `tests/test_planning_mode_subsystems.py`: 14 criteria + 1 subsystem → standard; 6 criteria + 3 independent → hierarchical; high + 1 → standard; high + 2 independent → hierarchical; overlapping paths refused at write-classification.
- `tests/test_planner.py` updated.

Acceptance: `criteria_count` never changes `planning_mode`.

---

### PR-09 — feat: planner repair pass with a bounded retry budget

**Fixes P8.** Branch `feat/planner-repair-retry-cap`.

Changes:
1. `hooks/validate_task_artifacts.py`: add `--json`: `{"ok": bool, "errors": [{"artifact": "spec.md"|"plan.md"|"execution-graph.json"|"manifest.json", "rule": "<short-id>", "message": str, "location": str|null}]}`. Map each `errors.append` in `lib_validate.validate_task_artifacts` to an `artifact` and a stable `rule` id (do this by returning structured errors from a new `validate_task_artifacts_structured` and keeping the string list function as a thin wrapper).
2. `hooks/plan_gap_analysis.py`: ensure `findings_from_report` output is included in the same JSON shape when `--json` is passed.
3. `hooks/ctl.py::cmd_run_spec_ready`: `respec_required` payload uses the same structured shape.
4. `agents/planning.md` (+ mirror): new **Phase: Repair**. Inputs: the artifact(s) named in the error list and the JSON errors. Rules: edit in place with `Edit`, change only what an error names, do not regenerate passing sections, do not re-read discovery inputs unless an error cites them, re-run nothing. Read budget: the artifacts + any file an error's `location` names.
5. `hooks/ctl.py`: new `planner-retry <task_dir> --phase <discovery|spec|plan|combined|arch-design>`: increments `manifest.retry_counts[phase]` (field exists, unused) through the ctl write path; prints `{"phase", "attempt", "allowed": bool, "max": n}`; `allowed` false when `attempt > policy.planner_max_repairs` (default 2). Exit 0 either way; exit 1 on bad input.
6. `hooks/ctl.py`: new `validation-override-receipt <task_dir> --artifact <name> --justification "<text>"`: writes `validation-override-<artifact>` receipt (non-blank justification required, mirrors the `--force` justification receipts). `plan-validated-receipt` accepts a live override receipt for the named rule ids only when `policy.allow_validation_override` is true (default true for `low`/`medium`, false for `high`/`critical`).
7. `skills/start/SKILL.md`: one shared block "On validation failure" referenced from Steps 2, 3, 5, 6:
   1. run `planner-retry --phase X`;
   2. if `allowed`: spawn the planner in **Repair** phase with the `--json` errors (or resume the previous planner session if the harness supports it; say which you did in the log line);
   3. re-run the same validator;
   4. if not `allowed`: present the remaining errors to the user via `AskUserQuestion` with options **provide guidance** (one final Repair pass seeded with the guidance), **accept with justification** (`validation-override-receipt`, only where policy allows), **fail task**.
   Delete every uncapped "send it back", "respawn planning", "reject lazy output mentally" instruction; judgment-based rejections get at most one Repair pass before the user sees the artifact.
8. `skills/plan/SKILL.md`: same block.

Tests:
- New `tests/test_planner_retry.py`: counter increments per phase; `allowed` flips at the cap; override receipt requires justification; override refused for `high` by default.
- `tests/test_validator_error_messages.py`: every structured error has `artifact` and `rule`; string wrapper output unchanged.
- Prose test: `skills/start/SKILL.md` contains no "respawn" outside the shared block; "Reject lazy output" phrase removed.

Acceptance: worst case per phase = initial spawn + 2 repair passes + one user decision.

---

### PR-10 — feat: enforce the planner read budget in tooling; fix turn headroom

**Fixes P9.** Branch `feat/planner-read-budget`. Land after PR-03.

Changes:
1. `hooks/lib_tool_budget.py`: add `PLANNER_PHASE_READ_ALLOWANCE = {"discovery": 8, "arch-design": 30, "spec": 10, "plan": 12, "combined": 15, "repair": 6}` and `PLANNER_WRITE_TURNS = 6`.
2. Phase signalling: `stamp_role` (PR-03) stores `planning` in `active-segment-role`; add a sibling hook-owned file `active-planner-phase` written by `planner-retry`/a new tiny `ctl set-planner-phase <task_dir> <phase>` that the skill calls once before each planner spawn (this is the one ceremony call that stays; it replaces the sidecar call). Default phase `plan` when absent.
3. `hooks/read_policy.py::decide_read` (44): add a `planning` branch before the non-audit fast-exit:
   - always allow: anything under `task_dir`;
   - allow: files named in `raw-input.md`, `discovery-notes.md`, `design-decisions.md`, `design-doc.md`, `spec.md` (extract paths with the regex `plan_gap_analysis._extract_plan_paths` uses; cache the set in `task_dir/planner-read-allowlist.json`, hook-owned);
   - otherwise: consume one unit of the phase quota from `task_dir/planner-read-quota.json` (same mechanism as `_handle_dead_code_grep_quota`); deny when exhausted with a message that states the quota and says "ask for the file as a discovery question or list it in the plan's Reference Code";
   - `Grep`/`Glob`: allowed only with a `path` argument inside the repo and only under quota; patterns rooted at the repo root with no path are denied.
4. `hooks/pre_tool_use.py` (~998-1088): route `Read|Grep|Glob` through `decide_read` when the resolved role is `planning` (today only `audit-*`). Keep fail-open behaviour for non-task contexts unchanged.
5. `hooks/router.py` (185): `_MAX_INJECTED_PROMPT_BYTES` stays for executors/auditors; add `_MAX_PLANNER_PROMPT_BYTES = 60_000` and make the planner prompt builder exit 1 with a message when exceeded.
6. `agents/planning.md` frontmatter (+ mirror): `maxTurns: 50`. Replace the "Read Budget (HARD CAP)" prose with a short statement that the budget is enforced per phase by the read policy, listing the allowances.
7. New `tests/test_planner_turn_headroom.py`: `maxTurns >= max(PLANNER_PHASE_READ_ALLOWANCE.values()) + PLANNER_WRITE_TURNS + 10`, mirroring `tests/test_runaway_defense_seal.py`.

Tests:
- `tests/test_read_policy.py`: planning allow/deny matrix; quota exhaustion; Grep without path denied; task-dir reads never consume quota.
- `tests/test_write_policy_hook.py` / pre_tool_use tests: Read routed for `planning`.
- `tests/test_router_inject_prompt_guardrail.py`: oversize planner prompt refused.

Acceptance: a planner in phase `plan` cannot read more than 12 repo files outside its allowlist; a repo-wide Grep is denied with an actionable message.

---

### PR-11 — fix: narrow the external-solution gate to named third-party systems

**Fixes P10.** Branch `fix/external-gate-triggers`.

Changes in `hooks/ctl.py`:
1. `_EXTERNAL_TRIGGER_TERMS` (166): keep `sdk`, `oauth`, `sso`, `webhook`, `stripe`, `docker`, `terraform`, `github actions`, `gitlab ci`, `aws`, `gcp`, `azure`; add `kubernetes`, `helm`, `grpc`, `graphql`, `openapi`, `saml`, `oidc`, `twilio`, `sendgrid`, `okta`, `auth0`. Remove `retry`, `queue`, `cache`, `pagination`, `rate limit`, `rate limiting`, `metrics`, `tracing`, `protocol`, `migration`, `migrate`, `setup`, `configure`, `integration`, `event bus`, `eventbus`, `cloud`.
2. `_compute_external_solution_gate` (~231): `migration_like` no longer includes `task_type == "full-stack"` or `"infra" in domains` by itself; `migration` type/domain recommends search only when the raw input also matches a vendor/version pattern (`\b(postgres|mysql|mongo|redis|django|rails|react|next\.js|spring|node)\s*\d`) or a term from the trigger list.
3. Read `policy.json::external_search_terms_extra: [str]` and append to the trigger list (lower-cased, max 50 entries).
4. `skills/start/SKILL.md` Step 2c: unchanged mechanics; adjust the explanatory sentence.

Tests (`tests/test_external_solution_gate.py`):
- "configure the retry queue and cache metrics" → not recommended;
- "add a Stripe webhook handler" → recommended;
- full-stack feature with no vendor → not recommended;
- "migrate to Postgres 16" → recommended; "migrate tests to pytest" → not recommended;
- project extra term matches.

Acceptance: internal refactors and features never return `search_required` from `run-spec-ready`.

---

### PR-12 — chore: start-phase budget guard (retire-or-condition for pre-execution)

**Fixes P11.** Branch `chore/start-phase-budget-guard`. Land last, after PR-02 and the ceremony removals.

Changes:
1. `PIPELINES.md`: extend "Retire-or-Condition Rule" with a "Start phase" subsection: any new mandatory step, spawn, receipt, or human gate before `PRE_EXECUTION_SNAPSHOT` must (a) retire one of equal scope, (b) be conditioned on `risk_level ∈ {high, critical}` or `planning_path == "staged"`, or (c) ship with a `planning_timeline` delta from at least three tasks.
2. New `tests/test_start_skill_budget.py`: parse `skills/start/SKILL.md` by `## Step` headings; count fenced `"$DYNOS"` commands, `Spawn`/`Agent` instructions, and `AskUserQuestion` occurrences per path. Pin ceilings in a constants block at the top of the test with a comment on how to change them:
   - combined path: ≤ 2 spawns, ≤ 2 human rounds, ≤ 18 deterministic commands;
   - staged medium: ≤ 3 spawns, ≤ 3 human rounds, ≤ 25 deterministic commands;
   - high/critical: ≤ 7 spawns, ≤ 5 human rounds.
   Set the actual ceilings to the counts observed after PR-01…11 plus a margin of 2; record the observed counts in the test docstring.
3. `scripts/check_release_hygiene.py`: when a PR diff adds a fenced `"$DYNOS"` command to `skills/start/SKILL.md` without changing `tests/test_start_skill_budget.py`, print a warning naming the rule (warning only; the test is the hard gate).
4. Retrospective regression flag: in the same place `pipeline_budget` is compared against tier ceilings (grep `audit_phase_llm_calls` consumers in `memory/` and `hooks/postmortem*.py`), add `planning_timeline.planner_spawns` and `pre_execution_seconds` with a "more than 1.5× trailing median of the last 5 tasks" flag, emitted as a prevention-rule candidate.

Tests: the new test itself; `tests/test_release_hygiene_scripts.py` for the warning.

Acceptance: adding a mandatory start-phase command without moving the ceiling fails `pytest`.

---

### PR-13 — fix: one finding-collection function with tier supersession for gate, plan, and repair-log

**Fixes A1, A2, A4.** Branch `fix/audit-findings-single-source`. Blocking defect; land right after PR-02.

Design: introduce one function that returns the **live finding set** for a task, and make every consumer use it. "Live" means: for an ensemble auditor whose cascade verdict is `escalated`, only the deep-tier report; for verdict `pass`, no blocking findings from any tier (voting tiers were clean by definition); for a single-model auditor, its latest `-attempt-N` report. Earlier-tier reports remain on disk as advisory context and are listed separately.

Changes:
1. `hooks/lib_validate.py`: add `collect_live_findings(task_dir, *, audit_plan: dict | None = None) -> LiveFindings` where `LiveFindings` is a small dataclass: `findings: dict[str, dict]` (keyed by **namespaced** id, see PR-14; until PR-14 lands key by `f"{auditor}:{model}:{id}"`), `by_auditor: dict[tuple[str, str], {"finding_count", "blocking_count", "binding": bool}]` keyed by `(auditor_name, model_used)`, `superseded_reports: list[Path]`, `advisory_findings: list[dict]`.
   - Group reports by `(auditor_name, model_used)` using the receipt (`audit-{auditor}` / shard receipts; reuse `select_eligible_reports` internals for receipt lookup) and fall back to `_REPORT_FILENAME_RE` groups 1–2.
   - For each auditor, call `lib_ensemble.evaluate_cascade(auditor_name, plan_entry, receipts_by_model)` with the entry from `audit-plan.json`. Binding report = escalation model's report when `verdict == "escalated"`; when `verdict == "pass"`, binding = none (zero blocking); when `status == "single"`, binding = the latest attempt for the plan model. Reports that are not binding are `superseded`.
   - Remove the `{file}:{line}:{category}` secondary dedup entirely. Shard-merge correctness (its original purpose, "AC 8") is handled by namespacing in PR-14; until then, dedup only exact duplicate ids **within one report**.
2. Replace the four `select_eligible_reports(task_dir, stage=None)` loops in `hooks/ctl.py` (`cmd_run_audit_findings_gate` 4319, the cycle-plan command at ~4391, `run-repair-log-build` at ~4631, and the ~4897 consumer) with `collect_live_findings`. `findings_by_auditor` becomes keyed by `"{auditor}@{model}"` and gains `binding`. Gate output adds `superseded_reports` and `advisory_finding_ids` so the operator can see what was dropped and why.
3. `_collect_audit_findings` becomes a thin wrapper over `collect_live_findings(...).findings` (keep the name; `test_validate_chain_no_orphan_readers.py` may pin it).
4. The repair-log validator (`lib_validate.py` ~960-1000) looks up `live_finding` in the same set. "not found in live audit reports" can now only fire when the builder and validator genuinely disagree, which the shared function makes impossible by construction.
5. `skills/audit/SKILL.md`: after `ensemble-next` reports `complete`, say explicitly that earlier-tier reports are advisory and that the gate output's `superseded_reports` lists them. No new mandatory step.

Tests:
- New `tests/test_collect_live_findings.py`: fixture with one ensemble auditor (haiku blocking ×2, fable 0 blocking, receipts for both, plan entry `ensemble: true`) → zero live blocking findings, haiku report in `superseded_reports`; verdict `pass` case; single-model auditor with attempts 1 and 2 → only attempt 2 live; two location-less findings in the same category from different auditors → both live (regression for the dedup).
- `tests/test_gate_done_ensemble.py`, `tests/test_ensemble_cascade_enforcement.py`, `tests/test_audit_sharding.py`: update expectations; `test_audit_sharding` asserts shard-merge behaviour — confirm it still holds via namespacing or exact-id dedup within a report.
- Reproduce the reporter's case end to end in `tests/test_repair_log_round_trip.py`: findings gate → cycle plan → `run-repair-log-build` → validator passes.

Acceptance: with the reporter's 31 reports, `run-repair-log-build` returns a valid repair-log whose finding ids are exactly the deep-tier blocking findings.

CHANGELOG must name the behaviour change: fast-tier findings of an escalated ensemble auditor no longer block.

---

### PR-14 — fix: namespace finding ids per report and reconcile severity from the binding report

**Fixes A3.** Branch `fix/finding-id-namespacing`. Land after PR-13.

Changes:
1. `hooks/lib_validate.py::collect_live_findings`: the canonical key is `f"{auditor_name}/{model_used}/{raw_id}"`. Each live finding row carries `raw_id`, `auditor_name`, `model_used`, `canonical_id`. The repair-log schema's `finding_id` field accepts the canonical form; a bare raw id is accepted only when it is unambiguous across the live set (exactly one match), otherwise the validator error names the candidates.
2. `run-repair-log-build` emits canonical ids. `repair-coordinator` prompt (`agents/repair-coordinator.md` + template mirror) is told the id format and to copy it verbatim.
3. Severity: the validator compares against the **binding** report's severity only. Drop the cross-tier "severity mismatch" error; replace it with a warning field in the gate output when tiers disagree.
4. Audit-report writer guidance (`agents/_shared/audit-report.md`): ids must be unique within a report; cross-report reuse is fine because the framework namespaces.
5. `hooks/ctl.py audit-receipt`: refuse a report whose `findings[].id` values are not unique within that report (clear message naming the duplicate).

Tests:
- `tests/test_finding_id_namespacing.py`: duplicate raw ids across tiers do not collide; bare-id lookup resolves when unique and errors with candidates when not; severity taken from binding report.
- `tests/test_repair_log_round_trip.py` extended with the reporter's `F-01`/`F-02` case.

Acceptance: two tiers emitting `F-02` and `F-01` with different severities produce a valid repair-log with no duplicate or mismatch errors.

---

### PR-15 — fix: one audit-report filename contract

**Fixes A5.** Branch `fix/audit-report-filename-contract`.

Changes:
1. Decide the canonical name as `{auditor}-{model}-attempt-{n}.json` (what the gate already enforces). `hooks/ctl.py`: add `ctl audit-report-path <task_dir> <auditor> --model <m>` that prints the next free attempt path; `audit-receipt` already takes `--report-path`, make it refuse paths that do not match `_REPORT_FILENAME_RE` with a message showing the expected shape.
2. `skills/audit/SKILL.md:202` and the per-auditor "write your report to" sentences in `agents/*-auditor.md` (+ template mirrors): replace `{auditor}-checkpoint-{timestamp}.json` with the `audit-report-path` output. Grep all skills and agents for `checkpoint-{timestamp}` and `-checkpoint.json`.
3. `write_policy.py` audit-report write rule: accept only the canonical pattern for `audit-*` roles so a wrong filename is denied at write time with the same message, not silently ignored by the gate later.

Tests: `tests/test_audit_evidence_existence.py`, `tests/test_write_policy*.py` (audit-report pattern), prose test that no skill or agent mentions `checkpoint-{timestamp}`.

Acceptance: an auditor cannot write a report the gate will ignore.

---

### PR-16 — feat: `ctl log-append` as the sanctioned way to write execution-log lines

**Fixes A6.** Branch `feat/ctl-log-append`. **Must land before PR-03.**

Changes:
1. `hooks/ctl.py`: `log-append <task_dir> --tag <GATE|SKIP|DECISION|DONE|ADVANCE|ROUTE|SPAWN|HUMAN|FAILED|NOTE> --message "<text>"`. Writes `{now_iso()} [{TAG}] {message}\n` through the ctl write path (role `ctl`), single line only (reject newlines), message ≤ 500 chars. Prints the written line. Tags are an allowlist; `[STAGE]` is reserved for `transition_task` and refused here.
2. `hooks/write_policy.py`: `execution-log.md` becomes ctl-owned for agent roles (deny direct writes with a message naming `log-append`). Keep the orchestrator allowance for one release behind `policy.json::allow_direct_log_append` (default false) so an old skill cache does not break; remove next release.
3. Every skill that says "append to the execution log" or shows `>> execution-log.md` (`skills/start`, `plan`, `execute`, `audit`, `repair`, `resume`): replace with the `log-append` command. Grep for `execution-log.md` and `[GATE]`, `[SKIP]`, `[DECISION]`.
4. Spec ACs that require a log line (the reporter's "run_probe.py exit 0 recorded in execution-log.md") are satisfiable via `log-append --tag NOTE`.

Tests: `tests/test_ctl_log_append.py` (format, tag allowlist, newline rejection, STAGE refused); `tests/test_write_policy_hook.py` (direct append denied for planning/executor roles, message names the command); prose test that no skill contains `>> .dynos/task-{id}/execution-log.md`.

Acceptance: with `active-segment-role` stamped `planning`, the orchestrator can still record a `[GATE]` line.

---

### PR-17 — fix: resolve shell variables and in-command assignments in the Bash write pre-filter

**Fixes A7.** Branch `fix/pre-tool-use-shell-vars`.

Changes:
1. `hooks/pre_tool_use.py`: before destination extraction, collect simple assignments in the command (`^\s*([A-Za-z_][A-Za-z0-9_]*)=("[^"]*"|'[^']*'|\S+)` per `;`/`&&`/newline segment) into a map; expand `$VAR`, `${VAR}` in candidate destinations from that map first, then `os.environ`. Unresolvable variables: keep today's literal behaviour but change the denial message to say "unresolved shell variable `$T` in write destination; set it in the same command or use an absolute path under `.dynos/task-{id}/_scratch/`".
2. Never expand command substitutions (`$(...)`, backticks); treat them as unresolved.
3. Keep heredoc stripping (`_strip_heredocs`) before the scan.

Tests: `tests/test_bash_destination_extraction.py`: `T=.dynos/task-x; echo hi > $T/_scratch/a` allowed; `${T}`; env var; unresolved var message; `$(pwd)` not expanded; quoted `>` in prompt text still not a destination (existing P0-b regression).

Acceptance: the reporter's `$T/_scratch/x` write is allowed.

---

### PR-18 — fix: honour to-be-created markers in gap analysis for endpoints and tables

**Fixes A8.** Branch `fix/gap-analysis-to-be-created`.

Changes:
1. `hooks/plan_gap_analysis.py::analyze_api_contracts` and `analyze_data_model`: a row is **planned** (excluded from `claimed_not_found`, listed under `planned`) when any of: a `Status`/`State`/`Notes` column contains `to-be-created`, `new`, or `planned` (case-insensitive); the endpoint/table cell carries a trailing `(new)` or `(to-be-created)`; or the row is under a sub-heading containing "to be created"/"new". Document the three forms in the plan template (`agents/planning.md` API Contracts / Data Model sections + mirror).
2. `findings_from_report`: planned rows never produce a gap finding; the report shows them under `planned` for the auditor.
3. After execution (`run-execute-setup` is pre-execution; the post-execution gap check, if any, grep `run_gap_analysis(` callers) planned rows **must** now exist: add `--require-planned` used by the audit-phase caller so a planned endpoint that was never built becomes a finding there, not before.

Tests: `tests/test_plan_gap_analysis.py`: each marker form; planned rows excluded pre-execution; `--require-planned` flags missing ones; greenfield repo with all-new endpoints → no gaps.

Acceptance: a greenfield plan marking every endpoint to-be-created passes `run-execute-setup`.

---

### PR-19 — fix: claude-md-auditor must not depend on an unset env var

**Fixes A9.** Branch `fix/claude-md-auditor-hooks-path`.

Changes:
1. `agents/claude-md-auditor.md:60` (+ mirror): replace `python3 "${PLUGIN_HOOKS}/lib_claude_md.py"` with `"$DYNOS" hook lib_claude_md` where `$DYNOS` is injected as an absolute path by the audit skill's prompt builder (`router.py audit-inject-prompt` already substitutes paths; add `DYNOS` to the substitution set and assert it is absolute).
2. The agent prompt: if the command fails to start (exit 127 / file not found), return an `envelope.status: "tool_unavailable"` with zero findings instead of a blocking `tool-error` finding. `audit-receipt` maps `tool_unavailable` to a non-blocking advisory and emits `auditor_tool_unavailable` so the gate neither blocks nor silently passes.
3. Grep every agent for `${PLUGIN_HOOKS}`, `${CLAUDE_PLUGIN_ROOT}`, `${CODEX_PLUGIN_ROOT}` used inside commands and route them through the same injected absolute `$DYNOS`.

Tests: `tests/test_claude_md_auditor.py` (prompt contains injected absolute path; no raw `${PLUGIN_HOOKS}`); `tests/test_audit_inject_prompt.py` (substitution asserted absolute); receipt mapping test.

Acceptance: with both plugin-root env vars unset, the auditor runs the helper by absolute path.

---

### PR-20 — fix: continuation status must not treat evidence presence as completion

**Fixes A11 (verify first).** Branch `fix/continuation-requires-receipt`.

Changes:
1. Read `_compute_execution_batch_payload` and `_segment_incompleteness` (`hooks/ctl.py` ~5437). Confirm how `cached_segments` is derived. If any path marks a segment complete without an `executor-done` receipt whose `evidence_sha256` matches the on-disk evidence file, change it: completion = receipt present **and** fresh **and** `_verify_execution_evidence` clean. Evidence presence alone → `incomplete_reasons: ["evidence present but no executor receipt"]`.
2. `compute_continuation_status`: `complete_segments` lists only receipt-backed segments; add `evidence_only_segments` for visibility.
3. If the investigation shows the reporter's case was a different path (e.g. the gate failure was recorded after the continuation check), document the actual cause in the PR and fix that instead.

Tests: `tests/test_next_continuation.py`: evidence file without receipt → `continuation_needed`; receipt with stale hash → incomplete; both fresh → complete.

Acceptance: a segment with evidence but no receipt is never reported complete.

---

### PR-21 — fix: git path handling for non-ASCII filenames in ownership checks

**Fixes A10.** Branch `fix/git-quotepath`.

Changes:
1. `hooks/ctl.py`: every `git diff --name-only` / `--diff-filter` invocation (2768, 4203, 4806; grep for others, including `lib_validate.py` and `verify_behavior_preserved.py`) runs with `-c core.quotepath=false` and `-z` where the parser can take NUL-separated output; decode as UTF-8 with `surrogateescape`. Factor into one helper `_git_changed_files(root, base, *, filters=None) -> list[str]`.
2. Normalise both sides (`files_expected` and git output) with `unicodedata.normalize("NFC", ...)` before comparison (macOS HFS+/APFS NFD vs NFC).

Tests: `tests/test_git_diff_gate.py`: temp repo with `café.txt` and a NFD-encoded name; ownership check passes with default git config.

Acceptance: the reporter's `café.txt` segment passes `run-execution-segment-done` without touching git config.

---

### PR-22 — fix: actionable message and auto-revalidation after `amend-artifact plan`

**Fixes A12.** Branch `fix/amend-plan-revalidate`.

Changes:
1. `hooks/ctl.py amend-artifact` (plan/spec): after a successful amend, run `validate_task_artifacts(strict=True, run_gap=False)`; if clean, re-issue the `plan-validated` receipt in the same command and print `plan_validated: true`; if not, print the structured errors (PR-09 shape if landed) and `next: fix errors, then run plan-validated-receipt`.
2. `hooks/lib_core.py:1075, 1797`: when the receipt exists but is stale, the error reads `plan-validated receipt is stale (plan.md changed after validation); run "<DYNOS> ctl plan-validated-receipt <task_dir>" after re-validating` instead of "plan was never validated". Keep the "never validated" wording only when no receipt exists at all.
3. `skills/plan/SKILL.md`, `skills/execute/SKILL.md`: one sentence each on what `amend-artifact` now does.

Tests: `tests/test_amend_artifact.py`, `tests/test_amend_plan_validated_drift.py`, `tests/test_gate_execution_freshness.py`: stale vs missing messages; auto re-issue on clean amend.

Acceptance: amend → `run-execute-setup` works without a manual receipt step when the amended plan validates.

---

## 2. Verification checklist per PR

1. `python3 -m pytest -q` green locally.
2. Prose tests updated, not weakened.
3. Template mirrors under `cli/assets/templates/base/` updated when `agents/` or skill text changed.
4. `CHANGELOG.md` entry under `## [Unreleased]`, concrete, one per PR. Versions untouched.
5. For PR-03, 04, 05, 07, 09: run a manual smoke in a scratch project: `/dynos-work:start "add a --json flag to scripts/check_release_hygiene.py"` and confirm the stage sequence in `execution-log.md` matches the PR's acceptance line, then `ctl timeline` (after PR-02) shows the expected spawn count.
5b. For PR-13 and PR-14: build the fixture from §0.2b (one ensemble auditor with haiku blocking ×2 and fable 0; two tiers of spec-completion sharing `F-01`/`F-02`) and run `run-audit-findings-gate` → `run-audit-repair-cycle-plan` → `run-repair-log-build`; the last must return a valid repair-log.
6. PR body: link the problem ID(s) from §0.2, paste the acceptance line, and note anything deferred.

## 3. Expected end state (medium two-domain task)

| Metric | Before | After |
|---|---|---|
| Planner spawns | 3 + uncapped respawns | 2 + ≤ 2 repairs per phase |
| Human rounds before execute | 4–5 | 2 |
| Orchestrator deterministic commands | ~55–65 | ~18–22 |
| Planner repo reads | unbounded | per-phase quota |
| Forced external research | on common words | on named vendors only |
| `high` risk triggered by | any db/security domain, bare "auth" | rubric + narrowed floor |
| Visibility | none for planning | model / approval / orchestrator / idle per task |

## 4. Out of scope (do not do in these PRs)

- Weakening audit-phase gates, receipt hashing, or spawn-log forgery defence.
- Auto-approving spec or plan for medium risk outside the existing `auto_approve_gates` residual path.
- Changing executor or auditor budgets or the ensemble cascade.
- Touching `hooks/dashboard-ui/`.
