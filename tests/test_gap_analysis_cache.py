"""Regression tests for plan_gap_analysis result caching.

Background: gap analysis was the heaviest deterministic check in the
foundry — three per-task call sites (planning, plan-audit, execute
preflight) each triggered an unbounded rglob over up to 2000 source
files. Caching by (plan.md content + repo top-level dir mtimes) keeps
the correctness bar identical while collapsing N redundant scans into
one.
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


def _setup(tmp_path: Path, plan_content: str = "# Plan\n## Technical Approach\nx\n") -> tuple[Path, Path]:
    """Return (repo_root, task_dir)."""
    task_dir = tmp_path / ".dynos" / "task-001"
    task_dir.mkdir(parents=True)
    (task_dir / "plan.md").write_text(plan_content)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def f(): pass\n")
    return tmp_path, task_dir


class TestGapCacheBehavior:
    def test_cache_hit_skips_underlying_scan(self, tmp_path: Path):
        root, task_dir = _setup(tmp_path)
        from plan_gap_analysis import run_gap_analysis

        # First call: scans the repo and writes the cache.
        with mock.patch("plan_gap_analysis.analyze_api_contracts") as mock_api, \
             mock.patch("plan_gap_analysis.analyze_data_model") as mock_dm:
            mock_api.return_value = {"skipped": True, "reason": "test"}
            mock_dm.return_value = {"skipped": True, "reason": "test"}
            result1 = run_gap_analysis(root, task_dir)
        assert mock_api.call_count == 1
        assert mock_dm.call_count == 1

        # Second call with same plan + repo: cache hit, no rescan.
        with mock.patch("plan_gap_analysis.analyze_api_contracts") as mock_api2, \
             mock.patch("plan_gap_analysis.analyze_data_model") as mock_dm2:
            result2 = run_gap_analysis(root, task_dir)
        assert mock_api2.call_count == 0, "cached: api scan should not run again"
        assert mock_dm2.call_count == 0, "cached: data model scan should not run again"
        assert result2 == result1, "cache must return the same report"

    def test_plan_change_invalidates_cache(self, tmp_path: Path):
        root, task_dir = _setup(tmp_path, plan_content="# Plan\n## Foo\nv1\n")
        from plan_gap_analysis import run_gap_analysis

        with mock.patch("plan_gap_analysis.analyze_api_contracts") as mock_api, \
             mock.patch("plan_gap_analysis.analyze_data_model") as mock_dm:
            mock_api.return_value = {"skipped": True}
            mock_dm.return_value = {"skipped": True}
            run_gap_analysis(root, task_dir)
            # Mutate plan
            (task_dir / "plan.md").write_text("# Plan\n## Bar\nv2\n")
            run_gap_analysis(root, task_dir)
        assert mock_api.call_count == 2, "plan content change must invalidate cache"

    def test_repo_dir_mtime_change_invalidates_cache(self, tmp_path: Path):
        root, task_dir = _setup(tmp_path)
        from plan_gap_analysis import run_gap_analysis

        with mock.patch("plan_gap_analysis.analyze_api_contracts") as mock_api, \
             mock.patch("plan_gap_analysis.analyze_data_model") as mock_dm:
            mock_api.return_value = {"skipped": True}
            mock_dm.return_value = {"skipped": True}
            run_gap_analysis(root, task_dir)
            # Touch the src/ dir to change its mtime
            time.sleep(0.01)
            (root / "src" / "newfile.py").write_text("y\n")
            new_mtime = time.time() + 5
            os.utime(root / "src", (new_mtime, new_mtime))
            run_gap_analysis(root, task_dir)
        assert mock_api.call_count == 2, "repo structure change must invalidate cache"

    def test_cache_disabled_via_env(self, tmp_path: Path, monkeypatch):
        root, task_dir = _setup(tmp_path)
        monkeypatch.setenv("DYNOS_GAP_CACHE", "0")
        from plan_gap_analysis import run_gap_analysis

        with mock.patch("plan_gap_analysis.analyze_api_contracts") as mock_api, \
             mock.patch("plan_gap_analysis.analyze_data_model") as mock_dm:
            mock_api.return_value = {"skipped": True}
            mock_dm.return_value = {"skipped": True}
            run_gap_analysis(root, task_dir)
            run_gap_analysis(root, task_dir)
        assert mock_api.call_count == 2, "cache disabled: every call must scan"

    def test_cache_file_corruption_falls_back_to_rescan(self, tmp_path: Path):
        root, task_dir = _setup(tmp_path)
        from plan_gap_analysis import run_gap_analysis, _cache_path

        # Pre-write an invalid cache file
        _cache_path(task_dir).write_text("{not json")

        with mock.patch("plan_gap_analysis.analyze_api_contracts") as mock_api, \
             mock.patch("plan_gap_analysis.analyze_data_model") as mock_dm:
            mock_api.return_value = {"skipped": True}
            mock_dm.return_value = {"skipped": True}
            run_gap_analysis(root, task_dir)
        assert mock_api.call_count == 1, "corrupt cache must fall back, not crash"

    def test_missing_plan_returns_error_does_not_cache(self, tmp_path: Path):
        task_dir = tmp_path / ".dynos" / "task-001"
        task_dir.mkdir(parents=True)
        # NO plan.md
        from plan_gap_analysis import run_gap_analysis, _cache_path
        result = run_gap_analysis(tmp_path, task_dir)
        assert "error" in result
        assert not _cache_path(task_dir).exists(), \
            "should not write a cache entry when plan is missing"
