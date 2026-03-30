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

### Step 3 — Audit (conditional auditor spawning)

Update `manifest.json` stage to `CHECKPOINT_AUDIT`.

**Determine which auditors to spawn:**

Read `manifest.json` `classification` field and `execution-graph.json`.

**Always spawn (universal auditors):**
- `spec-completion-auditor`
- `security-auditor`
- `code-quality-auditor`
- `dead-code-auditor`

**Conditionally spawn:**
- `ui-auditor` — spawn only if classification `domains` includes `"ui"` OR any segment in `execution-graph.json` has `executor` value `"ui-executor"`
- `db-schema-auditor` — spawn only if classification `domains` includes `"db"` OR any segment in `execution-graph.json` has `executor` value `"db-executor"`

**When classification is null or missing:** spawn only the 4 universal auditors. Skip `ui-auditor` and `db-schema-auditor`.

Append to log:
```
{timestamp} [STAGE] → CHECKPOINT_AUDIT
{timestamp} [SPAWN] {N} auditors in parallel ({list of names})
```

Spawn the determined auditors simultaneously.

Each writes its report to `.dynos/task-{id}/audit-reports/{auditor}-checkpoint-{timestamp}.json`.

Wait for all to complete. Append to log:
```
{timestamp} [DONE] audit complete
```

### Step 4 — Repair loop (if findings exist)

Read all audit reports. Collect all blocking findings.

**If no blocking findings:** proceed to Step 5.

**If blocking findings exist:**

Update stage to `REPAIR_PLANNING`. Append to log:
```
{timestamp} [REPAIR] {N} findings — {list of finding IDs}
{timestamp} [STAGE] → REPAIR_PLANNING
```

Spawn `repair-coordinator` agent with instruction: "Read all audit reports in `.dynos/task-{id}/audit-reports/`. Produce a repair plan. Assign each finding to an executor. For each repair task, list the files that will be modified. Write to `.dynos/task-{id}/repair-log.json`."

Wait for completion. Update stage to `REPAIR_EXECUTION`. Append to log:
```
{timestamp} [STAGE] → REPAIR_EXECUTION
```

Spawn executor agents (in parallel where file-safe) for each repair task as assigned in `repair-log.json`:
- `ui-executor`, `backend-executor`, `ml-executor`, `db-executor`, `refactor-executor`, `testing-executor`, `integration-executor`

Each executor receives: the specific finding, the file(s) to fix, and the relevant acceptance criteria text from `spec.md`.

After all repairs complete, append to log:
```
{timestamp} [DONE] repair-execution — all fixes applied
```

**Re-audit (domain-aware):** Determine which files were modified by the repair executors. Spawn only:
- `spec-completion-auditor` (always)
- `security-auditor` (always)
- Any auditor that originally reported a repaired finding
- Skip auditors whose domain was not touched by the repair (e.g., if only backend files were repaired, skip `ui-auditor` even if it ran in the initial audit)

Wait for results.

- If all clear: proceed to Step 5.
- If new findings: increment `retry_counts` for each finding. If any finding has exceeded 3 retries, set stage to `FAILED`, append `[FAILED] max retries exceeded for: {finding-ids}`, and stop. Otherwise loop back to repair.

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
6. Write `.dynos/task-{id}/task-retrospective.json` as a flat JSON object (no nesting beyond one level):

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
  "repair_cycle_count": 0
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
  "repair_cycle_count": 0
}
```

Append to log:
```
{timestamp} [DONE] reflect — task-retrospective.json written
```

**Learn (inline -- no subagent):**

After writing the retrospective, automatically aggregate all retrospectives in the project into the memory file. Follow the steps in `skills/learn/SKILL.md`:

1. Scan all `.dynos/task-*/task-retrospective.json` files in the current working directory.
2. Aggregate: top 5 finding categories, executor reliability rankings, average repair cycles by task type.
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
Print (listing only auditors that were actually spawned):
```
Audit complete — ALL PASSED

  {auditor-name}:  PASS
  ... (one line per spawned auditor)

Task complete. Snapshot branch dynos/task-{id}-snapshot can be deleted if desired.
```

---

## Standalone use (no active task)

If no active task is found, run the 4 universal auditors on `git diff --name-only HEAD`. Skip Step 5 (no DONE state to write). Print results and stop.
