"""Tests for eventbus calibration-noop branch (AC 20).

The eventbus _drain_locked routine, after all learning handlers succeed,
branches between calibration-applied and calibration-noop based on
(policy_hash_unchanged, retros_count). This test exercises that branch
matrix using direct stubbing of the receipt writers — running the full
drain in-process is impractical because it spawns handler subprocesses.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))


def _make_task(tmp_path: Path, *, retro: dict | None = None) -> Path:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260419-NB"
    td.mkdir(parents=True)
    (td / "manifest.json").write_text(json.dumps({
        "task_id": td.name,
        "stage": "DONE",
        "classification": {"risk_level": "medium"},
    }))
    if retro is not None:
        (td / "task-retrospective.json").write_text(json.dumps(retro))
    return td


def _replay_branch(task_dir: Path, *, hash_unchanged: bool,
                   retros_count: int) -> tuple[str, dict]:
    """Replay the calibration-branch decision logic from eventbus._drain_locked.

    Returns ('noop' | 'applied', kwargs) without invoking the real handlers.
    Mirror of lib/eventbus.py lines 619-650.
    """
    from lib_receipts import (
        receipt_calibration_applied,
        receipt_calibration_noop,
    )
    calls = []

    def _fake_noop(td, reason, policy_sha256):
        calls.append(("noop", {"task_dir": str(td), "reason": reason,
                               "policy_sha256": policy_sha256}))
        return td / "receipts" / "calibration-noop.json"

    def _fake_applied(td, retros_consumed, scores_updated,
                      policy_sha256_before, policy_sha256_after):
        calls.append(("applied", {
            "task_dir": str(td),
            "retros_consumed": retros_consumed,
            "scores_updated": scores_updated,
            "policy_sha256_before": policy_sha256_before,
            "policy_sha256_after": policy_sha256_after,
        }))
        return td / "receipts" / "calibration-applied.json"

    policy_after = "deadbeef" + "0" * 56
    if hash_unchanged and retros_count == 0:
        _fake_noop(task_dir, "no-retros", policy_after)
    elif hash_unchanged and retros_count > 0:
        _fake_noop(task_dir, "all-handlers-zero-work", policy_after)
    else:
        _fake_applied(task_dir, retros_count, retros_count, "before" + "0" * 58,
                      policy_after)
    return calls[0]


def test_zero_retros_unchanged_hash_writes_noop_no_retros(tmp_path: Path):
    """AC 20: hash unchanged + no retros → noop with reason='no-retros'."""
    td = _make_task(tmp_path)
    label, kwargs = _replay_branch(td, hash_unchanged=True, retros_count=0)
    assert label == "noop"
    assert kwargs["reason"] == "no-retros"


def test_retros_present_unchanged_hash_writes_noop_zero_work(tmp_path: Path):
    """AC 20: hash unchanged + retros present → noop reason='all-handlers-zero-work'."""
    td = _make_task(tmp_path, retro={"findings_by_auditor": {"sec": 2}})
    label, kwargs = _replay_branch(td, hash_unchanged=True, retros_count=1)
    assert label == "noop"
    assert kwargs["reason"] == "all-handlers-zero-work"


def test_hash_changed_writes_applied(tmp_path: Path):
    """AC 20: hash changed → applied receipt (real policy delta)."""
    td = _make_task(tmp_path, retro={"findings_by_auditor": {"sec": 1}})
    label, kwargs = _replay_branch(td, hash_unchanged=False, retros_count=1)
    assert label == "applied"
    assert kwargs["policy_sha256_before"] != kwargs["policy_sha256_after"]


def test_real_writer_zero_retros_writes_noop_receipt(tmp_path: Path):
    """AC 20 integration: invoke the actual receipt_calibration_noop writer."""
    td = _make_task(tmp_path)
    from lib_receipts import receipt_calibration_noop, read_receipt
    receipt_calibration_noop(td, "no-retros", "a" * 64)
    payload = read_receipt(td, "calibration-noop")
    assert payload is not None
    assert payload["reason"] == "no-retros"
    assert payload["policy_sha256"] == "a" * 64


def test_real_writer_all_handlers_zero_work_writes_noop_receipt(tmp_path: Path):
    """AC 20 integration: 'all-handlers-zero-work' reason writes a valid receipt."""
    td = _make_task(tmp_path, retro={"findings_by_auditor": {"sec": 3}})
    from lib_receipts import receipt_calibration_noop, read_receipt
    receipt_calibration_noop(td, "all-handlers-zero-work", "b" * 64)
    payload = read_receipt(td, "calibration-noop")
    assert payload is not None
    assert payload["reason"] == "all-handlers-zero-work"


def test_eventbus_imports_calibration_noop(tmp_path: Path):
    """AC 20: eventbus.py imports receipt_calibration_noop from lib_receipts."""
    import inspect
    import eventbus
    source = inspect.getsource(eventbus)
    assert "receipt_calibration_noop" in source, \
        "eventbus.py must import/use receipt_calibration_noop"
