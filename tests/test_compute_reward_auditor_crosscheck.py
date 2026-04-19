"""Tests for compute_reward auditor cross-check (AC 16).

Reports whose auditor_name is NOT in the audit-routing receipt's spawn list
are dropped (their findings don't count) and an auditor_not_in_routing
event is emitted. When audit-routing is missing entirely, the cross-check
is skipped (single auditor_cross_check_skipped event); all reports are kept.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_validate import compute_reward  # noqa: E402
from lib_receipts import receipt_audit_routing  # noqa: E402


def _make_task(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260419-CR"
    td.mkdir(parents=True)
    (td / "manifest.json").write_text(json.dumps({
        "task_id": td.name,
        "stage": "DONE",
        "classification": {"risk_level": "medium", "domains": [], "type": "feature"},
    }))
    return td


def _write_report(td: Path, auditor: str, findings: list[dict],
                  filename: str | None = None,
                  include_auditor_field: bool = True) -> Path:
    rd = td / "audit-reports"
    rd.mkdir(parents=True, exist_ok=True)
    fname = filename or f"{auditor}.json"
    path = rd / fname
    payload = {"findings": findings}
    if include_auditor_field:
        payload["auditor_name"] = auditor
    path.write_text(json.dumps(payload))
    return path


def _read_events(td: Path) -> list[dict]:
    events_path = td / "events.jsonl"
    if not events_path.exists():
        return []
    return [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]


def test_rogue_auditor_dropped_and_event_emitted(tmp_path: Path):
    """AC 16: auditor not in audit-routing → report dropped + event emitted."""
    td = _make_task(tmp_path)
    receipt_audit_routing(td, [{
        "name": "valid-auditor",
        "action": "spawn",
        "route_mode": "generic",
        "agent_path": None,
        "injected_agent_sha256": None,
    }])
    _write_report(td, "valid-auditor", [{"id": "F-1", "blocking": False}])
    _write_report(td, "rogue-auditor", [{"id": "F-X", "blocking": True}])

    result = compute_reward(td)
    # Only valid-auditor's finding counted; rogue dropped → no blocking findings.
    assert result["findings_by_auditor"].get("valid-auditor") == 1
    assert "rogue-auditor" not in result["findings_by_auditor"]
    # 0 blocking → quality_score = 0.9 (clean-task default with findings)
    assert result["quality_score"] == 0.9

    events = _read_events(td)
    drops = [e for e in events if e.get("event") == "auditor_not_in_routing"]
    assert len(drops) == 1
    assert drops[0]["auditor"] == "rogue-auditor"


def test_valid_auditor_in_routing_counted(tmp_path: Path):
    """AC 16: auditor in routing → counted normally."""
    td = _make_task(tmp_path)
    receipt_audit_routing(td, [
        {"name": "sec", "action": "spawn", "route_mode": "generic",
         "agent_path": None, "injected_agent_sha256": None},
        {"name": "perf", "action": "spawn", "route_mode": "generic",
         "agent_path": None, "injected_agent_sha256": None},
    ])
    _write_report(td, "sec", [{"id": "F-1", "blocking": False}])
    _write_report(td, "perf", [{"id": "F-2", "blocking": False}])

    result = compute_reward(td)
    assert result["findings_by_auditor"].get("sec") == 1
    assert result["findings_by_auditor"].get("perf") == 1


def test_missing_routing_skips_crosscheck_keeps_reports(tmp_path: Path):
    """AC 16: no audit-routing receipt → emit single skipped event, keep all reports."""
    td = _make_task(tmp_path)
    # No receipt_audit_routing call → receipt missing.
    _write_report(td, "any-auditor", [{"id": "F-1", "blocking": False}])

    result = compute_reward(td)
    assert result["findings_by_auditor"].get("any-auditor") == 1

    events = _read_events(td)
    skipped = [e for e in events if e.get("event") == "auditor_cross_check_skipped"]
    assert len(skipped) == 1
    # No drop events fired
    drops = [e for e in events if e.get("event") == "auditor_not_in_routing"]
    assert drops == []


def test_skip_action_in_routing_does_not_validate_reports(tmp_path: Path):
    """AC 16: routing entries with action='skip' are NOT in the spawn allowlist;
    a report from a skipped auditor is dropped."""
    td = _make_task(tmp_path)
    receipt_audit_routing(td, [{
        "name": "sec",
        "action": "skip",
        "reason": "low risk",
        "route_mode": "generic",
        "agent_path": None,
        "injected_agent_sha256": None,
    }])
    _write_report(td, "sec", [{"id": "F-1", "blocking": True}])

    result = compute_reward(td)
    # Skipped auditor → report dropped.
    assert "sec" not in result["findings_by_auditor"]


def test_auditor_resolved_from_filename_when_field_missing(tmp_path: Path):
    """AC 16: report with no auditor_name field → derive from filename stem."""
    td = _make_task(tmp_path)
    receipt_audit_routing(td, [{
        "name": "stem-auditor",
        "action": "spawn",
        "route_mode": "generic",
        "agent_path": None,
        "injected_agent_sha256": None,
    }])
    # File named after the auditor; no auditor_name field inside.
    _write_report(td, "stem-auditor", [{"id": "F-1", "blocking": False}],
                  include_auditor_field=False)

    result = compute_reward(td)
    assert result["findings_by_auditor"].get("stem-auditor") == 1
