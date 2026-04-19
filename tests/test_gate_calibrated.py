"""Tests for DONE -> CALIBRATED gate (AC 23)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_core import transition_task  # noqa: E402
from lib_receipts import receipt_calibration_applied  # noqa: E402


def _setup(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260418-CL"
    td.mkdir(parents=True)
    (td / "manifest.json").write_text(json.dumps({
        "task_id": "task-20260418-CL",
        "stage": "DONE",
        "classification": {"risk_level": "medium"},
    }))
    return td


def test_missing_calibration_applied_refuses(tmp_path: Path):
    td = _setup(tmp_path)
    with pytest.raises(ValueError, match="calibration-applied"):
        transition_task(td, "CALIBRATED")


def test_calibration_receipt_present_passes(tmp_path: Path, monkeypatch):
    """Migrated for task-20260419-006: AC 10 added a live policy-hash
    cross-check at this gate. Patch eventbus._compute_policy_hash to return
    the same value embedded in the receipt so the gate accepts it."""
    td = _setup(tmp_path)
    live_hash = "b" * 64
    import eventbus
    monkeypatch.setattr(eventbus, "_compute_policy_hash", lambda root: live_hash)
    receipt_calibration_applied(td, 1, 1, "a" * 64, live_hash)
    transition_task(td, "CALIBRATED")
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == "CALIBRATED"


def test_force_bypass_succeeds(tmp_path: Path):
    td = _setup(tmp_path)
    transition_task(td, "CALIBRATED", force=True)
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == "CALIBRATED"
