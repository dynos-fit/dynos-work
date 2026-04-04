---
name: audit
description: "Power user: Run checkpoint audit, repair any findings, then reach DONE — all in one shot. Use after /dynos-work:execute."
---

# dynos-work: Audit

Runs the full audit-to-done pipeline: audit → repair loop → DONE.

## What you do

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
PYTHONPATH="${PLUGIN_HOOKS}:${PYTHONPATH:-}" python3 "${PLUGIN_HOOKS}/dynorouter.py" audit-plan --root . --task-type {task_type} --domains {comma-separated-domains} [--fast-track]
```

This returns a JSON plan with each auditor's action (spawn/skip), model, and route mode. **Use this plan directly.** Do not re-derive model, skip, or routing decisions from markdown tables.

For each auditor in the plan:
- If `action: "skip"`: log `{timestamp} [SKIP] {name} — {reason}` and do not spawn
- If `action: "spawn"`: spawn with the specified `model` (null = default), using `agent_path` if `route_mode` is "learned" or "alongside"
- Log: `{timestamp} [ROUTE] {name} model={model} route={route_mode} source={route_source}`

The router handles fast-track reduction, skip policy, model policy, security floor enforcement, ensemble voting triggers, and learned agent routing in deterministic code. No prompt interpretation needed for these decisions. Do not re-derive skip thresholds, model assignments, or routing modes from markdown tables or retrospective files.

**Ensemble Voting for High-Risk Audits:** For `security-auditor` and `db-schema-auditor`, the router marks them for ensemble voting. Spawn two cheaper models (Haiku + Sonnet) in parallel first. If both return zero findings, the audit passes (`[VOTE] ... PASS`). If either returns findings, discard voting results and escalate to Opus (`[VOTE] ... Escalating to Opus`). The Opus result is final and binding.

**Visual Audit Pass:** For tasks where `domains` includes `"ui"`, run a visual audit: start the dev server, use a browser subagent to screenshot modified screens, then evaluate with Claude 3.5 Sonnet against the planning-phase Design Decisions. Report visual findings as category `vision-finding`. Log: `{timestamp} [VISION] UI audit complete -- {N} visual bugs found`.

**Alongside mode deduplication:** When generic and learned auditors both produce findings for the same role, deduplicate by key `{file}:{line}:{category}`. Count duplicates once, preferring the learned version. The deduplicated set feeds into repair and retrospective counts. Track whether learned findings are a superset of generic findings (for promotion decisions by the learn step).

Append to log:
```
{timestamp} [STAGE] → CHECKPOINT_AUDIT
{timestamp} [SPAWN] {N} auditors in parallel ({list of names})
```

Spawn the determined auditors simultaneously, passing the resolved model for each auditor in the subagent spawn configuration. For alongside-mode auditors, this means two spawns for that role (generic + learned), both counted in {N}.

Each writes its report to `.dynos/task-{id}/audit-reports/{auditor}-checkpoint-{timestamp}.json`.

**Token capture (applies to all subagent spawns in Steps 3-4):** After each subagent completes, record `total_tokens` from the Agent tool result as `{agent-name: token_count}`. If unavailable, record `null` and exclude from sums. For ensemble voting, sum all voting and escalation spawns. For agents spawned multiple times, sum their counts.

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

Update stage to `REPAIR_PLANNING`. Append to log:
```
{timestamp} [REPAIR-P1] {N} findings — {list of finding IDs}
{timestamp} [STAGE] → REPAIR_PLANNING
```

Spawn `repair-coordinator` agent with instruction: "Read the provided audit reports. Produce a repair plan for the given findings. Assign each finding to an executor. For each repair task, list the files that will be modified. Write to `.dynos/task-{id}/repair-log.json`."

Wait for completion. Update stage to `REPAIR_EXECUTION`. Append to log:
```
{timestamp} [STAGE] → REPAIR_EXECUTION
```

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

**Reflect (inline -- no subagent):**

Before writing `completion.json`, generate `task-retrospective.json` in the task directory. This runs inline as part of the DONE gate.

1. Scan `.dynos/task-{id}/audit-reports/*.json`. For each file, attempt to parse it as JSON. If parsing fails or the file is unreadable, skip it silently.
2. From successfully parsed audit reports, extract:
   - **Finding counts by auditor:** For each report, count the number of entries in the `findings` array. Key by `auditor_name`.
   - **Finding counts by category:** For each finding, use the finding ID prefix (the part before the hyphen-number, e.g. `sec` from `sec-001`) as the category. Count findings per category.
3. If `.dynos/task-{id}/repair-log.json` exists and is valid JSON:
   - **Executor repair frequency:** Iterate all tasks across all batches. Count how many repair tasks were assigned to each `assigned_executor` value.
   - **Repair cycle count:** Read the `repair_cycle` field (integer).
   - If the file is missing or malformed, set executor repair frequency to `{}` and repair cycle count to `0`.
4. Scan `.dynos/task-{id}/execution-log.md`. Count lines matching the pattern `[HUMAN] SPEC_REVIEW`. This is the **spec review iteration count**. If the file is missing, set to `0`.
5. Read `manifest.json`. Flatten the `classification` object into scalar fields: `task_type` (string), `task_domains` (comma-separated string, e.g. "ui,backend"), `task_risk_level` (string). Set **task outcome** to `DONE` (this gate only runs for successful completion).
6. Compute token and model tracking:
   - `token_usage_by_agent`: map agent name to total token count from Steps 3-4 (null if unavailable).
   - `total_token_usage`: sum of non-null values (0 if all null).
   - `model_used_by_agent`: map agent name to actual model used at spawn time (null if unavailable).
   - `agent_source`: map agent name to routing source: `"generic"`, `"learned:{prompt-name}"`, or for alongside mode both `"{name}": "generic"` and `"{name}:learned": "learned:{prompt-name}"`.
7. Compute alongside overlap (only for alongside-mode auditors):
   - Compare generic vs learned finding sets using dedup keys `{file}:{line}:{category}`.
   - Record per-auditor: `generic_finding_keys`, `learned_finding_keys`, `learned_is_superset` (boolean), `alongside_task_count` (incremented from prior retrospective, or 1).
   - Empty `{}` if no alongside auditors ran.
8. Compute spawn/waste tracking:
   - `subagent_spawn_count`: count `[SPAWN]` lines in execution-log.md (0 if missing).
   - `wasted_spawns`: count auditor reports with empty findings arrays.
   - `auditor_zero_finding_streaks`: per-auditor streaks from prior retrospective. Spawned + zero findings = increment; spawned + findings = reset to 0; skipped = carry forward. If prior retro missing or uses old format, treat prior streaks as 0.
   - `executor_zero_repair_streak`: consecutive most-recent executor segments with zero repair tasks (0 if repair-log missing).
9. Compute reward vector (each score clamped to `[0, 1]`):
   - `quality_score`: `1 - (surviving_findings / total_findings)`. If total is 0, use `0.9` (not 1.0 -- zero findings may indicate auditor gaps).
   - `cost_score`: `1 / (1 + (avg_tokens_per_spawn / budget))` where budget varies by risk (low=8K, med=12K, high=18K, critical=25K). `1.0` if no tokens/spawns. Apply `-0.05` penalty if all agents are generic (cold-start).
   - `efficiency_score`: `1 - (repair_cycles / 3) - (max(0, spec_reviews - 1) * 0.1)`.
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

**Learn (inline -- no subagent):** After writing the retrospective, aggregate all `.dynos/task-*/task-retrospective.json` files following `skills/learn/SKILL.md`. Write `dynos_patterns.md` to the project memory directory. Skip silently if no retrospectives found. Log: `{timestamp} [DONE] learn — dynos_patterns.md updated ({N} tasks aggregated)`.

**Trajectory rebuild (inline):**

```bash
python3 "${PLUGIN_HOOKS}/dynostrajectory.py" rebuild --root "${PROJECT_ROOT}"
```

Postmortems and improvement cycles are NOT run per-task. They accumulate and run in batch during the next maintenance cycle (via the background daemon or manual `dynomaintain.py run-once`). This keeps each task completion fast and batches improvements across multiple tasks for better signal.

Write `completion.json`. Transition the task to `DONE` by calling `transition_task(task_dir, "DONE")` from `dynoslib.py` (this sets both `stage` and `completion_at`). If calling the function directly is not possible, manually set both `"stage": "DONE"` and `"completion_at": "{ISO timestamp}"` in `manifest.json`. Append to log:
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
