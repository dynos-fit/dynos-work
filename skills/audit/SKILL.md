---
name: audit
description: "Run checkpoint audit, repair any findings, then reach DONE — all in one shot. Use after /dynos-work:execute."
---

# dynos-work: Audit

Runs the full audit-to-done pipeline: audit → repair loop → DONE.

All repair-log persistence must go through `python3 hooks/ctl.py write-repair-log ... --from ...`.
Do not hand-write `.dynos/task-{id}/repair-log.json`.

## Ruthlessness Standard

- Missing evidence is a finding.
- Partial compliance is non-compliance.
- Cosmetic fixes do not count as repairs.
- If a path is unverified, treat it as suspect.
- Prefer surfacing a real risk over preserving a comforting narrative.
- If two interpretations exist, audit the harsher one until disproved.
- Do not let a clean summary hide a dirty edge case.

## What you do

### Step 0 — Contract validation

After finding the active task, validate that all required inputs from the execute skill are present:

```text
python3 hooks/ctl.py validate-contract --skill audit --task-dir .dynos/task-{id}
```

If validation fails with missing required inputs (evidence files, snapshot SHA), print the errors and stop.

### Step 1 — Find active task

Find the most recent active task in `.dynos/`. Read `manifest.json`.

Verify stage is `CHECKPOINT_AUDIT`. If not, print the current stage and what command to run instead.

### Step 2 — Audit Setup

Build the deterministic audit setup first:

```text
python3 hooks/ctl.py run-audit-setup .dynos/task-{id}
```

This command reads `manifest.json`, derives the audit plan, writes `.dynos/task-{id}/audit-plan.json`, and computes diff scope from `snapshot.head_sha` (or `HEAD` fallback when needed). Use its JSON output directly. Do not hand-derive diff scope, task type, domains, fast-track, skip policy, or model selection in prompt logic.

### Step 3 — Audit (conditional auditor spawning with skip optimization)

When spawning auditors, tell them to attack the implementation, not narrate it. Favor findings with proof over summaries with tone.

For each auditor in the plan:
- If `action: "skip"`: log `{timestamp} [SKIP] {name} — {reason}` and do not spawn
- If `action: "spawn"`: spawn with the specified `model` (null = default)
- Log: `{timestamp} [ROUTE] {name} model={model} route={route_mode} source={route_source}`

**Pre-load diff context (run once, reuse for every auditor in this session):** Using `diff_base` from the audit plan output:

```bash
DIFF_CONTEXT=$(python3 "${PLUGIN_HOOKS}/build_prompt_context.py" --diff {diff_base} --root .)
```

Append `$DIFF_CONTEXT` verbatim to the base prompt for every auditor spawned in this session. Each auditor receives the changed file contents upfront and must not call Read, Grep, or Glob for those files. The script hard-caps output at 150K chars to prevent context blowup. If `$DIFF_CONTEXT` is empty (no changed files found), proceed without it.

**Learned Auditor Injection (MANDATORY — deterministic via router):** Build the auditor's spawn prompt with `router.py audit-inject-prompt`. Pipe the base prompt over stdin and capture stdout as the prompt you pass to the Agent tool — do NOT read the learned agent file yourself or build the prompt by hand. The router does the frontmatter stripping, applies the learned-auditor block under the literal heading `## Learned Auditor Instructions`, computes the SHA-256 of the exact bytes it prints, and atomically writes the per-model sidecar at `.dynos/task-{id}/receipts/_injected-auditor-prompts/{auditor_name}-{model_used}.sha256` (with a companion `.txt` of the same bytes; when no model is specified the literal `default` is substituted in the filename).

```bash
echo "{base prompt for this auditor}" | PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 "${PLUGIN_HOOKS}/router.py" audit-inject-prompt \
  --root . \
  --task-type {task_type} \
  --audit-plan .dynos/task-{id}/audit-plan.json \
  --auditor-name {name} \
  --model {model}
```

The command logs `learned_auditor_applied`, `learned_auditor_missing`, or `learned_auditor_error` to `.dynos/events.jsonl` depending on whether the named auditor's `agent_path` exists and could be read. On any IO failure the command exits 1 and prints a JSON error to stderr — fix the cause (audit-plan path, auditor name, or `--task-type`) before retrying.

After each auditor spawn returns, you MUST write the audit receipt via the deterministic ctl wrapper below. Do NOT hand-write a Python `receipt_audit_done(...)` call. The wrapper derives `finding_count` and `blocking_count` from the on-disk report file so the model cannot mix one auditor/model's counts with another auditor/model's report. The captured digest must be the contents of the sidecar file the router just wrote — read it back rather than re-hashing:

```bash
INJECTED_AGENT_SHA256=$(cat .dynos/task-{id}/receipts/_injected-auditor-prompts/{auditor_name}-{model_used}.sha256)
python3 hooks/ctl.py audit-receipt .dynos/task-{id} {auditor_name} \
  --model {model_used} \
  --report-path .dynos/task-{id}/audit-reports/{report_filename}.json \
  --tokens-used {tokens_used} \
  --route-mode {route_mode} \
  --agent-path {agent_path} \
  --injected-agent-sha256 "${INJECTED_AGENT_SHA256}"
```

`python3 hooks/ctl.py audit-receipt ...` calls `receipt_audit_done(...)`, which re-asserts the same sidecar exists at that exact path and that its contents match `injected_agent_sha256`. A mismatch raises `ValueError`. For `route_mode == "generic"` (no learned agent) the sidecar assertion is skipped and `injected_agent_sha256` may be `None`; `route_mode` and `agent_path` are still required keyword arguments. The wrapper derives counts from `--report-path`; when no report exists it writes literal zero findings only. The new `receipt_audit_routing` writer also enforces these fields per-entry, so any auditor entry missing `injected_agent_sha256` (when non-generic) or `agent_path` will hard-fail at the routing-receipt write.

The router handles fast-track reduction, skip policy, model policy, security floor enforcement, ensemble voting triggers, and learned agent routing in deterministic code. No prompt interpretation needed for these decisions. Do not re-derive skip thresholds, model assignments, or routing modes from markdown tables or retrospective files.

**Ensemble Voting:** If the router plan has `"ensemble": true` for an auditor, follow this sequential cascade instead of a single spawn:

1. Spawn **haiku** (first model in `ensemble_voting_models`).
2. If haiku returns **zero findings** → spawn **sonnet** (second model in `ensemble_voting_models`).
   - If sonnet returns **zero findings** → audit passes. Log: `{timestamp} [VOTE] {name} — PASS (haiku then sonnet: zero findings)`
   - If sonnet returns **any findings** → escalate: spawn `ensemble_escalation_model` (opus). Opus verdict is final and binding. Log: `{timestamp} [VOTE] {name} — Escalating to {escalation_model}`
3. If haiku returns **any findings** → skip sonnet entirely, escalate immediately: spawn `ensemble_escalation_model` (opus). Opus verdict is final and binding. Log: `{timestamp} [VOTE] {name} — haiku found issues, escalating directly to {escalation_model}`

If `"ensemble": false`, spawn normally with the single model from the plan.

**Visual Audit Pass:** For tasks where `domains` includes `"ui"`, run a visual audit: start the dev server, use a browser subagent to screenshot modified screens, then evaluate with Claude 3.5 Sonnet against the planning-phase Design Decisions. Report visual findings as category `vision-finding`. Log: `{timestamp} [VISION] UI audit complete -- {N} visual bugs found`.

**Alongside mode deduplication:** When generic and learned auditors both produce findings for the same role, deduplicate by key `{file}:{line}:{category}`. Count duplicates once, preferring the learned version. The deduplicated set feeds into repair and retrospective counts. Track whether learned findings are a superset of generic findings (for promotion decisions by the learn step).

Append to log (transition_task already auto-logged the `[STAGE] → CHECKPOINT_AUDIT` line; only emit the `[SPAWN]` line):
```
{timestamp} [SPAWN] {N} auditors in parallel ({list of names})
```

Spawn the determined auditors simultaneously, passing the resolved model for each auditor in the subagent spawn configuration. For alongside-mode auditors, this means two spawns for that role (generic + learned), both counted in {N}.

Each writes its report to `.dynos/task-{id}/audit-reports/{auditor}-checkpoint-{timestamp}.json`.

**Note on auditor write capability:** Most auditors are read-only (`Read, Grep, Glob` only — no Write/Edit). When such an auditor returns its report content as text in its message rather than writing the file itself, the orchestrator MUST materialize the file at the expected path using the Write tool. Auditors with Bash access (e.g. `security-auditor`, `code-quality-auditor`) can write their own reports via heredoc; check the agent's tools list before assuming.

**Token & event capture (applies to all events in Steps 3-5):** After each subagent spawn AND each deterministic check, record the event:

**For LLM subagent spawns** (auditors, repair executors):
```bash
PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 "${PLUGIN_HOOKS}/lib_tokens.py" record \
  --task-dir .dynos/task-{id} \
  --agent "{agent_name}" \
  --model "{model_name}" \
  --input-tokens {input_tokens} \
  --output-tokens {output_tokens} \
  --phase audit \
  --stage "AUDITING" \
  --type spawn \
  --detail "{what the agent did}"
```

**For deterministic steps** (router decisions, retrospective computation, repair-log validation):
```bash
PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 "${PLUGIN_HOOKS}/lib_tokens.py" record \
  --task-dir .dynos/task-{id} \
  --agent "{tool_name}" \
  --model "none" \
  --input-tokens 0 \
  --output-tokens 0 \
  --phase audit \
  --stage "AUDITING" \
  --type deterministic \
  --detail "{result summary}"
```

**For repair executor spawns**, use `--phase repair` and include `--segment {segment-id}` if the repair targets a specific segment.

Run this after EVERY event. The hook writes to `.dynos/task-{id}/token-usage.json` with a chronological event log plus aggregated totals. The retrospective's token fields are populated from this file.

**Specific events to record in this skill:**
- Step 3: router audit-plan (type=deterministic, detail="Router decided: spawn X, skip Y")
- Step 3: Each auditor spawn (type=spawn, phase=audit)
- Step 4: repair-log build (type=deterministic, phase=repair)
- Step 4: Each repair executor spawn (type=spawn, phase=repair, include --segment)
- Step 4: ctl check-ownership after repair (type=deterministic, phase=repair)
- Step 4: Re-audit spawns (type=spawn, phase=audit, detail="Re-audit after repair cycle N")
- Step 5: compute_reward / validate_retrospective_scores (type=deterministic, phase=audit, detail="Computed retrospective scores")

**Audit repair gate (deterministic):**

Decide whether audit proceeds to repair or reflect through ctl:

```bash
python3 "${PLUGIN_HOOKS}/ctl.py" run-audit-findings-gate .dynos/task-{id}
```

Use the JSON output as authoritative:
- `status == "repair_required"`: proceed to Step 4
- `status == "clear"`: skip Step 4 and proceed to Step 5

Do NOT re-count blocking findings, infer critical spec failure, or decide repair-vs-reflect from prompt logic.

### Step 4 — Deterministic repair loop

This step runs only when blocking findings exist.

**If `run-audit-findings-gate` returns `status == "clear"`:** skip this step entirely. Proceed to Step 5.

Build the repair queue through ctl:

```bash
python3 "${PLUGIN_HOOKS}/ctl.py" run-audit-repair-cycle-plan .dynos/task-{id}
```

Use this JSON output as authoritative:
- `phase` is the current repair-cycle label (`phase_1`, `phase_2`, or `repair_cycle_N`)
- `repair_cycle` is the exact cycle number the coordinator must write to `repair-log.json`
- `blocking_findings` is the exact finding set to repair now
- each finding's `retry_count` is authoritative
- each finding's optional `model_override` is authoritative
- `critical_spec_finding_ids` must be repaired first
- `transitioned_to_repair_planning == true` means ctl already advanced `CHECKPOINT_AUDIT|FINAL_AUDIT -> REPAIR_PLANNING`

Do NOT collect early findings, late findings, queued findings, or increment retry counts in prompt logic.

Append to log:
```
{timestamp} [REPAIR] {phase} cycle={repair_cycle} findings={list of finding IDs}
```

Build the repair log through ctl — Q-learning assignments are computed inside this command from `repair-cycle-plan.json`; do NOT construct or pipe a separate findings payload:

```text
python3 "${PLUGIN_HOOKS}/ctl.py" run-repair-log-build .dynos/task-{id}
```

This command reads `repair-cycle-plan.json`, calls Q-learning itself, derives `files_to_modify`, writes `.dynos/task-{id}/repair-log.json`, and validates the result. Do NOT ask an LLM to draft `repair-log.json`.

Wait for completion, then finalize repair execution readiness through the control plane:

```text
python3 "${PLUGIN_HOOKS}/ctl.py" run-repair-execution-ready .dynos/task-{id}
```

This command validates `repair-log.json` and advances `REPAIR_PLANNING -> REPAIR_EXECUTION`. Use its JSON output directly.

Build the repair execution groups through ctl:

```text
python3 "${PLUGIN_HOOKS}/ctl.py" run-repair-batch-plan .dynos/task-{id}
```

Use this JSON output as authoritative:
- `execution_groups` is the exact execution order
- groups with `parallel == true` may be spawned concurrently
- groups with `parallel == false` must be serialized
- `model_overrides` inside each batch are authoritative

Do NOT infer parallelism, shared-file conflicts, or execution order from prompt logic.

For each batch, spawn executor agents as assigned in `repair-log.json`:
- `ui-executor`, `backend-executor`, `ml-executor`, `db-executor`, `refactor-executor`, `testing-executor`, `integration-executor`

**Model escalation:** Use the `model_override` values returned by `run-repair-batch-plan` / `repair-log.json`. Do NOT recompute escalation from prose rules.

Each executor receives: the specific finding, the file(s) to fix, and the relevant acceptance criteria text from `spec.md`.

After all batches complete, append to log:
```
{timestamp} [DONE] repair-execution-{phase} — all fixes applied
```

**Phase 1 re-audit (domain-aware, incremental scope):** Build the re-audit plan through ctl:

```bash
python3 "${PLUGIN_HOOKS}/ctl.py" run-audit-reaudit-plan .dynos/task-{id}
```

Use its JSON output as authoritative:
- `modified_files` is the scoped re-audit file set
- `auditors_to_spawn` is the exact auditor list
- `full_scope_auditors` always receive the original broad scope
- `scoped_auditors` receive only `modified_files`

Do NOT hand-compute repair-modified files or choose the re-audit auditor set in prompt logic.

After re-audit, run `run-audit-findings-gate` again.

Update Q-learning outcomes through ctl:

```text
python3 "${PLUGIN_HOOKS}/ctl.py" run-repair-q-update .dynos/task-{id}
```

This command derives outcomes from `repair-log.json` plus the current blocking audit findings and updates the Q-tables deterministically. Do NOT build `outcomes` JSON by hand.

- If `status == "clear"`: proceed to Step 5.
- If `status == "repair_required"`: let the control plane decide whether another repair cycle is legal:

  ```text
  python3 "${PLUGIN_HOOKS}/ctl.py" run-repair-retry .dynos/task-{id}
  ```

  Interpret the JSON result:
  - `status == "repair_retry_ready"`: another repair cycle is allowed. Re-run `run-audit-repair-cycle-plan` and continue with the returned queue.
  - `status == "escalation_required"`: the retry cap blocked another loop. Write `.dynos/task-{id}/escalation.md`, transition to `FAILED`, and stop.

### Step 5 — Gate to DONE

Write the deterministic audit summary:

```bash
python3 "${PLUGIN_HOOKS}/ctl.py" run-audit-summary .dynos/task-{id}
```

This command writes `audit-summary.json` from the on-disk audit reports. Do not aggregate counts by hand.

**Reflect (deterministic reward computation):**

Generate the retrospective through the control plane:

```text
python3 hooks/ctl.py run-audit-reflect .dynos/task-{id}
```

This command computes `task-retrospective.json` and writes the `retrospective` receipt deterministically. Use its JSON output directly.

The written retrospective is complete. Do NOT reopen it to patch `model_used_by_agent`, `agent_source`, `alongside_overlap`, `auditor_zero_finding_streaks`, or `executor_zero_repair_streak` from prompt logic.

**agent_source cross-check:** Retrospective claims of `agent_source[role] = "learned:X"` are cross-checked by `memory/policy_engine.py::_extract_quads` against `.dynos/events.jsonl`. Claims without a matching `learned_agent_applied` event (same `task_id`, same `agent_name`, same `segment_id` — or `segment_id` matching `role.removeprefix("audit-")` for auditor roles) are reclassified to `"generic"` and an `agent_source_reclassified` event is emitted. Auditors must continue to populate `agent_source` honestly — the cross-check is verification, not a substitute for honest reporting. Retrospectives are still accepted when unmatched; only the EMA attribution is downgraded, and the `agent_source_reclassified` events are the audit trail a reviewer can use to spot systemic drift.

Append to log:
```
{timestamp} [DONE] reflect — task-retrospective.json written
```

**Deterministic Postmortem (Step 5a):** Generate the deterministic postmortem report before the LLM analysis so the LLM has access to anomaly detection, recurring patterns, and similar task comparisons.

```bash
PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 "${PLUGIN_HOOKS}/postmortem.py" generate --root . --task-id {task-id}
```

`memory/postmortem.py:generate_postmortem` (called by `postmortem.py generate`) now writes its own receipt internally as part of the same call. You do NOT need to write a receipt from this skill:

- On successful generation it calls `receipt_postmortem_generated(task_dir, postmortem_json_path)`; the writer itself opens the JSON, counts anomalies / recurring_patterns, and hashes both the JSON and its sibling markdown (task-007 B-001 self-compute contract). Writes `.dynos/task-{id}/receipts/postmortem-generated.json`.
- When the deterministic engine determines there is literally nothing to learn, it short-circuits the postmortem write and instead emits `receipt_postmortem_skipped(task_dir, reason, retrospective_sha256, subsumed_by)` with `reason` from the enum `{"clean-task", "no-findings"}` and `subsumed_by` as a required list argument. The decision is deterministic: (a) `reason="clean-task"`, `subsumed_by=[]` when ≥2 auditors ran AND every auditor reported `findings: []`; (b) `reason="no-findings"`, `subsumed_by=[]` when exactly 1 auditor ran AND it reported `findings: []`; (c) in every other case — any auditor reporting ≥1 finding — the engine does NOT skip; it runs the LLM postmortem and writes `receipt_postmortem_generated` instead. There is no skip path for high-quality repairs: any task with findings must run the full LLM postmortem. The `task-retrospective.json` is hashed at receipt-emission time.
- A receipt-write failure is logged as `postmortem_receipt_failed` / `postmortem_skip_receipt_failed` and does NOT corrupt the postmortem files themselves; the script returns its normal result so the audit pipeline keeps moving.

This writes `postmortems/{task-id}.json` and `postmortems/{task-id}.md` to the persistent project directory. Append to log:
```
{timestamp} [DONE] postmortem — anomalies={N}, recurring_patterns={N}
```

**LLM Postmortem Analysis (Step 5b):** After the deterministic postmortem, run LLM-powered failure analysis. This step is skipped for clean tasks (no findings, no repairs, quality >= 0.8).

1. Build the analysis prompt:
```bash
PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 "${PLUGIN_HOOKS}/postmortem_analysis.py" build-prompt .dynos/task-{id}
```

2. If the result has `"has_findings": true`, spawn an **opus** agent with the prompt from the `"prompt"` field. Instruct the agent to respond with ONLY the JSON object described in the prompt — no markdown, no explanation.

3. Parse the agent's JSON response and apply it:
```bash
echo '${AGENT_JSON_OUTPUT}' | PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 "${PLUGIN_HOOKS}/postmortem_analysis.py" apply .dynos/task-{id}
```

This writes `postmortem-analysis.json` to the task dir and merges new prevention rules into `prevention-rules.json`. These rules are automatically included in `project_rules.md` by the policy engine on the next task.

`memory/postmortem_analysis.py:apply_analysis` (called by `postmortem_analysis.py apply`) emits its own receipt internally — do NOT write one from this skill:

- When the agent JSON is empty or non-dict (the orchestrator passed nothing to apply), it emits `receipt_postmortem_skipped(task_dir, "no-findings", retrospective_sha256, subsumed_by=[])`.
- When the sanitized analysis yields zero usable rules, it emits `receipt_postmortem_analysis` internally, passing `analysis_path` and `rules_path` as keyword file-path arguments; hashes are derived from the on-disk files — no caller-supplied hashes are accepted. The receipt records `rules_added=0` and the current on-disk hash of `prevention-rules.json` (or 64 zeros when the file does not exist).
- When the merge appends rules, the same receipt is emitted AFTER the fcntl lock is released so the recorded hash captures the on-disk state visible to other readers. Do not call `receipt_postmortem_analysis` directly from this skill — it is emitted internally by `postmortem_analysis.py apply`.

Append to log:
```
{timestamp} [DONE] postmortem-analysis — {N} prevention rules added
```

If `has_findings` is false, skip this step and append:
```
{timestamp} [SKIP] postmortem-analysis — clean task, nothing to analyze
```

**Post-completion processing:** Improve, policy engine, dashboard, and registry refresh are handled automatically by the `task-completed` hook via the event bus. Do not run them inline. The hook fires after this skill completes and the task reaches DONE.

Finalize completion through ctl:

```text
python3 "${PLUGIN_HOOKS}/ctl.py" run-audit-finish .dynos/task-{id}
```

This command writes `completion.json` and advances `CHECKPOINT_AUDIT|FINAL_AUDIT -> DONE` deterministically. Do NOT call `transition_task(...)` directly from prompt logic.

Print (listing only auditors that were actually spawned, not skipped):
```
Audit complete — ALL PASSED

  {auditor-name}:  PASS
  ... (one line per spawned auditor)

Task complete. Snapshot branch dynos/task-{id}-snapshot can be deleted if desired.
```

---

## Standalone use (no active task)

If no active task is found, run the 5 universal auditors on `git diff --name-only HEAD`. Skip Step 5 (no DONE state to write). Print results and stop.
