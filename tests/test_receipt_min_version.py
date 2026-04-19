"""Tests for read_receipt min_version floor (AC 3).

``read_receipt`` auto-resolves a per-step floor via ``MIN_VERSION_PER_STEP``.
Receipts with ``contract_version`` below the floor are treated as missing.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_receipts import (  # noqa: E402
    MIN_VERSION_PER_STEP,
    _resolve_min_version,
    read_receipt,
)


def _make_task(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260419-MV"
    td.mkdir(parents=True)
    (td / "receipts").mkdir()
    return td


def _write_raw_receipt(td: Path, step: str, *, version: int | None, **extra):
    payload = {"step": step, "ts": "2025-01-01T00:00:00Z", "valid": True, **extra}
    if version is not None:
        payload["contract_version"] = version
    (td / "receipts" / f"{step}.json").write_text(json.dumps(payload))


def test_v1_executor_receipt_rejected_by_default(tmp_path: Path):
    """AC 3: v1 executor-seg-1 under default (floor=2) → None."""
    td = _make_task(tmp_path)
    _write_raw_receipt(td, "executor-seg-1", version=1)
    out = read_receipt(td, "executor-seg-1")
    assert out is None


def test_min_version_one_explicit_bypass(tmp_path: Path):
    """AC 3: explicit min_version=1 disables the floor → v1 payload returned."""
    td = _make_task(tmp_path)
    _write_raw_receipt(td, "executor-seg-1", version=1, segment_id="seg-1")
    out = read_receipt(td, "executor-seg-1", min_version=1)
    assert out is not None
    assert out["contract_version"] == 1
    assert out["segment_id"] == "seg-1"


def test_v3_receipt_passes_default(tmp_path: Path):
    """AC 3: v3 receipts pass floor=2 default."""
    td = _make_task(tmp_path)
    _write_raw_receipt(td, "executor-seg-1", version=3, segment_id="seg-1")
    out = read_receipt(td, "executor-seg-1")
    assert out is not None
    assert out["contract_version"] == 3


def test_v2_receipt_at_floor_passes(tmp_path: Path):
    """AC 3: v2 at floor=2 passes (floor check is >=)."""
    td = _make_task(tmp_path)
    _write_raw_receipt(td, "plan-validated", version=2)
    out = read_receipt(td, "plan-validated")
    assert out is not None


def test_wildcard_audit_hits_audit_floor(tmp_path: Path):
    """AC 3: audit-security-auditor hits audit-* floor=2."""
    assert _resolve_min_version("audit-security-auditor") == 2
    # v1 audit receipt → None
    td = _make_task(tmp_path)
    _write_raw_receipt(td, "audit-security-auditor", version=1)
    out = read_receipt(td, "audit-security-auditor")
    assert out is None


def test_wildcard_human_approval_floor(tmp_path: Path):
    """human-approval-* pattern resolves to floor=2."""
    assert _resolve_min_version("human-approval-SPEC_REVIEW") == 2
    td = _make_task(tmp_path)
    _write_raw_receipt(td, "human-approval-SPEC_REVIEW", version=1)
    assert read_receipt(td, "human-approval-SPEC_REVIEW") is None
    # v2 passes
    _write_raw_receipt(td, "human-approval-SPEC_REVIEW", version=2)
    assert read_receipt(td, "human-approval-SPEC_REVIEW") is not None


def test_unknown_step_defaults_to_v1(tmp_path: Path):
    """AC 3: unknown step names default to floor=1."""
    assert _resolve_min_version("some-random-unknown-step") == 1
    td = _make_task(tmp_path)
    _write_raw_receipt(td, "some-random-unknown-step", version=1)
    # floor=1 → v1 passes
    out = read_receipt(td, "some-random-unknown-step")
    assert out is not None


def test_legacy_receipt_without_contract_version_is_v1(tmp_path: Path):
    """AC 3: missing contract_version defaults to 1 → rejected by floor=2."""
    td = _make_task(tmp_path)
    _write_raw_receipt(td, "executor-seg-1", version=None, segment_id="seg-1")
    out = read_receipt(td, "executor-seg-1")
    assert out is None
    # But explicit min_version=1 lets it through
    out2 = read_receipt(td, "executor-seg-1", min_version=1)
    assert out2 is not None


def test_malformed_contract_version_treated_as_missing(tmp_path: Path):
    """AC 3: non-int contract_version → None (treated as missing)."""
    td = _make_task(tmp_path)
    payload = {
        "step": "executor-seg-1",
        "ts": "2025-01-01T00:00:00Z",
        "valid": True,
        "contract_version": "garbage",
    }
    (td / "receipts" / "executor-seg-1.json").write_text(json.dumps(payload))
    out = read_receipt(td, "executor-seg-1")
    assert out is None


def test_min_version_per_step_keys_include_expected_entries():
    """AC 3: dictionary schema check."""
    expected = {
        "executor-*", "audit-*", "plan-validated", "rules-check-passed",
        "calibration-applied", "calibration-noop", "human-approval-*",
    }
    for k in expected:
        assert k in MIN_VERSION_PER_STEP
        assert MIN_VERSION_PER_STEP[k] == 2
