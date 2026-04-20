"""Tests for DONE gate based on require_receipts_for_done (AC 10).

Migrated for task-20260419-006: receipt_rules_check_passed has new signature
(task_dir, mode) — counts are computed internally. Also AC 6 added a
registry-eligible auditor cross-check; we mock the auditor registry to be
empty for these tests so the cross-check is vacuous and we focus on the
chain-of-receipts logic this file was originally written to exercise.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

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


@pytest.fixture(autouse=True)
def _empty_auditor_registry(monkeypatch):
    """Make the registry cross-check vacuous so these tests focus on the
    classic DONE gate flow (auditor-presence + per-auditor receipts)."""
    import router
    monkeypatch.setattr(router, "_load_auditor_registry", lambda root: {
        "always": [], "fast_track": [], "domain_conditional": {},
    })


@pytest.fixture(autouse=True)
def _stub_run_checks(monkeypatch):
    """receipt_rules_check_passed self-computes via rules_engine.run_checks.
    Stub it to return [] so the writer always succeeds in these tests."""
    import rules_engine
    monkeypatch.setattr(rules_engine, "run_checks", lambda root, mode: [])


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
    receipt_retrospective(td)
    # Task-005's new gate (CHECKPOINT_AUDIT->DONE) requires rules-check-passed.
    # New signature (AC 1): (task_dir, mode) — counts computed internally.
    receipt_rules_check_passed(td, "all")
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
    transition_task(
        td,
        "DONE",
        force=True,
        force_reason="test: DONE gate bypass without required receipts",
        force_approver="test-suite",
    )
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
