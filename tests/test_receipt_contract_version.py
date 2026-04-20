"""Tests for contract_version=5 in every writer + backward compat.

Migrated for task-20260419-009:
- RECEIPT_CONTRACT_VERSION bumped 4->5 for force-override reason/approver bump
- receipt_plan_routing DELETED (A-001) — no longer in __all__
- receipt_retrospective / receipt_spec_validated / receipt_plan_validated /
  receipt_postmortem_generated / receipt_plan_audit signatures changed (B-class)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

import lib_receipts  # noqa: E402
from lib_receipts import (  # noqa: E402
    RECEIPT_CONTRACT_VERSION,
    read_receipt,
    receipt_audit_done,
    receipt_audit_routing,
    receipt_calibration_applied,
    receipt_executor_done,
    receipt_executor_routing,
    receipt_human_approval,
    receipt_plan_audit,
    receipt_plan_validated,
    receipt_planner_spawn,
    receipt_post_completion,
    receipt_postmortem_analysis,
    receipt_postmortem_generated,
    receipt_postmortem_skipped,
    receipt_retrospective,
    receipt_spec_validated,
    receipt_tdd_tests,
    validate_chain,
    write_receipt,
)


def _td(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260418-CV"
    td.mkdir(parents=True)
    return td


def test_contract_version_constant_is_five():
    """Task-20260419-009 (AC 24): contract bumped 4 -> 5 for
    force-override reason/approver. The rename (was _is_four) makes the
    current floor obvious to future readers — the old name would
    silently lie after a subsequent bump."""
    assert RECEIPT_CONTRACT_VERSION == 5


def test_write_receipt_embeds_contract_version(tmp_path: Path):
    """Every receipt embeds v5 via write_receipt."""
    td = _td(tmp_path)
    p = write_receipt(td, "spec-validated", criteria_count=1)
    payload = json.loads(p.read_text())
    assert payload["contract_version"] == 5


def test_v1_receipt_without_contract_version_is_readable(tmp_path: Path):
    """Legacy steps (not in MIN_VERSION_PER_STEP floor) still accept v1."""
    td = _td(tmp_path)
    receipts = td / "receipts"
    receipts.mkdir()
    legacy = {
        "step": "spec-validated",
        "ts": "2025-01-01T00:00:00Z",
        "valid": True,
        "criteria_count": 4,
        "spec_sha256": "deadbeef",
    }
    (receipts / "spec-validated.json").write_text(json.dumps(legacy))
    out = read_receipt(td, "spec-validated")
    assert out is not None
    assert out.get("valid") is True
    assert "contract_version" not in out


def test_v1_receipt_validate_chain_treats_as_missing(tmp_path: Path):
    """v2-mandatory steps (plan-validated) reject v1 receipts via MIN_VERSION floor."""
    td = _td(tmp_path)
    (td / "manifest.json").write_text(json.dumps({
        "task_id": td.name,
        "stage": "EXECUTION",
    }))
    receipts = td / "receipts"
    receipts.mkdir()
    legacy = {
        "step": "plan-validated",
        "ts": "2025-01-01T00:00:00Z",
        "valid": True,
        "segment_count": 2,
        "criteria_coverage": [1, 2],
        "validation_passed": True,
    }
    (receipts / "plan-validated.json").write_text(json.dumps(legacy))
    gaps = validate_chain(td)
    assert "plan-validated" in gaps


def test_plan_routing_writer_deleted():
    """Task-20260419-007 (AC 1): receipt_plan_routing is DELETED."""
    assert not hasattr(lib_receipts, "receipt_plan_routing"), (
        "receipt_plan_routing must be deleted per task-007 AC 1"
    )
    assert "receipt_plan_routing" not in lib_receipts.__all__
    assert "plan-routing" not in lib_receipts._LOG_MESSAGES
