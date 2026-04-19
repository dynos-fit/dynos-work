"""Tests for validate_chain calibration OR semantics at DONE (AC 24).

DONE stage requires EITHER calibration-applied OR calibration-noop.
When both are present the later-ts is logically chosen (gate, not chain).
When neither is present, validate_chain reports a single combined gap
'calibration (applied|noop)'.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_receipts import (  # noqa: E402
    receipt_audit_routing,
    receipt_calibration_applied,
    receipt_calibration_noop,
    receipt_executor_routing,
    receipt_plan_validated,
    receipt_post_completion,
    receipt_retrospective,
    validate_chain,
)


def _setup_done_task(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260419-VC"
    td.mkdir(parents=True)
    (td / "manifest.json").write_text(json.dumps({
        "task_id": td.name,
        "stage": "DONE",
    }))
    receipt_plan_validated(td, 1, [1])
    receipt_executor_routing(td, [])
    receipt_audit_routing(td, [])
    receipt_retrospective(td, 0.95, 0.9, 0.9, 100)
    receipt_post_completion(td, [])
    return td


def test_only_calibration_applied_satisfies(tmp_path: Path):
    """AC 24: applied alone satisfies the calibration requirement → no gap."""
    td = _setup_done_task(tmp_path)
    receipt_calibration_applied(td, 1, 1, "a" * 64, "b" * 64)
    gaps = validate_chain(td)
    assert not any("calibration" in g for g in gaps), \
        f"unexpected calibration gap with applied present: {gaps}"


def test_only_calibration_noop_satisfies(tmp_path: Path):
    """AC 24: noop alone satisfies the requirement → no gap."""
    td = _setup_done_task(tmp_path)
    receipt_calibration_noop(td, "no-retros", "c" * 64)
    gaps = validate_chain(td)
    assert not any("calibration" in g for g in gaps), \
        f"unexpected calibration gap with noop present: {gaps}"


def test_both_present_no_gap(tmp_path: Path):
    """AC 24: when both are present, the chain is satisfied (no gap)."""
    td = _setup_done_task(tmp_path)
    receipt_calibration_applied(td, 1, 1, "a" * 64, "b" * 64)
    receipt_calibration_noop(td, "no-retros", "b" * 64)
    gaps = validate_chain(td)
    assert not any("calibration" in g for g in gaps), \
        f"unexpected calibration gap with both present: {gaps}"


def test_neither_present_combined_gap_reported(tmp_path: Path):
    """AC 24: neither receipt → combined gap label 'calibration (applied|noop)'."""
    td = _setup_done_task(tmp_path)
    gaps = validate_chain(td)
    assert any("calibration (applied|noop)" in g for g in gaps), \
        f"expected combined calibration gap in {gaps}"
