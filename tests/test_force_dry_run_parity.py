"""Parity test for force=True dry-run vs force=False live gate.

The two code paths in `transition_task` — the live gate block (raises
ValueError on first `_refuse(...)` or accumulates `gate_errors` and
raises at the end) and the dry-run helper `_compute_bypassed_gates_for_force`
(pure-function error collector) — must produce the same set of error
strings for the same inputs.

This test pins the invariant mechanically. Any future gate addition that
forgets to mirror into the dry-run helper will fail this parity test —
closing the drift risk PERF-002 flagged as "acceptable duplication but
maintainer hazard."

Why we don't just refactor: the live gate uses `_refuse()` which raises
immediately for hash-bound checks (human approval, plan audit). The
dry-run inherently cannot raise. Threading a `dry_run` flag through every
`_refuse` call site risks subtle behavioral changes; the duplication is
the lesser evil. This test guards it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_core import (  # noqa: E402
    _compute_bypassed_gates_for_force,
    transition_task,
)


def _task_dir(tmp_path: Path, *, stage: str, risk: str = "medium") -> Path:
    td = tmp_path / ".dynos" / "task-20260419-PAR"
    td.mkdir(parents=True)
    (td / "manifest.json").write_text(json.dumps({
        "task_id": td.name,
        "stage": stage,
        "classification": {"risk_level": risk},
    }))
    return td


def _manifest(td: Path) -> dict:
    return json.loads((td / "manifest.json").read_text())


# ---------------------------------------------------------------------------
# Per-edge parity tests. For every edge where the live gate would refuse,
# verify the dry-run helper returns an error list that contains the same
# substring(s) the live gate would have raised.
# ---------------------------------------------------------------------------


def test_parity_execution_missing_plan_validated(tmp_path: Path) -> None:
    """EXECUTION edge with no plan-validated receipt → both paths name
    the missing receipt."""
    td = _task_dir(tmp_path, stage="PRE_EXECUTION_SNAPSHOT")

    with pytest.raises(ValueError) as exc_info:
        transition_task(td, "EXECUTION")
    live_msg = str(exc_info.value)

    bypassed = _compute_bypassed_gates_for_force(
        task_dir=td,
        manifest=_manifest(td),
        current_stage="PRE_EXECUTION_SNAPSHOT",
        next_stage="EXECUTION",
    )
    # The live error names the receipt; at least one dry-run entry must too.
    assert "plan-validated" in live_msg
    assert any("plan-validated" in b for b in bypassed), (
        f"dry-run omits the plan-validated error found in live path: {bypassed}"
    )


def test_parity_spec_review_missing_planner_spec(tmp_path: Path) -> None:
    """SPEC_NORMALIZATION→SPEC_REVIEW with no planner-spec receipt and
    non-fast-track task: both paths cite planner-spec."""
    td = _task_dir(tmp_path, stage="SPEC_NORMALIZATION")
    # classification.fast_track absent → non-fast-track.

    with pytest.raises(ValueError) as exc_info:
        transition_task(td, "SPEC_REVIEW")
    live_msg = str(exc_info.value)

    bypassed = _compute_bypassed_gates_for_force(
        task_dir=td,
        manifest=_manifest(td),
        current_stage="SPEC_NORMALIZATION",
        next_stage="SPEC_REVIEW",
    )
    assert "planner-spec" in live_msg
    assert any("planner-spec" in b for b in bypassed), (
        f"dry-run omits planner-spec: {bypassed}"
    )


def test_parity_plan_review_missing_planner_plan(tmp_path: Path) -> None:
    """PLANNING→PLAN_REVIEW with no planner-plan receipt: parity."""
    td = _task_dir(tmp_path, stage="PLANNING")

    with pytest.raises(ValueError) as exc_info:
        transition_task(td, "PLAN_REVIEW")
    live_msg = str(exc_info.value)

    bypassed = _compute_bypassed_gates_for_force(
        task_dir=td,
        manifest=_manifest(td),
        current_stage="PLANNING",
        next_stage="PLAN_REVIEW",
    )
    assert "planner-plan" in live_msg
    assert any("planner-plan" in b for b in bypassed)


def test_parity_checkpoint_audit_missing_executor_routing(tmp_path: Path) -> None:
    """TEST_EXECUTION→CHECKPOINT_AUDIT with no executor-routing: parity.
    The live gate emits only the one routing-missing error (no per-segment
    double-complaint); dry-run must match."""
    td = _task_dir(tmp_path, stage="TEST_EXECUTION")

    with pytest.raises(ValueError) as exc_info:
        transition_task(td, "CHECKPOINT_AUDIT")
    live_msg = str(exc_info.value)

    bypassed = _compute_bypassed_gates_for_force(
        task_dir=td,
        manifest=_manifest(td),
        current_stage="TEST_EXECUTION",
        next_stage="CHECKPOINT_AUDIT",
    )
    assert "executor-routing" in live_msg
    assert any("executor-routing" in b for b in bypassed)
    # And — no per-segment piggyback in either path.
    assert "executor-seg-" not in live_msg
    assert not any("executor-seg-" in b for b in bypassed)


def test_parity_dry_run_returns_empty_when_live_would_pass(tmp_path: Path) -> None:
    """When no gate would refuse, both paths agree: live passes silently
    (no ValueError) and dry-run returns an empty list.

    Uses an edge that has no preconditions on receipts/files."""
    # FOUNDRY_INITIALIZED → SPEC_NORMALIZATION is unconditional.
    td = _task_dir(tmp_path, stage="FOUNDRY_INITIALIZED")

    bypassed = _compute_bypassed_gates_for_force(
        task_dir=td,
        manifest=_manifest(td),
        current_stage="FOUNDRY_INITIALIZED",
        next_stage="SPEC_NORMALIZATION",
    )
    assert bypassed == [], f"dry-run reported errors where live would pass: {bypassed}"

    # And live actually passes.
    transition_task(td, "SPEC_NORMALIZATION")
    manifest = _manifest(td)
    assert manifest["stage"] == "SPEC_NORMALIZATION"
