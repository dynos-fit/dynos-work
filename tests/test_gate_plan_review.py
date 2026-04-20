"""Tests for PLAN_REVIEW -> PLAN_AUDIT gate (AC 4)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_core import transition_task  # noqa: E402
from lib_receipts import hash_file, receipt_human_approval  # noqa: E402


def _setup(tmp_path: Path, *, plan_text: str = "plan content\n") -> Path:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260418-P"
    td.mkdir(parents=True)
    (td / "manifest.json").write_text(json.dumps({
        "task_id": "task-20260418-P",
        "stage": "PLAN_REVIEW",
        "classification": {"risk_level": "medium"},
    }))
    (td / "plan.md").write_text(plan_text)
    return td


def test_missing_receipt_refuses(tmp_path: Path):
    td = _setup(tmp_path)
    with pytest.raises(ValueError, match="human-approval-PLAN_REVIEW"):
        transition_task(td, "PLAN_AUDIT")


def test_hash_drift_refuses(tmp_path: Path):
    td = _setup(tmp_path)
    sha = hash_file(td / "plan.md")
    receipt_human_approval(td, "PLAN_REVIEW", sha)
    (td / "plan.md").write_text("after approval drift\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        transition_task(td, "PLAN_AUDIT")


def test_force_bypass_succeeds(tmp_path: Path):
    td = _setup(tmp_path)
    transition_task(
        td,
        "PLAN_AUDIT",
        force=True,
        force_reason="test: PLAN_REVIEW human-approval gate bypass",
        force_approver="test-suite",
    )
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == "PLAN_AUDIT"


def test_matching_hash_passes(tmp_path: Path):
    td = _setup(tmp_path)
    sha = hash_file(td / "plan.md")
    receipt_human_approval(td, "PLAN_REVIEW", sha)
    transition_task(td, "PLAN_AUDIT")
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == "PLAN_AUDIT"
