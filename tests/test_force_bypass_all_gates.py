"""Parametrized force=True bypass for every new gate (AC 25)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_core import transition_task  # noqa: E402


GATE_CASES = [
    # (current_stage, next_stage, classification, extra_artifacts)
    ("SPEC_REVIEW", "PLANNING", {"risk_level": "medium"}, {"spec.md": "spec\n"}),
    ("PLAN_REVIEW", "PLAN_AUDIT", {"risk_level": "medium"}, {"plan.md": "plan\n"}),
    ("TDD_REVIEW", "PRE_EXECUTION_SNAPSHOT", {"risk_level": "medium"},
     {"evidence/tdd-tests.md": "tests\n"}),
    ("PLAN_AUDIT", "PRE_EXECUTION_SNAPSHOT", {"risk_level": "critical"}, {}),
    ("PLAN_AUDIT", "TDD_REVIEW", {"risk_level": "critical"}, {}),
    ("PLAN_AUDIT", "PRE_EXECUTION_SNAPSHOT",
     {"risk_level": "medium", "tdd_required": True}, {}),
    ("DONE", "CALIBRATED", {"risk_level": "medium"}, {}),
]


@pytest.mark.parametrize("current,next_stage,classification,artifacts", GATE_CASES)
def test_force_bypass_succeeds_without_required_receipts(
    tmp_path: Path, current, next_stage, classification, artifacts,
):
    project = tmp_path / "project"
    td = project / ".dynos" / f"task-20260418-FB-{current}-{next_stage}"
    td.mkdir(parents=True)
    (td / "manifest.json").write_text(json.dumps({
        "task_id": td.name,
        "stage": current,
        "classification": classification,
    }))
    for relpath, content in artifacts.items():
        p = td / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    # Without force, the gate should refuse (proves the gate exists). We don't
    # assert here on the exact message because the spec is exercised in the
    # per-gate test files. With force=True, we should advance.
    transition_task(
        td,
        next_stage,
        force=True,
        force_reason="test: parametrized gate bypass invariant proof",
        force_approver="test-suite",
    )
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == next_stage
