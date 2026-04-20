---
name: audit
description: "Run checkpoint audit, repair any findings, then reach DONE — all in one shot. Use after /dynos-work:execute."
---

# dynos-work: Audit

Runs the full audit-to-done pipeline: audit → repair loop → DONE.

## What you do

### Step 0 — Contract validation

After finding the active task, validate that all required inputs from the execute skill are present:

```text
python3 hooks/dynosctl.py validate-contract --skill audit --task-dir .dynos/task-{id}
```

If validation fails with missing required inputs (evidence files, snapshot SHA), print the errors and stop.

### Step 1 — Find active task

Find the most recent active task in `.dynos/`. Read `manifest.json`.

Verify stage is `CHECKPOINT_AUDIT`. If not, print the current stage and what command to run instead.

### Step 2 — Diff scope

Run `git diff --name-only {snapshot.head_sha}` to get all files changed by this task. Pass this list to every auditor. Auditors only inspect these files.

If no snapshot exists (standalone audit), use `git diff --name-only HEAD`.

### Step 3 — Audit (conditional auditor spawning with skip optimization)

Update `manifest.json` stage to `CHECKPOINT_AUDIT`.

**Determine which auditors to spawn (DETERMINISTIC ROUTER):**

Read `manifest.json` for `classification` and `fast_track`. Then run the deterministic router:

```bash
PYTHONPATH="{{HOOKS_PATH}}:${PYTHONPATH:-}" python3 "{{HOOKS_PATH}}/dynorouter.py" audit-plan --root . --task-type {task_type} --domains {comma-separated-domains} [--fast-track]
```

This returns a JSON plan with each auditor's action (spawn/skip), model, and route mode. **Use this plan directly.** Do not re-derive model, skip, or routing decisions from markdown tables.

For each auditor in the plan:
- If `action: "skip"`: log `{timestamp} [SKIP] {name} — {reason}` and do not spawn
- If `action: "spawn"`: spawn with the specified `model` (null = default)
- Log: `{timestamp} [ROUTE] {name} model={model} route={route_mode} source={route_source}`

**Learned Auditor Injection (MANDATORY — deterministic via router):** Build the auditor's spawn prompt with `dynorouter.py audit-inject-prompt`. Pipe the base prompt over stdin and capture stdout as the prompt you pass to the Agent tool — do NOT read the learned agent file yourself or build the prompt by hand. The router does the frontmatter stripping, applies the learned-auditor block under the literal heading `## Learned Auditor Instructions`, computes the SHA-256 of the exact bytes it prints, and atomically writes the per-model sidecar at `.dynos/task-{id}/receipts/_injected-auditor-prompts/{auditor_name}-{model_used}.sha256` (with a companion `.txt` of the same bytes; when no model is specified the literal `default` is substituted in the filename).

```bash
echo "{base prompt for this auditor}" | PYTHONPATH="{{HOOKS_PATH}}:${PYTHONPATH:-}" python3 "{{HOOKS_PATH}}/dynorouter.py" audit-inject-prompt \
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
python3 hooks/dynosctl.py audit-receipt .dynos/task-{id} {auditor_name} \
  --model {model_used} \
  --report-path .dynos/task-{id}/audit-reports/{report_filename}.json \
  --tokens-used {tokens_used} \
  --route-mode {route_mode} \
  --agent-path {agent_path} \
  --injected-agent-sha256 "${INJECTED_AGENT_SHA256}"
```

`python3 hooks/dynosctl.py audit-receipt ...` calls `receipt_audit_done(...)`, which re-asserts the same sidecar exists at that exact path and that its contents match `injected_agent_sha256`. A mismatch raises `ValueError`. For `route_mode == "generic"` (no learned agent) the sidecar assertion is skipped and `injected_agent_sha256` may be `None`; `route_mode` and `agent_path` are still required keyword arguments. The wrapper derives counts from `--report-path`; when no report exists it writes literal zero findings only. The `receipt_audit_routing` writer also enforces these fields per-entry, so any auditor entry missing `injected_agent_sha256` (when non-generic) or `agent_path` will hard-fail at the routing-receipt write.

The router handles fast-track reduction, skip policy, model policy, security floor enforcement, ensemble voting triggers, and learned agent routing in deterministic code. No prompt interpretation needed for these decisions. Do not re-derive skip thresholds, model assignments, or routing modes from markdown tables or retrospective files.

**Ensemble Voting:** If the router plan has `"ensemble": true` for an auditor, follow this protocol instead of a single spawn:

1. Spawn two auditors in parallel using the models listed in `ensemble_voting_models` (e.g., haiku and sonnet)
2. If both return zero findings: the audit passes for this auditor. Log: `{timestamp} [VOTE] {name} — PASS (both models agree: zero findings)`
3. If either returns findings: discard both voting results and escalate by spawning with `ensemble_escalation_model` (opus). The escalation result is final and binding. Log: `{timestamp} [VOTE] {name} — Escalating to {escalation_model}`

If `"ensemble": false`, spawn normally with the single model from the plan.

**Visual Audit Pass:** For tasks where `domains` includes `"ui"`, run a visual audit: start the dev server, use a browser subagent to screenshot modified screens, then evaluate with Claude 3.5 Sonnet against the planning-phase Design Decisions. Report visual findings as category `vision-finding`. Log: `{timestamp} [VISION] UI audit complete -- {N} visual bugs found`.

**Alongside mode deduplication:** When generic and learned auditors both produce findings for the same role, deduplicate by key `{file}:{line}:{category}`. Count duplicates once, preferring the learned version. The deduplicated set feeds into repair and retrospective counts. Track whether learned findings are a superset of generic findings (for promotion decisions by the learn step).

Append to log (the `[STAGE] → CHECKPOINT_AUDIT` line is auto-written by `transition_task`; the skill writes only the `[SPAWN]` line):
```
{timestamp} [SPAWN] {N} auditors in parallel ({list of names})
```

Spawn the determined auditors simultaneously, passing the resolved model for each auditor in the subagent spawn configuration. For alongside-mode auditors, this means two spawns for that role (generic + learned), both counted in {N}.

Each writes its report to `.dynos/task-{id}/audit-reports/{auditor}-checkpoint-{timestamp}.json`.

**Token & event capture (applies to all events in Steps 3-5):** After each subagent spawn AND each deterministic check, record the event:

**For LLM subagent spawns** (auditors, repair-coordinator, repair executors):
```bash
PYTHONPATH="{{HOOKS_PATH}}:${PYTHONPATH:-}" python3 "{{HOOKS_PATH}}/dynoslib_tokens.py" record \
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
PYTHONPATH="{{HOOKS_PATH}}:${PYTHONPATH:-}" python3 "{{HOOKS_PATH}}/dynoslib_tokens.py" record \
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
- Step 3: dynorouter audit-plan (type=deterministic, detail="Router decided: spawn X, skip Y")
- Step 3: Each auditor spawn (type=spawn, phase=audit)
- Step 4: repair-coordinator spawn (type=spawn, phase=repair)
- Step 4: Each repair executor spawn (type=spawn, phase=repair, include --segment)
- Step 4: dynosctl check-ownership after repair (type=deterministic, phase=repair)
- Step 4: Re-audit spawns (type=spawn, phase=audit, detail="Re-audit after repair cycle N")
- Step 5: compute_reward / validate_retrospective_scores (type=deterministic, phase=audit, detail="Computed retrospective scores")

**Eager two-phase repair trigger:**

Do not wait for all auditors to complete before proceeding. Instead, monitor auditor results as they arrive:

1. When the first auditor returns with one or more blocking findings, immediately proceed to Step 4 phase 1 repair with those findings. Do not wait for remaining auditors.
2. **Short-circuit on critical spec failure:** If `spec-completion-auditor` returns findings with severity `critical`, immediately proceed to Step 4 phase 1 repair with those critical findings, even if other auditors have not returned yet. This is a specific instance of the eager repair trigger.
3. If no auditor returns blocking findings after all auditors complete, append to log and proceed to Step 5:
   ```
   {timestamp} [DONE] audit complete — no blocking findings
   ```

Record which auditors are still running at the time phase 1 repair begins. Their results are **late findings** and feed into phase 2 (see Step 4).

### Step 4 — Two-phase repair loop

This step runs only when blocking findings exist. It uses a two-phase model: phase 1 repairs early findings (from the first auditor(s) to return), phase 2 repairs late findings (from auditors that completed after phase 1 began) plus any new findings from phase 1 re-audit.

**If no blocking findings from any auditor:** skip this step entirely. Proceed to Step 5.

**Phase 1 — Early findings repair:**

Collect all blocking findings available at the time of the eager trigger (Step 3). These are the phase 1 findings.

Update stage to `REPAIR_PLANNING` (transition_task auto-writes the `[STAGE] → REPAIR_PLANNING` log line). Append to log only the REPAIR-P1 line:
```
{timestamp} [REPAIR-P1] {N} findings — {list of finding IDs}
```

**Q-learning repair plan (deterministic):** Before spawning the repair coordinator, get executor and model assignments from the Q-learning planner:

```bash
echo '{"findings": [{finding objects}]}' | python3 "{{HOOKS_PATH}}/dynosctl.py" repair-plan --root . --task-type {task_type}
```

If the response has `"source": "q-learning"`, pass the assignments to the repair coordinator as constraints: "Use these executor and model assignments for the listed findings." The coordinator still decides batch ordering, instructions, and file lists — but executor and model choices come from the Q-table.

If `"source": "default"` (Q-learning disabled), the repair coordinator uses its own heuristic rules as before.

Log: `{timestamp} [REPAIR-PLAN] source={source} assignments={N}`

Spawn `repair-coordinator` agent with instruction: "Read the provided audit reports. Produce a repair plan for the given findings. Assign each finding to an executor. For each repair task, list the files that will be modified. Write to `.dynos/task-{id}/repair-log.json`."

Wait for completion. Update stage to `REPAIR_EXECUTION` (transition_task auto-writes the `[STAGE] → REPAIR_EXECUTION` log line — the skill does not manually append it).

**Parallel batch spawning:**

Read `repair-log.json` batches. Spawn batches where `parallel: true` concurrently. Batches where `parallel: false` are serialized (wait for preceding batches with shared files to complete first).

Append to log when spawning parallel batches:
```
{timestamp} [SPAWN] repair batch-1, batch-3 in parallel
{timestamp} [SPAWN] repair batch-2 — waiting, shares files with batch-1
```

For each batch, spawn executor agents as assigned in `repair-log.json`:
- `ui-executor`, `backend-executor`, `ml-executor`, `db-executor`, `refactor-executor`, `testing-executor`, `integration-executor`

**Model escalation:** For findings with `retry_count >= 2`, spawn with `model_override: "opus"`. Log: `{timestamp} [ESCALATE] finding {finding-id} retry {N} -> opus`.

Each executor receives: the specific finding, the file(s) to fix, and the relevant acceptance criteria text from `spec.md`.

After all batches complete, append to log:
```
{timestamp} [DONE] repair-execution-p1 — all phase 1 fixes applied
```

**Late-finding conflict resolution:** All findings from auditors that complete while phase 1 is running are queued for phase 2, regardless of file overlap.

**Q-learning update (after phase 1 repair):** After executors complete but before re-audit, build outcomes from the repair results and update Q-tables:

```bash
echo '{"outcomes": [{outcome objects}]}' | python3 "{{HOOKS_PATH}}/dynosctl.py" repair-update --root . --task-type {task_type}
```

Each outcome includes: finding_id, state (from repair-plan), executor, model, resolved (from re-audit), new_findings count, tokens_used. Set `next_state` to the encoded state for the next retry cycle if unresolved, or `null` if resolved/terminal.

This is a no-op if Q-learning is disabled. Log: `{timestamp} [Q-UPDATE] {N} outcomes, avg reward={avg}`

**Phase 1 re-audit (domain-aware, incremental scope):** Compute the repair-modified file list by unioning `repair-log.json` task file lists with `git diff --name-only` against the pre-repair commit. Scope re-audit to only these files (not the full Step 2 diff). Exception: `spec-completion-auditor` always gets the original full diff scope. All re-audit auditors receive previous reports for context. Spawn only: `spec-completion-auditor` (full scope), `security-auditor` (repair files), and auditors whose findings appear in this phase's repair-log (repair files). Skip domain-unrelated auditors. New findings from re-audit are added to the phase 2 queue.

**Phase 2 — Late findings and re-audit findings repair:**

Collect all queued findings: late auditor findings plus any new findings from phase 1 re-audit. If no queued findings exist, skip phase 2.

If queued findings exist, append to log:
```
{timestamp} [REPAIR-P2] {N} findings — {list of finding IDs}
```

Spawn `repair-coordinator` with the phase 2 findings. The coordinator writes to `repair-log.json` with an incremented `repair_cycle` value (phase 2 overwrites with the new cycle).

Apply the same parallel batch spawning and model escalation logic as phase 1.

**Retry count continuity:** Retry counts are continuous across phases. A finding first repaired in phase 1 at retry 1 and re-found in phase 2 is at retry 2, not retry 0. The `max_retries` limit (3) applies across both phases combined for a given finding.

After phase 2 repairs complete, run a phase 2 re-audit using the same domain-aware incremental scope logic as phase 1 re-audit (scoped to phase 2 repair-modified files).

- If all clear after phase 2 re-audit: append `{timestamp} [DONE] repair-execution-p2 — all phase 2 fixes applied` to log. Proceed to Step 5.
- If new findings remain: increment `retry_count` for each finding. If any finding has exceeded 3 retries, set stage to `FAILED`, append `[FAILED] max retries exceeded for: {finding-ids}`, and stop. Otherwise loop back into another repair cycle (continuing phase 2).

**Degenerate cases:** If no late auditors exist, phase 2 only contains re-audit findings (skip if none). If no blocking findings from any auditor, both phases are skipped entirely.

### Step 5 — Gate to DONE

Read all audit reports. Write `audit-summary.json`.

**Reflect (deterministic reward computation):**

Before writing `completion.json`, generate `task-retrospective.json` using the deterministic reward calculator:

```bash
python3 "{{HOOKS_PATH}}/dynosctl.py" compute-reward .dynos/task-{id} --write
```

This reads audit reports, repair-log, token-usage, execution-log, and manifest, then computes quality_score, cost_score, and efficiency_score deterministically. The model does NOT compute these scores — the Python runtime does.

After the command writes the retrospective, the model adds the following fields that require model judgment (not arithmetic):
   - `model_used_by_agent`: map agent name to actual model used at spawn time (null if unavailable).
   - `agent_source`: map agent name to routing source: `"generic"`, `"learned:{prompt-name}"`, or for alongside mode both `"{name}": "generic"` and `"{name}:learned": "learned:{prompt-name}"`.
   - `alongside_overlap`: for alongside-mode auditors, compare generic vs learned finding sets using dedup keys `{file}:{line}:{category}`. Record per-auditor: `generic_finding_keys`, `learned_finding_keys`, `learned_is_superset` (boolean), `alongside_task_count`. Empty `{}` if no alongside auditors ran.
   - `auditor_zero_finding_streaks`: per-auditor streaks from prior retrospective. Spawned + zero findings = increment; spawned + findings = reset to 0; skipped = carry forward.
   - `executor_zero_repair_streak`: consecutive most-recent executor segments with zero repair tasks.

Read the written `task-retrospective.json`, merge these fields into it, and write it back.
10. Write `.dynos/task-{id}/task-retrospective.json` as a flat JSON object (no nesting beyond one level):

```json
{
  "task_id": "task-{id}",
  "task_outcome": "DONE",
  "task_type": "feature",
  "task_domains": "ui,backend",
  "task_risk_level": "medium",
  "findings_by_auditor": { "security-auditor": 2, "code-quality-auditor": 1 },
  "findings_by_category": { "sec": 2, "cq": 1 },
  "executor_repair_frequency": { "backend-executor": 2 },
  "spec_review_iterations": 1,
  "repair_cycle_count": 0,
  "subagent_spawn_count": 12,
  "wasted_spawns": 2,
  "auditor_zero_finding_streaks": { "security-auditor": 0, "spec-completion-auditor": 1, "code-quality-auditor": 3, "dead-code-auditor": 5, "ui-auditor": 2 },
  "executor_zero_repair_streak": 3,
  "token_usage_by_agent": { "security-auditor": 45000, "code-quality-auditor": 38000, "spec-completion-auditor": 52000, "repair-coordinator": 15000, "backend-executor": 62000 },
  "total_token_usage": 212000,
  "model_used_by_agent": { "security-auditor": "opus", "code-quality-auditor": "sonnet", "spec-completion-auditor": "sonnet", "repair-coordinator": "sonnet", "backend-executor": "opus" },
  "agent_source": { "security-auditor": "generic", "code-quality-auditor": "learned:cq-v2", "code-quality-auditor:learned": "learned:cq-v2", "spec-completion-auditor": "generic" },
  "alongside_overlap": { "code-quality-auditor": { "generic_finding_keys": ["src/main.ts:10:cq"], "learned_finding_keys": ["src/main.ts:10:cq", "src/util.ts:3:cq"], "learned_is_superset": true, "alongside_task_count": 2 } },
  "quality_score": 0.67,
  "cost_score": 0.79,
  "efficiency_score": 1.0
}
```

If no audit reports or repair logs exist, write the same structure with zeroed-out counts (empty objects `{}`, zeros, and `1.0` for scores). Old retrospectives missing newer fields (`token_usage_by_agent`, `total_token_usage`, `model_used_by_agent`, `agent_source`, `alongside_overlap`, `quality_score`, `cost_score`, `efficiency_score`) are treated as `null`, not errors.

Append to log:
```
{timestamp} [DONE] reflect — task-retrospective.json written
```

**Postmortem skip decision (deterministic):** Before the LLM postmortem runs, the deterministic engine decides whether a skip receipt may be written. The decision is:

- ≥2 auditors ran AND every auditor reported `findings: []` → emit `receipt_postmortem_skipped(task_dir, reason="clean-task", retrospective_sha256=..., subsumed_by=[])`.
- Exactly 1 auditor ran AND it reported `findings: []` → emit `receipt_postmortem_skipped(task_dir, reason="no-findings", retrospective_sha256=..., subsumed_by=[])`.
- Any auditor reported ≥1 finding → do NOT skip; run the LLM postmortem and write `receipt_postmortem_generated(...)`.

If ANY auditor reports ≥1 finding (blocking or not), the LLM postmortem runs — no "quality-above-threshold" bypass. The `subsumed_by` argument is required on every skip receipt write; the empty list `[]` is valid only for `reason="clean-task"` or `reason="no-findings"`.

**Post-completion processing:** Learn, trajectory rebuild, evolve, postmortems, and dashboard refresh are handled automatically by the `task-completed` hook via the event bus. Do not run them inline. The hook fires after this skill completes and the task reaches DONE.

Write `completion.json`. Transition the task to `DONE` by calling `transition_task(task_dir, "DONE")` from `dynoslib.py` (this sets both `stage` and `completion_at`). Manual manifest editing is forbidden and will break the receipt chain and retrospective flush. Append to log:
```
{timestamp} [ADVANCE] CHECKPOINT_AUDIT → DONE
```
Print (listing only auditors that were actually spawned, not skipped):
```
Audit complete — ALL PASSED

  {auditor-name}:  PASS
  ... (one line per spawned auditor)

Task complete. Snapshot branch dynos/task-{id}-snapshot can be deleted if desired.
```

---

## Standalone use (no active task)

If no active task is found, run the 4 universal auditors on `git diff --name-only HEAD`. Skip Step 5 (no DONE state to write). Print results and stop.
