#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))


def _setup_task_dir(tmp_path: Path) -> Path:
    task_dir = tmp_path / ".dynos" / "task-20260403-001"
    task_dir.mkdir(parents=True)
    (task_dir / "manifest.json").write_text(
        json.dumps(
            {
                "task_id": "task-20260403-001",
                "created_at": "2026-04-03T00:00:00Z",
                "title": "Test task",
                "raw_input": "Build a thing",
                "stage": "PLAN_AUDIT",
                "classification": {
                    "type": "feature",
                    "domains": ["backend"],
                    "risk_level": "medium",
                    "notes": "test",
                },
                "retry_counts": {},
                "blocked_reason": None,
                "completion_at": None,
                "snapshot": {"head_sha": "0000000000000000000000000000000000000000"},
            },
            indent=2,
        )
        + "\n"
    )
    (task_dir / "spec.md").write_text(
        "# Normalized Spec\n\n"
        "## Task Summary\nA.\n\n"
        "## User Context\nB.\n\n"
        "## Acceptance Criteria\n"
        "1. First criterion\n"
        "2. Second criterion\n\n"
        "## Implicit Requirements Surfaced\nC.\n\n"
        "## Out of Scope\nD.\n\n"
        "## Assumptions\nsafe assumption: none\n\n"
        "## Risk Notes\nE.\n"
    )
    (task_dir / "plan.md").write_text(
        "# Implementation Plan\n\n"
        "## Technical Approach\nA.\n\n"
        "## Reference Code\nB.\n\n"
        "## Components / Modules\nC.\n\n"
        "## API Contracts\nContracts.\n\n"
        "## Data Flow\nD.\n\n"
        "## Error Handling Strategy\nE.\n\n"
        "## Test Strategy\nF.\n\n"
        "## Dependency Graph\nG.\n\n"
        "## Open Questions\nH.\n"
    )
    (task_dir / "execution-graph.json").write_text(
        json.dumps(
            {
                "task_id": "task-20260403-001",
                "segments": [
                    {
                        "id": "seg-1",
                        "executor": "backend-executor",
                        "description": "Build backend",
                        "files_expected": ["src/a.py"],
                        "depends_on": [],
                        "parallelizable": True,
                        "criteria_ids": [1],
                        "no_op_justified": True,
                        "no_op_reason": "unit test task dir outside git repo",
                    },
                    {
                        "id": "seg-2",
                        "executor": "testing-executor",
                        "description": "Build tests",
                        "files_expected": ["tests/test_a.py"],
                        "depends_on": ["seg-1"],
                        "parallelizable": True,
                        "criteria_ids": [2],
                        "no_op_justified": True,
                        "no_op_reason": "unit test task dir outside git repo",
                    },
                ],
            },
            indent=2,
        )
        + "\n"
    )
    return task_dir


def _run_ctl(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        ["python3", str(ROOT / "hooks" / "ctl.py"), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=merged_env,
        check=False,
    )


def test_validate_task_passes_for_valid_fixture(tmp_path) -> None:
    task_dir = _setup_task_dir(tmp_path)
    result = _run_ctl("validate-task", str(task_dir), "--strict")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Validation passed" in result.stdout


def test_validate_task_fails_for_invalid_criteria_mapping(tmp_path) -> None:
    task_dir = _setup_task_dir(tmp_path)
    graph_path = task_dir / "execution-graph.json"
    graph = json.loads(graph_path.read_text())
    graph["segments"][0]["criteria_ids"] = [3]
    graph_path.write_text(json.dumps(graph, indent=2) + "\n")
    result = _run_ctl("validate-task", str(task_dir), "--strict")
    assert result.returncode == 1
    assert "does not exist in spec" in result.stdout


def test_transition_rejects_illegal_stage_change(tmp_path) -> None:
    task_dir = _setup_task_dir(tmp_path)
    result = _run_ctl("transition", str(task_dir), "EXECUTION")
    assert result.returncode == 1
    assert "Illegal stage transition" in result.stderr


def test_transition_allows_legal_stage_change(tmp_path) -> None:
    task_dir = _setup_task_dir(tmp_path)
    result = _run_ctl("transition", str(task_dir), "PRE_EXECUTION_SNAPSHOT")
    assert result.returncode == 0, result.stdout + result.stderr
    manifest = json.loads((task_dir / "manifest.json").read_text())
    assert manifest["stage"] == "PRE_EXECUTION_SNAPSHOT"


def test_check_ownership_fails_for_foreign_file(tmp_path) -> None:
    task_dir = _setup_task_dir(tmp_path)
    result = _run_ctl(
        "check-ownership",
        str(task_dir),
        "seg-1",
        "src/a.py",
        "src/b.py",
    )
    assert result.returncode == 1
    assert "src/b.py" in result.stdout


def test_audit_receipt_derives_counts_from_report(tmp_path) -> None:
    task_dir = _setup_task_dir(tmp_path)
    audit_dir = task_dir / "audit-reports"
    audit_dir.mkdir(parents=True, exist_ok=True)
    report = audit_dir / "security-auditor.json"
    report.write_text(json.dumps({
        "findings": [
            {"id": "SEC-1", "blocking": True},
            {"id": "SEC-2", "blocking": False},
        ]
    }))
    result = _run_ctl(
        "audit-receipt",
        str(task_dir),
        "security-auditor",
        "--model",
        "haiku",
        "--report-path",
        str(report),
        "--tokens-used",
        "123",
        "--route-mode",
        "generic",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads((task_dir / "receipts" / "audit-security-auditor.json").read_text())
    assert receipt["finding_count"] == 2
    assert receipt["blocking_count"] == 1
    assert receipt["report_path"] == str(report)


def test_audit_receipt_missing_report_fails_without_manual_counts(tmp_path) -> None:
    task_dir = _setup_task_dir(tmp_path)
    missing = task_dir / "audit-reports" / "missing.json"
    result = _run_ctl(
        "audit-receipt",
        str(task_dir),
        "security-auditor",
        "--model",
        "haiku",
        "--report-path",
        str(missing),
        "--tokens-used",
        "123",
        "--route-mode",
        "generic",
    )
    assert result.returncode == 1
    assert "cannot derive finding_count/blocking_count automatically" in result.stderr


def test_run_spec_ready_writes_receipt_and_transitions(tmp_path) -> None:
    from lib_receipts import receipt_planner_spawn

    task_dir = _setup_task_dir(tmp_path)
    manifest_path = task_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["stage"] = "SPEC_NORMALIZATION"
    manifest.pop("fast_track", None)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    sidecar_dir = task_dir / "receipts" / "_injected-planner-prompts"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    digest = "b" * 64
    (sidecar_dir / "spec.sha256").write_text(digest)
    receipt_planner_spawn(
        task_dir,
        "spec",
        tokens_used=10,
        injected_prompt_sha256=digest,
    )

    result = _run_ctl("run-spec-ready", str(task_dir))
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "spec_review_ready"

    manifest = json.loads(manifest_path.read_text())
    assert manifest["stage"] == "SPEC_REVIEW"
    assert (task_dir / "receipts" / "spec-validated.json").exists()


def test_run_planning_mode_standard_vs_hierarchical_and_fast_track(tmp_path) -> None:
    task_dir = _setup_task_dir(tmp_path)

    standard = _run_ctl("run-planning-mode", str(task_dir))
    assert standard.returncode == 0, standard.stdout + standard.stderr
    payload = json.loads(standard.stdout)
    assert payload["planning_mode"] == "standard"
    assert payload["criteria_count"] == 2

    manifest_path = task_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["classification"]["risk_level"] = "high"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    hierarchical = _run_ctl("run-planning-mode", str(task_dir))
    payload = json.loads(hierarchical.stdout)
    assert payload["planning_mode"] == "hierarchical"

    manifest["fast_track"] = True
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    fast_track = _run_ctl("run-planning-mode", str(task_dir))
    payload = json.loads(fast_track.stdout)
    assert payload["planning_mode"] == "fast_track_combined"


def test_run_audit_setup_writes_audit_plan(tmp_path) -> None:
    task_dir = _setup_task_dir(tmp_path)
    manifest_path = task_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["stage"] = "CHECKPOINT_AUDIT"
    manifest["fast_track"] = False
    manifest["snapshot"] = {"head_sha": "abc123"}
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    result = _run_ctl("run-audit-setup", str(task_dir), env={"HOME": str(tmp_path)})
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "audit_setup_ready"
    assert Path(payload["audit_plan_path"]).exists()
    assert payload["task_type"] == "feature"
    assert isinstance(payload["spawn_auditors"], list)


def test_run_execute_setup_writes_routing_and_transitions(tmp_path) -> None:
    task_dir = _setup_task_dir(tmp_path)
    manifest_path = task_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["stage"] = "PRE_EXECUTION_SNAPSHOT"
    manifest["fast_track"] = False
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    os.environ["DYNOS_ALLOW_TEST_OVERRIDE"] = "1"
    try:
        from lib_receipts import receipt_plan_validated
        receipt_plan_validated(task_dir, validation_passed_override=True)
    finally:
        os.environ.pop("DYNOS_ALLOW_TEST_OVERRIDE", None)

    result = _run_ctl("run-execute-setup", str(task_dir), env={"HOME": str(tmp_path)})
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "execution_ready"
    assert payload["stage"] == "EXECUTION"
    assert Path(payload["receipt_path"]).exists()

    manifest = json.loads(manifest_path.read_text())
    assert manifest["stage"] == "EXECUTION"
    assert (task_dir / "receipts" / "executor-routing.json").exists()


def test_run_audit_findings_gate_detects_repair_and_critical_spec_failure(tmp_path) -> None:
    task_dir = _setup_task_dir(tmp_path)
    audit_dir = task_dir / "audit-reports"
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "spec-completion-auditor.json").write_text(json.dumps({
        "auditor_name": "spec-completion-auditor",
        "findings": [
            {"id": "SPEC-1", "blocking": True, "severity": "critical"},
            {"id": "SPEC-2", "blocking": False, "severity": "medium"},
        ],
    }))
    (audit_dir / "security-auditor.json").write_text(json.dumps({
        "auditor_name": "security-auditor",
        "findings": [
            {"id": "SEC-1", "blocking": True, "severity": "high"},
        ],
    }))

    result = _run_ctl("run-audit-findings-gate", str(task_dir))
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "repair_required"
    assert payload["critical_spec_failure"] is True
    assert payload["next_action"] == "repair_phase_1"
    assert sorted(payload["blocking_finding_ids"]) == ["SEC-1", "SPEC-1"]


def test_run_audit_findings_gate_clear_when_no_blocking(tmp_path) -> None:
    task_dir = _setup_task_dir(tmp_path)
    audit_dir = task_dir / "audit-reports"
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "code-quality-auditor.json").write_text(json.dumps({
        "auditor_name": "code-quality-auditor",
        "findings": [
            {"id": "CQ-1", "blocking": False, "severity": "medium"},
        ],
    }))

    result = _run_ctl("run-audit-findings-gate", str(task_dir))
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "clear"
    assert payload["critical_spec_failure"] is False
    assert payload["next_action"] == "reflect"
    assert payload["blocking_finding_ids"] == []


def test_run_audit_repair_cycle_plan_phase_1_transitions_and_prioritizes_critical_spec(tmp_path) -> None:
    task_dir = _setup_task_dir(tmp_path)
    manifest_path = task_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["stage"] = "CHECKPOINT_AUDIT"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    audit_dir = task_dir / "audit-reports"
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "spec-completion-auditor.json").write_text(json.dumps({
        "auditor_name": "spec-completion-auditor",
        "findings": [{"id": "SPEC-1", "blocking": True, "severity": "critical"}],
    }) + "\n")
    (audit_dir / "security-auditor.json").write_text(json.dumps({
        "auditor_name": "security-auditor",
        "findings": [{"id": "SEC-1", "blocking": True, "severity": "high"}],
    }) + "\n")

    result = _run_ctl("run-audit-repair-cycle-plan", str(task_dir))
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "repair_cycle_ready"
    assert payload["stage"] == "REPAIR_PLANNING"
    assert payload["transitioned_to_repair_planning"] is True
    assert payload["repair_cycle"] == 1
    assert payload["phase"] == "phase_1"
    assert payload["critical_spec_finding_ids"] == ["SPEC-1"]
    assert payload["blocking_finding_ids"] == ["SPEC-1", "SEC-1"]
    assert payload["blocking_findings"][0]["retry_count"] == 0
    assert (task_dir / "repair-cycle-plan.json").exists()


def test_run_audit_repair_cycle_plan_phase_2_carries_retry_counts(tmp_path) -> None:
    task_dir = _setup_task_dir(tmp_path)
    manifest_path = task_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["stage"] = "REPAIR_PLANNING"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    audit_dir = task_dir / "audit-reports"
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "security-auditor.json").write_text(json.dumps({
        "auditor_name": "security-auditor",
        "findings": [
            {"id": "SEC-1", "blocking": True, "severity": "high"},
            {"id": "SEC-2", "blocking": True, "severity": "medium"},
        ],
    }) + "\n")
    (task_dir / "repair-log.json").write_text(json.dumps({
        "repair_cycle": 1,
        "batches": [
            {
                "batch_id": "batch-1",
                "tasks": [
                    {
                        "finding_id": "SEC-1",
                        "assigned_executor": "backend-executor",
                        "files_to_modify": ["src/a.py"],
                        "retry_count": 1,
                    }
                ],
            }
        ],
    }) + "\n")

    result = _run_ctl("run-audit-repair-cycle-plan", str(task_dir))
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "repair_cycle_ready"
    assert payload["repair_cycle"] == 2
    assert payload["phase"] == "phase_2"
    findings_by_id = {entry["id"]: entry for entry in payload["blocking_findings"]}
    assert findings_by_id["SEC-1"]["retry_count"] == 2
    assert findings_by_id["SEC-1"]["model_override"] == "opus"
    assert findings_by_id["SEC-2"]["retry_count"] == 0


def test_run_audit_repair_cycle_plan_clear_when_no_blocking(tmp_path) -> None:
    task_dir = _setup_task_dir(tmp_path)
    manifest_path = task_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["stage"] = "CHECKPOINT_AUDIT"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    audit_dir = task_dir / "audit-reports"
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "security-auditor.json").write_text(json.dumps({
        "auditor_name": "security-auditor",
        "findings": [{"id": "SEC-1", "blocking": False, "severity": "low"}],
    }) + "\n")

    result = _run_ctl("run-audit-repair-cycle-plan", str(task_dir))
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "clear"
    assert payload["phase"] is None
    assert payload["blocking_findings"] == []


def test_run_audit_reaudit_plan_uses_repair_log_and_matching_auditors(tmp_path) -> None:
    task_dir = _setup_task_dir(tmp_path)
    manifest_path = task_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["snapshot"] = {"head_sha": "abc123"}
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    (task_dir / "repair-log.json").write_text(json.dumps({
        "repair_cycle": 2,
        "batches": [
            {
                "batch_id": "batch-1",
                    "tasks": [
                        {
                            "finding_id": "SEC-1",
                            "assigned_executor": "backend-executor",
                            "instruction": "Rotate the secret handling path",
                            "files_to_modify": ["src/a.py"],
                            "retry_count": 1,
                        }
                    ],
                }
        ],
    }) + "\n")
    audit_dir = task_dir / "audit-reports"
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "security-auditor.json").write_text(json.dumps({
        "auditor_name": "security-auditor",
        "findings": [{"id": "SEC-1", "blocking": True}],
    }))
    (audit_dir / "code-quality-auditor.json").write_text(json.dumps({
        "auditor_name": "code-quality-auditor",
        "findings": [{"id": "CQ-1", "blocking": False}],
    }))

    result = _run_ctl("run-audit-reaudit-plan", str(task_dir))
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "reaudit_plan_ready"
    assert payload["repair_cycle"] == 2
    assert payload["repaired_finding_ids"] == ["SEC-1"]
    assert "src/a.py" in payload["modified_files"]
    assert payload["auditors_to_spawn"] == ["spec-completion-auditor", "security-auditor"]
    assert payload["full_scope_auditors"] == ["spec-completion-auditor"]


def test_run_audit_summary_writes_summary_file(tmp_path) -> None:
    task_dir = _setup_task_dir(tmp_path)
    audit_dir = task_dir / "audit-reports"
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "security-auditor.json").write_text(json.dumps({
        "auditor_name": "security-auditor",
        "findings": [
            {"id": "SEC-1", "blocking": True},
            {"id": "SEC-2", "blocking": False},
        ],
    }))
    (audit_dir / "code-quality-auditor.json").write_text(json.dumps({
        "auditor_name": "code-quality-auditor",
        "findings": [
            {"id": "CQ-1", "blocking": False},
        ],
    }))

    result = _run_ctl("run-audit-summary", str(task_dir))
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "audit_summary_ready"
    assert payload["total_findings"] == 3
    assert payload["total_blocking"] == 1
    assert payload["audit_result"] == "fail"
    assert Path(payload["summary_path"]).exists()


def test_run_execution_batch_plan_marks_cached_segments_and_next_batch(tmp_path) -> None:
    from lib_receipts import receipt_executor_routing, receipt_plan_validated

    task_dir = _setup_task_dir(tmp_path)
    manifest_path = task_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["stage"] = "EXECUTION"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    os.environ["DYNOS_ALLOW_TEST_OVERRIDE"] = "1"
    try:
        receipt_plan_validated(task_dir, validation_passed_override=True)
    finally:
        os.environ.pop("DYNOS_ALLOW_TEST_OVERRIDE", None)

    receipt_executor_routing(task_dir, [
        {
            "segment_id": "seg-1",
            "executor": "backend-executor",
            "model": "sonnet",
            "route_mode": "generic",
            "agent_path": None,
        },
        {
            "segment_id": "seg-2",
            "executor": "testing-executor",
            "model": "sonnet",
            "route_mode": "generic",
            "agent_path": None,
        },
    ])

    evidence_dir = task_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "seg-1.md").write_text("seg-1 evidence\n")

    result = _run_ctl("run-execution-batch-plan", str(task_dir))
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "execution_batch_plan_ready"
    assert payload["cached_segments"] == ["seg-1"]
    assert payload["pending_segments"] == ["seg-2"]
    assert payload["critical_path_segments"] == ["seg-1"]
    assert [entry["segment_id"] for entry in payload["next_batch"]] == ["seg-2"]


def test_run_execution_batch_plan_drift_disables_cache(tmp_path) -> None:
    from lib_receipts import receipt_executor_routing, receipt_plan_validated

    task_dir = _setup_task_dir(tmp_path)
    manifest_path = task_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["stage"] = "EXECUTION"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    os.environ["DYNOS_ALLOW_TEST_OVERRIDE"] = "1"
    try:
        receipt_plan_validated(task_dir, validation_passed_override=True)
    finally:
        os.environ.pop("DYNOS_ALLOW_TEST_OVERRIDE", None)

    receipt_executor_routing(task_dir, [
        {
            "segment_id": "seg-1",
            "executor": "backend-executor",
            "model": "sonnet",
            "route_mode": "generic",
            "agent_path": None,
        },
        {
            "segment_id": "seg-2",
            "executor": "testing-executor",
            "model": "sonnet",
            "route_mode": "generic",
            "agent_path": None,
        },
    ])

    evidence_dir = task_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "seg-1.md").write_text("seg-1 evidence\n")
    (task_dir / "plan.md").write_text((task_dir / "plan.md").read_text() + "\nDrift.\n")

    result = _run_ctl("run-execution-batch-plan", str(task_dir))
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    seg1 = next(entry for entry in payload["segments"] if entry["segment_id"] == "seg-1")
    assert payload["specs_fresh"] is False
    assert seg1["status"] == "pending"
    assert "drift" in str(seg1["cache_reason"]).lower()
    assert [entry["segment_id"] for entry in payload["next_batch"]] == ["seg-1"]


def test_run_execution_batch_plan_completed_receipt_beats_cache(tmp_path) -> None:
    from lib_receipts import receipt_executor_done, receipt_executor_routing, receipt_plan_validated

    task_dir = _setup_task_dir(tmp_path)
    manifest_path = task_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["stage"] = "EXECUTION"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    os.environ["DYNOS_ALLOW_TEST_OVERRIDE"] = "1"
    try:
        receipt_plan_validated(task_dir, validation_passed_override=True)
    finally:
        os.environ.pop("DYNOS_ALLOW_TEST_OVERRIDE", None)

    receipt_executor_routing(task_dir, [
        {
            "segment_id": "seg-1",
            "executor": "backend-executor",
            "model": "sonnet",
            "route_mode": "generic",
            "agent_path": None,
        },
        {
            "segment_id": "seg-2",
            "executor": "testing-executor",
            "model": "sonnet",
            "route_mode": "generic",
            "agent_path": None,
        },
    ])

    evidence_dir = task_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = evidence_dir / "seg-1.md"
    evidence_path.write_text("seg-1 evidence\n")

    sidecar_dir = task_dir / "receipts" / "_injected-prompts"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    digest = "c" * 64
    (sidecar_dir / "seg-1.sha256").write_text(digest)
    receipt_executor_done(
        task_dir,
        segment_id="seg-1",
        executor_type="backend-executor",
        model_used="sonnet",
        injected_prompt_sha256=digest,
        agent_name=None,
        evidence_path=str(evidence_path),
        tokens_used=10,
        diff_verified_files=[],
        no_op_justified=False,
    )

    result = _run_ctl("run-execution-batch-plan", str(task_dir))
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["completed_segments"] == ["seg-1"]
    assert payload["cached_segments"] == []
    assert [entry["segment_id"] for entry in payload["next_batch"]] == ["seg-2"]


def test_run_execution_segment_done_writes_receipt_and_progress(tmp_path) -> None:
    from lib_receipts import receipt_executor_routing, receipt_plan_validated

    task_dir = _setup_task_dir(tmp_path)
    manifest_path = task_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["stage"] = "EXECUTION"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    os.environ["DYNOS_ALLOW_TEST_OVERRIDE"] = "1"
    try:
        receipt_plan_validated(task_dir, validation_passed_override=True)
    finally:
        os.environ.pop("DYNOS_ALLOW_TEST_OVERRIDE", None)

    receipt_executor_routing(task_dir, [
        {
            "segment_id": "seg-1",
            "executor": "backend-executor",
            "model": "sonnet",
            "route_mode": "generic",
            "agent_path": None,
        },
        {
            "segment_id": "seg-2",
            "executor": "testing-executor",
            "model": "sonnet",
            "route_mode": "generic",
            "agent_path": None,
        },
    ])

    evidence_dir = task_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = evidence_dir / "seg-1.md"
    evidence_path.write_text("seg-1 evidence\n")

    sidecar_dir = task_dir / "receipts" / "_injected-prompts"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    digest = "d" * 64
    (sidecar_dir / "seg-1.sha256").write_text(digest)

    result = _run_ctl(
        "run-execution-segment-done",
        str(task_dir),
        "seg-1",
        "--injected-prompt-sha256",
        digest,
        "--model",
        "sonnet",
        "--tokens-used",
        "12",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "segment_finalized"
    assert Path(payload["receipt_path"]).exists()
    assert payload["execution_progress"]["completed_segments"] == ["seg-1"]
    assert payload["execution_progress"]["next_batch"] == ["seg-2"]

    manifest = json.loads(manifest_path.read_text())
    assert manifest["execution_progress"]["completed_segments"] == ["seg-1"]


def test_run_execution_segment_done_rejects_ownership_violation(tmp_path) -> None:
    from lib_receipts import receipt_executor_routing, receipt_plan_validated

    task_dir = _setup_task_dir(tmp_path)
    manifest_path = task_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["stage"] = "EXECUTION"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    os.environ["DYNOS_ALLOW_TEST_OVERRIDE"] = "1"
    try:
        receipt_plan_validated(task_dir, validation_passed_override=True)
    finally:
        os.environ.pop("DYNOS_ALLOW_TEST_OVERRIDE", None)

    receipt_executor_routing(task_dir, [
        {
            "segment_id": "seg-1",
            "executor": "backend-executor",
            "model": "sonnet",
            "route_mode": "generic",
            "agent_path": None,
        },
        {
            "segment_id": "seg-2",
            "executor": "testing-executor",
            "model": "sonnet",
            "route_mode": "generic",
            "agent_path": None,
        },
    ])

    evidence_dir = task_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = evidence_dir / "seg-1.md"
    evidence_path.write_text("seg-1 evidence\n")

    sidecar_dir = task_dir / "receipts" / "_injected-prompts"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    digest = "e" * 64
    (sidecar_dir / "seg-1.sha256").write_text(digest)

    result = _run_ctl(
        "run-execution-segment-done",
        str(task_dir),
        "seg-1",
        "--injected-prompt-sha256",
        digest,
        "--files",
        "src/a.py",
        "src/rogue.py",
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "segment_invalid"
    assert payload["error"] == "ownership violation"
    assert "src/rogue.py" in payload["unauthorized_files"]


def test_run_execution_finish_transitions_when_no_pending_segments(tmp_path) -> None:
    from lib_receipts import (
        receipt_executor_done,
        receipt_executor_routing,
        receipt_plan_validated,
        receipt_rules_check_passed,
    )

    task_dir = _setup_task_dir(tmp_path)
    manifest_path = task_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["stage"] = "EXECUTION"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    os.environ["DYNOS_ALLOW_TEST_OVERRIDE"] = "1"
    try:
        receipt_plan_validated(task_dir, validation_passed_override=True)
    finally:
        os.environ.pop("DYNOS_ALLOW_TEST_OVERRIDE", None)

    receipt_executor_routing(task_dir, [
        {
            "segment_id": "seg-1",
            "executor": "backend-executor",
            "model": "sonnet",
            "route_mode": "generic",
            "agent_path": None,
        },
        {
            "segment_id": "seg-2",
            "executor": "testing-executor",
            "model": "sonnet",
            "route_mode": "generic",
            "agent_path": None,
        },
    ])

    evidence_dir = task_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    for seg_id in ("seg-1", "seg-2"):
        evidence_path = evidence_dir / f"{seg_id}.md"
        evidence_path.write_text(f"{seg_id} evidence\n")
        sidecar_dir = task_dir / "receipts" / "_injected-prompts"
        sidecar_dir.mkdir(parents=True, exist_ok=True)
        digest = ("f" if seg_id == "seg-1" else "a") * 64
        (sidecar_dir / f"{seg_id}.sha256").write_text(digest)
        receipt_executor_done(
            task_dir,
            segment_id=seg_id,
            executor_type="backend-executor" if seg_id == "seg-1" else "testing-executor",
            model_used="sonnet",
            injected_prompt_sha256=digest,
            agent_name=None,
            evidence_path=str(evidence_path),
            tokens_used=5,
            diff_verified_files=[],
            no_op_justified=False,
        )
    receipt_rules_check_passed(task_dir, "all")

    result = _run_ctl("run-execution-finish", str(task_dir))
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "test_execution_ready"
    assert payload["stage"] == "TEST_EXECUTION"

    manifest = json.loads(manifest_path.read_text())
    assert manifest["stage"] == "TEST_EXECUTION"


def test_run_execution_finish_blocks_when_pending_segments_remain(tmp_path) -> None:
    from lib_receipts import receipt_executor_routing, receipt_plan_validated

    task_dir = _setup_task_dir(tmp_path)
    manifest_path = task_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["stage"] = "EXECUTION"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    os.environ["DYNOS_ALLOW_TEST_OVERRIDE"] = "1"
    try:
        receipt_plan_validated(task_dir, validation_passed_override=True)
    finally:
        os.environ.pop("DYNOS_ALLOW_TEST_OVERRIDE", None)

    receipt_executor_routing(task_dir, [
        {
            "segment_id": "seg-1",
            "executor": "backend-executor",
            "model": "sonnet",
            "route_mode": "generic",
            "agent_path": None,
        },
        {
            "segment_id": "seg-2",
            "executor": "testing-executor",
            "model": "sonnet",
            "route_mode": "generic",
            "agent_path": None,
        },
    ])

    result = _run_ctl("run-execution-finish", str(task_dir))
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "blocked"
    assert payload["pending_segments"] == ["seg-1", "seg-2"]


def test_run_repair_execution_ready_validates_and_transitions(tmp_path) -> None:
    task_dir = _setup_task_dir(tmp_path)
    manifest_path = task_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["stage"] = "REPAIR_PLANNING"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    audit_dir = task_dir / "audit-reports"
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "security-auditor.json").write_text(json.dumps({
        "auditor_name": "security-auditor",
        "findings": [
            {"id": "sec-1", "severity": "high", "blocking": True},
        ],
    }) + "\n")
    (task_dir / "repair-log.json").write_text(json.dumps({
        "repair_cycle": 1,
        "batches": [
            {
                "batch_id": "batch-1",
                "tasks": [
                    {
                        "finding_id": "sec-1",
                        "auditor": "security-auditor",
                        "severity": "high",
                        "assigned_executor": "backend-executor",
                        "instruction": "Remove the hardcoded secret and use env configuration",
                        "files_to_modify": ["src/a.py"],
                        "retry_count": 0,
                    }
                ],
            }
        ],
    }) + "\n")

    result = _run_ctl("run-repair-execution-ready", str(task_dir))
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "repair_execution_ready"
    assert payload["stage"] == "REPAIR_EXECUTION"

    manifest = json.loads(manifest_path.read_text())
    assert manifest["stage"] == "REPAIR_EXECUTION"


def test_run_repair_log_build_writes_repair_log_deterministically(tmp_path) -> None:
    task_dir = _setup_task_dir(tmp_path)
    manifest_path = task_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["stage"] = "REPAIR_PLANNING"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    audit_dir = task_dir / "audit-reports"
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "security-auditor.json").write_text(json.dumps({
        "auditor_name": "security-auditor",
        "findings": [
            {"id": "SEC-1", "severity": "high", "blocking": True},
            {"id": "CQ-1", "severity": "medium", "blocking": True},
        ],
    }) + "\n")
    (task_dir / "repair-cycle-plan.json").write_text(json.dumps({
        "status": "repair_cycle_ready",
        "repair_cycle": 1,
        "phase": "phase_1",
        "blocking_findings": [
            {
                "id": "SEC-1",
                "file": "src/a.py",
                "blocking": True,
                "retry_count": 0,
            },
            {
                "id": "CQ-1",
                "evidence": {"file": "tests/test_a.py", "line": 9},
                "blocking": True,
                "retry_count": 2,
                "model_override": "opus",
            },
        ],
    }) + "\n")

    dynos_home = tmp_path / ".dynos-home"
    dynos_home.mkdir(exist_ok=True)
    result = _run_ctl("run-repair-log-build", str(task_dir), env={"DYNOS_HOME": str(dynos_home)})
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "repair_log_built"
    repair_log = json.loads((task_dir / "repair-log.json").read_text())
    assert repair_log["source"] == "deterministic_ctl"
    assert repair_log["repair_cycle"] == 1
    all_tasks = [task for batch in repair_log["batches"] for task in batch["tasks"]]
    by_finding = {task["finding_id"]: task for task in all_tasks}
    assert by_finding["SEC-1"]["files_to_modify"] == ["src/a.py"]
    assert by_finding["CQ-1"]["files_to_modify"] == ["tests/test_a.py"]
    assert by_finding["CQ-1"]["retry_count"] == 2


def test_run_repair_batch_plan_groups_parallel_batches_deterministically(tmp_path) -> None:
    task_dir = _setup_task_dir(tmp_path)
    manifest_path = task_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["stage"] = "REPAIR_EXECUTION"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    audit_dir = task_dir / "audit-reports"
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "security-auditor.json").write_text(json.dumps({
        "auditor_name": "security-auditor",
        "findings": [
            {"id": "sec-1", "severity": "high", "blocking": True},
            {"id": "cq-1", "severity": "medium", "blocking": True},
            {"id": "sec-2", "severity": "high", "blocking": True},
        ],
    }) + "\n")
    (task_dir / "repair-log.json").write_text(json.dumps({
        "repair_cycle": 2,
        "batches": [
            {
                "batch_id": "batch-1",
                "parallel": True,
                    "tasks": [
                        {
                            "finding_id": "sec-1",
                            "auditor": "security-auditor",
                            "severity": "high",
                            "assigned_executor": "backend-executor",
                            "instruction": "Fix backend issue one",
                            "files_to_modify": ["src/a.py"],
                            "retry_count": 0,
                        }
                    ],
                },
            {
                "batch_id": "batch-2",
                "parallel": True,
                    "tasks": [
                        {
                            "finding_id": "cq-1",
                            "auditor": "security-auditor",
                            "severity": "medium",
                            "assigned_executor": "testing-executor",
                            "instruction": "Tighten the test assertions",
                            "files_to_modify": ["tests/test_a.py"],
                            "retry_count": 2,
                            "model_override": "opus",
                    }
                ],
            },
            {
                "batch_id": "batch-3",
                "parallel": True,
                    "tasks": [
                        {
                            "finding_id": "sec-2",
                            "auditor": "security-auditor",
                            "severity": "high",
                            "assigned_executor": "backend-executor",
                            "instruction": "Fix backend issue two",
                            "files_to_modify": ["src/a.py"],
                            "retry_count": 0,
                        }
                ],
            },
        ],
    }) + "\n")

    result = _run_ctl("run-repair-batch-plan", str(task_dir))
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "repair_batch_plan_ready"
    assert payload["repair_cycle"] == 2
    assert payload["next_group"] == ["batch-1", "batch-2"]
    assert payload["execution_groups"][0]["parallel"] is True
    assert payload["execution_groups"][1]["parallel"] is True
    assert payload["execution_groups"][1]["batch_ids"] == ["batch-3"]
    assert payload["execution_groups"][0]["batches"][1]["model_overrides"] == {"cq-1": "opus"}


def test_run_repair_q_update_builds_outcomes_from_repair_log_and_reports(tmp_path) -> None:
    task_dir = _setup_task_dir(tmp_path)
    manifest_path = task_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["stage"] = "CHECKPOINT_AUDIT"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    (task_dir / "repair-log.json").write_text(json.dumps({
        "repair_cycle": 1,
        "batches": [
            {
                "batch_id": "batch-1",
                "tasks": [
                    {
                        "finding_id": "SEC-1",
                        "assigned_executor": "backend-executor",
                        "files_to_modify": ["src/a.py"],
                        "retry_count": 0,
                        "severity": "high",
                        "state": "SEC:high:feature:0",
                        "route_mode": "generic",
                        "model_override": "opus",
                    }
                ],
            }
        ],
    }) + "\n")
    audit_dir = task_dir / "audit-reports"
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "security-auditor.json").write_text(json.dumps({
        "auditor_name": "security-auditor",
        "findings": [
            {"id": "SEC-1", "blocking": True, "severity": "high"},
            {"id": "CQ-2", "blocking": True, "severity": "medium"},
        ],
    }) + "\n")
    dynos_home = tmp_path / ".dynos-home"
    dynos_home.mkdir(exist_ok=True)

    result = _run_ctl("run-repair-q-update", str(task_dir), env={"DYNOS_HOME": str(dynos_home)})
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "repair_q_updated"
    assert payload["outcome_count"] == 1
    assert payload["new_blocking_ids"] == ["CQ-2"]
    assert payload["update_result"]["updated"] in {True, False}


def test_run_repair_retry_reports_escalation_on_retry_cap(tmp_path) -> None:
    task_dir = _setup_task_dir(tmp_path)
    manifest_path = task_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["stage"] = "REPAIR_EXECUTION"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    (task_dir / "repair-log.json").write_text(json.dumps({
        "repair_cycle": 2,
        "batches": [
            {
                "batch_id": "batch-1",
                "tasks": [
                    {
                        "finding_id": "sec-1",
                        "assigned_executor": "backend-executor",
                        "files_to_modify": ["src/a.py"],
                        "retry_count": 3,
                    }
                ],
            }
        ],
    }) + "\n")

    result = _run_ctl("run-repair-retry", str(task_dir))
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "escalation_required"
    assert "retry" in payload["error"].lower()


def test_run_audit_reflect_writes_retro_and_receipt(tmp_path) -> None:
    from lib_receipts import receipt_audit_routing, receipt_executor_routing

    task_dir = _setup_task_dir(tmp_path)
    prior_task = task_dir.parent / "task-20260402-001"
    prior_task.mkdir(parents=True, exist_ok=True)
    (prior_task / "task-retrospective.json").write_text(json.dumps({
        "task_id": "task-20260402-001",
        "task_outcome": "DONE",
        "task_type": "feature",
        "task_domains": "backend",
        "task_risk_level": "medium",
        "findings_by_auditor": {},
        "findings_by_category": {},
        "executor_repair_frequency": {},
        "spec_review_iterations": 1,
        "repair_cycle_count": 0,
        "subagent_spawn_count": 0,
        "wasted_spawns": 0,
        "auditor_zero_finding_streaks": {"security-auditor": 2},
        "executor_zero_repair_streak": 4,
        "token_usage_by_agent": {},
        "total_token_usage": 0,
        "quality_score": 1.0,
        "cost_score": 1.0,
        "efficiency_score": 1.0,
        "agent_source": {},
        "model_used_by_agent": {},
    }) + "\n")
    audit_dir = task_dir / "audit-reports"
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / "security-auditor.json").write_text(json.dumps({
        "auditor_name": "security-auditor",
        "findings": [],
    }) + "\n")
    receipt_audit_routing(task_dir, [
        {
            "name": "security-auditor",
            "action": "spawn",
            "route_mode": "alongside",
            "agent_path": "learned/security-ruthless.md",
            "injected_agent_sha256": "abc123",
        }
    ])
    receipt_executor_routing(task_dir, [
        {
            "segment_id": "seg-1",
            "executor": "backend-executor",
            "model": "sonnet",
            "route_mode": "replace",
            "agent_path": "learned/backend-sharp.md",
        }
    ])

    result = _run_ctl("run-audit-reflect", str(task_dir))
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "reflect_ready"
    assert Path(payload["retrospective_path"]).exists()
    assert Path(payload["receipt_path"]).exists()
    retro = json.loads(Path(payload["retrospective_path"]).read_text())
    assert retro["agent_source"]["security-auditor"] == "generic"
    assert retro["agent_source"]["security-auditor:learned"] == "learned:security-ruthless"
    assert retro["agent_source"]["backend-executor"] == "learned:backend-sharp"
    assert retro["auditor_zero_finding_streaks"]["security-auditor"] == 3
    assert retro["executor_zero_repair_streak"] == 5
    assert retro["alongside_overlap"]["security-auditor"]["learned_is_superset"] is True


def test_run_audit_finish_writes_completion_and_transitions_done(tmp_path) -> None:
    from lib_receipts import (
        hash_file,
        receipt_audit_routing,
        receipt_postmortem_skipped,
        receipt_retrospective,
        receipt_rules_check_passed,
    )

    task_dir = _setup_task_dir(tmp_path)
    manifest_path = task_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["stage"] = "CHECKPOINT_AUDIT"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    (task_dir / "audit-summary.json").write_text(json.dumps({
        "audit_result": "pass",
        "total_findings": 0,
        "total_blocking": 0,
    }) + "\n")
    (task_dir / "task-retrospective.json").write_text(json.dumps({
        "task_id": "task-20260403-001",
        "task_outcome": "DONE",
        "task_type": "feature",
        "task_domains": "backend",
        "task_risk_level": "medium",
        "findings_by_auditor": {},
        "findings_by_category": {},
        "executor_repair_frequency": {},
        "spec_review_iterations": 1,
        "repair_cycle_count": 0,
        "subagent_spawn_count": 0,
        "wasted_spawns": 0,
        "auditor_zero_finding_streaks": {},
        "executor_zero_repair_streak": 0,
        "token_usage_by_agent": {},
        "total_token_usage": 0,
        "quality_score": 1.0,
        "cost_score": 1.0,
        "efficiency_score": 1.0,
        "model_used_by_agent": {},
        "agent_source": {},
        "alongside_overlap": {},
    }) + "\n")
    audit_dir = task_dir / "audit-reports"
    audit_dir.mkdir(exist_ok=True)
    (audit_dir / "report.json").write_text(json.dumps({"findings": []}) + "\n")
    receipts = task_dir / "receipts"
    receipts.mkdir(exist_ok=True)
    receipt_audit_routing(task_dir, [
        {"name": "spec-completion-auditor", "action": "skip", "reason": "fixture", "route_mode": "generic", "agent_path": None},
        {"name": "security-auditor", "action": "skip", "reason": "fixture", "route_mode": "generic", "agent_path": None},
        {"name": "code-quality-auditor", "action": "skip", "reason": "fixture", "route_mode": "generic", "agent_path": None},
        {"name": "dead-code-auditor", "action": "skip", "reason": "fixture", "route_mode": "generic", "agent_path": None},
        {"name": "performance-auditor", "action": "skip", "reason": "fixture", "route_mode": "generic", "agent_path": None},
    ])
    receipt_retrospective(task_dir)
    receipt_postmortem_skipped(
        task_dir,
        reason="no-findings",
        retrospective_sha256=hash_file(task_dir / "task-retrospective.json"),
        subsumed_by=[],
    )
    receipt_rules_check_passed(task_dir, "all")

    result = _run_ctl("run-audit-finish", str(task_dir))
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "done"
    assert Path(payload["completion_path"]).exists()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["stage"] == "DONE"


def test_write_execute_handoff_uses_live_manifest_stage(tmp_path) -> None:
    task_dir = _setup_task_dir(tmp_path)
    manifest_path = task_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["stage"] = "TEST_EXECUTION"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    result = _run_ctl("write-execute-handoff", str(task_dir))
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "execute_handoff_ready"
    assert payload["from_skill"] == "execute"
    assert payload["to_skill"] == "audit"
    assert payload["manifest_stage"] == "TEST_EXECUTION"
    assert payload["contract_version"] == "1.0.0"

    handoff = json.loads((task_dir / "handoff-execute-audit.json").read_text())
    assert handoff["manifest_stage"] == "TEST_EXECUTION"
    assert handoff["from_skill"] == "execute"
    assert handoff["to_skill"] == "audit"


def test_write_classification_persists_normalized_payload_and_syncs_manifest(tmp_path) -> None:
    task_dir = _setup_task_dir(tmp_path)
    payload_path = tmp_path / "classification.json"
    payload_path.write_text(json.dumps({
        "type": "bugfix",
        "domains": ["backend", "backend", "", "security"],
        "risk_level": "low",
        "notes": "  tighten auth path  ",
    }))

    result = _run_ctl("write-classification", str(task_dir), "--from", str(payload_path))
    assert result.returncode == 0, result.stdout + result.stderr

    classification = json.loads((task_dir / "classification.json").read_text())
    manifest = json.loads((task_dir / "manifest.json").read_text())
    assert classification == manifest["classification"]
    assert classification["domains"] == ["backend", "security"]
    assert classification["notes"] == "tighten auth path"
    assert classification["fast_track"] is False
    assert manifest["fast_track"] is False


def test_write_classification_rejects_invalid_domain(tmp_path) -> None:
    task_dir = _setup_task_dir(tmp_path)
    payload_path = tmp_path / "classification.json"
    payload_path.write_text(json.dumps({
        "type": "feature",
        "domains": ["backend", "unknown-domain"],
        "risk_level": "medium",
        "notes": "",
    }))

    result = _run_ctl("write-classification", str(task_dir), "--from", str(payload_path))
    assert result.returncode == 1
    assert "classification domain invalid" in result.stderr
