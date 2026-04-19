"""Tests for DONE -> CALIBRATED live-hash cross-check (AC 10).

The gate reads calibration-applied OR calibration-noop receipts (later-ts
wins), extracts the policy hash from the chosen receipt, and live-computes
the current policy hash. Mismatch refuses; match passes. force=True bypasses.
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
)


def _setup_done(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260419-CH"
    td.mkdir(parents=True)
    (td / "manifest.json").write_text(json.dumps({
        "task_id": td.name,
        "stage": "DONE",
        "classification": {"risk_level": "medium"},
    }))
    return td


def _patch_policy_hash(monkeypatch, value: str):
    import eventbus
    monkeypatch.setattr(eventbus, "_compute_policy_hash", lambda root: value)


def test_applied_matching_hash_passes(tmp_path: Path, monkeypatch):
    """AC 10: calibration-applied whose policy_sha256_after matches live → pass."""
    td = _setup_done(tmp_path)
    live = "a" * 64
    _patch_policy_hash(monkeypatch, live)
    receipt_calibration_applied(td, 1, 1, "b" * 64, live)
    transition_task(td, "CALIBRATED")
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == "CALIBRATED"


def test_applied_drifted_hash_refuses(tmp_path: Path, monkeypatch):
    """AC 10: receipt hash != live hash → refuse."""
    td = _setup_done(tmp_path)
    _patch_policy_hash(monkeypatch, "live_hash_xyz" + "0" * 50)
    receipt_calibration_applied(td, 1, 1, "b" * 64, "receipt_hash_abc" + "0" * 48)
    with pytest.raises(ValueError) as exc_info:
        transition_task(td, "CALIBRATED")
    msg = str(exc_info.value)
    assert "calibration" in msg
    assert "hash mismatch" in msg or "policy hash" in msg


def test_noop_matching_hash_passes(tmp_path: Path, monkeypatch):
    """AC 10: calibration-noop receipt supported; matching policy_sha256 → pass."""
    td = _setup_done(tmp_path)
    live = "c" * 64
    _patch_policy_hash(monkeypatch, live)
    receipt_calibration_noop(td, "no-retros", live)
    transition_task(td, "CALIBRATED")
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == "CALIBRATED"


def test_noop_drifted_hash_refuses(tmp_path: Path, monkeypatch):
    """AC 10: noop receipt with mismatched policy_sha256 → refuse."""
    td = _setup_done(tmp_path)
    _patch_policy_hash(monkeypatch, "live" + "0" * 60)
    receipt_calibration_noop(td, "no-retros", "rec" + "e" * 61)
    with pytest.raises(ValueError) as exc_info:
        transition_task(td, "CALIBRATED")
    msg = str(exc_info.value)
    assert "calibration" in msg
    assert "hash mismatch" in msg or "policy hash" in msg


def test_missing_both_receipts_refuses(tmp_path: Path, monkeypatch):
    """AC 10: neither applied nor noop → refuse (no calibration receipt)."""
    td = _setup_done(tmp_path)
    _patch_policy_hash(monkeypatch, "a" * 64)
    with pytest.raises(ValueError):
        transition_task(td, "CALIBRATED")


def test_force_true_bypasses(tmp_path: Path, monkeypatch):
    """AC 10: force=True bypasses even with mismatched hash."""
    td = _setup_done(tmp_path)
    _patch_policy_hash(monkeypatch, "live_hash" + "0" * 55)
    receipt_calibration_applied(td, 1, 1, "b" * 64, "different_hash" + "0" * 50)
    transition_task(td, "CALIBRATED", force=True)
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == "CALIBRATED"
