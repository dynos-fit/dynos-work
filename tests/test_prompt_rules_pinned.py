"""Executor and auditor prompts use the task's pinned prevention rules.

Field report: a rule one task's postmortem added (cq-647abf11dc58) reached a
different, already-running task and forced it to restructure code it had
already written. The deterministic rules check already read the task's
pinned ``rules-snapshot``; the prompt-building paths read the live shared
file. They now read the snapshot too.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

from lib_core import _persistent_project_dir  # noqa: E402
from lib_receipts import pin_rules_snapshot  # noqa: E402
from router import build_executor_plan, task_prevention_rules  # noqa: E402


def _rule(rule_id: str, text: str, executor: str = "all") -> dict:
    return {"rule_id": rule_id, "rule": text, "executor": executor, "category": "cq",
            "template": "advisory", "params": {}, "added_at": "2026-11-12T00:00:00Z"}


def _write_rules(root: Path, rules: list[dict]) -> None:
    persistent = _persistent_project_dir(root)
    persistent.mkdir(parents=True, exist_ok=True)
    (persistent / "prevention-rules.json").write_text(json.dumps({"rules": rules}))


def _task(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "project"
    td = root / ".dynos" / "task-20261112-001"
    td.mkdir(parents=True)
    (td / "manifest.json").write_text(json.dumps({"task_id": td.name, "stage": "EXECUTION"}))
    return root, td


def test_rule_added_mid_task_does_not_reach_that_task(tmp_path: Path) -> None:
    root, td = _task(tmp_path)
    _write_rules(root, [_rule("cq-old", "Keep functions short.")])
    pin_rules_snapshot(td)
    _write_rules(root, [
        _rule("cq-old", "Keep functions short."),
        _rule("cq-647abf11dc58", "Never suppress implementation_imports in lib/."),
        _rule("trend-1", "Watch N+1 queries.", executor="auditor-only"),
    ])

    assert [r["rule_id"] for r in task_prevention_rules(root, td)] == ["cq-old"]
    # No task context, or an unpinned legacy task, still sees the live file.
    assert len(task_prevention_rules(root, None)) == 3

    segments = [{"id": "seg-1", "executor": "backend-executor", "description": "x",
                 "files_expected": ["lib/a.py"]}]
    pinned_plan = json.dumps(build_executor_plan(root, "feature", segments, task_dir=td))
    live_plan = json.dumps(build_executor_plan(root, "feature", segments))
    assert "implementation_imports" not in pinned_plan
    assert "implementation_imports" in live_plan


def test_rules_for_all_executors_reach_executor_prompts(tmp_path: Path) -> None:
    """postmortem_analysis writes executor="all" for rules that name no
    executor; build_executor_plan used to match only "" or the exact name,
    so every such rule was silently dropped from executor prompts."""
    root, _ = _task(tmp_path)
    _write_rules(root, [
        _rule("cq-all", "Validate inputs at boundaries.", executor="all"),
        _rule("cq-ui", "Use design tokens.", executor="ui-executor"),
        _rule("trend", "Watch N+1.", executor="auditor-only"),
    ])
    segments = [{"id": "seg-1", "executor": "backend-executor", "description": "x", "files_expected": ["a.py"]}]
    rules = build_executor_plan(root, "feature", segments)["segments"][0]["prevention_rules"]
    texts = json.dumps(rules)
    assert "Validate inputs at boundaries." in texts
    assert "design tokens" not in texts and "N+1" not in texts
