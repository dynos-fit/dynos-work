"""Tests for PRE_EXECUTION_SNAPSHOT -> EXECUTION tdd-tests gate (AC 8).

When manifest.classification.tdd_required is True, the transition requires:
  * a tdd-tests receipt (v2+) whose tests_evidence_sha256 matches the live
    sha256 of evidence/tdd-tests.md.
When tdd_required is False (or absent), the gate is inert. force=True bypasses.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_core import transition_task  # noqa: E402
from lib_receipts import hash_file, receipt_tdd_tests  # noqa: E402


@pytest.fixture(autouse=True)
def _enable_test_override(monkeypatch):
    """task-007 B-004: receipt_plan_validated honors
    validation_passed_override only when DYNOS_ALLOW_TEST_OVERRIDE=1."""
    monkeypatch.setenv("DYNOS_ALLOW_TEST_OVERRIDE", "1")


def _setup(tmp_path: Path, *, tdd_required: bool | None = True,
           risk_level: str = "high") -> Path:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260419-TD"
    td.mkdir(parents=True)
    classification = {"risk_level": risk_level}
    if tdd_required is not None:
        classification["tdd_required"] = tdd_required
    (td / "manifest.json").write_text(json.dumps({
        "task_id": td.name,
        "stage": "PRE_EXECUTION_SNAPSHOT",
        "classification": classification,
    }))
    # Self-computing receipt_plan_validated hashes these artifacts.
    (td / "spec.md").write_text("# spec\n")
    (td / "plan.md").write_text("# plan\n")
    (td / "execution-graph.json").write_text('{"segments": []}\n')
    # plan-validated receipt is required for EXECUTION transition.
    from lib_receipts import receipt_plan_validated
    receipt_plan_validated(td, validation_passed_override=True)
    return td


def _write_evidence(td: Path, content: str = "# TDD tests\n- test A\n- test B\n") -> Path:
    evidence_dir = td / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    path = evidence_dir / "tdd-tests.md"
    path.write_text(content)
    return path


def test_tdd_required_false_gate_inert(tmp_path: Path):
    """AC 8: tdd_required=False → gate is inert, transition passes."""
    td = _setup(tmp_path, tdd_required=False)
    # No tdd-tests receipt, no evidence — should still transition.
    transition_task(td, "EXECUTION")
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == "EXECUTION"


def test_tdd_required_absent_low_risk_gate_inert(tmp_path: Path):
    """Task-007 AC 12: tdd_required absent + risk=low → apply_fast_track
    backfills tdd_required=False → gate is inert. The historical "absent ==
    inert" contract still holds for low-risk tasks; high/critical are handled
    separately by test_tdd_required_absent_high_risk_backfilled_true."""
    td = _setup(tmp_path, tdd_required=None, risk_level="low")
    transition_task(td, "EXECUTION")
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == "EXECUTION"
    assert manifest["classification"]["tdd_required"] is False


def test_tdd_required_absent_high_risk_backfilled_true(tmp_path: Path):
    """Task-007 AC 12: tdd_required absent + risk=high → apply_fast_track
    backfills tdd_required=True → gate now enforces the tdd-tests receipt.
    Without a receipt, the transition must refuse."""
    td = _setup(tmp_path, tdd_required=None, risk_level="high")
    with pytest.raises(ValueError, match="tdd-tests"):
        transition_task(td, "EXECUTION")


def test_tdd_required_true_with_valid_receipt_passes(tmp_path: Path):
    """AC 8: tdd_required=True + valid receipt + matching live hash → pass."""
    td = _setup(tmp_path, tdd_required=True)
    evidence_path = _write_evidence(td)
    evidence_hash = hash_file(evidence_path)
    receipt_tdd_tests(
        td,
        test_file_paths=["tests/test_foo.py"],
        tests_evidence_sha256=evidence_hash,
        tokens_used=100,
        model_used="sonnet",
    )
    transition_task(td, "EXECUTION")
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == "EXECUTION"


def test_tdd_required_true_missing_receipt_refuses(tmp_path: Path):
    """AC 8: tdd_required=True + no tdd-tests receipt → refuse."""
    td = _setup(tmp_path, tdd_required=True)
    _write_evidence(td)
    with pytest.raises(ValueError) as exc_info:
        transition_task(td, "EXECUTION")
    msg = str(exc_info.value)
    assert "tdd-tests" in msg
    assert "missing" in msg or "tdd_required" in msg


def test_tdd_required_true_hash_drift_refuses(tmp_path: Path):
    """AC 8: evidence file drifted after receipt written → refuse with
    message containing 'tdd-tests' and 'hash mismatch'."""
    td = _setup(tmp_path, tdd_required=True)
    evidence_path = _write_evidence(td, "original content\n")
    evidence_hash = hash_file(evidence_path)
    receipt_tdd_tests(
        td,
        test_file_paths=["tests/test_foo.py"],
        tests_evidence_sha256=evidence_hash,
        tokens_used=100,
        model_used="sonnet",
    )
    # Drift the evidence file
    evidence_path.write_text("drifted content\n")
    with pytest.raises(ValueError) as exc_info:
        transition_task(td, "EXECUTION")
    msg = str(exc_info.value)
    assert "tdd-tests" in msg
    assert "hash mismatch" in msg


def test_force_true_bypasses_gate(tmp_path: Path):
    """AC 8: force=True bypasses even with missing receipt."""
    td = _setup(tmp_path, tdd_required=True)
    # No receipt, no evidence
    transition_task(
        td,
        "EXECUTION",
        force=True,
        force_reason="test: tdd-tests receipt gate bypass",
        force_approver="test-suite",
    )
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == "EXECUTION"


def test_tdd_required_true_missing_evidence_refuses(tmp_path: Path):
    """AC 8: receipt present but evidence file missing → refuse."""
    td = _setup(tmp_path, tdd_required=True)
    receipt_tdd_tests(
        td,
        test_file_paths=["tests/test_foo.py"],
        tests_evidence_sha256="deadbeef" * 8,
        tokens_used=100,
        model_used="sonnet",
    )
    # No evidence written
    with pytest.raises(ValueError) as exc_info:
        transition_task(td, "EXECUTION")
    msg = str(exc_info.value)
    assert "tdd-tests" in msg
