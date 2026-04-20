"""Tests for SPEC_REVIEW -> PLANNING gate (AC 3)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_core import transition_task  # noqa: E402
from lib_receipts import hash_file, receipt_human_approval  # noqa: E402


def _setup(tmp_path: Path, *, spec_text: str = "spec content\n") -> Path:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260418-S"
    td.mkdir(parents=True)
    (td / "manifest.json").write_text(json.dumps({
        "task_id": "task-20260418-S",
        "stage": "SPEC_REVIEW",
        "classification": {"risk_level": "medium"},
    }))
    (td / "spec.md").write_text(spec_text)
    return td


def _write_approval_direct(td: Path, artifact_sha256: str) -> None:
    """Write a human-approval-SPEC_REVIEW receipt JSON directly.

    Bypasses ``receipt_human_approval`` — which fires the synchronous
    scheduler dispatch through ``write_receipt`` and auto-advances the
    manifest to PLANNING. These tests exercise ``transition_task``'s own
    gate logic in isolation; they must NOT let the scheduler race them.
    """
    receipts = td / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    (receipts / "human-approval-SPEC_REVIEW.json").write_text(json.dumps({
        "step": "human-approval-SPEC_REVIEW",
        "ts": "2026-04-19T00:00:00Z",
        "valid": True,
        "contract_version": 4,
        "stage": "SPEC_REVIEW",
        "artifact_sha256": artifact_sha256,
        "approver": "human",
    }, indent=2))


def test_missing_receipt_refuses(tmp_path: Path):
    td = _setup(tmp_path)
    with pytest.raises(ValueError, match="human-approval-SPEC_REVIEW"):
        transition_task(td, "PLANNING")


def test_hash_drift_refuses(tmp_path: Path):
    td = _setup(tmp_path)
    sha = hash_file(td / "spec.md")
    _write_approval_direct(td, sha)
    # Drift the spec after approval
    (td / "spec.md").write_text("modified content\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        transition_task(td, "PLANNING")


def test_force_bypass_succeeds(tmp_path: Path):
    td = _setup(tmp_path)
    # No receipt; force=True must succeed
    transition_task(
        td,
        "PLANNING",
        force=True,
        force_reason="test: SPEC_REVIEW human-approval gate bypass",
        force_approver="test-suite",
    )
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == "PLANNING"


def test_matching_hash_passes(tmp_path: Path):
    td = _setup(tmp_path)
    sha = hash_file(td / "spec.md")
    _write_approval_direct(td, sha)
    transition_task(td, "PLANNING")
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == "PLANNING"
