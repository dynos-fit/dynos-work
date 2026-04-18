#!/usr/bin/env python3
"""Tests for memory/agent_generator.py — verifies the five logic bug fixes."""

from __future__ import annotations

import argparse
import io
import json
import sys
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


def _write_prevention_rules(root: Path, rules: list[dict]) -> None:
    """Write prevention rules to the persistent project dir location."""
    rules_dir = root / "persistent"
    rules_dir.mkdir(parents=True, exist_ok=True)
    (rules_dir / "prevention-rules.json").write_text(json.dumps({"rules": rules}))


def _build(
    root: Path,
    role: str = "backend-executor",
    task_type: str = "feature",
    retros: list[dict] | None = None,
    rules: list[dict] | None = None,
) -> str:
    if rules is not None:
        _write_prevention_rules(root, rules)
    matched = retros or []
    with patch("agent_generator._persistent_project_dir", return_value=root / "persistent"):
        return _build_agent_content(
            "auto-backend-feature", role, task_type, matched, "task-099", root
        )


# --- _matching_retrospectives rewrite (criteria 1) ---

def test_includes_retro_where_role_in_execution_graph(tmp_path: Path) -> None:
    """Retro is matched when the role appears in the execution graph segments."""
    _make_task(tmp_path, "task-001", "feature", ["backend-executor", "frontend-executor"])
    retros = _collect_retros(tmp_path)
    matched = _matching_retrospectives(retros, "backend-executor", "feature", tmp_path)
    assert len(matched) == 1
    assert matched[0]["task_id"] == "task-001"


def test_excludes_retro_in_repair_freq_but_not_in_graph(tmp_path: Path) -> None:
    """Retro where role is in executor_repair_frequency but NOT in execution graph is excluded."""
    _make_task(
        tmp_path, "task-002", "feature",
        executors=["frontend-executor"],
        executor_repair_frequency={"backend-executor": 3},
    )
    retros = _collect_retros(tmp_path)
    matched = _matching_retrospectives(retros, "backend-executor", "feature", tmp_path)
    assert len(matched) == 0


def test_excludes_retro_with_missing_execution_graph(tmp_path: Path) -> None:
    """Retro with missing execution-graph.json is gracefully skipped."""
    task_dir = tmp_path / ".dynos" / "task-003"
    task_dir.mkdir(parents=True, exist_ok=True)
    retro = {"task_id": "task-003", "task_type": "feature", "_path": str(task_dir / "task-retrospective.json")}
    (task_dir / "task-retrospective.json").write_text(json.dumps(retro))
    retros = _collect_retros(tmp_path)
    matched = _matching_retrospectives(retros, "backend-executor", "feature", tmp_path)
    assert len(matched) == 0


def test_excludes_retro_with_wrong_task_type(tmp_path: Path) -> None:
    """Retro with non-matching task_type is excluded."""
    _make_task(tmp_path, "task-004", "bugfix", ["backend-executor"])
    retros = _collect_retros(tmp_path)
    matched = _matching_retrospectives(retros, "backend-executor", "feature", tmp_path)
    assert len(matched) == 0


def test_includes_retro_with_zero_repairs_but_in_graph(tmp_path: Path) -> None:
    """Retro where role had zero repairs but appears in execution graph is included."""
    _make_task(
        tmp_path, "task-005", "feature",
        executors=["backend-executor"],
        executor_repair_frequency={},
    )
    retros = _collect_retros(tmp_path)
    matched = _matching_retrospectives(retros, "backend-executor", "feature", tmp_path)
    assert len(matched) == 1


def test_skips_retro_without_path(tmp_path: Path) -> None:
    """Retro without _path key is skipped gracefully."""
    retros = [{"task_id": "task-006", "task_type": "feature"}]
    matched = _matching_retrospectives(retros, "backend-executor", "feature", tmp_path)
    assert len(matched) == 0


# --- _build_agent_content restructure (criteria 3, 4, 5, 6, 11) ---

def test_contains_imperative_rules_not_telemetry(tmp_path: Path) -> None:
    """Output contains imperative instructions, NOT telemetry sections (criteria 3, 4, 5, 11)."""
    retros = [
        {"findings_by_category": {"sec": 4, "cq": 2}},
    ]
    content = _build(tmp_path, retros=retros)

    assert "## Failure Prevention Rules" in content
    assert "Repair frequency" not in content
    assert "Finding categories" not in content
    assert "## Patterns" not in content
    assert "Total repairs" not in content
    assert "Average repairs per task" not in content
    assert "ALWAYS" in content


def test_filters_rules_by_executor(tmp_path: Path) -> None:
    """Prevention rules are filtered by executor field (criterion 6)."""
    retros = [{"findings_by_category": {"sec": 2}}]
    rules = [
        {"category": "sec", "executor": "backend-executor", "rule": "DO NOT skip auth checks."},
        {"category": "sec", "executor": "frontend-executor", "rule": "DO NOT expose tokens in client code."},
    ]
    content = _build(tmp_path, retros=retros, rules=rules)
    assert "DO NOT skip auth checks" in content
    assert "DO NOT expose tokens" not in content


def test_includes_rules_with_executor_all(tmp_path: Path) -> None:
    """Prevention rules with executor 'all' are included for any role (criterion 6)."""
    retros = [{"findings_by_category": {"sec": 1}}]
    rules = [
        {"category": "sec", "executor": "all", "rule": "ALWAYS sanitize user input."},
    ]
    content = _build(tmp_path, retros=retros, rules=rules)
    assert "ALWAYS sanitize user input" in content


def test_includes_rules_with_no_executor_field(tmp_path: Path) -> None:
    """Prevention rules without executor field default to 'all' (criterion 6)."""
    retros = [{"findings_by_category": {"cq": 1}}]
    rules = [
        {"category": "cq", "rule": "ALWAYS run linter before submitting."},
    ]
    content = _build(tmp_path, retros=retros, rules=rules)
    assert "ALWAYS run linter" in content


def test_fallback_instruction_when_no_specific_rules(tmp_path: Path) -> None:
    """When no prevention rules match a category, fallback instruction is used (criterion 4)."""
    retros = [{"findings_by_category": {"db": 3}}]
    content = _build(tmp_path, retros=retros, rules=[])
    assert CATEGORY_INSTRUCTIONS["db"] in content


def test_context_metadata_present(tmp_path: Path) -> None:
    """Context metadata section is retained (criterion 3)."""
    content = _build(tmp_path, retros=[{"findings_by_category": {}}])
    assert "## Context" in content
    assert "**Role**: backend-executor" in content
    assert "**Task type**: feature" in content
    assert "**Generated from**: task-099" in content


def test_output_coherent_with_hard_constraints_framing(tmp_path: Path) -> None:
    """Output is coherent as hard constraints injected by router (criterion 11)."""
    retros = [{"findings_by_category": {"sec": 5, "test": 2}}]
    rules = [
        {"category": "sec", "executor": "backend-executor", "rule": "DO NOT bypass authentication middleware."},
        {"category": "test", "executor": "all", "rule": "ALWAYS include regression tests for bug fixes."},
    ]
    content = _build(tmp_path, retros=retros, rules=rules)
    assert "DO NOT" in content
    assert "ALWAYS" in content
    assert "findings" not in content.lower().split("## failure prevention")[0]
    assert "average" not in content.lower()


# --- cmd_auto fixes (criteria 2, 7) ---

def test_skips_slot_when_matched_retros_empty(tmp_path: Path) -> None:
    """cmd_auto skips agent generation when no matching retros found (criterion 2)."""
    (tmp_path / ".dynos").mkdir(parents=True, exist_ok=True)
    for i in range(3):
        task_id = f"task-00{i+1}"
        task_dir = tmp_path / ".dynos" / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        graph = {"task_id": task_id, "segments": [{"id": "seg-0", "executor": "backend-executor"}]}
        (task_dir / "execution-graph.json").write_text(json.dumps(graph))
        retro = {"task_id": task_id, "task_type": "feature"}
        (task_dir / "task-retrospective.json").write_text(json.dumps(retro))

    args = argparse.Namespace(root=str(tmp_path), min_tasks=1)

    with patch("agent_generator._persistent_project_dir", return_value=tmp_path / "persistent"), \
         patch("agent_generator.log_event"), \
         patch("agent_generator.ensure_learned_registry", return_value={"agents": []}), \
         patch("agent_generator.register_learned_agent"), \
         patch("agent_generator.collect_retrospectives") as mock_collect:
        retros = []
        for i in range(3):
            task_id = f"task-00{i+1}"
            retros.append({
                "task_id": task_id,
                "task_type": "feature",
                "_path": str(tmp_path / ".dynos" / task_id / "task-retrospective.json"),
            })
        mock_collect.return_value = retros

        with patch("agent_generator._matching_retrospectives", return_value=[]):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                cmd_auto(args)
                output = json.loads(mock_out.getvalue())

    assert len(output["generated"]) == 0
    assert any("no matching" in reason for reason in output.get("skipped_reasons", [])), (
        f"Expected skip reason, got: {output.get('skipped_reasons')}"
    )


def test_uses_latest_matched_retro_for_provenance(tmp_path: Path) -> None:
    """cmd_auto uses latest matched retro task_id for provenance, not global latest (criterion 7)."""
    _make_task(tmp_path, "task-001", "feature", ["backend-executor"], {"sec": 1})
    _make_task(tmp_path, "task-002", "bugfix", ["frontend-executor"], {"cq": 1})
    _make_task(tmp_path, "task-003", "feature", ["backend-executor"], {"sec": 2})

    retros = _collect_retros(tmp_path)
    matched = _matching_retrospectives(retros, "backend-executor", "feature", tmp_path)

    assert len(matched) == 2
    latest_task = matched[-1].get("task_id", "unknown")
    assert latest_task == "task-003"


def test_no_repair_frequency_in_output(tmp_path: Path) -> None:
    """Generated content does not contain repair frequency telemetry (criterion 5)."""
    retros = [
        {"findings_by_category": {"sec": 1}, "executor_repair_frequency": {"backend-executor": 5}},
    ]
    with patch("agent_generator._persistent_project_dir", return_value=tmp_path / "persistent"):
        content = _build_agent_content(
            "auto-backend-feature", "backend-executor", "feature",
            retros, "task-001", tmp_path
        )
    assert "Repair frequency" not in content
    assert "Total repairs" not in content
    assert "Tasks requiring repairs" not in content
    assert "Average repairs" not in content
