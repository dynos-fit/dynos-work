"""Tests for PLAN_AUDIT exit risk gate (AC 11)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_core import transition_task  # noqa: E402
from lib_receipts import receipt_plan_audit, hash_file  # noqa: E402


def _setup(tmp_path: Path, *, risk: str, tdd_required: bool | None = None) -> Path:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260418-PA"
    td.mkdir(parents=True)
    # Create the three artifacts the PLAN_AUDIT exit gate's hash-binding
    # check re-hashes. Without these files the hash match would fail and
    # high/critical-risk transitions would refuse even with a receipt.
    (td / "spec.md").write_text("# spec")
    (td / "plan.md").write_text("# plan")
    (td / "execution-graph.json").write_text('{"segments": []}')
    classification: dict = {"risk_level": risk}
    if tdd_required is not None:
        classification["tdd_required"] = tdd_required
    (td / "manifest.json").write_text(json.dumps({
        "task_id": "task-20260418-PA",
        "stage": "PLAN_AUDIT",
        "classification": classification,
    }))
    return td


def _write_plan_audit(td: Path) -> None:
    """Write a plan-audit-check receipt bound to the current artifact hashes."""
    receipt_plan_audit(
        td,
        tokens_used=100,
        finding_count=0,
        spec_sha256=hash_file(td / "spec.md"),
        plan_sha256=hash_file(td / "plan.md"),
        graph_sha256=hash_file(td / "execution-graph.json"),
    )


def test_critical_without_receipt_refuses(tmp_path: Path):
    td = _setup(tmp_path, risk="critical")
    with pytest.raises(ValueError, match="plan-audit-check"):
        transition_task(td, "PRE_EXECUTION_SNAPSHOT")


def test_high_without_receipt_refuses(tmp_path: Path):
    td = _setup(tmp_path, risk="high")
    with pytest.raises(ValueError, match="plan-audit-check"):
        transition_task(td, "TDD_REVIEW")


def test_critical_with_receipt_passes(tmp_path: Path):
    td = _setup(tmp_path, risk="critical")
    _write_plan_audit(td)
    transition_task(td, "PRE_EXECUTION_SNAPSHOT")
    assert json.loads((td / "manifest.json").read_text())["stage"] == "PRE_EXECUTION_SNAPSHOT"


def test_low_without_receipt_passes(tmp_path: Path):
    td = _setup(tmp_path, risk="low")
    transition_task(td, "PRE_EXECUTION_SNAPSHOT")
    assert json.loads((td / "manifest.json").read_text())["stage"] == "PRE_EXECUTION_SNAPSHOT"


def test_medium_without_receipt_passes(tmp_path: Path):
    td = _setup(tmp_path, risk="medium")
    transition_task(td, "PRE_EXECUTION_SNAPSHOT")
    assert json.loads((td / "manifest.json").read_text())["stage"] == "PRE_EXECUTION_SNAPSHOT"


def test_tdd_required_blocks_plan_audit_to_pre_exec(tmp_path: Path):
    td = _setup(tmp_path, risk="medium", tdd_required=True)
    with pytest.raises(ValueError, match="tdd_required"):
        transition_task(td, "PRE_EXECUTION_SNAPSHOT")


def test_tdd_required_does_not_block_plan_audit_to_tdd_review(tmp_path: Path):
    td = _setup(tmp_path, risk="medium", tdd_required=True)
    transition_task(td, "TDD_REVIEW")
    assert json.loads((td / "manifest.json").read_text())["stage"] == "TDD_REVIEW"
