"""Tests for the EXECUTION gate's hash-bound plan-validated freshness (F1, CRITERION 1).

The EXECUTION gate at `hooks/lib_core.py` used to accept any
`plan-validated` receipt (presence check). F1 extends the gate so a
*stale* receipt — one written when the artifacts had different content
than on disk today — cannot authorize the transition. Drift and missing
emit distinct error strings so the operator can tell them apart.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_core import transition_task  # noqa: E402
from lib_receipts import receipt_plan_validated  # noqa: E402


def _setup_task(tmp_path: Path) -> Path:
    """Build a task at PRE_EXECUTION_SNAPSHOT with the three planning artifacts
    present (spec.md / plan.md / execution-graph.json). PRE_EXECUTION_SNAPSHOT
    is the only stage that can advance to EXECUTION per
    ALLOWED_STAGE_TRANSITIONS."""
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260419-EX"
    td.mkdir(parents=True)
    (td / "spec.md").write_text("# spec v1\n")
    (td / "plan.md").write_text("# plan v1\n")
    (td / "execution-graph.json").write_text('{"task_id": "task-20260419-EX", "segments": []}\n')
    (td / "manifest.json").write_text(json.dumps({
        "task_id": "task-20260419-EX",
        "stage": "PRE_EXECUTION_SNAPSHOT",
        "classification": {"risk_level": "medium"},
    }))
    return td


def test_gate_refuses_when_plan_drifts(tmp_path: Path) -> None:
    """Write a valid plan-validated receipt, then mutate plan.md. The
    EXECUTION gate must refuse because the captured artifact hashes no
    longer match disk. The error must name the drifted artifact via the
    literal substring `plan.md hash` so operators can tell drift from
    a missing-receipt failure."""
    td = _setup_task(tmp_path)
    receipt_plan_validated(td, segment_count=0, criteria_coverage=[])
    # Mutate plan.md AFTER the receipt was written. The receipt's
    # captured hash is now stale.
    (td / "plan.md").write_text("# plan v2 DRIFTED\n")

    with pytest.raises(ValueError) as excinfo:
        transition_task(td, "EXECUTION")
    msg = str(excinfo.value)
    assert "plan.md hash" in msg, (
        f"drift message must contain 'plan.md hash' substring — got: {msg!r}"
    )
    # Manifest must NOT have advanced.
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == "PRE_EXECUTION_SNAPSHOT"


def test_gate_accepts_when_plan_unchanged(tmp_path: Path) -> None:
    """With a fresh receipt and unchanged artifacts, the EXECUTION
    transition must succeed and the manifest must reflect the new stage."""
    td = _setup_task(tmp_path)
    receipt_plan_validated(td, segment_count=0, criteria_coverage=[])

    transition_task(td, "EXECUTION")
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == "EXECUTION"


def test_gate_still_refuses_when_receipt_missing(tmp_path: Path) -> None:
    """No receipt at all. The pre-existing `plan was never validated`
    message must still fire — F1 only *extends* the gate, never weakens
    the missing-receipt branch."""
    td = _setup_task(tmp_path)
    # Intentionally do NOT write a plan-validated receipt.

    with pytest.raises(ValueError) as excinfo:
        transition_task(td, "EXECUTION")
    msg = str(excinfo.value)
    assert "plan was never validated" in msg, (
        f"missing-receipt message must contain 'plan was never validated' — "
        f"got: {msg!r}"
    )


def test_drift_and_missing_produce_distinct_messages(tmp_path: Path) -> None:
    """F12: the two failure modes must emit semantically distinct strings.
    An operator diagnosing a refusal must be able to tell hash drift
    apart from never-validated at a glance — without that, F1's whole
    point (surface drift) is invisible.

    Concretely we assert:
      - the drift message DOES NOT contain the never-validated phrase.
      - the missing-receipt message DOES NOT contain the drift phrase.
    """
    # --- drift path -------------------------------------------------
    td_drift = _setup_task(tmp_path)
    receipt_plan_validated(td_drift, segment_count=0, criteria_coverage=[])
    (td_drift / "plan.md").write_text("# drifted\n")
    with pytest.raises(ValueError) as drift_excinfo:
        transition_task(td_drift, "EXECUTION")
    drift_msg = str(drift_excinfo.value)

    # --- missing path -----------------------------------------------
    # Use a second task dir so we don't pollute the drift state.
    project2 = tmp_path / "project2"
    td_missing = project2 / ".dynos" / "task-20260419-EX2"
    td_missing.mkdir(parents=True)
    (td_missing / "spec.md").write_text("# s\n")
    (td_missing / "plan.md").write_text("# p\n")
    (td_missing / "execution-graph.json").write_text("{}\n")
    (td_missing / "manifest.json").write_text(json.dumps({
        "task_id": "task-20260419-EX2",
        "stage": "PRE_EXECUTION_SNAPSHOT",
        "classification": {"risk_level": "medium"},
    }))
    with pytest.raises(ValueError) as miss_excinfo:
        transition_task(td_missing, "EXECUTION")
    miss_msg = str(miss_excinfo.value)

    # Distinctness — each branch must NOT wear the other branch's vocabulary.
    assert "was never validated" not in drift_msg, (
        f"drift message leaked never-validated vocabulary: {drift_msg!r}"
    )
    assert "hash drift" not in miss_msg, (
        f"missing message leaked hash-drift vocabulary: {miss_msg!r}"
    )
    # Sanity check — the two messages are different strings.
    assert drift_msg != miss_msg
