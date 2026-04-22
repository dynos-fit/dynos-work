from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_validate import validate_manifest, validate_repair_log, validate_task_artifacts


def _write_base_task(tmp_path: Path) -> Path:
    task_dir = tmp_path / ".dynos" / "task-hardening"
    task_dir.mkdir(parents=True)
    (task_dir / "manifest.json").write_text(json.dumps({
        "task_id": "task-hardening",
        "created_at": "2026-04-21T00:00:00Z",
        "raw_input": "test task",
        "stage": "PLANNING",
        "classification": {
            "type": "feature",
            "domains": ["backend"],
            "risk_level": "medium",
            "notes": "",
        },
    }))
    (task_dir / "spec.md").write_text(
        "# Spec\n\n"
        "## Task Summary\nx\n\n"
        "## User Context\nx\n\n"
        "## Acceptance Criteria\n"
        "1. First criterion\n"
        "2. Second criterion\n\n"
        "## Implicit Requirements Surfaced\nx\n\n"
        "## Out of Scope\nx\n\n"
        "## Assumptions\nx\n\n"
        "## Risk Notes\nx\n"
    )
    (task_dir / "plan.md").write_text(
        "# Plan\n\n"
        "## Technical Approach\nx\n\n"
        "## Reference Code\nx\n\n"
        "## Components / Modules\nx\n\n"
        "## API Contracts\nN/A\n\n"
        "## Data Flow\nx\n\n"
        "## Error Handling Strategy\nx\n\n"
        "## Test Strategy\nx\n\n"
        "## Dependency Graph\nx\n\n"
        "## Open Questions\nx\n"
    )
    (task_dir / "execution-graph.json").write_text(json.dumps({
        "task_id": "task-hardening",
        "segments": [
            {
                "id": "seg-1",
                "executor": "backend-executor",
                "description": "Implement change",
                "files_expected": ["src/app.py"],
                "depends_on": [],
                "criteria_ids": [1],
            },
            {
                "id": "seg-2",
                "executor": "testing-executor",
                "description": "Verify change",
                "files_expected": ["tests/test_app.py"],
                "depends_on": ["seg-1"],
                "criteria_ids": [2],
            },
        ],
    }))
    return task_dir


def test_validate_manifest_rejects_non_object_classification() -> None:
    errors = validate_manifest({
        "task_id": "t1",
        "created_at": "2026-04-21T00:00:00Z",
        "raw_input": "x",
        "stage": "PLANNING",
        "classification": "backend-only",
    })
    assert "classification must be an object" in errors


def test_validate_manifest_rejects_duplicate_domains_and_bad_flags() -> None:
    errors = validate_manifest({
        "task_id": "t1",
        "created_at": "2026-04-21T00:00:00Z",
        "raw_input": "x",
        "stage": "PLANNING",
        "classification": {
            "type": "feature",
            "risk_level": "medium",
            "domains": ["backend", "backend", ""],
            "notes": [],
            "tdd_required": "yes",
            "fast_track": "no",
        },
    })
    assert "classification.domains contains duplicate entry: 'backend'" in errors
    assert "classification.domains entries must be non-empty strings" in errors
    assert "classification.notes must be a string" in errors
    assert "classification.tdd_required must be a boolean" in errors
    assert "classification.fast_track must be a boolean" in errors


def test_validate_task_artifacts_rejects_graph_segment_shape_drift(tmp_path: Path) -> None:
    task_dir = _write_base_task(tmp_path)
    (task_dir / "execution-graph.json").write_text(json.dumps({
        "task_id": "task-hardening",
        "segments": [
            {
                "id": "seg 1",
                "executor": "backend-executor",
                "description": "",
                "files_expected": ["src/app.py", "src/app.py", "../escape.py"],
                "depends_on": ["seg 1", "seg 1", 7],
                "criteria_ids": [1, 1, "2"],
            }
        ],
    }))

    errors = validate_task_artifacts(task_dir, run_gap=False)
    assert "seg 1: segment id must match [A-Za-z0-9][A-Za-z0-9_.-]*" in errors
    assert "seg 1: description must be a non-empty string" in errors
    assert "seg 1: duplicate file in files_expected: src/app.py" in errors
    assert "seg 1: file path must stay inside repo: ../escape.py" in errors
    assert "seg 1: depends_on cannot reference itself" in errors
    assert "seg 1: duplicate depends_on entry: seg 1" in errors
    assert "seg 1: depends_on entries must be non-empty strings" in errors
    assert "seg 1: duplicate criteria_id: 1" in errors
    assert "seg 1: criteria_id must be an integer" in errors


def test_validate_task_artifacts_rejects_graph_task_id_mismatch(tmp_path: Path) -> None:
    task_dir = _write_base_task(tmp_path)
    graph = json.loads((task_dir / "execution-graph.json").read_text())
    graph["task_id"] = "other-task"
    (task_dir / "execution-graph.json").write_text(json.dumps(graph))

    errors = validate_task_artifacts(task_dir, run_gap=False)
    assert "execution graph task_id mismatch: graph='other-task' manifest='task-hardening'" in errors


def test_validate_repair_log_accepts_current_affected_files_schema(tmp_path: Path) -> None:
    task_dir = _write_base_task(tmp_path)
    audit_dir = task_dir / "audit-reports"
    audit_dir.mkdir()
    (audit_dir / "security-auditor.json").write_text(json.dumps({
        "auditor_name": "security-auditor",
        "findings": [
            {"id": "sec-1", "severity": "high", "blocking": True},
        ],
    }))
    (task_dir / "repair-log.json").write_text(json.dumps({
        "task_id": "task-hardening",
        "repair_cycle": 1,
        "batches": [
            {
                "batch_id": "batch-1",
                "parallel": True,
                "tasks": [
                    {
                        "finding_id": "sec-1",
                        "auditor": "security-auditor",
                        "severity": "high",
                        "instruction": "Move secret to env var",
                        "assigned_executor": "backend-executor",
                        "affected_files": ["src/app.py"],
                        "retry_count": 1,
                        "max_retries": 3,
                        "status": "pending",
                        "model_override": "opus",
                    }
                ],
            }
        ],
    }))

    assert validate_repair_log(task_dir) == []


def test_validate_repair_log_rejects_duplicate_findings_and_bad_paths(tmp_path: Path) -> None:
    task_dir = _write_base_task(tmp_path)
    audit_dir = task_dir / "audit-reports"
    audit_dir.mkdir()
    (audit_dir / "security-auditor.json").write_text(json.dumps({
        "auditor_name": "security-auditor",
        "findings": [
            {"id": "sec-1", "severity": "high", "blocking": True},
        ],
    }))
    (task_dir / "repair-log.json").write_text(json.dumps({
        "task_id": 7,
        "repair_cycle": -1,
        "batches": [
            {
                "batch_id": "batch-1",
                "parallel": "yes",
                "tasks": [
                    {
                        "finding_id": "sec-1",
                        "auditor": "",
                        "severity": "urgent",
                        "instruction": "",
                        "assigned_executor": "backend-executor",
                        "affected_files": ["src/app.py", "src/app.py", "../escape.py"],
                        "retry_count": -1,
                        "max_retries": 0,
                        "status": "queued",
                        "model_override": "goku",
                    }
                ],
            },
            {
                "batch_id": "batch-2",
                "tasks": [
                    {
                        "finding_id": "sec-1",
                        "instruction": "Do thing",
                        "assigned_executor": "testing-executor",
                        "affected_files": ["tests/test_app.py"],
                    }
                ],
            },
        ],
    }))

    errors = validate_repair_log(task_dir)
    assert "repair-log task_id must be a string" in errors
    assert "repair-log repair_cycle must be a non-negative integer" in errors
    assert "batch-1: parallel must be a boolean" in errors
    assert "batch-1: auditor must be a non-empty string when present" in errors
    assert "batch-1: invalid severity 'urgent'" in errors
    assert "batch-1: instruction must be a non-empty string" in errors
    assert "batch-1: duplicate affected_files entry: src/app.py" in errors
    assert "batch-1: affected_files must stay inside repo: ../escape.py" in errors
    assert "batch-1: retry_count must be a non-negative integer" in errors
    assert "batch-1: max_retries must be a positive integer when present" in errors
    assert "batch-1: invalid status 'queued'" in errors
    assert "batch-1: invalid model_override 'goku'" in errors
    assert "duplicate finding_id across repair-log batches: sec-1" in errors


def test_validate_repair_log_cross_checks_live_audit_reports(tmp_path: Path) -> None:
    task_dir = _write_base_task(tmp_path)
    audit_dir = task_dir / "audit-reports"
    audit_dir.mkdir()
    (audit_dir / "security-auditor.json").write_text(json.dumps({
        "auditor_name": "security-auditor",
        "findings": [
            {"id": "sec-1", "severity": "high", "blocking": True},
        ],
    }))
    (task_dir / "repair-log.json").write_text(json.dumps({
        "task_id": "wrong-task",
        "repair_cycle": 1,
        "batches": [
            {
                "batch_id": "batch-1",
                "parallel": True,
                "tasks": [
                    {
                        "finding_id": "missing-1",
                        "auditor": "code-quality-auditor",
                        "severity": "low",
                        "instruction": "Do thing",
                        "assigned_executor": "backend-executor",
                        "affected_files": ["src/app.py"],
                    },
                    {
                        "finding_id": "sec-1",
                        "auditor": "code-quality-auditor",
                        "severity": "low",
                        "instruction": "Do thing",
                        "assigned_executor": "backend-executor",
                        "affected_files": ["src/app.py"],
                    },
                ],
            }
        ],
    }))

    errors = validate_repair_log(task_dir)
    assert "repair-log task_id mismatch: repair-log='wrong-task' manifest='task-hardening'" in errors
    assert "batch-1: finding_id not found in live audit reports: missing-1" in errors
    assert "batch-1: auditor mismatch for sec-1: repair-log='code-quality-auditor' audit-report='security-auditor'" in errors
    assert "batch-1: severity mismatch for sec-1: repair-log='low' audit-report='high'" in errors
