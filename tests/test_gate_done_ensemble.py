"""Tests for ensemble voting enforcement in require_receipts_for_done (AC 7).

When a routing entry has ensemble=True, the gate requires:
  - per-model receipts audit-{name}-{model} for every voting model, OR
  - a single audit-{name} receipt with model_used matching a voting model.
Accept iff all voting models report blocking_count=0, OR an escalation
receipt exists with model_used == escalation_model. model_used fields
must sit in voting_models ∪ {escalation_model}.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_core import require_receipts_for_done  # noqa: E402
from lib_receipts import (  # noqa: E402
    receipt_audit_routing,
    receipt_postmortem_skipped,
    write_receipt,
)


def _setup_task(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260419-EN"
    td.mkdir(parents=True)
    (td / "manifest.json").write_text(json.dumps({
        "task_id": td.name,
        "stage": "CHECKPOINT_AUDIT",
        "classification": {"risk_level": "medium"},
    }))
    (td / "task-retrospective.json").write_text(json.dumps({"quality_score": 0.95}))
    receipt_postmortem_skipped(td, "no-findings", "f" * 64, subsumed_by=[])
    return td


def _mock_empty_registry(monkeypatch):
    """Empty registry so only the explicit routing entries drive the flow."""
    import router
    monkeypatch.setattr(router, "_load_auditor_registry", lambda root: {
        "always": [], "fast_track": [], "domain_conditional": {},
    })


def _write_audit_receipt(td: Path, step: str, *, model_used: str,
                         blocking_count: int = 0, contract_version: int = 3) -> Path:
    """Hand-write an audit-* receipt with the full v3 schema the gate reads."""
    return write_receipt(
        td,
        step,
        auditor_name=step.split("-", 1)[1] if "-" in step else step,
        model_used=model_used,
        finding_count=blocking_count,
        blocking_count=blocking_count,
        report_path=None,
        report_sha256=None,
        tokens_used=100,
        route_mode="generic",
        agent_path=None,
        injected_agent_sha256=None,
    )


def test_ensemble_zero_blocking_consensus_passes(tmp_path: Path, monkeypatch):
    """AC 7: two voting-model receipts with blocking=0 → ensemble accepted."""
    _mock_empty_registry(monkeypatch)
    td = _setup_task(tmp_path)
    receipt_audit_routing(td, [{
        "name": "sec",
        "action": "spawn",
        "ensemble": True,
        "ensemble_voting_models": ["haiku", "sonnet"],
        "ensemble_escalation_model": "opus",
        "route_mode": "generic",
        "agent_path": None,
        "injected_agent_sha256": None,
    }])
    _write_audit_receipt(td, "audit-sec-haiku", model_used="haiku", blocking_count=0)
    _write_audit_receipt(td, "audit-sec-sonnet", model_used="sonnet", blocking_count=0)
    gaps = require_receipts_for_done(td)
    assert not any("sec" in g for g in gaps), f"unexpected ensemble gap(s): {gaps}"


def test_ensemble_disagreement_without_escalation_refuses(tmp_path: Path, monkeypatch):
    """AC 7: voting-model receipts disagree (non-zero blocking) and no
    escalation → gap."""
    _mock_empty_registry(monkeypatch)
    td = _setup_task(tmp_path)
    receipt_audit_routing(td, [{
        "name": "sec",
        "action": "spawn",
        "ensemble": True,
        "ensemble_voting_models": ["haiku", "sonnet"],
        "ensemble_escalation_model": "opus",
        "route_mode": "generic",
        "agent_path": None,
        "injected_agent_sha256": None,
    }])
    _write_audit_receipt(td, "audit-sec-haiku", model_used="haiku", blocking_count=3)
    _write_audit_receipt(td, "audit-sec-sonnet", model_used="sonnet", blocking_count=0)
    gaps = require_receipts_for_done(td)
    assert any("disagree" in g for g in gaps), f"expected disagree gap in {gaps}"


def test_ensemble_disagreement_with_escalation_passes(tmp_path: Path, monkeypatch):
    """AC 7: disagreement + escalation receipt with correct model_used → pass."""
    _mock_empty_registry(monkeypatch)
    td = _setup_task(tmp_path)
    receipt_audit_routing(td, [{
        "name": "sec",
        "action": "spawn",
        "ensemble": True,
        "ensemble_voting_models": ["haiku", "sonnet"],
        "ensemble_escalation_model": "opus",
        "route_mode": "generic",
        "agent_path": None,
        "injected_agent_sha256": None,
    }])
    _write_audit_receipt(td, "audit-sec-haiku", model_used="haiku", blocking_count=3)
    _write_audit_receipt(td, "audit-sec-sonnet", model_used="sonnet", blocking_count=0)
    _write_audit_receipt(td, "audit-sec-opus", model_used="opus", blocking_count=0)
    gaps = require_receipts_for_done(td)
    assert not any("disagree" in g or "sec ensemble missing" in g for g in gaps), \
        f"unexpected gap(s) with escalation present: {gaps}"


def test_ensemble_model_used_not_in_voting_set_refuses(tmp_path: Path, monkeypatch):
    """AC 7: receipt's model_used is outside voting_models ∪ {escalation} → gap."""
    _mock_empty_registry(monkeypatch)
    td = _setup_task(tmp_path)
    receipt_audit_routing(td, [{
        "name": "sec",
        "action": "spawn",
        "ensemble": True,
        "ensemble_voting_models": ["haiku", "sonnet"],
        "ensemble_escalation_model": "opus",
        "route_mode": "generic",
        "agent_path": None,
        "injected_agent_sha256": None,
    }])
    # Write a receipt for the 'haiku' slot but mark model_used as an unknown model.
    _write_audit_receipt(td, "audit-sec-haiku", model_used="llama", blocking_count=0)
    _write_audit_receipt(td, "audit-sec-sonnet", model_used="sonnet", blocking_count=0)
    gaps = require_receipts_for_done(td)
    assert any(
        "model_used=llama" in g and "not in voting set" in g
        for g in gaps
    ), f"expected model_used-mismatch gap in {gaps}"


def test_ensemble_missing_voting_model_receipt_refuses(tmp_path: Path, monkeypatch):
    """AC 7: voting model receipt absent + no escalation → gap naming the model."""
    _mock_empty_registry(monkeypatch)
    td = _setup_task(tmp_path)
    receipt_audit_routing(td, [{
        "name": "sec",
        "action": "spawn",
        "ensemble": True,
        "ensemble_voting_models": ["haiku", "sonnet"],
        "ensemble_escalation_model": "opus",
        "route_mode": "generic",
        "agent_path": None,
        "injected_agent_sha256": None,
    }])
    _write_audit_receipt(td, "audit-sec-haiku", model_used="haiku", blocking_count=0)
    # sonnet missing
    gaps = require_receipts_for_done(td)
    assert any("sonnet" in g and ("missing" in g or "escalation" in g) for g in gaps), \
        f"expected missing-model gap in {gaps}"
