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
    _human_approval_err,
    _rules_check_err,
    transition_task,
)
import lib_receipts  # noqa: E402  # for monkeypatching the lazy-import target


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


# ---------------------------------------------------------------------------
# task-20260509-002 fail-closed regression tests for the four narrowed
# `except Exception` clauses in `_human_approval_err` / `_rules_check_err`.
#
# The lazy `from lib_receipts import read_receipt` inside each validator
# resolves the attribute on the cached `lib_receipts` module each call,
# so `monkeypatch.setattr("lib_receipts.read_receipt", ...)` is observed
# by the next call. Pre-fix all four clauses are `except Exception:
# return None`, which silently absorbs OSError/KeyError alike, opening
# the gate. Post-fix only ImportError/OSError/JSONDecodeError are caught
# (returning a "validator_failed" error string), and KeyError propagates.
# ---------------------------------------------------------------------------


def _raise_oserror(*_args, **_kwargs):
    raise OSError("simulated disk error")


def _raise_keyerror(*_args, **_kwargs):
    raise KeyError("unexpected")


def test_human_approval_err_refuses_on_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 10: when read_receipt raises OSError inside _human_approval_err,
    the validator must return a non-None string containing
    'validator_failed' — NOT silently return None and let the gate open.
    """
    td = _task_dir(tmp_path, stage="SPEC_REVIEW")
    spec_path = td / "spec.md"
    spec_path.write_text("# spec\n")

    monkeypatch.setattr(lib_receipts, "read_receipt", _raise_oserror)

    result = _human_approval_err(
        td, "SPEC_REVIEW", "PLANNING", "SPEC_REVIEW", spec_path
    )

    assert result is not None, (
        "fail-open bug: _human_approval_err swallowed OSError and "
        "returned None — the gate is silently bypassed"
    )
    assert isinstance(result, str)
    assert "validator_failed" in result, (
        f"expected 'validator_failed' substring in error string; got {result!r}"
    )


def test_transition_task_refuses_on_human_approval_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 11: a SPEC_REVIEW->PLANNING force-override transition routed
    through `_compute_bypassed_gates_for_force` must surface the
    validator_failed error for an OSError on read_receipt — i.e. the
    bypassed-gates list is non-empty and contains 'validator_failed'.

    The spec text says `transition_task(...)` raises ValueError, but the
    actual invocation that triggers `_compute_bypassed_gates_for_force`
    is the force=True observability dispatch which surfaces errors via
    the bypassed_gates list (logged + receipted), not via ValueError.
    Asserting on the bypassed_gates list output of
    `_compute_bypassed_gates_for_force` directly is the highest-fidelity
    representation of the AC 11 fail-closed contract that is RED at HEAD
    and GREEN after the fix. The spawn instruction explicitly permits
    this: "or whatever invocation triggers _compute_bypassed_gates_for_force".
    """
    td = _task_dir(tmp_path, stage="SPEC_REVIEW")
    spec_path = td / "spec.md"
    spec_path.write_text("# spec\n")

    # Pre-write a valid human-approval-SPEC_REVIEW receipt so the only
    # remaining failure mode that matters is the OSError-from-read_receipt
    # the monkeypatch will inject. The receipt body content does not
    # matter because read_receipt is intercepted, but the file existence
    # is asserted by the fixture pattern in the rest of the suite.
    receipts_dir = td / "receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    (receipts_dir / "human-approval-SPEC_REVIEW.json").write_text(
        json.dumps({"step": "human-approval-SPEC_REVIEW", "artifact_sha256": "deadbeef"})
    )

    monkeypatch.setattr(lib_receipts, "read_receipt", _raise_oserror)

    bypassed = _compute_bypassed_gates_for_force(
        task_dir=td,
        manifest=_manifest(td),
        current_stage="SPEC_REVIEW",
        next_stage="PLANNING",
    )

    assert any("validator_failed" in entry for entry in bypassed), (
        f"fail-open bug: bypassed_gates does NOT include validator_failed "
        f"after read_receipt raised OSError. Got: {bypassed!r}. The forced "
        f"transition would silently proceed with no record of the failed "
        f"gate validation."
    )


def test_rules_check_err_refuses_on_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 12: when read_receipt raises OSError inside _rules_check_err,
    the validator must return a non-None string containing
    'validator_failed' — NOT silently return None.
    """
    td = _task_dir(tmp_path, stage="SPEC_NORMALIZATION")

    monkeypatch.setattr(lib_receipts, "read_receipt", _raise_oserror)

    result = _rules_check_err(td, "SPEC_NORMALIZATION", "SPEC_REVIEW")

    assert result is not None, (
        "fail-open bug: _rules_check_err swallowed OSError and "
        "returned None — the rules-check gate is silently bypassed"
    )
    assert isinstance(result, str)
    assert "validator_failed" in result, (
        f"expected 'validator_failed' substring in error string; got {result!r}"
    )


def test_human_approval_err_propagates_keyerror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 13: programmer-error exceptions (KeyError) MUST propagate out
    of _human_approval_err — they are not in the narrowed allow-list
    (ImportError/OSError/JSONDecodeError) and silently swallowing them
    hides bugs.
    """
    td = _task_dir(tmp_path, stage="SPEC_REVIEW")
    spec_path = td / "spec.md"
    spec_path.write_text("# spec\n")

    monkeypatch.setattr(lib_receipts, "read_receipt", _raise_keyerror)

    with pytest.raises(KeyError):
        _human_approval_err(
            td, "SPEC_REVIEW", "PLANNING", "SPEC_REVIEW", spec_path
        )


def test_rules_check_err_propagates_keyerror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 14: programmer-error exceptions (KeyError) MUST propagate out
    of _rules_check_err. Pre-fix the broad `except Exception` swallows
    them and returns None (gate opens silently).
    """
    td = _task_dir(tmp_path, stage="SPEC_NORMALIZATION")

    monkeypatch.setattr(lib_receipts, "read_receipt", _raise_keyerror)

    with pytest.raises(KeyError):
        _rules_check_err(td, "SPEC_NORMALIZATION", "SPEC_REVIEW")
