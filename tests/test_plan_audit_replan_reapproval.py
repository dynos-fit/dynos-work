"""A plan repaired after human approval goes back through review.

`run-plan-audit` runs at PLAN_AUDIT, after the human approved plan.md at
PLAN_REVIEW. When it returned `replan_required`, the skills sent the planner
to repair plan.md in place, so `run-execute-setup` later refused with "plan.md
was mutated after human approval". The command now moves the task back to
PLANNING itself, so the repaired plan is re-validated by `run-planning` and
shown to the human again at PLAN_REVIEW.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PLAN = (
    "# Implementation Plan\n\n"
    "## Technical Approach\nA.\n\n"
    "## API Contracts\n\n"
    "| Method | Endpoint | Purpose |\n"
    "|---|---|---|\n"
    "| GET | /api/does-not-exist | read |\n\n"
    "## Test Strategy\nB.\n"
)


def _task(tmp_path: Path, stage: str) -> Path:
    task_dir = tmp_path / ".dynos" / "task-20261028-001"
    task_dir.mkdir(parents=True)
    (task_dir / "manifest.json").write_text(json.dumps({
        "task_id": task_dir.name,
        "created_at": "2026-10-28T00:00:00Z",
        "raw_input": "x",
        "stage": stage,
        "classification": {"type": "feature", "domains": ["backend"], "risk_level": "medium", "notes": "n"},
    }))
    (task_dir / "plan.md").write_text(PLAN)
    return task_dir


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "ctl.py"), *args],
        cwd=ROOT, text=True, capture_output=True, check=False,
        env={**os.environ, "DYNOS_GAP_CACHE": "0"},
    )


def test_replan_at_plan_audit_returns_the_task_to_planning(tmp_path: Path) -> None:
    task_dir = _task(tmp_path, "PLAN_AUDIT")
    result = _run("run-plan-audit", str(task_dir))
    assert result.returncode == 1, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "replan_required"
    assert payload["transitioned_to"] == "PLANNING"
    assert payload["next_action"] == "repair_plan_then_reapprove"
    assert json.loads((task_dir / "manifest.json").read_text())["stage"] == "PLANNING"


def test_replan_outside_plan_audit_leaves_the_stage_alone(tmp_path: Path) -> None:
    task_dir = _task(tmp_path, "PLANNING")
    result = _run("run-plan-audit", str(task_dir))
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "replan_required"
    assert payload["transitioned_to"] is None
    assert json.loads((task_dir / "manifest.json").read_text())["stage"] == "PLANNING"
