# Canonical Audit Report Schema

## Ruthlessness Standard

- Findings must describe the broken mechanism, not just the symptom.
- Evidence must point to concrete inspected code or generated output, not assumptions.
- If coverage or confidence is weak, say so explicitly instead of padding the report.

All auditors write their report as a JSON file to `.dynos/task-{id}/audit-reports/{auditor_name}-{model}-attempt-{n}.json` using this schema:

```json
{
  "auditor_name": "string — the auditor's name (e.g. 'security-auditor')",
  "run_id": "string — unique run identifier",
  "task_id": "string — the task ID",
  "status": "in_progress | partial | complete",
  "verdict": "pass | fail | warning",
  "confidence": "number | null — 0.0 to 1.0; null if confidence cannot be determined",
  "incomplete_reason": "string | null — human-readable reason when status is in_progress or partial; null when complete",
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
  "can_block_completion": "boolean"
}
```

Auditor-specific notes:
- `spec-completion-auditor`: `requirement_coverage` is mandatory and must cover every numbered criterion. Finding IDs use prefix `sc-`. Severity is always `critical`.
- `security-auditor`: `severity` meanings: `critical` = direct exploitability, `major` = significant risk, `minor` = defense-in-depth. Finding IDs use prefix `sec-` (category `security`) or `comp-` (category `compliance`).
- `code-quality-auditor`: Distinguish blocking from warning in the `blocking` field. Finding IDs use prefix `cq-`. Categories: `code-quality` (default), `doc-accuracy` (when `.md` files in diff), or `typecheck-lint` (always runs — uses deterministic `typecheck_lint.py` hook).
- `ui-auditor`: Every UI state must have specific file evidence. Finding IDs use prefix `ui-`.
- `db-schema-auditor`: `severity` meanings: `critical` = data loss risk or broken referential integrity, `major` = missing index or N+1, `minor` = naming or minor optimization. Finding IDs use prefix `db-`.
- `dead-code-auditor`: `severity` meanings: `critical` = unreferenced files or unused exports from core modules, `major` = dead functions or unused imports in multiple files, `minor` = single unused import or isolated commented-out block. Finding IDs use prefix `dc-`.
- `performance-auditor`: `severity` meanings: `critical` = will cause outage at scale (connection leak, unbounded query), `major` = significant latency risk (N+1, missing index, missing timeout), `minor` = optimization opportunity. Finding IDs use prefix `perf-`. Category: `performance`.

## Final-Message Envelope

Your final message — the last text you return to the orchestrator — MUST be ONLY the following single-line JSON object, bare with no code fences and no surrounding prose:

{"report_path": "<absolute-path-to-written-report>", "findings_count": N, "blocking_count": M}

Rules:
- `report_path` must exactly match the path string you used in your Write or Bash call — character for character.
- `findings_count` is the integer count of all entries in the `findings` array.
- `blocking_count` is the integer count of findings where `blocking` is `true`.
- The JSON must fit on a single line. No code fences. No prose before or after. No whitespace before the opening `{` or after the closing `}`.
- Even if you found zero findings, emit the envelope with `findings_count: 0` and `blocking_count: 0`.
