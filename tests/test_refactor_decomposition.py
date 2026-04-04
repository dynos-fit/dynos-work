#!/usr/bin/env python3
"""TDD tests for dynoslib decomposition refactor (task-20260404-003).

These tests verify the 20 acceptance criteria from the spec. They are written
TDD-first: they will fail until the refactor is implemented. After the refactor,
all tests must pass without modification.

Acceptance criteria mapping:
  AC 1  -- dynoslib facade re-exports all names
  AC 2  -- dynoslib_core exports
  AC 3  -- dynoslib_validate exports
  AC 4  -- dynoslib_trajectory exports
  AC 5  -- dynoslib_registry exports
  AC 6  -- dynoslib_benchmark exports
  AC 7  -- dynoslib_queue exports
  AC 8  -- sub-module imports from core, no circular imports
  AC 9  -- dynoglobal subprocess invocation (no direct maintenance_cycle import)
  AC 10 -- maintenance_cycle file lock
  AC 11 -- project_dir and is_pid_running in dynoslib_core
  AC 12 -- dynoglobal_stats.py exports
  AC 13 -- dynopostmortem_improve.py exports
  AC 14 -- dyno_cli_base.py with cli_main
  AC 15 -- bin/dynos unchanged
  AC 18 -- no new subcommands
  AC 19 -- dynoslib.py has no __main__ block
  AC 20 -- dynomaintain run-once outputs JSON, returns 0
"""

from __future__ import annotations

import ast
import importlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = ROOT / "hooks"

# Ensure hooks/ is importable
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))


# ===================================================================
# AC 1: dynoslib facade re-exports every name from sub-modules
# ===================================================================

class TestDynoslibFacadeReExports:
    """AC 1: Every existing `from dynoslib import X` continues to resolve."""

    # Complete list of names currently imported by consumer modules
    CONSUMER_IMPORTS = [
        # From dynoslib_core (AC 2)
        "now_iso",
        "_persistent_project_dir",
        "load_json",
        "write_json",
        "require",
        "_safe_float",
        "project_policy",
        "benchmark_policy_config",
        "COMPOSITE_WEIGHTS",
        "STAGE_ORDER",
        "ALLOWED_STAGE_TRANSITIONS",
        "NEXT_COMMAND",
        "VALID_EXECUTORS",
        "VALID_CLASSIFICATION_TYPES",
        "VALID_DOMAINS",
        "VALID_RISK_LEVELS",
        "TOKEN_ESTIMATES",
        "transition_task",
        "next_command_for_stage",
        "find_active_tasks",
        "collect_retrospectives",
        "retrospective_task_ids",
        "task_recency_index",
        "tasks_since",
        "trajectories_store_path",
        "learned_agents_root",
        "learned_registry_path",
        "benchmark_history_path",
        "benchmark_index_path",
        "automation_queue_path",
        # From dynoslib_validate (AC 3)
        "REQUIRED_SPEC_HEADINGS",
        "REQUIRED_PLAN_HEADINGS",
        "validate_generated_html",
        "collect_headings",
        "parse_acceptance_criteria",
        "detect_cycle",
        "validate_manifest",
        "compute_fast_track",
        "apply_fast_track",
        "validate_task_artifacts",
        "validate_repair_log",
        "validate_retrospective",
        "check_segment_ownership",
        # From dynoslib_trajectory (AC 4)
        "ensure_trajectory_store",
        "compute_quality_score",
        "estimate_token_usage",
        "load_token_usage",
        "validate_retrospective_scores",
        "make_trajectory_entry",
        "rebuild_trajectory_store",
        "_domain_overlap",
        "trajectory_similarity",
        "search_trajectories",
        "collect_task_summaries",
        "retrospective_benchmark_score",
        # From dynoslib_registry (AC 5)
        "ensure_learned_registry",
        "register_learned_agent",
        "apply_evaluation_to_registry",
        "MAX_REGISTRY_BENCHMARKS",
        "resolve_registry_route",
        "entry_is_stale",
        # From dynoslib_benchmark (AC 6)
        "ensure_benchmark_history",
        "ensure_benchmark_index",
        "compute_benchmark_summary",
        "_category_summaries",
        "evaluate_candidate",
        "append_benchmark_run",
        "upsert_fixture_trace",
        "benchmark_fixtures_dir",
        "iter_benchmark_fixtures",
        "matching_fixtures_for_registry_entry",
        "benchmark_fixture_score",
        "synthesize_fixture_for_entry",
        "MAX_BENCHMARK_HISTORY_RUNS",
        # From dynoslib_queue (AC 7)
        "ensure_automation_queue",
        "enqueue_automation_item",
        "replace_automation_queue",
        "queue_identity",
        # AC 11 -- relocated from dynoglobal
        "project_dir",
        "is_pid_running",
    ]

    @pytest.mark.parametrize("name", CONSUMER_IMPORTS)
    def test_import_resolves(self, name: str) -> None:
        """Every public name is importable from dynoslib."""
        import dynoslib
        assert hasattr(dynoslib, name), f"dynoslib.{name} not found"

    def test_facade_module_is_importable(self) -> None:
        """dynoslib can be imported without error."""
        import dynoslib  # noqa: F401


# ===================================================================
# AC 2: dynoslib_core.py exports
# ===================================================================

class TestDynoslibCoreExports:
    """AC 2: dynoslib_core contains the expected names."""

    EXPECTED_NAMES = [
        "now_iso",
        "_persistent_project_dir",
        "load_json",
        "write_json",
        "require",
        "_safe_float",
        "project_policy",
        "benchmark_policy_config",
        "COMPOSITE_WEIGHTS",
        "STAGE_ORDER",
        "ALLOWED_STAGE_TRANSITIONS",
        "NEXT_COMMAND",
        "VALID_EXECUTORS",
        "VALID_CLASSIFICATION_TYPES",
        "VALID_DOMAINS",
        "VALID_RISK_LEVELS",
        "TOKEN_ESTIMATES",
        "transition_task",
        "next_command_for_stage",
        "find_active_tasks",
        "collect_retrospectives",
        "retrospective_task_ids",
        "task_recency_index",
        "tasks_since",
        "trajectories_store_path",
        "learned_agents_root",
        "learned_registry_path",
        "benchmark_history_path",
        "benchmark_index_path",
        "automation_queue_path",
    ]

    @pytest.mark.parametrize("name", EXPECTED_NAMES)
    def test_core_exports(self, name: str) -> None:
        import dynoslib_core
        assert hasattr(dynoslib_core, name), f"dynoslib_core.{name} not found"


# ===================================================================
# AC 3: dynoslib_validate.py exports
# ===================================================================

class TestDynoslibValidateExports:
    """AC 3: dynoslib_validate contains the expected names."""

    EXPECTED_NAMES = [
        "REQUIRED_SPEC_HEADINGS",
        "REQUIRED_PLAN_HEADINGS",
        "validate_generated_html",
        "collect_headings",
        "parse_acceptance_criteria",
        "detect_cycle",
        "validate_manifest",
        "compute_fast_track",
        "apply_fast_track",
        "validate_task_artifacts",
        "validate_repair_log",
        "validate_retrospective",
        "check_segment_ownership",
    ]

    @pytest.mark.parametrize("name", EXPECTED_NAMES)
    def test_validate_exports(self, name: str) -> None:
        import dynoslib_validate
        assert hasattr(dynoslib_validate, name), f"dynoslib_validate.{name} not found"


# ===================================================================
# AC 4: dynoslib_trajectory.py exports
# ===================================================================

class TestDynoslibTrajectoryExports:
    """AC 4: dynoslib_trajectory contains the expected names."""

    EXPECTED_NAMES = [
        "ensure_trajectory_store",
        "compute_quality_score",
        "estimate_token_usage",
        "load_token_usage",
        "validate_retrospective_scores",
        "make_trajectory_entry",
        "rebuild_trajectory_store",
        "_domain_overlap",
        "trajectory_similarity",
        "search_trajectories",
        "collect_task_summaries",
        "retrospective_benchmark_score",
    ]

    @pytest.mark.parametrize("name", EXPECTED_NAMES)
    def test_trajectory_exports(self, name: str) -> None:
        import dynoslib_trajectory
        assert hasattr(dynoslib_trajectory, name), f"dynoslib_trajectory.{name} not found"


# ===================================================================
# AC 5: dynoslib_registry.py exports
# ===================================================================

class TestDynoslibRegistryExports:
    """AC 5: dynoslib_registry contains the expected names."""

    EXPECTED_NAMES = [
        "ensure_learned_registry",
        "register_learned_agent",
        "apply_evaluation_to_registry",
        "MAX_REGISTRY_BENCHMARKS",
        "resolve_registry_route",
        "entry_is_stale",
    ]

    @pytest.mark.parametrize("name", EXPECTED_NAMES)
    def test_registry_exports(self, name: str) -> None:
        import dynoslib_registry
        assert hasattr(dynoslib_registry, name), f"dynoslib_registry.{name} not found"


# ===================================================================
# AC 6: dynoslib_benchmark.py exports
# ===================================================================

class TestDynoslibBenchmarkExports:
    """AC 6: dynoslib_benchmark contains the expected names."""

    EXPECTED_NAMES = [
        "ensure_benchmark_history",
        "ensure_benchmark_index",
        "compute_benchmark_summary",
        "_category_summaries",
        "evaluate_candidate",
        "append_benchmark_run",
        "upsert_fixture_trace",
        "benchmark_fixtures_dir",
        "iter_benchmark_fixtures",
        "matching_fixtures_for_registry_entry",
        "benchmark_fixture_score",
        "synthesize_fixture_for_entry",
        "MAX_BENCHMARK_HISTORY_RUNS",
    ]

    @pytest.mark.parametrize("name", EXPECTED_NAMES)
    def test_benchmark_exports(self, name: str) -> None:
        import dynoslib_benchmark
        assert hasattr(dynoslib_benchmark, name), f"dynoslib_benchmark.{name} not found"


# ===================================================================
# AC 7: dynoslib_queue.py exports
# ===================================================================

class TestDynoslibQueueExports:
    """AC 7: dynoslib_queue contains the expected names."""

    EXPECTED_NAMES = [
        "ensure_automation_queue",
        "enqueue_automation_item",
        "replace_automation_queue",
        "queue_identity",
    ]

    @pytest.mark.parametrize("name", EXPECTED_NAMES)
    def test_queue_exports(self, name: str) -> None:
        import dynoslib_queue
        assert hasattr(dynoslib_queue, name), f"dynoslib_queue.{name} not found"


# ===================================================================
# AC 8: No circular imports between sub-modules
# ===================================================================

class TestNoCircularImports:
    """AC 8: Sub-modules import from dynoslib_core, not from the facade.
    No circular imports exist between any pair of sub-modules."""

    SUB_MODULES = [
        "dynoslib_core",
        "dynoslib_validate",
        "dynoslib_trajectory",
        "dynoslib_registry",
        "dynoslib_benchmark",
        "dynoslib_queue",
    ]

    def test_sub_modules_do_not_import_from_facade(self) -> None:
        """No sub-module imports from dynoslib (the facade)."""
        for mod_name in self.SUB_MODULES:
            mod_path = HOOKS_DIR / f"{mod_name}.py"
            assert mod_path.exists(), f"{mod_name}.py does not exist"
            source = mod_path.read_text()
            tree = ast.parse(source, filename=str(mod_path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "dynoslib":
                    pytest.fail(
                        f"{mod_name}.py imports from 'dynoslib' facade "
                        f"(line {node.lineno}). Sub-modules must import "
                        f"from dynoslib_core or peer sub-modules."
                    )

    @pytest.mark.parametrize("mod_name", SUB_MODULES)
    def test_each_sub_module_imports_cleanly(self, mod_name: str) -> None:
        """Each sub-module can be imported independently without ImportError."""
        mod = importlib.import_module(mod_name)
        assert mod is not None

    def test_all_sub_modules_import_together(self) -> None:
        """Importing all sub-modules in any order does not raise circular import errors."""
        for mod_name in self.SUB_MODULES:
            importlib.import_module(mod_name)
        # Reverse order
        for mod_name in reversed(self.SUB_MODULES):
            importlib.reload(importlib.import_module(mod_name))


# ===================================================================
# AC 9: dynoglobal no longer imports maintenance_cycle directly
# ===================================================================

class TestDaemonSubprocessSeparation:
    """AC 9: dynoglobal uses subprocess.run to invoke dynomaintain
    instead of importing maintenance_cycle directly."""

    def test_no_direct_maintenance_cycle_import(self) -> None:
        """dynoglobal.py does not contain 'from dynomaintain import maintenance_cycle'."""
        source = (HOOKS_DIR / "dynoglobal.py").read_text()
        tree = ast.parse(source, filename="dynoglobal.py")
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "dynomaintain":
                imported_names = [alias.name for alias in node.names]
                assert "maintenance_cycle" not in imported_names, (
                    "dynoglobal.py still imports maintenance_cycle from dynomaintain"
                )

    def test_subprocess_invocation_pattern_exists(self) -> None:
        """dynoglobal.py contains subprocess.run invocation for dynomaintain."""
        source = (HOOKS_DIR / "dynoglobal.py").read_text()
        assert "subprocess.run" in source, (
            "dynoglobal.py does not contain subprocess.run invocation"
        )
        assert "dynomaintain" in source, (
            "dynoglobal.py does not reference dynomaintain in subprocess call"
        )

    def test_subprocess_uses_run_once_subcommand(self) -> None:
        """The subprocess invocation uses the 'run-once' subcommand."""
        source = (HOOKS_DIR / "dynoglobal.py").read_text()
        assert "run-once" in source, (
            "dynoglobal.py subprocess call does not use 'run-once' subcommand"
        )

    def test_subprocess_captures_output(self) -> None:
        """The subprocess invocation captures stdout for JSON parsing."""
        source = (HOOKS_DIR / "dynoglobal.py").read_text()
        assert "capture_output=True" in source or "stdout=" in source, (
            "dynoglobal.py subprocess call does not capture output"
        )

    def test_subprocess_has_timeout(self) -> None:
        """The subprocess invocation has a timeout parameter."""
        source = (HOOKS_DIR / "dynoglobal.py").read_text()
        assert "timeout=" in source, (
            "dynoglobal.py subprocess call does not have a timeout"
        )


# ===================================================================
# AC 10: maintenance_cycle acquires file lock
# ===================================================================

class TestCycleLock:
    """AC 10: maintenance_cycle acquires exclusive file lock on cycle.lock."""

    def test_maintenance_cycle_uses_flock(self) -> None:
        """maintenance_cycle in dynomaintain.py uses fcntl.flock for cycle.lock."""
        source = (HOOKS_DIR / "dynomaintain.py").read_text()
        assert "cycle.lock" in source, (
            "dynomaintain.py does not reference cycle.lock"
        )
        assert "fcntl.flock" in source or "fcntl.LOCK_EX" in source, (
            "dynomaintain.py does not use fcntl.flock"
        )

    def test_lock_has_finally_block(self) -> None:
        """The lock is released in a finally block."""
        source = (HOOKS_DIR / "dynomaintain.py").read_text()
        # Parse the AST and find try/finally around the lock
        tree = ast.parse(source, filename="dynomaintain.py")
        found_try_finally = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Try) and node.finalbody:
                # Check if this try block or its finally body references flock
                try_source = ast.get_source_segment(source, node)
                if try_source and "flock" in try_source:
                    found_try_finally = True
                    break
        assert found_try_finally, (
            "dynomaintain.py does not have a try/finally block around flock"
        )

    def test_lock_skip_returns_correct_dict(self) -> None:
        """When lock is held, maintenance_cycle returns skip dict."""
        source = (HOOKS_DIR / "dynomaintain.py").read_text()
        assert "skipped" in source, (
            "dynomaintain.py does not return a skip indicator when lock is held"
        )
        assert "cycle lock held" in source or "cycle lock" in source.lower(), (
            "dynomaintain.py does not mention cycle lock in skip reason"
        )


# ===================================================================
# AC 11: project_dir and is_pid_running relocated to dynoslib_core
# ===================================================================

class TestFunctionRelocation:
    """AC 11: project_dir and is_pid_running are in dynoslib_core,
    importable via dynoslib facade."""

    def test_project_dir_in_dynoslib_core(self) -> None:
        import dynoslib_core
        assert hasattr(dynoslib_core, "project_dir")
        assert callable(dynoslib_core.project_dir)

    def test_is_pid_running_in_dynoslib_core(self) -> None:
        import dynoslib_core
        assert hasattr(dynoslib_core, "is_pid_running")
        assert callable(dynoslib_core.is_pid_running)

    def test_project_dir_importable_via_facade(self) -> None:
        from dynoslib import project_dir
        assert callable(project_dir)

    def test_is_pid_running_importable_via_facade(self) -> None:
        from dynoslib import is_pid_running
        assert callable(is_pid_running)

    def test_dynoglobal_imports_from_dynoslib_core(self) -> None:
        """dynoglobal.py imports project_dir from dynoslib_core (or dynoslib)."""
        source = (HOOKS_DIR / "dynoglobal.py").read_text()
        tree = ast.parse(source, filename="dynoglobal.py")
        # project_dir should NOT be defined as a function in dynoglobal.py anymore
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "project_dir":
                pytest.fail(
                    "dynoglobal.py still defines project_dir as a local function. "
                    "It should import it from dynoslib_core."
                )

    def test_dynopostmortem_imports_project_dir_from_dynoslib(self) -> None:
        """dynopostmortem.py imports project_dir from dynoslib, not dynoglobal."""
        source = (HOOKS_DIR / "dynopostmortem.py").read_text()
        tree = ast.parse(source, filename="dynopostmortem.py")
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "dynoglobal":
                imported_names = [alias.name for alias in node.names]
                assert "project_dir" not in imported_names, (
                    "dynopostmortem.py still imports project_dir from dynoglobal"
                )


# ===================================================================
# AC 12: dynoglobal_stats.py exports
# ===================================================================

class TestDynoglobalStatsExtraction:
    """AC 12: dynoglobal_stats.py exports extract_project_stats,
    aggregate_cross_project_stats, and promote_prevention_rules."""

    def test_module_exists(self) -> None:
        assert (HOOKS_DIR / "dynoglobal_stats.py").exists(), (
            "dynoglobal_stats.py does not exist in hooks/"
        )

    EXPECTED_NAMES = [
        "extract_project_stats",
        "aggregate_cross_project_stats",
        "promote_prevention_rules",
    ]

    @pytest.mark.parametrize("name", EXPECTED_NAMES)
    def test_dynoglobal_stats_exports(self, name: str) -> None:
        import dynoglobal_stats
        assert hasattr(dynoglobal_stats, name), (
            f"dynoglobal_stats.{name} not found"
        )

    def test_dynoglobal_still_exposes_stats_functions(self) -> None:
        """dynoglobal.py re-imports and exposes the stats functions."""
        import dynoglobal
        for name in self.EXPECTED_NAMES:
            assert hasattr(dynoglobal, name), (
                f"dynoglobal.{name} no longer accessible after extraction"
            )


# ===================================================================
# AC 13: dynopostmortem_improve.py exports
# ===================================================================

class TestDynopostmortemImproveExtraction:
    """AC 13: dynopostmortem_improve.py exports the improvement engine."""

    def test_module_exists(self) -> None:
        assert (HOOKS_DIR / "dynopostmortem_improve.py").exists(), (
            "dynopostmortem_improve.py does not exist in hooks/"
        )

    EXPECTED_NAMES = [
        "_improvements_dir",
        "_load_applied_ids",
        "_save_applied_id",
        "propose_improvements",
        "apply_improvement",
        "run_improvement_cycle",
        "cmd_improve",
        "cmd_list_pending",
        "cmd_approve",
        "cmd_propose",
    ]

    @pytest.mark.parametrize("name", EXPECTED_NAMES)
    def test_improve_exports(self, name: str) -> None:
        import dynopostmortem_improve
        assert hasattr(dynopostmortem_improve, name), (
            f"dynopostmortem_improve.{name} not found"
        )

    def test_dynopostmortem_still_wires_improve_subcommand(self) -> None:
        """dynopostmortem.py still has 'improve' in its CLI parser."""
        source = (HOOKS_DIR / "dynopostmortem.py").read_text()
        assert "improve" in source, (
            "dynopostmortem.py no longer wires the 'improve' subcommand"
        )


# ===================================================================
# AC 14: dyno_cli_base.py with cli_main
# ===================================================================

class TestCliBase:
    """AC 14: dyno_cli_base.py exists with cli_main function."""

    def test_module_exists(self) -> None:
        assert (HOOKS_DIR / "dyno_cli_base.py").exists(), (
            "dyno_cli_base.py does not exist in hooks/"
        )

    def test_cli_main_is_callable(self) -> None:
        import dyno_cli_base
        assert hasattr(dyno_cli_base, "cli_main")
        assert callable(dyno_cli_base.cli_main)

    def test_cli_main_accepts_build_parser_fn(self) -> None:
        """cli_main accepts a build_parser_fn callable."""
        import inspect
        import dyno_cli_base
        sig = inspect.signature(dyno_cli_base.cli_main)
        params = list(sig.parameters)
        assert len(params) >= 1, "cli_main must accept at least one parameter"

    def test_hook_modules_use_cli_main(self) -> None:
        """At least some hook modules use cli_main from dyno_cli_base."""
        # Check a representative set of hook modules
        sample_modules = [
            "dynorouter.py",
            "dynoplanner.py",
            "dynosctl.py",
            "dynomaintain.py",
        ]
        uses_cli_main = 0
        for mod_name in sample_modules:
            source = (HOOKS_DIR / mod_name).read_text()
            if "cli_main" in source:
                uses_cli_main += 1
        assert uses_cli_main >= 2, (
            f"Only {uses_cli_main} of {len(sample_modules)} sample modules use cli_main"
        )

    def test_hook_modules_preserve_sys_path_insert(self) -> None:
        """Hook modules still have sys.path.insert before any local imports."""
        sample_modules = [
            "dynorouter.py",
            "dynoplanner.py",
            "dynomaintain.py",
        ]
        for mod_name in sample_modules:
            source = (HOOKS_DIR / mod_name).read_text()
            assert "sys.path.insert" in source or "sys.path" in source, (
                f"{mod_name} lost its sys.path.insert one-liner"
            )


# ===================================================================
# AC 15, 18: bin/dynos unchanged, no new subcommands
# ===================================================================

class TestBinDynosUnchanged:
    """AC 15: bin/dynos shell script is unchanged.
    AC 18: No new CLI subcommands added or removed."""

    EXPECTED_SUBCOMMANDS = [
        "route",
        "plan",
        "patterns",
        "ctl",
        "postmortem",
        "registry",
        "global",
        "evolve",
        "trajectory",
        "dashboard",
        "local",
        "maintain",
        "bench",
        "report",
        "proactive",
        "init",
        "list",
        "remove",
        "pause",
        "resume",
    ]

    def test_bin_dynos_exists(self) -> None:
        assert (ROOT / "bin" / "dynos").exists()

    def test_bin_dynos_has_all_expected_subcommands(self) -> None:
        content = (ROOT / "bin" / "dynos").read_text()
        for subcmd in self.EXPECTED_SUBCOMMANDS:
            assert subcmd in content, (
                f"bin/dynos missing expected subcommand: {subcmd}"
            )

    def test_bin_dynos_routes_to_correct_hook_files(self) -> None:
        """bin/dynos routes subcommands to the correct hook module files."""
        content = (ROOT / "bin" / "dynos").read_text()
        expected_routes = {
            "route": "dynorouter.py",
            "plan": "dynoplanner.py",
            "patterns": "dynopatterns.py",
            "ctl": "dynosctl.py",
            "postmortem": "dynopostmortem.py",
            "global": "dynoglobal.py",
            "maintain": "dynomaintain.py",
        }
        for subcmd, hook_file in expected_routes.items():
            assert hook_file in content, (
                f"bin/dynos does not route '{subcmd}' to '{hook_file}'"
            )

    def test_no_new_subcommands_in_case_block(self) -> None:
        """The case block in bin/dynos has exactly the expected subcommands."""
        content = (ROOT / "bin" / "dynos").read_text()
        # Extract top-level subcommand names from case patterns like "  route)"
        import re
        # Match only top-level case entries (2-space indent, not nested)
        case_cmds = re.findall(r'^  (\w+)\)', content, re.MULTILINE)
        for cmd in case_cmds:
            if cmd in ("help", "esac"):
                continue
            assert cmd in self.EXPECTED_SUBCOMMANDS, (
                f"bin/dynos has unexpected new subcommand: {cmd}"
            )


# ===================================================================
# AC 19: dynoslib.py has no __main__ block
# ===================================================================

class TestDynoslibNoMainBlock:
    """AC 19: dynoslib.py has no if __name__ == '__main__' block."""

    def test_no_main_block(self) -> None:
        source = (HOOKS_DIR / "dynoslib.py").read_text()
        assert "__main__" not in source, (
            "dynoslib.py contains a __main__ block"
        )

    def test_dynoslib_importable_as_module(self) -> None:
        """dynoslib can be imported without executing a main block."""
        import dynoslib  # noqa: F401
        # If this does not raise, the module is importable


# ===================================================================
# AC 20: dynomaintain run-once outputs JSON, returns 0
# ===================================================================

class TestDynomaintainRunOnceContract:
    """AC 20: dynomaintain.py run-once outputs JSON stdout, exit code 0."""

    def test_cmd_run_once_exists(self) -> None:
        """dynomaintain.py has a cmd_run_once function."""
        import dynomaintain
        assert hasattr(dynomaintain, "cmd_run_once")
        assert callable(dynomaintain.cmd_run_once)

    def test_cmd_run_once_prints_json(self, tmp_path: Path) -> None:
        """cmd_run_once prints valid JSON to stdout."""
        # Set up minimal project structure
        dynos_dir = tmp_path / ".dynos" / "maintenance"
        dynos_dir.mkdir(parents=True)

        result = subprocess.run(
            [
                sys.executable,
                str(HOOKS_DIR / "dynomaintain.py"),
                "run-once",
                "--root",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, (
            f"dynomaintain run-once returned {result.returncode}. "
            f"stderr: {result.stderr}"
        )
        # stdout must be valid JSON
        output = result.stdout.strip()
        assert output, "dynomaintain run-once produced no stdout"
        parsed = json.loads(output)
        assert isinstance(parsed, dict), "dynomaintain run-once stdout is not a JSON object"

    def test_cmd_run_once_json_has_ok_field(self, tmp_path: Path) -> None:
        """The JSON output includes an 'ok' field."""
        dynos_dir = tmp_path / ".dynos" / "maintenance"
        dynos_dir.mkdir(parents=True)

        result = subprocess.run(
            [
                sys.executable,
                str(HOOKS_DIR / "dynomaintain.py"),
                "run-once",
                "--root",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0 and result.stdout.strip():
            parsed = json.loads(result.stdout.strip())
            assert "ok" in parsed, "run-once JSON output missing 'ok' field"


# ===================================================================
# Integration: sub-module files exist
# ===================================================================

class TestSubModuleFilesExist:
    """Verify all new sub-module files exist in hooks/."""

    NEW_MODULES = [
        "dynoslib_core.py",
        "dynoslib_validate.py",
        "dynoslib_trajectory.py",
        "dynoslib_registry.py",
        "dynoslib_benchmark.py",
        "dynoslib_queue.py",
        "dynoglobal_stats.py",
        "dynopostmortem_improve.py",
        "dyno_cli_base.py",
    ]

    @pytest.mark.parametrize("filename", NEW_MODULES)
    def test_file_exists(self, filename: str) -> None:
        assert (HOOKS_DIR / filename).exists(), (
            f"hooks/{filename} does not exist"
        )


# ===================================================================
# Behavioral: is_pid_running works correctly via facade
# ===================================================================

class TestIsPidRunningBehavior:
    """Behavioral test for is_pid_running via dynoslib facade."""

    def test_own_pid_is_running(self) -> None:
        from dynoslib import is_pid_running
        assert is_pid_running(os.getpid()) is True

    def test_dead_pid_is_not_running(self) -> None:
        from dynoslib import is_pid_running
        assert is_pid_running(99999999) is False


# ===================================================================
# Behavioral: project_dir returns correct path via facade
# ===================================================================

class TestProjectDirBehavior:
    """Behavioral test for project_dir via dynoslib facade."""

    def test_project_dir_returns_path(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("DYNOS_HOME", str(tmp_path))
        from dynoslib import project_dir
        result = project_dir(tmp_path / "my-project")
        assert isinstance(result, Path)
        assert result.exists()
        assert "projects" in str(result)
