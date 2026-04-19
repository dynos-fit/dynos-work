"""Tests that every new gate emits a gate_refused event when refusing (AC 26).

The state-machine gates added in seg-2 (AC 6, 7, 8, 9, 10, 11) all sit
inside the _refuse() helper which emits a gate_refused event before
raising ValueError. This test verifies the event surfaces in events.jsonl
on each refusal path.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_core import transition_task  # noqa: E402
from lib_receipts import (  # noqa: E402
    receipt_calibration_applied,
    receipt_calibration_noop,
    receipt_plan_validated,
    receipt_tdd_tests,
)


def _read_events(td: Path) -> list[dict]:
    p = td / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def _gate_refused_events(td: Path) -> list[dict]:
    return [e for e in _read_events(td) if e.get("event") == "gate_refused"]


def test_classify_to_spec_normalization_refusal_emits_event(tmp_path: Path):
    """AC 26 + AC 9: critical without tdd_required → gate_refused event."""
    td = tmp_path / "project" / ".dynos" / "task-20260419-EV1"
    td.mkdir(parents=True)
    (td / "manifest.json").write_text(json.dumps({
        "task_id": td.name,
        "stage": "CLASSIFY_AND_SPEC",
        "classification": {"risk_level": "critical"},
    }))
    with pytest.raises(ValueError):
        transition_task(td, "SPEC_NORMALIZATION")
    events = _gate_refused_events(td)
    assert len(events) >= 1
    assert events[0]["stage_from"] == "CLASSIFY_AND_SPEC"
    assert events[0]["stage_to"] == "SPEC_NORMALIZATION"


def test_pre_exec_to_execution_tdd_tests_refusal_emits_event(tmp_path: Path):
    """AC 26 + AC 8: tdd_required=True missing receipt → gate_refused."""
    td = tmp_path / "project" / ".dynos" / "task-20260419-EV2"
    td.mkdir(parents=True)
    (td / "manifest.json").write_text(json.dumps({
        "task_id": td.name,
        "stage": "PRE_EXECUTION_SNAPSHOT",
        "classification": {"risk_level": "high", "tdd_required": True},
    }))
    receipt_plan_validated(td, 1, [1])
    # Force evidence file but no receipt → refuse
    (td / "evidence").mkdir()
    (td / "evidence" / "tdd-tests.md").write_text("# TDD\n")
    with pytest.raises(ValueError):
        transition_task(td, "EXECUTION")
    events = _gate_refused_events(td)
    assert len(events) >= 1
    assert events[0]["stage_to"] == "EXECUTION"


def test_done_to_calibrated_drift_refusal_emits_event(tmp_path: Path, monkeypatch):
    """AC 26 + AC 10: hash mismatch → gate_refused event."""
    td = tmp_path / "project" / ".dynos" / "task-20260419-EV3"
    td.mkdir(parents=True)
    (td / "manifest.json").write_text(json.dumps({
        "task_id": td.name,
        "stage": "DONE",
    }))
    receipt_calibration_applied(td, 1, 1, "b" * 64, "receipt_hash" + "0" * 52)
    import eventbus
    monkeypatch.setattr(eventbus, "_compute_policy_hash", lambda root: "live_hash" + "0" * 55)
    with pytest.raises(ValueError):
        transition_task(td, "CALIBRATED")
    events = _gate_refused_events(td)
    assert len(events) >= 1
    assert events[0]["stage_to"] == "CALIBRATED"


def test_done_to_calibrated_missing_receipt_emits_event(tmp_path: Path, monkeypatch):
    """AC 26: missing calibration receipt → gate_refused."""
    td = tmp_path / "project" / ".dynos" / "task-20260419-EV4"
    td.mkdir(parents=True)
    (td / "manifest.json").write_text(json.dumps({
        "task_id": td.name,
        "stage": "DONE",
    }))
    import eventbus
    monkeypatch.setattr(eventbus, "_compute_policy_hash", lambda root: "x" * 64)
    with pytest.raises(ValueError):
        transition_task(td, "CALIBRATED")
    events = _gate_refused_events(td)
    assert len(events) >= 1


def test_gate_refused_event_carries_reason_field(tmp_path: Path):
    """AC 26: gate_refused events carry the human-readable reason string."""
    td = tmp_path / "project" / ".dynos" / "task-20260419-EV5"
    td.mkdir(parents=True)
    (td / "manifest.json").write_text(json.dumps({
        "task_id": td.name,
        "stage": "CLASSIFY_AND_SPEC",
        "classification": {"risk_level": "high"},
    }))
    with pytest.raises(ValueError):
        transition_task(td, "SPEC_NORMALIZATION")
    events = _gate_refused_events(td)
    assert len(events) >= 1
    assert "reason" in events[0]
    assert "tdd_required" in events[0]["reason"]
