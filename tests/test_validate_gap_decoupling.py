"""Regression tests for the validate_task_artifacts gap-analysis decoupling.

Background: validate_task_artifacts() unconditionally fired run_gap_analysis()
on every call when plan.md was present. With three call sites per task
(planning, plan-audit, execute preflight) and an unbounded repo walk inside
gap analysis (rglob up to 2000 files), this was the dominant source of CPU
in the deterministic validators.

The fix adds a `run_gap` parameter (default True for backwards compatibility)
that callers can flip to False when they've already validated the same plan
and only need the cheap structural checks.

The CLI accepts a `--no-gap` flag for the same purpose.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))


def _setup_minimal_task(tmp_path: Path) -> Path:
    """Create a minimal task with manifest, spec, plan, and graph."""
    task_dir = tmp_path / ".dynos" / "task-20260418-001"
    task_dir.mkdir(parents=True)
    (task_dir / "manifest.json").write_text(json.dumps({
        "task_id": "task-20260418-001",
        "stage": "PLANNING",
        "created_at": "2026-04-18T00:00:00Z",
        "raw_input": "test",
        "classification": {
            "type": "refactor",
            "domains": ["backend"],
            "risk_level": "low",
        },
    }))
    (task_dir / "spec.md").write_text(
        "# Spec\n\n## Task Summary\nx\n## User Context\nx\n"
        "## Acceptance Criteria\n1. crit one\n## Implicit Requirements Surfaced\nx\n"
        "## Out of Scope\nx\n## Assumptions\nx\n## Risk Notes\nx\n"
    )
    (task_dir / "plan.md").write_text(
        "# Plan\n\n## Technical Approach\nx\n## Reference Code\nx\n"
        "## Components / Modules\nx\n## Data Flow\nx\n"
        "## Error Handling Strategy\nx\n## Test Strategy\nx\n"
        "## Dependency Graph\nx\n## Open Questions\nx\n"
        "## API Contracts\nN/A — no API surface added or modified by this task.\n"
    )
    (task_dir / "execution-graph.json").write_text(json.dumps({
        "task_id": "task-20260418-001",
        "segments": [{
            "id": "seg-1",
            "executor": "refactor-executor",
            "description": "test",
            "files_expected": ["foo.py"],
            "depends_on": [],
            "criteria_ids": [1],
        }],
    }))
    return task_dir


class TestRunGapParameter:
    def test_run_gap_true_invokes_gap_analysis(self, tmp_path: Path):
        task_dir = _setup_minimal_task(tmp_path)
        from lib_validate import validate_task_artifacts
        with mock.patch("plan_gap_analysis.run_gap_analysis") as mock_gap:
            mock_gap.return_value = {"api_contracts": {"skipped": True}, "data_model": {"skipped": True}}
            errors = validate_task_artifacts(task_dir, run_gap=True)
        assert mock_gap.called, "gap analysis must run when run_gap=True"
        assert errors == []

    def test_run_gap_false_skips_gap_analysis(self, tmp_path: Path):
        """The whole point of the decoupling: run_gap=False must skip the
        ~2000-file repo walk inside gap analysis."""
        task_dir = _setup_minimal_task(tmp_path)
        from lib_validate import validate_task_artifacts
        with mock.patch("plan_gap_analysis.run_gap_analysis") as mock_gap:
            errors = validate_task_artifacts(task_dir, run_gap=False)
        assert not mock_gap.called, "gap analysis must NOT run when run_gap=False"
        assert errors == [], "structural checks should still pass"

    def test_default_preserves_backwards_compat(self, tmp_path: Path):
        """Existing callers that pass no run_gap argument must continue to
        invoke gap analysis (default True)."""
        task_dir = _setup_minimal_task(tmp_path)
        from lib_validate import validate_task_artifacts
        with mock.patch("plan_gap_analysis.run_gap_analysis") as mock_gap:
            mock_gap.return_value = {"api_contracts": {"skipped": True}, "data_model": {"skipped": True}}
            validate_task_artifacts(task_dir)
        assert mock_gap.called, "default behavior must remain run_gap=True"

    def test_structural_errors_still_caught_with_no_gap(self, tmp_path: Path):
        """Skipping gap analysis must NOT skip the structural checks
        (missing headings, criteria coverage, etc.)."""
        task_dir = _setup_minimal_task(tmp_path)
        # Break the spec
        (task_dir / "spec.md").write_text("# Spec\n\nNo headings at all.\n")

        from lib_validate import validate_task_artifacts
        with mock.patch("plan_gap_analysis.run_gap_analysis"):
            errors = validate_task_artifacts(task_dir, run_gap=False)
        assert any("missing heading" in e for e in errors), \
            f"structural errors must still surface; got {errors}"


class TestCliNoGapFlag:
    def test_cli_passes_run_gap_false_when_flag_present(self, tmp_path: Path):
        task_dir = _setup_minimal_task(tmp_path)
        with mock.patch("validate_task_artifacts.validate_task_artifacts") as mock_v, \
             mock.patch("sys.argv", ["validate_task_artifacts.py", str(task_dir), "--no-gap"]):
            mock_v.return_value = []
            from validate_task_artifacts import main
            main()
        assert mock_v.call_args.kwargs.get("run_gap") is False

    def test_cli_default_runs_gap(self, tmp_path: Path):
        task_dir = _setup_minimal_task(tmp_path)
        with mock.patch("validate_task_artifacts.validate_task_artifacts") as mock_v, \
             mock.patch("sys.argv", ["validate_task_artifacts.py", str(task_dir)]):
            mock_v.return_value = []
            from validate_task_artifacts import main
            main()
        assert mock_v.call_args.kwargs.get("run_gap") is True
