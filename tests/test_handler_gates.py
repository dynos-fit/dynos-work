"""Regression tests for payload-aware handler gates.

Two specific gates added:
- run_dashboard: skip when dashboard-data.json was modified within
  _DASHBOARD_DEBOUNCE_SECONDS (debouncing bursty completions).
- run_improve: skip when the task-retrospective.json shows zero
  findings AND zero repair cycles (nothing to learn from).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))


class TestDashboardDebounce:
    def test_recent_dashboard_skips_subprocess(self, tmp_path: Path):
        """If dashboard-data.json was written <30s ago, run_dashboard
        must short-circuit and not invoke the subprocess."""
        (tmp_path / ".dynos").mkdir()
        data_path = tmp_path / ".dynos" / "dashboard-data.json"
        data_path.write_text("{}")
        # mtime defaults to now, well within 30s

        from eventbus import run_dashboard
        with mock.patch("eventbus._run") as mock_run:
            assert run_dashboard(tmp_path, {}) is True
        assert not mock_run.called, "recent dashboard must skip subprocess"

    def test_stale_dashboard_runs_subprocess(self, tmp_path: Path):
        """If dashboard-data.json is older than the debounce window,
        regenerate normally."""
        (tmp_path / ".dynos").mkdir()
        data_path = tmp_path / ".dynos" / "dashboard-data.json"
        data_path.write_text("{}")
        # Set mtime to 60s ago (past the 30s window)
        old = time.time() - 60
        os.utime(data_path, (old, old))

        from eventbus import run_dashboard
        with mock.patch("eventbus._run") as mock_run:
            mock_run.return_value = True
            run_dashboard(tmp_path, {})
        assert mock_run.called, "stale dashboard must trigger regeneration"

    def test_missing_dashboard_runs_subprocess(self, tmp_path: Path):
        """First run (no dashboard yet) must invoke the subprocess."""
        (tmp_path / ".dynos").mkdir()
        from eventbus import run_dashboard
        with mock.patch("eventbus._run") as mock_run:
            mock_run.return_value = True
            run_dashboard(tmp_path, {})
        assert mock_run.called, "missing dashboard must trigger first generation"


class TestImproveSkipOnCleanTask:
    def _make_task_with_retro(self, tmp_path: Path, findings_total: int, repair_cycles: int) -> Path:
        task_dir = tmp_path / ".dynos" / "task-001"
        task_dir.mkdir(parents=True)
        retro = {
            "task_id": "task-001",
            "task_outcome": "DONE",
            "findings_by_auditor": (
                {"security-auditor": findings_total} if findings_total > 0 else {}
            ),
            "repair_cycle_count": repair_cycles,
        }
        (task_dir / "task-retrospective.json").write_text(json.dumps(retro))
        return task_dir

    def test_skip_when_zero_findings_zero_repairs(self, tmp_path: Path):
        task_dir = self._make_task_with_retro(tmp_path, findings_total=0, repair_cycles=0)
        from eventbus import run_improve
        with mock.patch("eventbus._run") as mock_run:
            assert run_improve(tmp_path, {"task_dir": str(task_dir)}) is True
        assert not mock_run.called, "clean task offers no learning signal; skip"

    def test_runs_when_findings_exist(self, tmp_path: Path):
        task_dir = self._make_task_with_retro(tmp_path, findings_total=2, repair_cycles=0)
        from eventbus import run_improve
        with mock.patch("eventbus._run") as mock_run:
            mock_run.return_value = True
            run_improve(tmp_path, {"task_dir": str(task_dir)})
        assert mock_run.called, "findings present — improve should run"

    def test_runs_when_repair_cycles_nonzero(self, tmp_path: Path):
        task_dir = self._make_task_with_retro(tmp_path, findings_total=0, repair_cycles=1)
        from eventbus import run_improve
        with mock.patch("eventbus._run") as mock_run:
            mock_run.return_value = True
            run_improve(tmp_path, {"task_dir": str(task_dir)})
        assert mock_run.called, "repair cycles present — improve should run"

    def test_runs_when_payload_missing_task_dir(self, tmp_path: Path):
        """Conservative fall-through: no payload info → run anyway."""
        from eventbus import run_improve
        with mock.patch("eventbus._run") as mock_run:
            mock_run.return_value = True
            run_improve(tmp_path, {})
        assert mock_run.called, "no payload info — fall through to handler"

    def test_runs_when_retro_missing(self, tmp_path: Path):
        """If the task dir exists but retrospective is missing, fall through."""
        task_dir = tmp_path / ".dynos" / "task-001"
        task_dir.mkdir(parents=True)
        from eventbus import run_improve
        with mock.patch("eventbus._run") as mock_run:
            mock_run.return_value = True
            run_improve(tmp_path, {"task_dir": str(task_dir)})
        assert mock_run.called, "no retro — fall through, don't silently skip"
