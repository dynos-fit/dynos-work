# Canonical Audit Report Schema

All auditors write their report as a JSON file to `.dynos/task-{id}/audit-reports/{auditor-name}-{timestamp}.json` using this schema:

```json
{
  "auditor_name": "string — the auditor's name (e.g. 'security-auditor')",
  "run_id": "string — unique run identifier",
  "task_id": "string — the task ID",
  "status": "pass | fail | warning",
  "severity": "critical | major | minor",
  "findings": [
    {
      "id": "string — prefix-NNN (e.g. 'sec-001')",
      "category": "string — finding category (e.g. 'security', 'compliance'). Optional for backwards compatibility; defaults to the auditor's primary category if omitted",
      "description": "string — what is wrong",
      "location": "string — file:line",
      "severity": "critical | major | minor",
      "blocking": "boolean"
    }
  ],
  "requirement_coverage": [
    {
      "requirement_id": "string",
      "requirement_text": "string",
      "status": "covered | partial | missing",
      "evidence": "string — file:line and what it proves"
    }
  ],
  "evidence_checked": ["string — file paths inspected"],
  "repair_tasks": [
    {
      "finding_id": "string — matches a finding id",
      "description": "string — precise remediation instruction",
      "assigned_executor": "string — executor type (e.g. 'ui-executor')",
      "affected_files": ["string — file paths"]
    }
  ],
  "confidence": "number — 0.0 to 1.0",
  "can_block_completion": "boolean"
}
```

Auditor-specific notes:
- `spec-completion-auditor`: `requirement_coverage` is mandatory and must cover every numbered criterion. Finding IDs use prefix `sc-`. Severity is always `critical`.
- `security-auditor`: `severity` meanings: `critical` = direct exploitability, `major` = significant risk, `minor` = defense-in-depth. Finding IDs use prefix `sec-` (category `security`) or `comp-` (category `compliance`).
- `code-quality-auditor`: Distinguish blocking from warning in the `blocking` field. Finding IDs use prefix `cq-`. Categories: `code-quality` (default) or `doc-accuracy` (only when `.md` files are in the diff — uses deterministic `validate_docs_accuracy.py` hook).
- `ui-auditor`: Every UI state must have specific file evidence. Finding IDs use prefix `ui-`.
- `db-schema-auditor`: `severity` meanings: `critical` = data loss risk or broken referential integrity, `major` = missing index or N+1, `minor` = naming or minor optimization. Finding IDs use prefix `db-`.
- `dead-code-auditor`: `severity` meanings: `critical` = unreferenced files or unused exports from core modules, `major` = dead functions or unused imports in multiple files, `minor` = single unused import or isolated commented-out block. Finding IDs use prefix `dc-`.
