#!/usr/bin/env python3

from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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
                    },
                    {
                        "id": "seg-2",
                        "executor": "testing-executor",
                        "description": "Build tests",
                        "files_expected": ["tests/test_a.py"],
                        "depends_on": ["seg-1"],
                        "parallelizable": True,
                        "criteria_ids": [2],
                    },
                ],
            },
            indent=2,
        )
        + "\n"
    )
    return task_dir


def _run_ctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(ROOT / "hooks" / "ctl.py"), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
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


def test_run_external_solution_gate_recommends_search_for_integration_tasks(tmp_path) -> None:
    task_dir = _setup_task_dir(tmp_path)
    manifest_path = task_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["raw_input"] = "Set up Stripe webhook retry handling for our API integration"
    manifest["classification"]["type"] = "migration"
    manifest["classification"]["domains"] = ["backend", "infra"]
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    result = _run_ctl("run-external-solution-gate", str(task_dir))
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "external_solution_gate_ready"
    assert payload["search_recommended"] is True
    gate = json.loads((task_dir / "external-solution-gate.json").read_text())
    assert gate["search_recommended"] is True
    assert gate["search_used"] is False
    assert gate["candidates"] == []
    assert gate["recommended_choice"] is None
    assert "stripe" in gate["decision_basis"]["trigger_matches"]


def test_run_external_solution_gate_skips_search_for_file_scoped_bugfix(tmp_path) -> None:
    task_dir = _setup_task_dir(tmp_path)
    manifest_path = task_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["raw_input"] = "Fix regression in src/auth/service.py failing test for null token handling"
    manifest["classification"]["type"] = "bugfix"
    manifest["classification"]["domains"] = ["backend"]
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    result = _run_ctl("run-external-solution-gate", str(task_dir))
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["search_recommended"] is False
    gate = json.loads((task_dir / "external-solution-gate.json").read_text())
    assert gate["search_recommended"] is False
    assert gate["query_reason"] == "local repo evidence is sufficient"
    assert gate["decision_basis"]["file_scoped"] is True


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
