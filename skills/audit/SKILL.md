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

Spawn `repair-coordinator` agent with instruction: "Read all audit reports in `.dynos/task-{id}/audit-reports/`. Produce a repair plan. Assign each finding to an executor. Write to `.dynos/task-{id}/repair-log.json`."

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

**Re-audit:** spawn only the auditors that reported the repaired findings (plus always spec-completion and security). Wait for results.

- If all clear: proceed to Step 5.
- If new findings: increment `retry_counts` for each finding. If any finding has exceeded 3 retries, set stage to `FAILED`, append `[FAILED] max retries exceeded for: {finding-ids}`, and stop. Otherwise loop back to repair.

### Step 5 — Gate to DONE

Read all audit reports. Write `audit-summary.json`.

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
