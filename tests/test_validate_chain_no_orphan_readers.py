"""Tests for validate_chain orphan-reader elimination (AC 5).

Every step enumerated inside validate_chain's ``all_receipts`` list (or any
receipt required by a stage) MUST have a corresponding writer function in
lib_receipts. plan-routing was pruned from the required chain — it is no
longer reported as a gap.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

import lib_receipts  # noqa: E402
from lib_receipts import (  # noqa: E402
    receipt_plan_validated,
    validate_chain,
    write_receipt,
)


def _setup_task(tmp_path: Path, *, stage: str = "EXECUTION") -> Path:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260419-OR"
    td.mkdir(parents=True)
    (td / "manifest.json").write_text(json.dumps({
        "task_id": td.name,
        "stage": stage,
    }))
    return td


def test_plan_routing_not_in_required_chain(tmp_path: Path):
    """AC 5: at EXECUTION stage with only plan-validated, there is NO gap
    for plan-routing (even though the writer still exists for future use)."""
    td = _setup_task(tmp_path, stage="EXECUTION")
    receipt_plan_validated(td, 1, [1, 2], validation_passed=True)
    gaps = validate_chain(td)
    # plan-routing must NOT appear in gaps — it has been pruned.
    assert not any("plan-routing" in g for g in gaps), f"plan-routing still appears in gaps: {gaps}"


def test_every_required_chain_entry_has_writer(tmp_path: Path):
    """AC 5: every receipt name that validate_chain's static chain enumerates
    must map to a callable writer function in lib_receipts."""
    # The static all_receipts list names (per seg-1 evidence, plan-routing pruned).
    expected_chain_writers: dict[str, str] = {
        "spec-validated": "receipt_spec_validated",
        "plan-validated": "receipt_plan_validated",
        "executor-routing": "receipt_executor_routing",
        "audit-routing": "receipt_audit_routing",
        "retrospective": "receipt_retrospective",
        "post-completion": "receipt_post_completion",
    }
    for step_name, writer_name in expected_chain_writers.items():
        assert hasattr(lib_receipts, writer_name), (
            f"chain step '{step_name}' has no writer '{writer_name}' in lib_receipts"
        )
        writer = getattr(lib_receipts, writer_name)
        assert callable(writer)


def test_plan_routing_writer_still_exists_for_future_use(tmp_path: Path):
    """AC 5: plan-routing writer itself remains exported (for reinstatement)."""
    assert hasattr(lib_receipts, "receipt_plan_routing")
    assert callable(lib_receipts.receipt_plan_routing)


def test_at_done_stage_calibration_alternatives(tmp_path: Path):
    """AC 5 + AC 24: at DONE, a missing calibration receipt reports the
    combined gap string, not just 'calibration-applied' (which has no writer
    guaranteed independent of noop)."""
    td = _setup_task(tmp_path, stage="DONE")
    # Write every required receipt EXCEPT calibration — confirm the gap
    # string references BOTH alternatives (applied|noop).
    from lib_receipts import (
        receipt_audit_routing,
        receipt_executor_routing,
        receipt_post_completion,
        receipt_retrospective,
    )
    receipt_plan_validated(td, 0, [], validation_passed=True)
    receipt_executor_routing(td, [])
    receipt_audit_routing(td, [])
    receipt_retrospective(td, 0.95, 0.9, 0.9, 1000)
    receipt_post_completion(td, [])
    gaps = validate_chain(td)
    # Every mandatory chain receipt present; calibration gap uses the combined label.
    assert any("calibration (applied|noop)" in g for g in gaps), (
        f"expected combined calibration gap in {gaps}"
    )
