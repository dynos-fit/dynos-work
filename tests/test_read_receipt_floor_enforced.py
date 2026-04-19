"""Parametrized tests for MIN_VERSION_PER_STEP floor enforcement (AC 21).

For each (step, floor) pair in MIN_VERSION_PER_STEP, write a receipt at
floor-1 and confirm read_receipt returns None; write one at floor and
confirm the receipt is returned. This tests the actual lookup, not just
the mapping.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_receipts import (  # noqa: E402
    MIN_VERSION_PER_STEP,
    read_receipt,
)


def _make_task(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260419-FE"
    td.mkdir(parents=True)
    (td / "receipts").mkdir()
    return td


def _write_raw(td: Path, step: str, version: int):
    payload = {
        "step": step,
        "ts": "2025-01-01T00:00:00Z",
        "valid": True,
        "contract_version": version,
    }
    (td / "receipts" / f"{step}.json").write_text(json.dumps(payload))


def _concrete_step_for(key: str) -> str:
    """Wildcard keys end with '*'; replace with a real instance."""
    if key.endswith("*"):
        return key[:-1] + "test-instance"
    return key


@pytest.mark.parametrize(
    "key,floor",
    sorted(MIN_VERSION_PER_STEP.items()),
)
def test_below_floor_returns_none(tmp_path: Path, key: str, floor: int):
    """AC 21: receipt at floor-1 → read_receipt returns None."""
    td = _make_task(tmp_path)
    step = _concrete_step_for(key)
    if floor <= 1:
        pytest.skip(f"floor={floor} cannot be below 1")
    _write_raw(td, step, floor - 1)
    out = read_receipt(td, step)
    assert out is None, f"step={step} v={floor-1} should be rejected (floor={floor})"


@pytest.mark.parametrize(
    "key,floor",
    sorted(MIN_VERSION_PER_STEP.items()),
)
def test_at_floor_returns_payload(tmp_path: Path, key: str, floor: int):
    """AC 21: receipt at floor → read_receipt returns the payload."""
    td = _make_task(tmp_path)
    step = _concrete_step_for(key)
    _write_raw(td, step, floor)
    out = read_receipt(td, step)
    assert out is not None, f"step={step} v={floor} should be accepted (floor={floor})"
    assert out["contract_version"] == floor
