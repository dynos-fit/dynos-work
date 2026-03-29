---
name: final-audit
description: "Power user: Run all 6 auditors (including dead-code) for final gate. Use after /dynos-work:audit passes. All 6 must pass to reach DONE."
---

# dynos-work: Final Audit

Runs all 6 auditors simultaneously with no evidence reuse. This is the final gate before DONE. All 6 must pass.

## What you do

### Step 1 — Find active task

Find the most recent active task in `.dynos/`. Read `manifest.json`.

Verify stage is `FINAL_AUDIT`. If not, print the current stage and what command to run instead.

### Step 2 — Diff scope

Run `git diff --name-only {snapshot.head_sha}` to get all files changed by this task. This is the file list passed to every auditor.

### Step 3 — Spawn all 6 auditors in parallel

Update `manifest.json` stage to `FINAL_AUDIT`. Append to log:
```
{timestamp} [STAGE] → FINAL_AUDIT
{timestamp} [SPAWN] all 6 auditors in parallel — final gate
```

Spawn simultaneously:
- `spec-completion-auditor`
- `security-auditor`
- `code-quality-auditor`
- `ui-auditor`
- `db-schema-auditor`
- `dead-code-auditor`

Each receives: `spec.md`, `plan.md`, all evidence files, the diff-scoped file list. Each writes its report to `.dynos/task-{id}/audit-reports/{auditor}-final-{timestamp}.json`.

**No evidence reuse. Every auditor runs fresh.**

Wait for ALL 6 to complete before reading results.

### Step 4 — Gate on results

Read all 6 reports. Write `audit-summary.json`.

Append to log:
```
{timestamp} [DONE] all auditors complete
```

**If all 6 pass:**
Update stage to `COMPLETION_REVIEW`. Append to log:
```
{timestamp} [ADVANCE] FINAL_AUDIT → COMPLETION_REVIEW
```

Write `completion.json`. Update stage to `DONE`. Print:

```
Final Audit — ALL PASSED

spec-completion:  PASS
security:         PASS
code-quality:     PASS
ui:               PASS
db-schema:        PASS
dead-code:        PASS

Task complete. Snapshot branch dynos/task-{id}-snapshot can be deleted if desired.
```

**If any fail:**
Update stage to `REPAIR_PLANNING`. Append to log:
```
{timestamp} [REPAIR] {N} findings — {list of finding IDs}
{timestamp} [ADVANCE] FINAL_AUDIT → REPAIR_PLANNING
```
Print:
```
Final Audit — FAILURES

[list each failing auditor and its blocking findings]

Next: /dynos-work:repair
```
