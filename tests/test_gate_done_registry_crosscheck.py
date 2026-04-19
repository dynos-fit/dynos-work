"""Tests for require_receipts_for_done registry cross-check (AC 6).

Re-derives the eligible auditor set from _load_auditor_registry +
manifest.classification.domains. Registry-eligible auditors missing from
audit-routing are gaps; skips need reasons; spawns need receipts; unknown
actions are gaps. Routing entries NOT in registry are accepted as extras
(warning-only).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_core import require_receipts_for_done  # noqa: E402
from lib_receipts import (  # noqa: E402
    receipt_audit_done,
    receipt_audit_routing,
    receipt_postmortem_skipped,
)


def _setup_task(tmp_path: Path, *, domains: list[str] | None = None,
                fast_track: bool = False) -> Path:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260419-RG"
    td.mkdir(parents=True)
    classification = {"risk_level": "medium"}
    if domains is not None:
        classification["domains"] = domains
    manifest = {
        "task_id": td.name,
        "stage": "CHECKPOINT_AUDIT",
        "classification": classification,
    }
    if fast_track:
        manifest["fast_track"] = True
    (td / "manifest.json").write_text(json.dumps(manifest))
    # A clean retrospective so quality>=0.8.
    (td / "task-retrospective.json").write_text(json.dumps({"quality_score": 0.95}))
    # Postmortem skipped so that layer is satisfied.
    receipt_postmortem_skipped(td, "no-findings", "f" * 64, subsumed_by=[])
    return td


def _mock_registry(monkeypatch, always: list[str] | None = None,
                   domain_conditional: dict[str, list[str]] | None = None,
                   fast_track: list[str] | None = None):
    """Patch router's _load_auditor_registry to return a known-fixed set."""
    registry = {
        "always": always if always is not None else ["security-auditor"],
        "fast_track": fast_track if fast_track is not None else ["security-auditor"],
        "domain_conditional": domain_conditional if domain_conditional is not None else {},
    }
    import router
    monkeypatch.setattr(router, "_load_auditor_registry", lambda root: registry)
    return registry


def test_registry_eligible_auditor_missing_from_routing_is_gap(tmp_path: Path, monkeypatch):
    """AC 6: eligible security-auditor missing from routing → gap."""
    _mock_registry(monkeypatch, always=["security-auditor"])
    td = _setup_task(tmp_path, domains=[])
    # Routing is empty — but security-auditor is registry-eligible.
    receipt_audit_routing(td, [])
    gaps = require_receipts_for_done(td)
    assert any("security-auditor" in g and "missing registry-eligible" in g for g in gaps), \
        f"expected missing registry-eligible gap in {gaps}"


def test_skip_without_reason_is_gap(tmp_path: Path, monkeypatch):
    """AC 6: skip action with empty reason → gap."""
    _mock_registry(monkeypatch, always=["security-auditor"])
    td = _setup_task(tmp_path, domains=[])
    receipt_audit_routing(td, [
        {
            "name": "security-auditor",
            "action": "skip",
            "reason": "",
            "route_mode": "generic",
            "agent_path": None,
            "injected_agent_sha256": None,
        }
    ])
    gaps = require_receipts_for_done(td)
    assert any("security-auditor" in g and "skip without reason" in g for g in gaps), \
        f"expected skip-without-reason gap in {gaps}"


def test_skip_with_reason_passes(tmp_path: Path, monkeypatch):
    """AC 6: skip + valid reason → no gap for that auditor."""
    _mock_registry(monkeypatch, always=["security-auditor"])
    td = _setup_task(tmp_path, domains=[])
    receipt_audit_routing(td, [
        {
            "name": "security-auditor",
            "action": "skip",
            "reason": "clean task, no findings",
            "route_mode": "generic",
            "agent_path": None,
            "injected_agent_sha256": None,
        }
    ])
    gaps = require_receipts_for_done(td)
    # security-auditor should not appear in any gap
    assert not any("security-auditor" in g for g in gaps), f"unexpected gap(s): {gaps}"


def test_spawn_with_matching_receipt_passes(tmp_path: Path, monkeypatch):
    """AC 6: eligible auditor marked spawn + receipt present → passes."""
    _mock_registry(monkeypatch, always=["security-auditor"])
    td = _setup_task(tmp_path, domains=[])
    receipt_audit_routing(td, [
        {
            "name": "security-auditor",
            "action": "spawn",
            "route_mode": "generic",
            "agent_path": None,
            "injected_agent_sha256": None,
        }
    ])
    receipt_audit_done(
        td, "security-auditor", "haiku", 0, 0, None, 100,
        route_mode="generic", agent_path=None, injected_agent_sha256=None,
    )
    gaps = require_receipts_for_done(td)
    assert not any("security-auditor" in g for g in gaps), f"unexpected gap(s): {gaps}"


def test_spawn_without_receipt_is_gap(tmp_path: Path, monkeypatch):
    """AC 6: eligible auditor marked spawn but no audit-{name} receipt → gap."""
    _mock_registry(monkeypatch, always=["security-auditor"])
    td = _setup_task(tmp_path, domains=[])
    receipt_audit_routing(td, [
        {
            "name": "security-auditor",
            "action": "spawn",
            "route_mode": "generic",
            "agent_path": None,
            "injected_agent_sha256": None,
        }
    ])
    gaps = require_receipts_for_done(td)
    assert any("audit-security-auditor" in g and "missing" in g for g in gaps), \
        f"expected audit-security-auditor missing gap in {gaps}"


def test_unknown_action_on_eligible_is_gap(tmp_path: Path, monkeypatch):
    """AC 6: eligible auditor with unknown action → gap."""
    _mock_registry(monkeypatch, always=["security-auditor"])
    td = _setup_task(tmp_path, domains=[])
    receipt_audit_routing(td, [
        {
            "name": "security-auditor",
            "action": "dance",
            "route_mode": "generic",
            "agent_path": None,
            "injected_agent_sha256": None,
        }
    ])
    gaps = require_receipts_for_done(td)
    assert any("security-auditor" in g and "unknown action" in g for g in gaps), \
        f"expected unknown action gap in {gaps}"


def test_routing_entry_not_in_registry_accepted_as_extra(tmp_path: Path, monkeypatch):
    """AC 6: routing entry that is NOT in registry_eligible does NOT cause
    a 'missing registry-eligible' error. Extras are accepted."""
    _mock_registry(monkeypatch, always=[])  # Empty registry
    td = _setup_task(tmp_path, domains=[])
    # Routing has an extra auditor not in registry AND provides the receipt.
    receipt_audit_routing(td, [
        {
            "name": "extra-auditor",
            "action": "spawn",
            "route_mode": "generic",
            "agent_path": None,
            "injected_agent_sha256": None,
        }
    ])
    receipt_audit_done(
        td, "extra-auditor", "haiku", 0, 0, None, 100,
        route_mode="generic", agent_path=None, injected_agent_sha256=None,
    )
    gaps = require_receipts_for_done(td)
    # No "missing registry-eligible" gap — extras are fine.
    assert not any("missing registry-eligible" in g for g in gaps), \
        f"unexpected registry-eligible error for extra: {gaps}"


def test_domain_conditional_eligible_required(tmp_path: Path, monkeypatch):
    """AC 6: domain-conditional auditors become eligible when their domain
    appears in classification.domains."""
    _mock_registry(
        monkeypatch,
        always=[],
        domain_conditional={"ui": ["ui-auditor"]},
    )
    td = _setup_task(tmp_path, domains=["ui"])
    receipt_audit_routing(td, [])  # ui-auditor missing
    gaps = require_receipts_for_done(td)
    assert any("ui-auditor" in g for g in gaps), \
        f"expected ui-auditor gap in {gaps}"


def test_fast_track_uses_fast_track_list(tmp_path: Path, monkeypatch):
    """AC 6: fast_track=True → registry['fast_track'] is the eligible set."""
    _mock_registry(
        monkeypatch,
        always=["always-auditor"],
        fast_track=["fast-auditor"],
    )
    td = _setup_task(tmp_path, domains=[], fast_track=True)
    receipt_audit_routing(td, [])  # neither present
    gaps = require_receipts_for_done(td)
    # Only fast-auditor is eligible when fast_track=True.
    assert any("fast-auditor" in g for g in gaps)
    # always-auditor should NOT be required under fast_track.
    assert not any("always-auditor" in g for g in gaps), \
        f"always-auditor should not be required under fast_track: {gaps}"
