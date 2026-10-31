"""Gap-analysis misses at PLAN_AUDIT are advisory for an unchanged plan.

The approved plan already passed run-planning's gap check. When the
plan-validated receipt's hashes still match spec.md / plan.md /
execution-graph.json, a gap miss at PLAN_AUDIT comes from repo drift or a
scanner false negative, not the plan, so run-plan-audit records it as a
warning (output, plan-audit-gap-warnings.json, plan-audit-check receipt)
instead of sending the task back to PLANNING for a fresh human approval.
A plan changed since validation still blocks.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

from lib_receipts import read_receipt, write_receipt  # noqa: E402
from receipts.stage import _hash_artifact  # noqa: E402

PLAN = (
    "# Implementation Plan\n\n"
    "## Technical Approach\nA.\n\n"
    "## API Contracts\n\n"
    "| Method | Endpoint | Purpose |\n"
    "|---|---|---|\n"
    "| GET | /api/does-not-exist | read |\n\n"
    "## Data Model\n\n"
    "| Model | Scope |\n|---|---|\n"
    "| GhostModel | existing |\n\n"
    "## Test Strategy\nB.\n"
)


def _task(tmp_path: Path) -> Path:
    task_dir = tmp_path / ".dynos" / "task-20261031-001"
    task_dir.mkdir(parents=True)
    (task_dir / "manifest.json").write_text(json.dumps({
        "task_id": task_dir.name,
        "created_at": "2026-10-31T00:00:00Z",
        "raw_input": "x",
        "stage": "PLAN_AUDIT",
        "classification": {"type": "feature", "domains": ["backend"], "risk_level": "medium", "notes": "n"},
    }))
    (task_dir / "plan.md").write_text(PLAN)
    return task_dir


def _validate(task_dir: Path) -> None:
    """Stand-in for run-planning's plan-validated receipt over the current plan."""
    write_receipt(
        task_dir, "plan-validated", segment_count=0, criteria_coverage=[], validation_passed=True,
        artifact_hashes={name: _hash_artifact(task_dir / name)
                         for name in ("spec.md", "plan.md", "execution-graph.json")},
    )


def _run(task_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "ctl.py"), "run-plan-audit", str(task_dir)],
        cwd=ROOT, text=True, capture_output=True, check=False,
        env={**os.environ, "DYNOS_GAP_CACHE": "0"},
    )


def test_unchanged_validated_plan_gets_advisory_gap_warnings(tmp_path: Path) -> None:
    task_dir = _task(tmp_path)
    _validate(task_dir)

    result = _run(task_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "passed"
    assert payload["gap_advisory"] is True
    assert "unverified API contract: GET /api/does-not-exist" in payload["gap_warnings"]
    assert "unverified data model entry: ghostmodel" in payload["gap_warnings"]
    assert "run-planning" in payload["gap_advisory_reason"]
    assert json.loads((task_dir / "manifest.json").read_text())["stage"] == "PLAN_AUDIT"

    artifact = json.loads((task_dir / "plan-audit-gap-warnings.json").read_text())
    assert artifact["advisory"] is True
    assert artifact["warnings"] == payload["gap_warnings"]
    assert artifact["api_contracts_unverified"] == [{"endpoint": "/api/does-not-exist", "method": "GET"}]
    assert payload["gap_warnings_path"] == str(task_dir / "plan-audit-gap-warnings.json")

    receipt = read_receipt(task_dir, "plan-audit-check")
    assert receipt is not None
    assert receipt["gap_warnings"] == payload["gap_warnings"]
    assert receipt["gap_warnings_path"] == payload["gap_warnings_path"]


def test_plan_changed_since_validation_still_blocks(tmp_path: Path) -> None:
    task_dir = _task(tmp_path)
    _validate(task_dir)
    (task_dir / "plan.md").write_text(PLAN + "\nEdited after validation.\n")

    result = _run(task_dir)

    assert result.returncode == 1, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "replan_required"
    assert payload["transitioned_to"] == "PLANNING"
    assert not (task_dir / "plan-audit-gap-warnings.json").exists()


def test_no_plan_validated_receipt_still_blocks(tmp_path: Path) -> None:
    task_dir = _task(tmp_path)
    result = _run(task_dir)
    assert result.returncode == 1
    assert json.loads(result.stdout)["status"] == "replan_required"


def test_failed_validation_receipt_does_not_make_misses_advisory(tmp_path: Path) -> None:
    task_dir = _task(tmp_path)
    write_receipt(
        task_dir, "plan-validated", segment_count=0, criteria_coverage=[], validation_passed=False,
        artifact_hashes={name: _hash_artifact(task_dir / name)
                         for name in ("spec.md", "plan.md", "execution-graph.json")},
    )
    result = _run(task_dir)
    assert result.returncode == 1
    assert json.loads(result.stdout)["status"] == "replan_required"


def test_clean_rerun_supersedes_stale_warnings(tmp_path: Path) -> None:
    task_dir = _task(tmp_path)
    _validate(task_dir)
    assert _run(task_dir).returncode == 0
    # The repo now defines both claims; a rerun finds nothing to warn about.
    (tmp_path / "app.py").write_text(
        "@app.get('/api/does-not-exist')\ndef f():\n    pass\n\n"
        "class GhostModel(Base):\n    pass\n"
    )
    result = _run(task_dir)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["gap_advisory"] is False
    assert payload["gap_warnings"] == []
    artifact = json.loads((task_dir / "plan-audit-gap-warnings.json").read_text())
    assert artifact["advisory"] is False and artifact["warnings"] == []
