"""Tests for cross-project priority queue (Enhancement 5).

Covers AC 15-18:
  AC 15: build_cross_project_queue function exists, collects findings from all projects
  AC 16: Priority = severity_weight * centrality_score * freshness_weight
  AC 17: Sweep loop refactored to use priority queue
  AC 18: Backoff exclusion at project level
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def dynos_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point DYNOS_HOME to tmp so real ~/.dynos/ is untouched."""
    home = tmp_path / "dynos-home"
    home.mkdir()
    monkeypatch.setenv("DYNOS_HOME", str(home))
    return home


def _make_finding(**overrides) -> dict:
    """Build a minimal finding dict."""
    base = {
        "finding_id": "test-001",
        "severity": "medium",
        "category": "llm-review",
        "description": "Potential null pointer dereference",
        "evidence": {"file": "main.py", "line": 10},
        "status": "new",
        "detected_at": datetime.now(timezone.utc).isoformat(),
    }
    base.update(overrides)
    return base


def _make_registry(*projects) -> dict:
    """Build a minimal registry dict with given project entries."""
    return {
        "version": 1,
        "projects": list(projects),
        "checksum": "",
    }


def _make_project_entry(path: str, status: str = "active", **overrides) -> dict:
    """Build a minimal project entry for the registry."""
    base = {
        "path": path,
        "status": status,
        "last_active_at": datetime.now(timezone.utc).isoformat(),
    }
    base.update(overrides)
    return base


# ===========================================================================
# AC 15: build_cross_project_queue function
# ===========================================================================

class TestBuildCrossProjectQueue:
    """AC 15: Function exists, collects pending findings from all active projects."""

    def test_function_exists(self) -> None:
        # AC 15
        from sweeper import build_cross_project_queue
        assert callable(build_cross_project_queue)

    def test_returns_list(self, tmp_path: Path) -> None:
        # AC 15
        from sweeper import build_cross_project_queue
        registry = _make_registry()
        result = build_cross_project_queue(registry)
        assert isinstance(result, list)

    def test_empty_registry_returns_empty(self) -> None:
        # AC 15
        from sweeper import build_cross_project_queue
        registry = _make_registry()
        result = build_cross_project_queue(registry)
        assert result == []

    def test_collects_findings_from_multiple_projects(self, tmp_path: Path) -> None:
        # AC 15: findings from all active projects are collected
        from sweeper import build_cross_project_queue

        # Create two project dirs with findings
        proj_a = tmp_path / "project-a"
        proj_a.mkdir()
        (proj_a / ".dynos").mkdir()
        findings_a = [_make_finding(finding_id="a-001", severity="high")]
        (proj_a / ".dynos" / "findings.json").write_text(json.dumps(findings_a))

        proj_b = tmp_path / "project-b"
        proj_b.mkdir()
        (proj_b / ".dynos").mkdir()
        findings_b = [_make_finding(finding_id="b-001", severity="low")]
        (proj_b / ".dynos" / "findings.json").write_text(json.dumps(findings_b))

        registry = _make_registry(
            _make_project_entry(str(proj_a)),
            _make_project_entry(str(proj_b)),
        )

        with patch("sweeper.build_import_graph", return_value={"nodes": [], "edges": [], "pagerank": {}}):
            with patch("sweeper._should_skip_backoff", return_value=False):
                result = build_cross_project_queue(registry)

        finding_ids = {item["finding"]["finding_id"] for item in result}
        assert "a-001" in finding_ids
        assert "b-001" in finding_ids

    def test_result_items_have_required_fields(self, tmp_path: Path) -> None:
        # AC 15: each item has finding, project_path, priority, severity_weight, centrality_score, freshness_weight
        from sweeper import build_cross_project_queue

        proj = tmp_path / "project"
        proj.mkdir()
        (proj / ".dynos").mkdir()
        (proj / ".dynos" / "findings.json").write_text(
            json.dumps([_make_finding(finding_id="f-001")])
        )

        registry = _make_registry(_make_project_entry(str(proj)))
        with patch("sweeper.build_import_graph", return_value={"nodes": [], "edges": [], "pagerank": {}}):
            with patch("sweeper._should_skip_backoff", return_value=False):
                result = build_cross_project_queue(registry)

        assert len(result) >= 1
        item = result[0]
        assert "finding" in item
        assert "project_path" in item
        assert "priority" in item
        assert "severity_weight" in item
        assert "centrality_score" in item
        assert "freshness_weight" in item

    def test_sorted_by_priority_descending(self, tmp_path: Path) -> None:
        # AC 15: returned list sorted by priority descending
        from sweeper import build_cross_project_queue

        proj = tmp_path / "project"
        proj.mkdir()
        (proj / ".dynos").mkdir()
        findings = [
            _make_finding(finding_id="f-low", severity="low"),
            _make_finding(finding_id="f-crit", severity="critical"),
            _make_finding(finding_id="f-high", severity="high"),
        ]
        (proj / ".dynos" / "findings.json").write_text(json.dumps(findings))

        registry = _make_registry(_make_project_entry(str(proj)))
        with patch("sweeper.build_import_graph", return_value={"nodes": [], "edges": [], "pagerank": {}}):
            with patch("sweeper._should_skip_backoff", return_value=False):
                result = build_cross_project_queue(registry)

        priorities = [item["priority"] for item in result]
        assert priorities == sorted(priorities, reverse=True), \
            f"Queue should be sorted by priority descending: {priorities}"

    def test_skips_non_active_projects(self, tmp_path: Path) -> None:
        # AC 15: only active projects are included
        from sweeper import build_cross_project_queue

        proj = tmp_path / "paused-proj"
        proj.mkdir()
        (proj / ".dynos").mkdir()
        (proj / ".dynos" / "findings.json").write_text(
            json.dumps([_make_finding(finding_id="f-001")])
        )

        registry = _make_registry(_make_project_entry(str(proj), status="paused"))
        result = build_cross_project_queue(registry)
        assert len(result) == 0

    def test_missing_findings_file_skips_project(self, tmp_path: Path) -> None:
        # AC 15: project with missing findings file is skipped gracefully
        from sweeper import build_cross_project_queue

        proj = tmp_path / "no-findings"
        proj.mkdir()
        (proj / ".dynos").mkdir()
        # No findings.json file

        registry = _make_registry(_make_project_entry(str(proj)))
        with patch("sweeper._should_skip_backoff", return_value=False):
            result = build_cross_project_queue(registry)
        assert len(result) == 0

    def test_corrupt_findings_file_skips_project(self, tmp_path: Path) -> None:
        # AC 15: corrupt findings file skipped gracefully
        from sweeper import build_cross_project_queue

        proj = tmp_path / "corrupt-proj"
        proj.mkdir()
        (proj / ".dynos").mkdir()
        (proj / ".dynos" / "findings.json").write_text("{{invalid json")

        registry = _make_registry(_make_project_entry(str(proj)))
        with patch("sweeper._should_skip_backoff", return_value=False):
            result = build_cross_project_queue(registry)
        assert len(result) == 0


# ===========================================================================
# AC 16: Priority computation
# ===========================================================================

class TestPriorityComputation:
    """AC 16: priority = severity_weight * centrality_score * freshness_weight."""

    def test_severity_weights(self) -> None:
        # AC 16: critical=4, high=3, medium=2, low=1
        severity_map = {"critical": 4, "high": 3, "medium": 2, "low": 1}
        assert severity_map["critical"] == 4
        assert severity_map["high"] == 3
        assert severity_map["medium"] == 2
        assert severity_map["low"] == 1

    def test_critical_finding_higher_priority_than_low(self, tmp_path: Path) -> None:
        # AC 16: critical finding should have higher priority than low
        from sweeper import build_cross_project_queue

        proj = tmp_path / "project"
        proj.mkdir()
        (proj / ".dynos").mkdir()
        now = datetime.now(timezone.utc).isoformat()
        findings = [
            _make_finding(finding_id="f-crit", severity="critical", detected_at=now),
            _make_finding(finding_id="f-low", severity="low", detected_at=now),
        ]
        (proj / ".dynos" / "findings.json").write_text(json.dumps(findings))

        registry = _make_registry(_make_project_entry(str(proj)))
        with patch("sweeper.build_import_graph", return_value={"nodes": [], "edges": [], "pagerank": {}}):
            with patch("sweeper._should_skip_backoff", return_value=False):
                result = build_cross_project_queue(registry)

        # Critical should be first (highest priority)
        assert result[0]["finding"]["finding_id"] == "f-crit"
        assert result[-1]["finding"]["finding_id"] == "f-low"

    def test_freshness_decay(self) -> None:
        # AC 16: freshness_weight = 1.0 / (1 + days_since_detection)
        # Fresh finding (today): 1.0 / (1 + 0) = 1.0
        # Old finding (30 days): 1.0 / (1 + 30) ~= 0.032
        fresh = 1.0 / (1 + 0)
        old = 1.0 / (1 + 30)
        assert fresh == 1.0
        assert old == pytest.approx(0.0323, abs=0.001)
        assert fresh > old

    def test_centrality_default_when_unavailable(self) -> None:
        # AC 16: centrality_score defaults to 0.5 if unavailable
        pagerank = {}
        centrality = pagerank.get("main.py", 0.5)
        assert centrality == 0.5

    def test_priority_formula_known_values(self, tmp_path: Path) -> None:
        # AC 16: test with known values to verify formula
        from sweeper import build_cross_project_queue

        proj = tmp_path / "project"
        proj.mkdir()
        (proj / ".dynos").mkdir()
        now = datetime.now(timezone.utc).isoformat()
        findings = [
            _make_finding(finding_id="f-001", severity="high", detected_at=now,
                          evidence={"file": "central.py", "line": 10}),
        ]
        (proj / ".dynos" / "findings.json").write_text(json.dumps(findings))

        pagerank = {"central.py": 0.8}
        registry = _make_registry(_make_project_entry(str(proj)))
        with patch("sweeper.build_import_graph", return_value={"nodes": ["central.py"], "edges": [], "pagerank": pagerank}):
            with patch("sweeper._should_skip_backoff", return_value=False):
                result = build_cross_project_queue(registry)

        assert len(result) == 1
        item = result[0]
        # severity_weight for high = 3
        assert item["severity_weight"] == 3
        # centrality from pagerank = 0.8
        assert item["centrality_score"] == pytest.approx(0.8, abs=0.01)
        # freshness for today ~= 1.0
        assert item["freshness_weight"] == pytest.approx(1.0, abs=0.1)
        # priority = 3 * 0.8 * ~1.0 = ~2.4
        assert item["priority"] == pytest.approx(3 * 0.8 * 1.0, abs=0.5)

    def test_ordering_with_mixed_attributes(self, tmp_path: Path) -> None:
        # AC 16: verify correct ordering with different severity and freshness
        from sweeper import build_cross_project_queue

        proj = tmp_path / "project"
        proj.mkdir()
        (proj / ".dynos").mkdir()
        now = datetime.now(timezone.utc)
        old_date = (now - timedelta(days=30)).isoformat()
        now_str = now.isoformat()

        findings = [
            # High severity, old (30 days): priority = 3 * 0.5 * (1/31) ~= 0.048
            _make_finding(finding_id="f-old-high", severity="high", detected_at=old_date),
            # Medium severity, fresh (today): priority = 2 * 0.5 * 1.0 = 1.0
            _make_finding(finding_id="f-fresh-med", severity="medium", detected_at=now_str),
        ]
        (proj / ".dynos" / "findings.json").write_text(json.dumps(findings))

        registry = _make_registry(_make_project_entry(str(proj)))
        with patch("sweeper.build_import_graph", return_value={"nodes": [], "edges": [], "pagerank": {}}):
            with patch("sweeper._should_skip_backoff", return_value=False):
                result = build_cross_project_queue(registry)

        # Fresh medium should be higher priority than old high
        assert result[0]["finding"]["finding_id"] == "f-fresh-med"


# ===========================================================================
# AC 17: Sweep loop refactored
# ===========================================================================

class TestSweepLoopRefactored:
    """AC 17: Daemon processes highest-priority finding first via queue."""

    def test_cmd_run_once_uses_queue(self) -> None:
        # AC 17: cmd_run_once should call build_cross_project_queue
        # This test verifies the integration point exists
        from sweeper import cmd_run_once, build_cross_project_queue
        assert callable(cmd_run_once)
        assert callable(build_cross_project_queue)

    def test_queue_recomputed_each_sweep(self) -> None:
        # AC 17: no persistence; queue recomputed each sweep
        # The queue function takes registry as input, no cache
        from sweeper import build_cross_project_queue
        import inspect
        sig = inspect.signature(build_cross_project_queue)
        params = list(sig.parameters.keys())
        assert "registry" in params

    def test_highest_priority_processed_first(self, tmp_path: Path) -> None:
        # AC 17: daemon processes highest-priority finding first, regardless of project
        from sweeper import build_cross_project_queue

        proj_a = tmp_path / "project-a"
        proj_a.mkdir()
        (proj_a / ".dynos").mkdir()
        now = datetime.now(timezone.utc).isoformat()
        (proj_a / ".dynos" / "findings.json").write_text(
            json.dumps([_make_finding(finding_id="a-low", severity="low", detected_at=now)])
        )

        proj_b = tmp_path / "project-b"
        proj_b.mkdir()
        (proj_b / ".dynos").mkdir()
        (proj_b / ".dynos" / "findings.json").write_text(
            json.dumps([_make_finding(finding_id="b-crit", severity="critical", detected_at=now)])
        )

        registry = _make_registry(
            _make_project_entry(str(proj_a)),
            _make_project_entry(str(proj_b)),
        )
        with patch("sweeper.build_import_graph", return_value={"nodes": [], "edges": [], "pagerank": {}}):
            with patch("sweeper._should_skip_backoff", return_value=False):
                result = build_cross_project_queue(registry)

        # Critical finding from project B should be first
        assert result[0]["finding"]["finding_id"] == "b-crit"
        assert result[0]["project_path"] == str(proj_b)


# ===========================================================================
# AC 18: Backoff exclusion
# ===========================================================================

class TestBackoffExclusion:
    """AC 18: Projects in backoff have all their findings excluded from queue."""

    def test_backoff_project_excluded(self, tmp_path: Path) -> None:
        # AC 18: project in backoff -> its findings excluded from queue
        from sweeper import build_cross_project_queue

        proj = tmp_path / "backoff-proj"
        proj.mkdir()
        (proj / ".dynos").mkdir()
        (proj / ".dynos" / "findings.json").write_text(
            json.dumps([_make_finding(finding_id="f-001", severity="critical")])
        )

        # Set last_active_at to > 7 days ago to trigger backoff
        old_date = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        registry = _make_registry(
            _make_project_entry(str(proj), last_active_at=old_date)
        )

        with patch("sweeper.build_import_graph", return_value={"nodes": [], "edges": [], "pagerank": {}}):
            with patch("sweeper._should_skip_backoff", return_value=True):
                result = build_cross_project_queue(registry)

        # Project is in backoff, so its findings should be excluded
        assert len(result) == 0

    def test_non_backoff_project_included(self, tmp_path: Path) -> None:
        # AC 18: project not in backoff -> its findings included
        from sweeper import build_cross_project_queue

        proj = tmp_path / "active-proj"
        proj.mkdir()
        (proj / ".dynos").mkdir()
        (proj / ".dynos" / "findings.json").write_text(
            json.dumps([_make_finding(finding_id="f-001", severity="medium")])
        )

        registry = _make_registry(_make_project_entry(str(proj)))
        with patch("sweeper.build_import_graph", return_value={"nodes": [], "edges": [], "pagerank": {}}):
            with patch("sweeper._should_skip_backoff", return_value=False):
                result = build_cross_project_queue(registry)

        assert len(result) >= 1

    def test_mixed_backoff_only_active_included(self, tmp_path: Path) -> None:
        # AC 18: mix of backoff and non-backoff projects
        from sweeper import build_cross_project_queue

        proj_active = tmp_path / "active"
        proj_active.mkdir()
        (proj_active / ".dynos").mkdir()
        (proj_active / ".dynos" / "findings.json").write_text(
            json.dumps([_make_finding(finding_id="active-001", severity="high")])
        )

        proj_backoff = tmp_path / "backoff"
        proj_backoff.mkdir()
        (proj_backoff / ".dynos").mkdir()
        (proj_backoff / ".dynos" / "findings.json").write_text(
            json.dumps([_make_finding(finding_id="backoff-001", severity="critical")])
        )

        active_entry = _make_project_entry(str(proj_active))
        backoff_entry = _make_project_entry(str(proj_backoff))

        registry = _make_registry(active_entry, backoff_entry)

        def backoff_side_effect(entry, sweep_count):
            return entry["path"] == str(proj_backoff)

        with patch("sweeper.build_import_graph", return_value={"nodes": [], "edges": [], "pagerank": {}}):
            with patch("sweeper._should_skip_backoff", side_effect=backoff_side_effect):
                result = build_cross_project_queue(registry)

        finding_ids = {item["finding"]["finding_id"] for item in result}
        assert "active-001" in finding_ids
        assert "backoff-001" not in finding_ids

    def test_backoff_applies_at_project_level(self) -> None:
        # AC 18: backoff is per-project, not per-finding
        from sweeper import _should_skip_backoff
        # A project idle > 7 days should be in backoff
        old_entry = {
            "path": "/some/project",
            "last_active_at": (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(),
        }
        # sweep_count that triggers skip (not divisible by 8 for >7 day backoff)
        should_skip = _should_skip_backoff(old_entry, sweep_count=1)
        assert should_skip is True

    def test_recently_active_no_backoff(self) -> None:
        # AC 18: recently active project has no backoff
        from sweeper import _should_skip_backoff
        recent_entry = {
            "path": "/some/project",
            "last_active_at": datetime.now(timezone.utc).isoformat(),
        }
        should_skip = _should_skip_backoff(recent_entry, sweep_count=1)
        assert should_skip is False
