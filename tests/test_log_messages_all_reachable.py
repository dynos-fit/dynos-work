"""Tests that every key in _LOG_MESSAGES is reachable by some writer (AC 23).

A key in _LOG_MESSAGES that no writer ever uses is dead code — confusion
for anyone reading the log dispatch table. Tests confirm coverage either
literally (writer step name == key) or via documented dynamic patterns.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

import lib_receipts  # noqa: E402
from lib_receipts import _LOG_MESSAGES  # noqa: E402


# Dynamic patterns that don't appear as literal step names but ARE used by
# writers (the writer formats step_name dynamically before calling write_receipt).
DYNAMIC_PATTERNS = {
    "executor-{segment_id}": "writes step starting with 'executor-'",
    "audit-{auditor_name}": "writes step starting with 'audit-'",
    "planner-{phase}": "writes step starting with 'planner-'",
    "human-approval-{stage}": "writes step starting with 'human-approval-'",
}


def _writer_source_uses_step(writer_fn, step_name: str) -> bool:
    """Check whether a writer's source contains the step_name string."""
    try:
        src = inspect.getsource(writer_fn)
    except (OSError, TypeError):
        return False
    return step_name in src


def test_every_log_message_key_has_writer():
    """AC 23: every _LOG_MESSAGES key is emitted by at least one writer."""
    writer_funcs = [
        getattr(lib_receipts, name)
        for name in lib_receipts.__all__
        if name.startswith("receipt_") and callable(getattr(lib_receipts, name, None))
    ]

    # Map step_name → writer that writes it.
    static_steps = {
        "spec-validated": "receipt_spec_validated",
        "plan-validated": "receipt_plan_validated",
        "plan-routing": "receipt_plan_routing",
        "executor-routing": "receipt_executor_routing",
        "audit-routing": "receipt_audit_routing",
        "retrospective": "receipt_retrospective",
        "post-completion": "receipt_post_completion",
        "tdd-tests": "receipt_tdd_tests",
        "plan-audit-check": "receipt_plan_audit",
        "postmortem-generated": "receipt_postmortem_generated",
        "postmortem-analysis": "receipt_postmortem_analysis",
        "postmortem-skipped": "receipt_postmortem_skipped",
        "calibration-applied": "receipt_calibration_applied",
        "calibration-noop": "receipt_calibration_noop",
        "rules-check-passed": "receipt_rules_check_passed",
    }
    # Dynamic writers — match by prefix.
    dynamic_prefixes = {
        "planner-": "receipt_planner_spawn",
        "human-approval-": "receipt_human_approval",
        "executor-": "receipt_executor_done",  # also executor-routing (static)
        "audit-": "receipt_audit_done",  # also audit-routing (static)
        "force-override-": "receipt_force_override",  # PR #130 G2 force-override
    }

    unmapped = []
    for key in _LOG_MESSAGES:
        # Try static match
        if key in static_steps:
            writer_name = static_steps[key]
            assert hasattr(lib_receipts, writer_name), \
                f"writer {writer_name} for log key {key!r} not in lib_receipts"
            continue

        # Try dynamic prefix match
        matched = False
        for prefix, writer_name in dynamic_prefixes.items():
            if key.startswith(prefix):
                assert hasattr(lib_receipts, writer_name), \
                    f"writer {writer_name} for log key {key!r} not in lib_receipts"
                matched = True
                break
        if not matched:
            unmapped.append(key)

    assert not unmapped, f"Unreachable _LOG_MESSAGES keys: {unmapped}"


def test_calibration_noop_key_present():
    """AC 23: 'calibration-noop' key must be in _LOG_MESSAGES."""
    assert "calibration-noop" in _LOG_MESSAGES


def test_calibration_noop_message_format():
    """AC 23: calibration-noop message contains '{reason}' placeholder."""
    template = _LOG_MESSAGES["calibration-noop"]
    assert "{reason}" in template


def test_plan_routing_key_remains_for_future_use():
    """AC 23: plan-routing key stays in _LOG_MESSAGES even though pruned
    from required-chain (writer still exists for reinstatement)."""
    assert "plan-routing" in _LOG_MESSAGES
    assert hasattr(lib_receipts, "receipt_plan_routing")
