"""Tests for ensemble voting enforcement in require_receipts_for_done (AC 7).

Cascade protocol (haiku → sonnet → opus):
  - haiku=0 findings → run sonnet
    - sonnet=0 → PASS (both receipts present, both zero)
    - sonnet≠0 → escalate to opus (haiku+sonnet+opus receipts)
  - haiku≠0 findings → skip sonnet, escalate directly to opus (haiku+opus receipts only)

The gate accepts iff:
  - all voting-model receipts present AND all finding_count==0, OR
  - an escalation receipt exists with model_used == escalation_model
    (sonnet receipt may be absent when haiku already escalated directly).
model_used on every receipt must sit in voting_models ∪ {escalation_model}.
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
                         blocking_count: int = 0, finding_count: int | None = None,
                         contract_version: int = 3) -> Path:
    """Hand-write an audit-* receipt with the full v3 schema the gate reads.

    ``finding_count`` defaults to ``blocking_count`` (the common case); pass it
    explicitly to model a non-blocking finding (finding_count > 0, blocking=0).
    """
    if finding_count is None:
        finding_count = blocking_count
    return write_receipt(
        td,
        step,
        auditor_name=step.split("-", 1)[1] if "-" in step else step,
        model_used=model_used,
        finding_count=finding_count,
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


def test_ensemble_nonblocking_finding_no_escalation_refuses(tmp_path: Path, monkeypatch):
    """Cascade binds on finding_count, not blocking_count.

    A voting model that finds a NON-blocking issue (finding_count>0, blocking=0)
    must still escalate to opus. Running it at sonnet with both shards zero-
    blocking used to pass (the closed seam) — now it must produce a gap.
    """
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
    # haiku reports a non-blocking finding; sonnet clean; no opus escalation.
    _write_audit_receipt(td, "audit-sec-haiku", model_used="haiku",
                         blocking_count=0, finding_count=2)
    _write_audit_receipt(td, "audit-sec-sonnet", model_used="sonnet", blocking_count=0)
    gaps = require_receipts_for_done(td)
    assert any("sec" in g and "non-zero findings" in g for g in gaps), \
        f"expected escalation gap for non-blocking haiku finding: {gaps}"


def test_ensemble_nonblocking_finding_with_opus_passes(tmp_path: Path, monkeypatch):
    """Non-blocking haiku finding + opus escalation shard → accepted."""
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
    _write_audit_receipt(td, "audit-sec-haiku", model_used="haiku",
                         blocking_count=0, finding_count=2)
    _write_audit_receipt(td, "audit-sec-opus", model_used="opus",
                         blocking_count=0, finding_count=0)
    gaps = require_receipts_for_done(td)
    assert not any("sec" in g for g in gaps), \
        f"unexpected gap with opus escalation on non-blocking finding: {gaps}"


def test_ensemble_haiku_finds_issues_no_escalation_refuses(tmp_path: Path, monkeypatch):
    """Cascade: haiku finds issues, sonnet skipped, no opus escalation → gap."""
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
    # haiku finds issues → sonnet correctly skipped → but opus not written yet
    _write_audit_receipt(td, "audit-sec-haiku", model_used="haiku", blocking_count=3)
    gaps = require_receipts_for_done(td)
    assert any("sec" in g for g in gaps), f"expected gap without escalation receipt: {gaps}"


def test_ensemble_haiku_finds_issues_direct_opus_passes(tmp_path: Path, monkeypatch):
    """Cascade: haiku finds issues → skip sonnet → opus escalation → pass."""
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
    # haiku finds issues → sonnet skipped → opus escalation written
    _write_audit_receipt(td, "audit-sec-haiku", model_used="haiku", blocking_count=3)
    _write_audit_receipt(td, "audit-sec-opus", model_used="opus", blocking_count=0)
    gaps = require_receipts_for_done(td)
    assert not any("sec" in g for g in gaps), \
        f"unexpected gap with direct opus escalation: {gaps}"


def test_ensemble_haiku_clean_sonnet_finds_issues_opus_passes(tmp_path: Path, monkeypatch):
    """Cascade: haiku=0 → sonnet finds issues → opus escalation → pass."""
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
    # haiku clean → sonnet finds issues → escalate to opus
    _write_audit_receipt(td, "audit-sec-haiku", model_used="haiku", blocking_count=0)
    _write_audit_receipt(td, "audit-sec-sonnet", model_used="sonnet", blocking_count=2)
    _write_audit_receipt(td, "audit-sec-opus", model_used="opus", blocking_count=0)
    gaps = require_receipts_for_done(td)
    assert not any("sec" in g for g in gaps), \
        f"unexpected gap with sonnet→opus escalation: {gaps}"


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
    # haiku slot written but model_used is an unknown model
    _write_audit_receipt(td, "audit-sec-haiku", model_used="llama", blocking_count=0)
    _write_audit_receipt(td, "audit-sec-sonnet", model_used="sonnet", blocking_count=0)
    gaps = require_receipts_for_done(td)
    assert any(
        "model_used=llama" in g and "not in voting set" in g
        for g in gaps
    ), f"expected model_used-mismatch gap in {gaps}"


def test_ensemble_missing_voting_model_receipt_refuses(tmp_path: Path, monkeypatch):
    """Cascade: haiku=0 but sonnet missing and no escalation → gap naming sonnet."""
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
    # haiku clean → executor should have run sonnet but didn't, and no escalation
    _write_audit_receipt(td, "audit-sec-haiku", model_used="haiku", blocking_count=0)
    # sonnet missing, no opus
    gaps = require_receipts_for_done(td)
    assert any("sonnet" in g and ("missing" in g or "escalation" in g) for g in gaps), \
        f"expected missing-model gap in {gaps}"


def test_ensemble_collapsed_single_receipt_refuses(tmp_path: Path, monkeypatch):
    """Ensemble accounting requires audit-{auditor}-{model}, not audit-{auditor}."""
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
    _write_audit_receipt(td, "audit-sec", model_used="haiku", blocking_count=0)
    gaps = require_receipts_for_done(td)
    assert any("haiku" in g and "sonnet" in g for g in gaps), \
        f"expected per-model receipt gap, got {gaps}"
