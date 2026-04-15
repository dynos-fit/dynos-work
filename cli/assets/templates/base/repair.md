---
name: repair
description: "Internal dynos-work skill. Manually repair a specific finding. Use when you want to fix one issue without running the full audit loop. /dynos-work:audit handles repair automatically."
---

# dynos-work: Repair

Manually repair a specific finding from an audit report. Use this when you need to fix a precise issue without running the full audit pipeline.

## What you do

1. Find the active task in `.dynos/`
2. Read the latest audit reports
3. If the user specifies a finding ID, repair that finding only
4. If no finding specified, show all open blocking findings and ask which to repair
5. Spawn the appropriate executor subagent with the precise repair instruction
6. After repair, re-run only the auditor(s) that reported the finding (plus always spec-completion and security)
7. Report new audit result

## Usage

```
/dynos-work:repair                    — shows all open findings, ask which to fix
/dynos-work:repair sec-003            — repairs finding sec-003
/dynos-work:repair --all              — repairs all open findings in parallel (where file-safe)
```

## Executor selection

Based on the finding's `assigned_executor` field from the audit report. If not specified, infer from file extension:
- UI files → `ui-executor`
- Backend/API files → `backend-executor`
- Schema/migration → `db-executor`
- Config/env → `integration-executor`
- Tests → `testing-executor`

## Hard rules

- Always re-audit after repair — do not assume the fix worked
- Always include spec-completion and security in the re-audit
- Update `repair-log.json` with the result
