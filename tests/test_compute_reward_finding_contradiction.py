"""Tests for finding_contradiction event replacing silent sanitizer (AC 14).

The OLD behavior: findings whose blocking=True but whose recommendation
said "no action required" / "correctly implemented" were silently
downgraded (blocking=False, severity=minor). The NEW behavior: emit a
finding_contradiction event and leave the finding blocking=True. The
contradiction surfaces as telemetry instead of being swallowed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_validate import compute_reward  # noqa: E402


def _make_task(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260419-CT"
    td.mkdir(parents=True)
    (td / "manifest.json").write_text(json.dumps({
        "task_id": td.name,
        "stage": "DONE",
        "classification": {"risk_level": "medium", "domains": [], "type": "feature"},
    }))
    # NO audit-routing receipt — so compute_reward falls back to accept-all.
    return td


def _write_report(td: Path, auditor: str, findings: list[dict]) -> Path:
    rd = td / "audit-reports"
    rd.mkdir(parents=True, exist_ok=True)
    path = rd / f"{auditor}.json"
    path.write_text(json.dumps({
        "auditor_name": auditor,
        "findings": findings,
    }))
    return path


def _read_events(td: Path) -> list[dict]:
    events_path = td / "events.jsonl"
    if not events_path.exists():
        return []
    events = []
    for line in events_path.read_text().splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


def test_contradictory_finding_stays_blocking_and_emits_event(tmp_path: Path):
    """AC 14: blocking=True + recommendation='no action required' → finding
    stays blocking AND event emitted."""
    td = _make_task(tmp_path)
    _write_report(td, "sec", [{
        "id": "F-1",
        "blocking": True,
        "recommendation": "No action required — code is correctly implemented.",
    }])
    result = compute_reward(td)
    # The old sanitizer would have set quality_score=0.9 (total_blocking=0).
    # The new logic keeps blocking=1; no pre-repair → surviving = total_blocking,
    # so quality_score = 1.0 - 1/1 = 0.0 (NOT 0.9).
    assert result["quality_score"] == 0.0, \
        f"finding should still be blocking; got quality_score={result['quality_score']}"
    assert result["findings_by_auditor"].get("sec") == 1

    events = _read_events(td)
    contra = [e for e in events if e.get("event") == "finding_contradiction"]
    assert len(contra) == 1
    assert contra[0]["auditor"] == "sec"
    assert contra[0]["finding_id"] == "F-1"


def test_contradictory_finding_recommendation_variants_all_caught(tmp_path: Path):
    """AC 14: every known exemption phrase triggers the event."""
    td = _make_task(tmp_path)
    phrases = [
        "no action required",
        "no action needed",
        "correctly implemented",
        "properly implemented",
        "no changes needed",
        "no fix needed",
    ]
    findings = [
        {"id": f"F-{i}", "blocking": True, "recommendation": phrase}
        for i, phrase in enumerate(phrases)
    ]
    _write_report(td, "sec", findings)
    compute_reward(td)
    events = _read_events(td)
    contra = [e for e in events if e.get("event") == "finding_contradiction"]
    assert len(contra) == len(phrases), \
        f"expected {len(phrases)} contradictions, got {len(contra)}"


def test_non_contradictory_finding_no_event(tmp_path: Path):
    """AC 14: a plain blocking finding with no contradiction → no event."""
    td = _make_task(tmp_path)
    _write_report(td, "sec", [{
        "id": "F-2",
        "blocking": True,
        "recommendation": "Fix the SQL injection at src/db.py:42",
    }])
    compute_reward(td)
    events = _read_events(td)
    contra = [e for e in events if e.get("event") == "finding_contradiction"]
    assert len(contra) == 0


def test_quality_score_reflects_real_blocking_count(tmp_path: Path):
    """AC 14: quality_score reflects real blocking count — not downgraded away."""
    td = _make_task(tmp_path)
    _write_report(td, "sec", [
        {"id": "F-1", "blocking": True, "recommendation": "no action required"},
        {"id": "F-2", "blocking": True, "recommendation": "Fix this."},
    ])
    result = compute_reward(td)
    # Both findings stay blocking; no pre-repair file → surviving=total_blocking=2;
    # quality_score = 1.0 - (2/2) = 0.0
    assert result["quality_score"] == 0.0
    assert result["findings_by_auditor"].get("sec") == 2


def test_recommendation_snippet_truncated(tmp_path: Path):
    """AC 14: recommendation_snippet is bounded to 120 chars in event payload."""
    td = _make_task(tmp_path)
    long_rec = "no action required. " + ("X" * 500)
    _write_report(td, "sec", [{
        "id": "F-L", "blocking": True, "recommendation": long_rec,
    }])
    compute_reward(td)
    events = _read_events(td)
    contra = [e for e in events if e.get("event") == "finding_contradiction"]
    assert len(contra) == 1
    assert len(contra[0]["recommendation_snippet"]) <= 120
