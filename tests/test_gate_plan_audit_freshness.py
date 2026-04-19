"""Tests for the PLAN_AUDIT exit gate hash-bound freshness check (F4, CRITERION 4).

PLAN_AUDIT → {TDD_REVIEW, PRE_EXECUTION_SNAPSHOT} on high/critical-risk
tasks now requires `plan_audit_matches(task_dir) is True`. Drift
(descriptive string) and missing (False) emit distinct error messages.
Low/medium-risk tasks still skip the receipt requirement entirely so
this test file's low-risk case preserves the pre-F4 behavior.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_core import transition_task  # noqa: E402
from lib_receipts import hash_file, receipt_plan_audit  # noqa: E402


def _setup(tmp_path: Path, *, risk: str) -> Path:
    project = tmp_path / "project"
    td = project / ".dynos" / f"task-20260419-PAF-{risk}"
    td.mkdir(parents=True)
    (td / "spec.md").write_text("# spec\n")
    (td / "plan.md").write_text("# plan\n")
    (td / "execution-graph.json").write_text('{"segments": []}\n')
    (td / "manifest.json").write_text(json.dumps({
        "task_id": td.name,
        "stage": "PLAN_AUDIT",
        "classification": {"risk_level": risk},
    }))
    return td


def _write_fresh_audit(td: Path) -> None:
    receipt_plan_audit(
        td,
        tokens_used=100,
        finding_count=0,
        spec_sha256=hash_file(td / "spec.md"),
        plan_sha256=hash_file(td / "plan.md"),
        graph_sha256=hash_file(td / "execution-graph.json"),
    )


def test_refuses_when_plan_edited_after_audit(tmp_path: Path) -> None:
    """Write the audit receipt with fresh hashes, then mutate plan.md.
    The high-risk PLAN_AUDIT → PRE_EXECUTION_SNAPSHOT transition must
    refuse with a message naming `plan.md` (the drifted artifact)."""
    td = _setup(tmp_path, risk="high")
    _write_fresh_audit(td)
    # Drift the plan AFTER audit wrote its hashes.
    (td / "plan.md").write_text("# plan EDITED AFTER AUDIT\n")
    with pytest.raises(ValueError) as excinfo:
        transition_task(td, "PRE_EXECUTION_SNAPSHOT")
    msg = str(excinfo.value)
    assert "plan.md" in msg, (
        f"PLAN_AUDIT drift message must name the drifted artifact — got: {msg!r}"
    )
    # And this is a drift failure, not a missing-receipt failure.
    # The missing-receipt branch emits "missing receipt plan-audit-check";
    # the drift branch emits "plan-audit-check: plan.md hash drift".
    assert "missing receipt" not in msg, (
        f"drift path leaked missing-receipt vocabulary: {msg!r}"
    )
    # Manifest did NOT advance.
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == "PLAN_AUDIT"


def test_accepts_when_all_artifacts_unchanged(tmp_path: Path) -> None:
    """Fresh audit receipt, no subsequent mutation → transition succeeds."""
    td = _setup(tmp_path, risk="high")
    _write_fresh_audit(td)
    transition_task(td, "PRE_EXECUTION_SNAPSHOT")
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == "PRE_EXECUTION_SNAPSHOT"


def test_missing_receipt_emits_missing_message(tmp_path: Path) -> None:
    """No receipt at all → pre-existing missing-receipt message path.
    Drift and missing are distinct outcomes — this asserts the missing
    branch still behaves as before F4."""
    td = _setup(tmp_path, risk="critical")
    with pytest.raises(ValueError) as excinfo:
        transition_task(td, "PRE_EXECUTION_SNAPSHOT")
    msg = str(excinfo.value)
    # The pre-F4 message format is preserved by spec.
    assert "plan-audit-check" in msg
    # Must NOT look like a drift string — drift reads "plan.md hash drift";
    # missing reads "missing receipt plan-audit-check at ...".
    assert "hash drift" not in msg, (
        f"missing-receipt message leaked drift vocabulary: {msg!r}"
    )


def test_low_risk_still_skips_llm_audit_check(tmp_path: Path) -> None:
    """Low-risk tasks never consulted `plan-audit-check`. F4 preserves
    that — a low-risk task with zero receipts (and all three artifacts
    present so we don't fail elsewhere) advances cleanly."""
    td = _setup(tmp_path, risk="low")
    # No receipt; no mutation; low-risk; transition must succeed.
    transition_task(td, "PRE_EXECUTION_SNAPSHOT")
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == "PRE_EXECUTION_SNAPSHOT"
