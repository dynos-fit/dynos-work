"""Tests for DONE gate based on require_receipts_for_done (AC 10)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_core import transition_task  # noqa: E402
from lib_receipts import (  # noqa: E402
    receipt_audit_routing,
    receipt_audit_done,
    receipt_postmortem_skipped,
    receipt_retrospective,
    receipt_rules_check_passed,
    write_receipt,
)


def _setup(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260418-DG"
    td.mkdir(parents=True)
    (td / "manifest.json").write_text(json.dumps({
        "task_id": "task-20260418-DG",
        "stage": "CHECKPOINT_AUDIT",
        "classification": {"risk_level": "medium"},
    }))
    # Pre-create the artifacts the legacy DONE gate also wants
    (td / "task-retrospective.json").write_text(json.dumps({"quality_score": 0.95}))
    audit_dir = td / "audit-reports"
    audit_dir.mkdir()
    (audit_dir / "report.json").write_text(json.dumps({"findings": []}))
    # The legacy DONE gate wants a `retrospective` receipt as well
    receipt_retrospective(td, 0.95, 0.9, 0.9, 1000)
    # Task-005's new gate (CHECKPOINT_AUDIT->DONE) requires rules-check-passed
    receipt_rules_check_passed(td, rules_evaluated=0, violations_count=0,
                                error_violations=0, mode="all")
    return td


def _write_postmortem_skipped(td: Path):
    # task-20260419-002 G2: subsumed_by is required; empty list is
    # valid because reason is `no-findings`.
    receipt_postmortem_skipped(td, "no-findings", "deadbeef" * 8, subsumed_by=[])


def test_missing_audit_routing_refuses(tmp_path: Path):
    td = _setup(tmp_path)
    _write_postmortem_skipped(td)
    with pytest.raises(ValueError, match="audit-routing"):
        transition_task(td, "DONE")


def test_missing_per_auditor_receipt_refuses(tmp_path: Path):
    td = _setup(tmp_path)
    _write_postmortem_skipped(td)
    receipt_audit_routing(td, [{
        "name": "security-auditor",
        "action": "spawn",
        "route_mode": "generic",
        "agent_path": None,
        "injected_agent_sha256": None,
    }])
    with pytest.raises(ValueError, match="audit-security-auditor"):
        transition_task(td, "DONE")


def test_empty_auditors_passes(tmp_path: Path):
    td = _setup(tmp_path)
    _write_postmortem_skipped(td)
    receipt_audit_routing(td, [])
    transition_task(td, "DONE")
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == "DONE"


def test_force_bypass_succeeds(tmp_path: Path):
    td = _setup(tmp_path)
    # Nothing else: no audit-routing, no postmortem
    transition_task(td, "DONE", force=True)
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == "DONE"


def test_full_chain_with_spawned_auditor_passes(tmp_path: Path):
    td = _setup(tmp_path)
    _write_postmortem_skipped(td)
    receipt_audit_routing(td, [{
        "name": "sec",
        "action": "spawn",
        "route_mode": "generic",
        "agent_path": None,
        "injected_agent_sha256": None,
    }])
    receipt_audit_done(td, "sec", "haiku", 0, 0, None, 100,
                       route_mode="generic", agent_path=None,
                       injected_agent_sha256=None)
    transition_task(td, "DONE")
    assert json.loads((td / "manifest.json").read_text())["stage"] == "DONE"
