"""Tests for PR #16 — DORA metrics in retrospective.

Validates:
  - compute_reward includes DORA fields (lead_time_seconds, change_failure, recovery_time_seconds)
  - Old retrospectives without DORA fields still parse
  - New DORA fields validate correctly
  - stats-dora CLI produces valid output
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
ROOT = Path(__file__).resolve().parent.parent


def _make_task_dir(tmp_path: Path, created_at: str, completed_at: str | None = None,
                   stage: str = "DONE", log_content: str = "") -> Path:
    """Create a minimal task directory for compute_reward."""
    task_dir = tmp_path / ".dynos" / "task-test-001"
    task_dir.mkdir(parents=True)
    manifest = {
        "task_id": "task-test-001",
        "created_at": created_at,
        "completed_at": completed_at,
        "title": "Test",
        "raw_input": "Test task",
        "stage": stage,
        "classification": {"type": "feature", "domains": ["backend"], "risk_level": "medium", "notes": ""},
    }
    (task_dir / "manifest.json").write_text(json.dumps(manifest))
    (task_dir / "spec.md").write_text("# Normalized Spec\n## Task Summary\nT\n## Acceptance Criteria\n1. Done\n")
    (task_dir / "execution-log.md").write_text(log_content)
    return task_dir


# ---------------------------------------------------------------------------
# compute_reward DORA fields
# ---------------------------------------------------------------------------

class TestComputeRewardDora:
    def test_lead_time_computed(self, tmp_path: Path):
        from lib_validate import compute_reward
        t0 = "2026-04-15T10:00:00Z"
        t1 = "2026-04-15T10:30:00Z"
        task_dir = _make_task_dir(tmp_path, created_at=t0, completed_at=t1)
        result = compute_reward(task_dir)
        assert result["lead_time_seconds"] == 1800  # 30 minutes

    def test_lead_time_none_without_completed_at(self, tmp_path: Path):
        from lib_validate import compute_reward
        task_dir = _make_task_dir(tmp_path, created_at="2026-04-15T10:00:00Z", completed_at=None)
        result = compute_reward(task_dir)
        assert result["lead_time_seconds"] is None

    def test_change_failure_true_on_repair_failed(self, tmp_path: Path):
        from lib_validate import compute_reward
        task_dir = _make_task_dir(tmp_path, created_at="2026-04-15T10:00:00Z", stage="REPAIR_FAILED")
        result = compute_reward(task_dir)
        assert result["change_failure"] is True

    def test_change_failure_false_on_done(self, tmp_path: Path):
        from lib_validate import compute_reward
        task_dir = _make_task_dir(tmp_path, created_at="2026-04-15T10:00:00Z",
                                  completed_at="2026-04-15T10:30:00Z", stage="DONE")
        result = compute_reward(task_dir)
        assert result["change_failure"] is False

    def test_recovery_time_none_on_success(self, tmp_path: Path):
        from lib_validate import compute_reward
        task_dir = _make_task_dir(tmp_path, created_at="2026-04-15T10:00:00Z",
                                  completed_at="2026-04-15T10:30:00Z", stage="DONE")
        result = compute_reward(task_dir)
        assert result["recovery_time_seconds"] is None

    def test_lead_time_handles_timezone(self, tmp_path: Path):
        from lib_validate import compute_reward
        task_dir = _make_task_dir(tmp_path,
                                  created_at="2026-04-15T10:00:00+00:00",
                                  completed_at="2026-04-15T11:00:00+00:00")
        result = compute_reward(task_dir)
        assert result["lead_time_seconds"] == 3600

    def test_all_dora_fields_present(self, tmp_path: Path):
        from lib_validate import compute_reward
        task_dir = _make_task_dir(tmp_path, created_at="2026-04-15T10:00:00Z",
                                  completed_at="2026-04-15T10:30:00Z")
        result = compute_reward(task_dir)
        assert "lead_time_seconds" in result
        assert "change_failure" in result
        assert "recovery_time_seconds" in result

    def test_existing_fields_preserved(self, tmp_path: Path):
        from lib_validate import compute_reward
        task_dir = _make_task_dir(tmp_path, created_at="2026-04-15T10:00:00Z",
                                  completed_at="2026-04-15T10:30:00Z")
        result = compute_reward(task_dir)
        # Existing fields still present
        assert "quality_score" in result
        assert "cost_score" in result
        assert "efficiency_score" in result
        assert "task_id" in result
        assert "task_type" in result


# ---------------------------------------------------------------------------
# validate_retrospective — backwards compat
# ---------------------------------------------------------------------------

class TestValidateRetrospectiveBackwardsCompat:
    def test_old_retro_without_dora_fields_passes(self, tmp_path: Path):
        from lib_validate import validate_retrospective
        task_dir = tmp_path / ".dynos" / "task-old"
        task_dir.mkdir(parents=True)
        retro = {
            "task_id": "task-old",
            "task_outcome": "DONE",
            "task_type": "feature",
            "task_domains": "backend",
            "task_risk_level": "medium",
            "findings_by_auditor": {},
            "findings_by_category": {},
            "executor_repair_frequency": {},
            "spec_review_iterations": 1,
            "repair_cycle_count": 0,
            "subagent_spawn_count": 5,
            "wasted_spawns": 0,
            "auditor_zero_finding_streaks": {},
            "executor_zero_repair_streak": 0,
            "quality_score": 0.9,
            "cost_score": 0.8,
            "efficiency_score": 1.0,
        }
        (task_dir / "task-retrospective.json").write_text(json.dumps(retro))
        errors = validate_retrospective(task_dir)
        assert not any("lead_time" in e or "change_failure" in e or "recovery" in e for e in errors)

    def test_new_retro_with_dora_fields_passes(self, tmp_path: Path):
        from lib_validate import validate_retrospective
        task_dir = tmp_path / ".dynos" / "task-new"
        task_dir.mkdir(parents=True)
        retro = {
            "task_id": "task-new",
            "task_outcome": "DONE",
            "task_type": "feature",
            "task_domains": "backend",
            "task_risk_level": "medium",
            "findings_by_auditor": {},
            "findings_by_category": {},
            "executor_repair_frequency": {},
            "spec_review_iterations": 1,
            "repair_cycle_count": 0,
            "subagent_spawn_count": 5,
            "wasted_spawns": 0,
            "auditor_zero_finding_streaks": {},
            "executor_zero_repair_streak": 0,
            "quality_score": 0.9,
            "cost_score": 0.8,
            "efficiency_score": 1.0,
            "lead_time_seconds": 1800,
            "change_failure": False,
            "recovery_time_seconds": None,
        }
        (task_dir / "task-retrospective.json").write_text(json.dumps(retro))
        errors = validate_retrospective(task_dir)
        assert not any("lead_time" in e or "change_failure" in e or "recovery" in e for e in errors)

    def test_invalid_lead_time_fails(self, tmp_path: Path):
        from lib_validate import validate_retrospective
        task_dir = tmp_path / ".dynos" / "task-bad"
        task_dir.mkdir(parents=True)
        retro = {
            "task_id": "task-bad", "task_outcome": "DONE", "task_type": "feature",
            "task_domains": "backend", "task_risk_level": "medium",
            "findings_by_auditor": {}, "findings_by_category": {},
            "executor_repair_frequency": {}, "spec_review_iterations": 1,
            "repair_cycle_count": 0, "subagent_spawn_count": 5, "wasted_spawns": 0,
            "auditor_zero_finding_streaks": {}, "executor_zero_repair_streak": 0,
            "lead_time_seconds": -100,
        }
        (task_dir / "task-retrospective.json").write_text(json.dumps(retro))
        errors = validate_retrospective(task_dir)
        assert any("lead_time_seconds" in e for e in errors)

    def test_invalid_change_failure_type_fails(self, tmp_path: Path):
        from lib_validate import validate_retrospective
        task_dir = tmp_path / ".dynos" / "task-bad2"
        task_dir.mkdir(parents=True)
        retro = {
            "task_id": "task-bad2", "task_outcome": "DONE", "task_type": "feature",
            "task_domains": "backend", "task_risk_level": "medium",
            "findings_by_auditor": {}, "findings_by_category": {},
            "executor_repair_frequency": {}, "spec_review_iterations": 1,
            "repair_cycle_count": 0, "subagent_spawn_count": 5, "wasted_spawns": 0,
            "auditor_zero_finding_streaks": {}, "executor_zero_repair_streak": 0,
            "change_failure": "yes",
        }
        (task_dir / "task-retrospective.json").write_text(json.dumps(retro))
        errors = validate_retrospective(task_dir)
        assert any("change_failure" in e for e in errors)


# ---------------------------------------------------------------------------
# CLI: stats-dora
# ---------------------------------------------------------------------------

class TestStatsDoraCli:
    def test_runs_with_no_retros(self, tmp_path: Path):
        result = subprocess.run(
            [sys.executable, str(ROOT / "hooks" / "ctl.py"), "stats-dora", "--root", str(tmp_path)],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        assert "No retrospectives" in result.stdout

    def test_json_output_shape(self, tmp_path: Path):
        # Create a task with retrospective
        task_dir = tmp_path / ".dynos" / "task-20260415-001"
        task_dir.mkdir(parents=True)
        retro = {
            "task_id": "task-20260415-001",
            "task_outcome": "DONE",
            "task_type": "feature",
            "task_domains": "backend",
            "task_risk_level": "medium",
            "lead_time_seconds": 1800,
            "change_failure": False,
            "recovery_time_seconds": None,
            "quality_score": 0.9,
        }
        (task_dir / "task-retrospective.json").write_text(json.dumps(retro))

        result = subprocess.run(
            [sys.executable, str(ROOT / "hooks" / "ctl.py"), "stats-dora", "--root", str(tmp_path), "--json"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "total_tasks" in data
        assert "avg_lead_time_seconds" in data
        assert "change_failure_rate" in data
        assert "avg_recovery_time_seconds" in data
