"""Tests that v1 receipts (no contract_version) remain readable (AC 30)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_receipts import read_receipt, validate_chain  # noqa: E402


def _setup(tmp_path: Path, *, stage: str) -> Path:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260418-V1"
    td.mkdir(parents=True)
    (td / "manifest.json").write_text(json.dumps({
        "task_id": td.name,
        "stage": stage,
    }))
    (td / "receipts").mkdir()
    return td


def _write_v1_receipt(td: Path, name: str, **payload):
    payload = {"step": name, "ts": "2025-01-01T00:00:00Z", "valid": True, **payload}
    # Critical: NO contract_version field
    (td / "receipts" / f"{name}.json").write_text(json.dumps(payload))


def test_v1_receipt_read_returns_payload(tmp_path: Path):
    td = _setup(tmp_path, stage="EXECUTION")
    _write_v1_receipt(td, "spec-validated", criteria_count=3, spec_sha256="x" * 64)
    out = read_receipt(td, "spec-validated")
    assert out is not None
    assert out["criteria_count"] == 3
    assert "contract_version" not in out


def test_v1_receipt_passes_validate_chain(tmp_path: Path):
    """v1 receipts for LEGACY steps (not in MIN_VERSION_PER_STEP floor) remain
    readable. plan-validated was bumped to v2-mandatory in task-006; legacy
    steps like spec-validated stay backward-compatible.
    """
    td = _setup(tmp_path, stage="EXECUTION")
    _write_v1_receipt(td, "spec-validated", criteria_count=3,
                      spec_sha256="x" * 64)
    gaps = validate_chain(td)
    assert "spec-validated" not in gaps


def test_v1_receipt_with_valid_false_returns_none(tmp_path: Path):
    """Defensive: an invalid v1 receipt must be skipped, even without contract_version."""
    td = _setup(tmp_path, stage="EXECUTION")
    payload = {"step": "spec-validated", "ts": "2025-01-01T00:00:00Z", "valid": False}
    (td / "receipts" / "spec-validated.json").write_text(json.dumps(payload))
    out = read_receipt(td, "spec-validated")
    assert out is None


def test_v1_receipts_rejected_below_floor(tmp_path: Path):
    """Task-006 contract bump: v1 receipts for v2-mandatory steps are rejected
    (treated as missing by validate_chain). This is the INTENDED breaking
    change for plan-validated, executor-*, audit-*, rules-check-passed,
    calibration-*, and human-approval-*.
    """
    td = _setup(tmp_path, stage="DONE")
    _write_v1_receipt(td, "plan-validated", segment_count=0, criteria_coverage=[])
    _write_v1_receipt(td, "executor-routing", segments=[])
    _write_v1_receipt(td, "audit-routing", auditors=[])
    gaps = validate_chain(td)
    # All three are floor=2; v1 entries are treated as missing → appear in gaps.
    assert "plan-validated" in gaps
    assert "executor-routing" in gaps
    assert "audit-routing" in gaps
