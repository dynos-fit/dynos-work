#!/usr/bin/env python3
"""Tests for dynoglobal.py -- registry, daemon lifecycle, policy merge,
cross-project aggregation, and isolation."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# Ensure hooks/ is importable
_HOOKS_DIR = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS_DIR not in sys.path:
    sys.path.insert(0, _HOOKS_DIR)


@pytest.fixture(autouse=True)
def dynos_home(tmp_path, monkeypatch):
    """Point DYNOS_HOME to a temporary directory so real ~/.dynos/ is untouched."""
    monkeypatch.setenv("DYNOS_HOME", str(tmp_path))
    # Force module to re-read DYNOS_HOME on each call (it already reads env
    # dynamically via global_home()).
    return tmp_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_project(tmp_path: Path, name: str, retros: list[dict] | None = None) -> Path:
    """Create a mock project directory with optional retrospective data."""
    proj = tmp_path / name
    dynos_dir = proj / ".dynos"
    dynos_dir.mkdir(parents=True)
    if retros:
        for i, retro in enumerate(retros):
            task_dir = dynos_dir / f"task-{i:04d}"
            task_dir.mkdir()
            (task_dir / "task-retrospective.json").write_text(
                json.dumps(retro, indent=2)
            )
    return proj


# ===================================================================
# Registry CRUD tests (AC 38)
# ===================================================================

class TestRegistryCRUD:
    """Tests for register, unregister, list, pause, resume, set-active."""

    def test_register_new_project(self, tmp_path):
        from dynoglobal import register_project, list_projects

        proj = _make_project(tmp_path, "proj-a")
        reg = register_project(proj)
        assert any(e["path"] == str(proj) for e in reg["projects"])
        assert len(list_projects()) == 1

    def test_register_idempotent(self, tmp_path):
        from dynoglobal import register_project, list_projects

        proj = _make_project(tmp_path, "proj-a")
        register_project(proj)
        register_project(proj)
        assert len(list_projects()) == 1

    def test_register_non_directory_raises(self, tmp_path):
        from dynoglobal import register_project

        fake = tmp_path / "no-such-dir"
        with pytest.raises(ValueError, match="not a directory"):
            register_project(fake)

    def test_unregister_removes_entry(self, tmp_path):
        from dynoglobal import register_project, unregister_project, list_projects

        proj = _make_project(tmp_path, "proj-a")
        register_project(proj)
        unregister_project(proj)
        assert len(list_projects()) == 0

    def test_unregister_idempotent(self, tmp_path):
        from dynoglobal import unregister_project

        proj = tmp_path / "never-registered"
        proj.mkdir()
        # Should not raise
        unregister_project(proj)

    def test_list_returns_all_projects(self, tmp_path):
        from dynoglobal import register_project, list_projects

        for name in ("alpha", "beta", "gamma"):
            register_project(_make_project(tmp_path, name))
        projects = list_projects()
        assert len(projects) == 3
        paths = {e["path"] for e in projects}
        for name in ("alpha", "beta", "gamma"):
            assert str(tmp_path / name) in paths

    def test_pause_sets_status(self, tmp_path):
        from dynoglobal import register_project, set_project_status, list_projects

        proj = _make_project(tmp_path, "proj-a")
        register_project(proj)
        set_project_status(proj, "paused")
        entries = list_projects()
        assert entries[0]["status"] == "paused"

    def test_pause_idempotent(self, tmp_path):
        from dynoglobal import register_project, set_project_status

        proj = _make_project(tmp_path, "proj-a")
        register_project(proj)
        set_project_status(proj, "paused")
        set_project_status(proj, "paused")  # no error

    def test_resume_sets_active(self, tmp_path):
        from dynoglobal import register_project, set_project_status, list_projects

        proj = _make_project(tmp_path, "proj-a")
        register_project(proj)
        set_project_status(proj, "paused")
        set_project_status(proj, "active")
        entries = list_projects()
        assert entries[0]["status"] == "active"

    def test_resume_idempotent(self, tmp_path):
        from dynoglobal import register_project, set_project_status

        proj = _make_project(tmp_path, "proj-a")
        register_project(proj)
        set_project_status(proj, "active")
        set_project_status(proj, "active")  # no error

    def test_set_active_updates_last_active_at(self, tmp_path):
        from dynoglobal import register_project, set_project_status, list_projects

        proj = _make_project(tmp_path, "proj-a")
        register_project(proj)
        before = list_projects()[0]["last_active_at"]
        set_project_status(proj, "active")
        after = list_projects()[0]["last_active_at"]
        assert after >= before

    def test_set_status_invalid_raises(self, tmp_path):
        from dynoglobal import register_project, set_project_status

        proj = _make_project(tmp_path, "proj-a")
        register_project(proj)
        with pytest.raises(ValueError, match="invalid status"):
            set_project_status(proj, "deleted")

    def test_set_status_unregistered_raises(self, tmp_path):
        from dynoglobal import set_project_status

        proj = tmp_path / "nonexistent"
        proj.mkdir()
        with pytest.raises(ValueError, match="not registered"):
            set_project_status(proj, "active")

    def test_registry_has_required_entry_fields(self, tmp_path):
        from dynoglobal import register_project, list_projects

        proj = _make_project(tmp_path, "proj-a")
        register_project(proj)
        entry = list_projects()[0]
        assert "path" in entry
        assert "registered_at" in entry
        assert "last_active_at" in entry
        assert "status" in entry

    def test_registry_version_increments(self, tmp_path):
        from dynoglobal import register_project, load_registry

        proj = _make_project(tmp_path, "proj-a")
        register_project(proj)
        v1 = load_registry()["version"]
        proj2 = _make_project(tmp_path, "proj-b")
        register_project(proj2)
        v2 = load_registry()["version"]
        assert v2 > v1

    def test_registry_checksum_computed(self, tmp_path):
        from dynoglobal import register_project, load_registry, _compute_checksum

        proj = _make_project(tmp_path, "proj-a")
        register_project(proj)
        reg = load_registry()
        assert reg["checksum"] == _compute_checksum(reg)


# ===================================================================
# Daemon lifecycle tests (AC 39)
# ===================================================================

class TestDaemonLifecycle:
    """Tests for PID file management and status reporting.

    We test the control logic, not actual long-running processes.
    """

    def test_start_writes_pid_file(self, tmp_path):
        from dynoglobal import daemon_pid_path, ensure_global_dirs

        ensure_global_dirs()
        pid_path = daemon_pid_path()
        assert not pid_path.exists()
        pid_path.write_text(f"{os.getpid()}\n")
        assert pid_path.exists()
        assert int(pid_path.read_text().strip()) == os.getpid()

    def test_stop_removes_pid_file(self, tmp_path):
        from dynoglobal import daemon_pid_path, ensure_global_dirs

        ensure_global_dirs()
        pid_path = daemon_pid_path()
        pid_path.write_text(f"{os.getpid()}\n")
        pid_path.unlink()
        assert not pid_path.exists()

    def test_current_daemon_pid_returns_pid_when_running(self, tmp_path):
        from dynoglobal import daemon_pid_path, current_daemon_pid, ensure_global_dirs

        ensure_global_dirs()
        # Write our own PID (which is alive)
        daemon_pid_path().write_text(f"{os.getpid()}\n")
        assert current_daemon_pid() == os.getpid()

    def test_current_daemon_pid_returns_none_when_no_file(self, tmp_path):
        from dynoglobal import current_daemon_pid, ensure_global_dirs

        ensure_global_dirs()
        assert current_daemon_pid() is None

    def test_current_daemon_pid_returns_none_for_dead_pid(self, tmp_path):
        from dynoglobal import daemon_pid_path, current_daemon_pid, ensure_global_dirs

        ensure_global_dirs()
        # PID 99999999 is almost certainly not running
        daemon_pid_path().write_text("99999999\n")
        assert current_daemon_pid() is None

    def test_is_pid_running_true_for_self(self):
        from dynoglobal import is_pid_running

        assert is_pid_running(os.getpid()) is True

    def test_is_pid_running_false_for_bad_pid(self):
        from dynoglobal import is_pid_running

        assert is_pid_running(99999999) is False

    def test_daemon_stop_sentinel_path(self, tmp_path):
        from dynoglobal import daemon_stop_path, ensure_global_dirs

        ensure_global_dirs()
        stop = daemon_stop_path()
        assert str(stop).endswith("stop")
        assert not stop.exists()
        stop.write_text("stop\n")
        assert stop.exists()

    def test_cmd_status_reports_not_running(self, tmp_path, capsys):
        import argparse
        from dynoglobal import cmd_status, ensure_global_dirs

        ensure_global_dirs()
        args = argparse.Namespace()
        ret = cmd_status(args)
        assert ret == 0
        output = json.loads(capsys.readouterr().out)
        assert output["running"] is False
        assert output["pid"] is None

    def test_cmd_status_reports_running(self, tmp_path, capsys):
        import argparse
        from dynoglobal import cmd_status, daemon_pid_path, ensure_global_dirs

        ensure_global_dirs()
        daemon_pid_path().write_text(f"{os.getpid()}\n")
        args = argparse.Namespace()
        ret = cmd_status(args)
        assert ret == 0
        output = json.loads(capsys.readouterr().out)
        assert output["running"] is True
        assert output["pid"] == os.getpid()

    def test_cmd_status_includes_project_summary(self, tmp_path, capsys):
        import argparse
        from dynoglobal import cmd_status, register_project, ensure_global_dirs

        ensure_global_dirs()
        proj = _make_project(tmp_path, "proj-a")
        register_project(proj)
        args = argparse.Namespace()
        cmd_status(args)
        output = json.loads(capsys.readouterr().out)
        assert output["projects_maintained"] == 1
        assert len(output["per_project_summary"]) == 1


# ===================================================================
# Cross-project aggregation tests (AC 41)
# ===================================================================

class TestCrossProjectAggregation:
    """Tests for statistics merge and prevention rule promotion."""

    def _register_projects_with_retros(self, tmp_path):
        """Register two mock projects with known retrospective data."""
        from dynoglobal import register_project

        retros_a = [
            {
                "task_type": "feature",
                "quality_score": 8.0,
                "executor_repair_frequency": {"backend": 1.0},
                "prevention_rules": ["always validate input"],
            },
            {
                "task_type": "bugfix",
                "quality_score": 7.0,
                "executor_repair_frequency": {"backend": 2.0},
                "prevention_rules": ["always validate input", "check nulls"],
            },
        ]
        retros_b = [
            {
                "task_type": "feature",
                "quality_score": 9.0,
                "executor_repair_frequency": {"frontend": 0.5},
                "prevention_rules": ["always validate input"],
            },
        ]
        proj_a = _make_project(tmp_path, "proj-a", retros_a)
        proj_b = _make_project(tmp_path, "proj-b", retros_b)
        register_project(proj_a)
        register_project(proj_b)
        return proj_a, proj_b

    def test_aggregate_merges_task_counts(self, tmp_path):
        from dynoglobal import aggregate_cross_project_stats

        self._register_projects_with_retros(tmp_path)
        result = aggregate_cross_project_stats()
        assert result["project_count"] == 2
        assert result["task_counts_by_type"]["feature"] == 2  # 1 from A + 1 from B
        assert result["task_counts_by_type"]["bugfix"] == 1

    def test_aggregate_merges_quality_scores(self, tmp_path):
        from dynoglobal import aggregate_cross_project_stats

        self._register_projects_with_retros(tmp_path)
        result = aggregate_cross_project_stats()
        # proj_a avg = (8+7)/2 = 7.5, proj_b avg = 9.0
        # overall = (7.5+9.0)/2 = 8.25
        assert result["average_quality_score"] == 8.25

    def test_aggregate_total_tasks(self, tmp_path):
        from dynoglobal import aggregate_cross_project_stats

        self._register_projects_with_retros(tmp_path)
        result = aggregate_cross_project_stats()
        assert result["total_tasks"] == 3

    def test_aggregate_writes_to_patterns_dir(self, tmp_path):
        from dynoglobal import aggregate_cross_project_stats, patterns_dir

        self._register_projects_with_retros(tmp_path)
        aggregate_cross_project_stats()
        stats_file = patterns_dir() / "cross-project-stats.json"
        assert stats_file.exists()
        data = json.loads(stats_file.read_text())
        assert "task_counts_by_type" in data

    def test_aggregate_keyed_by_metric_not_project(self, tmp_path):
        from dynoglobal import aggregate_cross_project_stats

        self._register_projects_with_retros(tmp_path)
        result = aggregate_cross_project_stats()
        # Keys must be metric names, not project paths
        for key in result:
            assert "proj-a" not in str(key)
            assert "proj-b" not in str(key)

    def test_promote_rules_at_2_project_threshold(self, tmp_path):
        from dynoglobal import promote_prevention_rules

        self._register_projects_with_retros(tmp_path)
        result = promote_prevention_rules()
        promoted_texts = [r["rule"] for r in result["rules"]]
        # "always validate input" appears in both projects -> promoted
        assert "always validate input" in promoted_texts
        # "check nulls" only in proj-a -> not promoted
        assert "check nulls" not in promoted_texts

    def test_promote_rules_written_to_patterns_dir(self, tmp_path):
        from dynoglobal import promote_prevention_rules, patterns_dir

        self._register_projects_with_retros(tmp_path)
        promote_prevention_rules()
        rules_file = patterns_dir() / "global-prevention-rules.json"
        assert rules_file.exists()
        data = json.loads(rules_file.read_text())
        assert "rules" in data
        assert data["threshold"] == 2

    def test_promote_rules_includes_project_count(self, tmp_path):
        from dynoglobal import promote_prevention_rules

        self._register_projects_with_retros(tmp_path)
        result = promote_prevention_rules()
        for rule_entry in result["rules"]:
            assert rule_entry["project_count"] >= 2

    def test_no_project_data_in_aggregated_stats(self, tmp_path):
        from dynoglobal import aggregate_cross_project_stats

        self._register_projects_with_retros(tmp_path)
        result = aggregate_cross_project_stats()
        result_str = json.dumps(result)
        # No project paths should leak
        assert "proj-a" not in result_str
        assert "proj-b" not in result_str

    def test_extract_project_stats_anonymous(self, tmp_path):
        from dynoglobal import extract_project_stats

        retros = [
            {
                "task_type": "feature",
                "quality_score": 9.0,
                "executor_repair_frequency": {"backend": 1.0},
                "prevention_rules": ["rule one"],
            }
        ]
        proj = _make_project(tmp_path, "my-secret-project", retros)
        stats = extract_project_stats(proj)
        stats_str = json.dumps(stats)
        assert "my-secret-project" not in stats_str
        assert "total_tasks" in stats
        assert "task_counts_by_type" in stats

    def test_single_project_no_promotion(self, tmp_path):
        from dynoglobal import register_project, promote_prevention_rules

        retros = [
            {
                "task_type": "feature",
                "quality_score": 8.0,
                "prevention_rules": ["lonely rule"],
            }
        ]
        proj = _make_project(tmp_path, "only-project", retros)
        register_project(proj)
        result = promote_prevention_rules()
        assert len(result["rules"]) == 0


# ===================================================================
# Isolation tests (AC 42)
# ===================================================================

class TestIsolation:
    """Tests that mock projects cannot access each other's .dynos/ dirs,
    and that global pattern files are read-only from the project perspective."""

    def test_project_a_cannot_read_project_b_dynos(self, tmp_path):
        """extract_project_stats only reads its own .dynos/ directory."""
        from dynoglobal import extract_project_stats

        retros_a = [{"task_type": "feature", "quality_score": 9.0, "prevention_rules": ["rule-A"]}]
        retros_b = [{"task_type": "bugfix", "quality_score": 5.0, "prevention_rules": ["rule-B"]}]
        proj_a = _make_project(tmp_path, "proj-a", retros_a)
        proj_b = _make_project(tmp_path, "proj-b", retros_b)

        stats_a = extract_project_stats(proj_a)
        stats_b = extract_project_stats(proj_b)

        # proj_a should only see its own data
        assert stats_a["task_counts_by_type"].get("feature") == 1
        assert "bugfix" not in stats_a["task_counts_by_type"]
        assert "rule-A" in stats_a["prevention_rule_frequencies"]
        assert "rule-B" not in stats_a["prevention_rule_frequencies"]

        # proj_b should only see its own data
        assert stats_b["task_counts_by_type"].get("bugfix") == 1
        assert "feature" not in stats_b["task_counts_by_type"]
        assert "rule-B" in stats_b["prevention_rule_frequencies"]
        assert "rule-A" not in stats_b["prevention_rule_frequencies"]

    def test_project_cannot_write_to_another_projects_dynos(self, tmp_path):
        """Verify that calling functions on proj_a does not create or modify
        files in proj_b's .dynos/ directory."""
        from dynoglobal import register_project, extract_project_stats

        proj_a = _make_project(tmp_path, "proj-a", [{"task_type": "feature", "quality_score": 8.0}])
        proj_b = _make_project(tmp_path, "proj-b", [{"task_type": "bugfix", "quality_score": 7.0}])

        # Record proj_b .dynos/ contents before any operations on proj_a
        proj_b_dynos = proj_b / ".dynos"
        before_files = set(str(p) for p in proj_b_dynos.rglob("*"))

        register_project(proj_a)
        extract_project_stats(proj_a)

        after_files = set(str(p) for p in proj_b_dynos.rglob("*"))
        assert before_files == after_files, "proj_a operations modified proj_b .dynos/"

    def test_global_patterns_not_written_by_project_operations(self, tmp_path):
        """Project-level operations (register, extract stats) should not write
        to the global patterns directory (only aggregation does)."""
        from dynoglobal import register_project, extract_project_stats, patterns_dir, ensure_global_dirs

        ensure_global_dirs()
        pat_dir = patterns_dir()
        before = set(str(p) for p in pat_dir.rglob("*")) if pat_dir.exists() else set()

        proj = _make_project(tmp_path, "proj-a", [{"task_type": "feature", "quality_score": 8.0}])
        register_project(proj)
        extract_project_stats(proj)

        after = set(str(p) for p in pat_dir.rglob("*")) if pat_dir.exists() else set()
        assert before == after, "project operations wrote to global patterns dir"

    def test_aggregate_does_not_leak_project_paths(self, tmp_path):
        from dynoglobal import register_project, aggregate_cross_project_stats, patterns_dir

        proj_a = _make_project(tmp_path, "secret-proj-a", [{"task_type": "feature", "quality_score": 8.0}])
        proj_b = _make_project(tmp_path, "secret-proj-b", [{"task_type": "bugfix", "quality_score": 7.0}])
        register_project(proj_a)
        register_project(proj_b)

        aggregate_cross_project_stats()
        stats_file = patterns_dir() / "cross-project-stats.json"
        content = stats_file.read_text()
        assert "secret-proj-a" not in content
        assert "secret-proj-b" not in content

    def test_global_home_uses_env_var(self, tmp_path):
        from dynoglobal import global_home

        assert global_home() == tmp_path
