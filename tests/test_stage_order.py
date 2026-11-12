"""Tests for STAGE_ORDER and ALLOWED_STAGE_TRANSITIONS shape (AC 5)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_core import ALLOWED_STAGE_TRANSITIONS, STAGE_ORDER  # noqa: E402


def test_tdd_review_after_plan_audit():
    assert "TDD_REVIEW" in STAGE_ORDER
    assert "PLAN_AUDIT" in STAGE_ORDER
    assert STAGE_ORDER.index("TDD_REVIEW") == STAGE_ORDER.index("PLAN_AUDIT") + 1


def test_calibrated_after_done():
    assert "CALIBRATED" in STAGE_ORDER
    assert "DONE" in STAGE_ORDER
    assert STAGE_ORDER.index("CALIBRATED") == STAGE_ORDER.index("DONE") + 1


def test_plan_audit_transitions_include_tdd_review():
    transitions = ALLOWED_STAGE_TRANSITIONS["PLAN_AUDIT"]
    assert "TDD_REVIEW" in transitions
    assert "PRE_EXECUTION_SNAPSHOT" in transitions
    assert "FAILED" in transitions


def test_tdd_review_transitions_only_to_pre_exec_or_failed():
    transitions = ALLOWED_STAGE_TRANSITIONS["TDD_REVIEW"]
    assert transitions == {"PRE_EXECUTION_SNAPSHOT", "FAILED"}


def test_done_transitions_to_calibrated_and_failed():
    transitions = ALLOWED_STAGE_TRANSITIONS["DONE"]
    assert "CALIBRATED" in transitions
    assert "FAILED" in transitions


def test_calibrated_is_terminal():
    # CALIBRATED's only exit is the receipt-gated reopen edge (`ctl reopen`).
    assert ALLOWED_STAGE_TRANSITIONS["CALIBRATED"] == {"CHECKPOINT_AUDIT"}


def test_allowed_transitions_keys_are_subset_of_stage_order():
    # Every key and target must be a known stage
    stages = set(STAGE_ORDER)
    for src, dests in ALLOWED_STAGE_TRANSITIONS.items():
        assert src in stages, f"{src} not in STAGE_ORDER"
        for d in dests:
            assert d in stages, f"target {d} (from {src}) not in STAGE_ORDER"


def test_no_self_loops_in_transitions():
    for src, dests in ALLOWED_STAGE_TRANSITIONS.items():
        assert src not in dests, f"{src} has a self-loop"


def test_acyclic_via_no_back_transition_to_terminal():
    # CANCELLED is final. CALIBRATED and FAILED leave only through the
    # receipt-gated reopen edge to CHECKPOINT_AUDIT; no other way back.
    assert ALLOWED_STAGE_TRANSITIONS.get("CANCELLED", set()) == set()
    for terminal in ("CALIBRATED", "FAILED"):
        assert ALLOWED_STAGE_TRANSITIONS.get(terminal, set()) == {"CHECKPOINT_AUDIT"}
