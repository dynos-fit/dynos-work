"""Tests for TDD_REVIEW -> PRE_EXECUTION_SNAPSHOT gate (AC 6)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_core import transition_task  # noqa: E402
from lib_receipts import hash_file, receipt_human_approval  # noqa: E402


def _setup(tmp_path: Path, *, tdd_text: str = "tdd evidence\n") -> Path:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260418-T"
    td.mkdir(parents=True)
    (td / "manifest.json").write_text(json.dumps({
        "task_id": "task-20260418-T",
        "stage": "TDD_REVIEW",
        "classification": {"risk_level": "medium"},
    }))
    evidence = td / "evidence"
    evidence.mkdir()
    (evidence / "tdd-tests.md").write_text(tdd_text)
    return td


def test_missing_receipt_refuses(tmp_path: Path):
    td = _setup(tmp_path)
    with pytest.raises(ValueError, match="human-approval-TDD_REVIEW"):
        transition_task(td, "PRE_EXECUTION_SNAPSHOT")


def test_hash_drift_refuses(tmp_path: Path):
    td = _setup(tmp_path)
    artifact = td / "evidence" / "tdd-tests.md"
    sha = hash_file(artifact)
    receipt_human_approval(td, "TDD_REVIEW", sha)
    artifact.write_text("changed after approval\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        transition_task(td, "PRE_EXECUTION_SNAPSHOT")


def test_force_bypass_succeeds(tmp_path: Path):
    td = _setup(tmp_path)
    transition_task(
        td,
        "PRE_EXECUTION_SNAPSHOT",
        force=True,
        force_reason="test: TDD_REVIEW human-approval gate bypass",
        force_approver="test-suite",
    )
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == "PRE_EXECUTION_SNAPSHOT"


def test_matching_hash_passes(tmp_path: Path):
    td = _setup(tmp_path)
    artifact = td / "evidence" / "tdd-tests.md"
    sha = hash_file(artifact)
    receipt_human_approval(td, "TDD_REVIEW", sha)
    transition_task(td, "PRE_EXECUTION_SNAPSHOT")
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == "PRE_EXECUTION_SNAPSHOT"
