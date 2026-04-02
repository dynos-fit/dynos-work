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

**Determine which auditors to spawn:**

Read `manifest.json` `classification` field and `execution-graph.json`.

**Always spawn (universal, skip-exempt auditors):**
- `spec-completion-auditor`
- `security-auditor`
- `code-quality-auditor`

**Always spawn (skip-eligible universal auditor):**
- `dead-code-auditor`

**Conditionally spawn (skip-eligible):**
- `ui-auditor` — spawn only if classification `domains` includes `"ui"` OR any segment in `execution-graph.json` has `executor` value `"ui-executor"`
- `db-schema-auditor` — spawn only if classification `domains` includes `"db"` OR any segment in `execution-graph.json` has `executor` value `"db-executor"`

**When classification is null or missing:** spawn only the 4 universal auditors. Skip `ui-auditor` and `db-schema-auditor`.

**Auditor skip on zero-finding streak:**

Before spawning, check whether any skip-eligible auditors should be skipped based on their zero-finding streak from prior tasks.

1. Find the most recent completed task directory (not the current task). Look for `.dynos/task-*/task-retrospective.json` files, sort by task ID descending, and select the first one that is not the current task.
2. Read the `auditor_zero_finding_streaks` field (object mapping auditor name to streak count). If the field is missing, the file does not exist, or the file contains the old single-integer `auditor_zero_finding_streak` format instead, treat all streaks as `0` (skip no one).
3. For each skip-eligible auditor (`dead-code-auditor`, `ui-auditor`, `db-schema-auditor`): if its streak value in `auditor_zero_finding_streaks` is `>= 3`, do not spawn it. Append to log:
   ```
   {timestamp} [SKIP] {auditor-name} — zero-finding streak {N}
   ```
4. Skip-exempt auditors (`security-auditor`, `spec-completion-auditor`, `code-quality-auditor`) are never skipped regardless of streak.

Append to log:
```
{timestamp} [STAGE] → CHECKPOINT_AUDIT
{timestamp} [SPAWN] {N} auditors in parallel ({list of names})
```

Spawn the determined auditors simultaneously.

Each writes its report to `.dynos/task-{id}/audit-reports/{auditor}-checkpoint-{timestamp}.json`.

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

**Model escalation:** For each finding where `retry_count >= 2`, spawn the executor with `model_override: "opus"` in the subagent spawn configuration. Findings at retry 0 or 1 use the default model. This is a per-finding model routing change, not an instruction-text change. Two findings in the same batch may have different retry counts and therefore different model assignments. Append to log:
```
{timestamp} [ESCALATE] finding {finding-id} retry {N} -> opus
```

Each executor receives: the specific finding, the file(s) to fix, and the relevant acceptance criteria text from `spec.md`.

After all batches complete, append to log:
```
{timestamp} [DONE] repair-execution-p1 — all phase 1 fixes applied
```

**Late-finding conflict resolution:**

While phase 1 repair is running, remaining auditors from Step 3 may complete. Collect their results:

1. If a late auditor reports findings in files currently being repaired by phase 1, those findings are **not** applied to phase 1. They are queued for phase 2.
2. If a late auditor reports findings in files **not** being repaired by phase 1, those findings are also queued for phase 2.
3. All late findings go to phase 2, regardless of file overlap.

**Phase 1 re-audit (domain-aware, incremental scope):**

Before spawning re-audit auditors, determine which files were modified during phase 1 repair:
1. Read `repair-log.json` task entries to collect the list of files each executor was assigned to modify.
2. Additionally run `git diff --name-only` against the pre-repair commit (the commit hash recorded before repair execution began) to catch any files modified that were not explicitly listed.
3. Union these two sets into the **repair-modified file list**.

Scope re-audit auditors to only the repair-modified file list -- NOT the full original diff scope from Step 2. This avoids redundantly re-auditing files that were already clean.

**Exception:** `spec-completion-auditor` always receives the **original full diff scope** (from Step 2), since spec completion checks overall requirement coverage across the entire task, not file-level issues.

All re-audit auditors still receive the previous audit reports for context (so they know what was originally found and what to verify as fixed).

Spawn only:
- `spec-completion-auditor` (always, full original diff scope)
- `security-auditor` (always, repair-modified files only)
- Any auditor whose findings appear as tasks in the current phase's `repair-log.json` (repair-modified files only)
- Skip auditors whose domain was not touched by the repair (e.g., if only backend files were repaired, skip `ui-auditor` even if it ran in the initial audit)

Wait for results. Any new findings from re-audit are added to the phase 2 queue.

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

**Degenerate cases:**

- If no late auditors exist (all auditors completed before phase 1 began, or all skippable auditors were skipped), phase 2 only contains re-audit findings (if any). If no re-audit findings exist, phase 2 is skipped entirely.
- If no blocking findings exist from any auditor, both phases are skipped and behavior is identical to the previous pipeline (proceed directly to Step 5).

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
6. Compute spawn/waste tracking fields:
   - `subagent_spawn_count`: Scan `.dynos/task-{id}/execution-log.md`. Count all lines containing `[SPAWN]`. If the file is missing, set to `0`.
   - `wasted_spawns`: Count auditor runs that produced zero findings. For each parsed audit report from step 1, if the `findings` array is empty (length 0), increment this counter.
   - `auditor_zero_finding_streaks`: Compute per-auditor zero-finding streaks. Read the most recent prior task's `task-retrospective.json` (not the current task) to get the previous streak values. If it does not exist, or uses the old single-integer `auditor_zero_finding_streak` format, treat all prior streaks as `0`.
     - For each auditor that was **spawned** in this audit cycle and produced **zero findings**: increment its streak from the prior retrospective value by 1.
     - For each auditor that was **spawned** and produced **one or more findings**: reset its streak to `0`.
     - For auditors that were **skipped** (not spawned) in this audit cycle: carry forward their streak value unchanged from the prior retrospective.
   - `executor_zero_repair_streak`: Read `repair-log.json`. Sort executor segments by execution order. Starting from the most recent, count consecutive executor segments that needed zero repairs (i.e., were not assigned any repair tasks). Stop counting at the first segment that had repairs. If `repair-log.json` is missing or malformed, set to `0`.
7. Write `.dynos/task-{id}/task-retrospective.json` as a flat JSON object (no nesting beyond one level):

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
  "executor_zero_repair_streak": 3
}
```

If no audit reports or repair logs exist, write the file with zeroed-out counts:

```json
{
  "task_id": "task-{id}",
  "task_outcome": "DONE",
  "task_type": "feature",
  "task_domains": "backend",
  "task_risk_level": "low",
  "findings_by_auditor": {},
  "findings_by_category": {},
  "executor_repair_frequency": {},
  "spec_review_iterations": 0,
  "repair_cycle_count": 0,
  "subagent_spawn_count": 0,
  "wasted_spawns": 0,
  "auditor_zero_finding_streaks": {},
  "executor_zero_repair_streak": 0
}
```

Append to log:
```
{timestamp} [DONE] reflect — task-retrospective.json written
```

**Learn (inline -- no subagent):**

After writing the retrospective, automatically aggregate all retrospectives in the project into the memory file. Follow the steps in `skills/learn/SKILL.md`:

1. Scan all `.dynos/task-*/task-retrospective.json` files in the current working directory.
2. Aggregate: top 5 finding categories, executor reliability rankings, average repair cycles by task type, prevention rules (from finding descriptions), and spawn efficiency metrics.
3. Determine project memory path (`~/.claude/projects/<project>/memory/` where `<project>` is derived from the cwd by replacing `/` with `-`).
4. Write (or overwrite) `dynos_patterns.md` to the project memory directory using the format defined in `skills/learn/SKILL.md`.

If no retrospective files are found (edge case), skip silently.

Append to log:
```
{timestamp} [DONE] learn — dynos_patterns.md updated ({N} tasks aggregated)
```

Write `completion.json`. Update stage to `DONE`. Append to log:
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
