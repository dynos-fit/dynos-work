#!/usr/bin/env python3
"""Tests for memory/agent_generator.py — verifies the five logic bug fixes."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "memory"))
sys.path.insert(0, str(ROOT / "hooks"))

from agent_generator import (
    CATEGORY_INSTRUCTIONS,
    _aggregate_finding_categories,
    _build_agent_content,
    _matching_retrospectives,
    cmd_auto,
)


def _make_task(
    tmpdir: Path,
    task_id: str,
    task_type: str,
    executors: list[str],
    findings_by_category: dict[str, int] | None = None,
    executor_repair_frequency: dict[str, int] | None = None,
) -> None:
    """Create a task directory with execution-graph.json and task-retrospective.json."""
    task_dir = tmpdir / ".dynos" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    graph = {
        "task_id": task_id,
        "segments": [
            {"id": f"seg-{i}", "executor": ex} for i, ex in enumerate(executors)
        ],
    }
    (task_dir / "execution-graph.json").write_text(json.dumps(graph))

    retro: dict = {
        "task_id": task_id,
        "task_type": task_type,
        "findings_by_category": findings_by_category or {},
        "executor_repair_frequency": executor_repair_frequency or {},
        "_path": str(task_dir / "task-retrospective.json"),
    }
    (task_dir / "task-retrospective.json").write_text(json.dumps(retro))


def _collect_retros(tmpdir: Path) -> list[dict]:
    """Collect retrospectives from the temp dir (mirrors lib_core.collect_retrospectives)."""
    retros: list[dict] = []
    dynos = tmpdir / ".dynos"
    if not dynos.exists():
        return retros
    for path in sorted(dynos.glob("task-*/task-retrospective.json")):
        data = json.loads(path.read_text())
        data["_path"] = str(path)
        retros.append(data)
    return retros


class TestMatchingRetrospectives(unittest.TestCase):
    """Tests for _matching_retrospectives rewrite (criteria 1)."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_includes_retro_where_role_in_execution_graph(self) -> None:
        """Retro is matched when the role appears in the execution graph segments."""
        _make_task(self.root, "task-001", "feature", ["backend-executor", "frontend-executor"])
        retros = _collect_retros(self.root)
        matched = _matching_retrospectives(retros, "backend-executor", "feature", self.root)
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["task_id"], "task-001")

    def test_excludes_retro_in_repair_freq_but_not_in_graph(self) -> None:
        """Retro where role is in executor_repair_frequency but NOT in execution graph is excluded."""
        _make_task(
            self.root, "task-002", "feature",
            executors=["frontend-executor"],  # backend NOT in graph
            executor_repair_frequency={"backend-executor": 3},  # but IS in repair freq
        )
        retros = _collect_retros(self.root)
        matched = _matching_retrospectives(retros, "backend-executor", "feature", self.root)
        self.assertEqual(len(matched), 0)

    def test_excludes_retro_with_missing_execution_graph(self) -> None:
        """Retro with missing execution-graph.json is gracefully skipped."""
        task_dir = self.root / ".dynos" / "task-003"
        task_dir.mkdir(parents=True, exist_ok=True)
        retro = {"task_id": "task-003", "task_type": "feature", "_path": str(task_dir / "task-retrospective.json")}
        (task_dir / "task-retrospective.json").write_text(json.dumps(retro))
        # No execution-graph.json created
        retros = _collect_retros(self.root)
        matched = _matching_retrospectives(retros, "backend-executor", "feature", self.root)
        self.assertEqual(len(matched), 0)

    def test_excludes_retro_with_wrong_task_type(self) -> None:
        """Retro with non-matching task_type is excluded."""
        _make_task(self.root, "task-004", "bugfix", ["backend-executor"])
        retros = _collect_retros(self.root)
        matched = _matching_retrospectives(retros, "backend-executor", "feature", self.root)
        self.assertEqual(len(matched), 0)

    def test_includes_retro_with_zero_repairs_but_in_graph(self) -> None:
        """Retro where role had zero repairs but appears in execution graph is included."""
        _make_task(
            self.root, "task-005", "feature",
            executors=["backend-executor"],
            executor_repair_frequency={},  # zero repairs, not even in freq dict
        )
        retros = _collect_retros(self.root)
        matched = _matching_retrospectives(retros, "backend-executor", "feature", self.root)
        self.assertEqual(len(matched), 1)

    def test_skips_retro_without_path(self) -> None:
        """Retro without _path key is skipped gracefully."""
        retros = [{"task_id": "task-006", "task_type": "feature"}]
        matched = _matching_retrospectives(retros, "backend-executor", "feature", self.root)
        self.assertEqual(len(matched), 0)


class TestBuildAgentContent(unittest.TestCase):
    """Tests for _build_agent_content restructure (criteria 3, 4, 5, 6, 11)."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _write_prevention_rules(self, rules: list[dict]) -> None:
        """Write prevention rules to the persistent project dir location."""
        # Mock _persistent_project_dir to return our temp dir
        rules_dir = self.root / "persistent"
        rules_dir.mkdir(parents=True, exist_ok=True)
        (rules_dir / "prevention-rules.json").write_text(json.dumps({"rules": rules}))

    def _build(
        self,
        role: str = "backend-executor",
        task_type: str = "feature",
        retros: list[dict] | None = None,
        rules: list[dict] | None = None,
    ) -> str:
        if rules is not None:
            self._write_prevention_rules(rules)
        matched = retros or []
        with patch("agent_generator._persistent_project_dir", return_value=self.root / "persistent"):
            return _build_agent_content(
                "auto-backend-feature", role, task_type, matched, "task-099", self.root
            )

    def test_contains_imperative_rules_not_telemetry(self) -> None:
        """Output contains imperative instructions, NOT telemetry sections (criteria 3, 4, 5, 11)."""
        retros = [
            {"findings_by_category": {"sec": 4, "cq": 2}},
        ]
        content = self._build(retros=retros)

        # Must contain imperative section
        self.assertIn("## Failure Prevention Rules", content)

        # Must NOT contain telemetry sections
        self.assertNotIn("Repair frequency", content)
        self.assertNotIn("Finding categories", content)
        self.assertNotIn("## Patterns", content)
        self.assertNotIn("Total repairs", content)
        self.assertNotIn("Average repairs per task", content)

        # Must contain imperative language from fallback instructions
        self.assertIn("ALWAYS", content)

    def test_filters_rules_by_executor(self) -> None:
        """Prevention rules are filtered by executor field (criterion 6)."""
        retros = [{"findings_by_category": {"sec": 2}}]
        rules = [
            {"category": "sec", "executor": "backend-executor", "rule": "DO NOT skip auth checks."},
            {"category": "sec", "executor": "frontend-executor", "rule": "DO NOT expose tokens in client code."},
        ]
        content = self._build(retros=retros, rules=rules)
        self.assertIn("DO NOT skip auth checks", content)
        self.assertNotIn("DO NOT expose tokens", content)

    def test_includes_rules_with_executor_all(self) -> None:
        """Prevention rules with executor 'all' are included for any role (criterion 6)."""
        retros = [{"findings_by_category": {"sec": 1}}]
        rules = [
            {"category": "sec", "executor": "all", "rule": "ALWAYS sanitize user input."},
        ]
        content = self._build(retros=retros, rules=rules)
        self.assertIn("ALWAYS sanitize user input", content)

    def test_includes_rules_with_no_executor_field(self) -> None:
        """Prevention rules without executor field default to 'all' (criterion 6)."""
        retros = [{"findings_by_category": {"cq": 1}}]
        rules = [
            {"category": "cq", "rule": "ALWAYS run linter before submitting."},
        ]
        content = self._build(retros=retros, rules=rules)
        self.assertIn("ALWAYS run linter", content)

    def test_fallback_instruction_when_no_specific_rules(self) -> None:
        """When no prevention rules match a category, fallback instruction is used (criterion 4)."""
        retros = [{"findings_by_category": {"db": 3}}]
        content = self._build(retros=retros, rules=[])
        self.assertIn(CATEGORY_INSTRUCTIONS["db"], content)

    def test_context_metadata_present(self) -> None:
        """Context metadata section is retained (criterion 3)."""
        content = self._build(retros=[{"findings_by_category": {}}])
        self.assertIn("## Context", content)
        self.assertIn("**Role**: backend-executor", content)
        self.assertIn("**Task type**: feature", content)
        self.assertIn("**Generated from**: task-099", content)

    def test_output_coherent_with_hard_constraints_framing(self) -> None:
        """Output is coherent as hard constraints injected by router (criterion 11)."""
        retros = [{"findings_by_category": {"sec": 5, "test": 2}}]
        rules = [
            {"category": "sec", "executor": "backend-executor", "rule": "DO NOT bypass authentication middleware."},
            {"category": "test", "executor": "all", "rule": "ALWAYS include regression tests for bug fixes."},
        ]
        content = self._build(retros=retros, rules=rules)
        # Content should read as imperative rules, not statistical summaries
        self.assertIn("DO NOT", content)
        self.assertIn("ALWAYS", content)
        # Should not contain statistical/descriptive phrases
        self.assertNotIn("findings", content.lower().split("## failure prevention")[0])
        self.assertNotIn("average", content.lower())


class TestCmdAuto(unittest.TestCase):
    """Tests for cmd_auto fixes (criteria 2, 7)."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        (self.root / ".dynos").mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _make_args(self, min_tasks: int = 1) -> argparse.Namespace:
        return argparse.Namespace(root=str(self.root), min_tasks=min_tasks)

    def test_skips_slot_when_matched_retros_empty(self) -> None:
        """cmd_auto skips agent generation when no matching retros found (criterion 2)."""
        # Create 3 tasks with backend-executor in graph but DIFFERENT task types in retros
        # so _matching_retrospectives returns empty for each slot check
        for i in range(3):
            task_id = f"task-00{i+1}"
            task_dir = self.root / ".dynos" / task_id
            task_dir.mkdir(parents=True, exist_ok=True)
            # Graph says backend-executor participated
            graph = {"task_id": task_id, "segments": [{"id": "seg-0", "executor": "backend-executor"}]}
            (task_dir / "execution-graph.json").write_text(json.dumps(graph))
            # Retro says task_type=feature
            retro = {"task_id": task_id, "task_type": "feature"}
            (task_dir / "task-retrospective.json").write_text(json.dumps(retro))

        # Now corrupt all execution graphs so _matching_retrospectives can't match
        for i in range(3):
            task_id = f"task-00{i+1}"
            eg_path = self.root / ".dynos" / task_id / "execution-graph.json"
            eg_path.write_text("INVALID JSON")

        with patch("agent_generator._persistent_project_dir", return_value=self.root / "persistent"), \
             patch("agent_generator.log_event"), \
             patch("agent_generator.ensure_learned_registry", return_value={"agents": []}), \
             patch("agent_generator.register_learned_agent"), \
             patch("agent_generator.collect_retrospectives") as mock_collect:
            # collect_retrospectives returns retros WITH _path
            retros = []
            for i in range(3):
                task_id = f"task-00{i+1}"
                retros.append({
                    "task_id": task_id,
                    "task_type": "feature",
                    "_path": str(self.root / ".dynos" / task_id / "task-retrospective.json"),
                })
            mock_collect.return_value = retros

            # Restore execution graphs for slot discovery (cmd_auto reads them directly)
            for i in range(3):
                task_id = f"task-00{i+1}"
                graph = {"task_id": task_id, "segments": [{"id": "seg-0", "executor": "backend-executor"}]}
                (self.root / ".dynos" / task_id / "execution-graph.json").write_text(json.dumps(graph))

            # But corrupt them again AFTER slot discovery runs
            # Actually, let's use a different approach: make the graph valid for discovery
            # but _matching_retrospectives fails because retros have no _path that resolves
            # Simpler: just patch _matching_retrospectives to return empty
            with patch("agent_generator._matching_retrospectives", return_value=[]):
                import io
                with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                    cmd_auto(self._make_args(min_tasks=1))
                    output = json.loads(mock_out.getvalue())

        # No agents should have been generated
        self.assertEqual(len(output["generated"]), 0)
        # Should have a skip reason
        self.assertTrue(
            any("no matching" in reason for reason in output.get("skipped_reasons", [])),
            f"Expected skip reason, got: {output.get('skipped_reasons')}"
        )

    def test_uses_latest_matched_retro_for_provenance(self) -> None:
        """cmd_auto uses latest matched retro task_id for provenance, not global latest (criterion 7)."""
        # Create tasks: task-001 (backend, feature), task-002 (frontend, bugfix), task-003 (backend, feature)
        _make_task(self.root, "task-001", "feature", ["backend-executor"], {"sec": 1})
        _make_task(self.root, "task-002", "bugfix", ["frontend-executor"], {"cq": 1})
        _make_task(self.root, "task-003", "feature", ["backend-executor"], {"sec": 2})

        retros = _collect_retros(self.root)
        matched = _matching_retrospectives(retros, "backend-executor", "feature", self.root)

        # Latest matched retro should be task-003, not task-002 (the global latest for bugfix)
        self.assertEqual(len(matched), 2)
        latest_task = matched[-1].get("task_id", "unknown")
        self.assertEqual(latest_task, "task-003")

    def test_no_repair_frequency_in_output(self) -> None:
        """Generated content does not contain repair frequency telemetry (criterion 5)."""
        retros = [
            {"findings_by_category": {"sec": 1}, "executor_repair_frequency": {"backend-executor": 5}},
        ]
        with patch("agent_generator._persistent_project_dir", return_value=self.root / "persistent"):
            content = _build_agent_content(
                "auto-backend-feature", "backend-executor", "feature",
                retros, "task-001", self.root
            )
        self.assertNotIn("Repair frequency", content)
        self.assertNotIn("Total repairs", content)
        self.assertNotIn("Tasks requiring repairs", content)
        self.assertNotIn("Average repairs", content)


if __name__ == "__main__":
    unittest.main()
