"""Tests for receipt_calibration_noop writer + applied refuse-on-no-op (AC 4).

New writer: receipts/calibration-noop.json with enum-validated reason.
calibration-applied refuses when retros_consumed>0 but hashes are identical
(that is a no-op, not an applied calibration).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_receipts import (  # noqa: E402
    receipt_calibration_applied,
    receipt_calibration_noop,
)


def _make_task(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260419-CN"
    td.mkdir(parents=True)
    return td


def test_noop_happy_path_writes_receipt(tmp_path: Path):
    """AC 4: valid reason + policy_sha256 → receipt written."""
    td = _make_task(tmp_path)
    out = receipt_calibration_noop(td, "no-retros", "a" * 64)
    payload = json.loads(out.read_text())
    assert payload["step"] == "calibration-noop"
    assert payload["reason"] == "no-retros"
    assert payload["policy_sha256"] == "a" * 64
    assert payload["valid"] is True


def test_noop_reason_enum_validated(tmp_path: Path):
    """AC 4: invalid reason → ValueError."""
    td = _make_task(tmp_path)
    with pytest.raises(ValueError, match="invalid calibration-noop reason"):
        receipt_calibration_noop(td, "made-up-reason", "a" * 64)


def test_noop_all_handlers_zero_work_valid(tmp_path: Path):
    """AC 4: 'all-handlers-zero-work' is a valid reason."""
    td = _make_task(tmp_path)
    out = receipt_calibration_noop(td, "all-handlers-zero-work", "b" * 64)
    payload = json.loads(out.read_text())
    assert payload["reason"] == "all-handlers-zero-work"


def test_noop_empty_policy_sha_refuses(tmp_path: Path):
    """AC 4: policy_sha256 must be non-empty string."""
    td = _make_task(tmp_path)
    with pytest.raises(ValueError):
        receipt_calibration_noop(td, "no-retros", "")


def test_applied_refuses_when_retros_consumed_but_no_policy_change(tmp_path: Path):
    """AC 4: retros_consumed > 0 + before == after → ValueError naming
    'calibration-noop' as alternative writer."""
    td = _make_task(tmp_path)
    with pytest.raises(ValueError) as exc_info:
        receipt_calibration_applied(
            td,
            retros_consumed=5,
            scores_updated=0,
            policy_sha256_before="a" * 64,
            policy_sha256_after="a" * 64,
        )
    msg = str(exc_info.value)
    assert "calibration-noop" in msg
    assert "REFUSES" in msg or "REFUSES" in msg.upper() or "refuse" in msg.lower()


def test_applied_accepts_zero_retros_same_hash(tmp_path: Path):
    """AC 4 edge case: retros_consumed=0 + same hash → NOT a no-op (no retros
    to "consume" means nothing to complain about). Writer must succeed."""
    td = _make_task(tmp_path)
    out = receipt_calibration_applied(
        td,
        retros_consumed=0,
        scores_updated=0,
        policy_sha256_before="a" * 64,
        policy_sha256_after="a" * 64,
    )
    assert out.exists()


def test_applied_normal_path_passes(tmp_path: Path):
    """AC 4: retros_consumed > 0 AND before != after → applied writer works."""
    td = _make_task(tmp_path)
    out = receipt_calibration_applied(
        td,
        retros_consumed=3,
        scores_updated=2,
        policy_sha256_before="a" * 64,
        policy_sha256_after="b" * 64,
    )
    payload = json.loads(out.read_text())
    assert payload["policy_sha256_before"] == "a" * 64
    assert payload["policy_sha256_after"] == "b" * 64
    assert payload["retros_consumed"] == 3
