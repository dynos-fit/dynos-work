"""AC 24: postmortem prevention-rule schema validation tests.

Each test calls `apply_analysis(task_dir, analysis)` with a fixture
analysis dict carrying a single prevention rule. We assert that:
  - rules with malformed templates / params are REJECTED, not merged
  - the returned dict's `rejected_count` correctly reflects rejections
  - `template == "advisory"` is accepted regardless of params shape
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))
sys.path.insert(0, str(ROOT / "memory"))

import postmortem_analysis  # noqa: E402
from postmortem_analysis import apply_analysis  # noqa: E402


def _make_task_dir(tmp_path: Path, dynos_home: Path, *, task_id: str = "task-test") -> Path:
    """Build the minimal directory shape apply_analysis expects.

    `apply_analysis` derives the project root from `task_dir.parent.parent`,
    then writes prevention-rules.json under
    `_persistent_project_dir(root)`. We point DYNOS_HOME at *dynos_home*
    so the persistent dir lands inside the test workspace.
    """
    project = tmp_path / "project"
    task_dir = project / ".dynos" / task_id
    task_dir.mkdir(parents=True)
    (task_dir / "task-retrospective.json").write_text(
        json.dumps({"task_id": task_id, "task_outcome": "done"})
    )
    return task_dir


def _read_persisted_rules(dynos_home: Path, project: Path) -> list[dict]:
    # Derive the slug through the production resolver (path-prefixed for a
    # non-git project dir) so we read the SAME persistent dir apply_analysis
    # wrote to. The legacy str-replace scheme no longer matches production.
    from lib_project_id import resolve_project_id

    slug = resolve_project_id(project)
    rules_path = dynos_home / "projects" / slug / "prevention-rules.json"
    if not rules_path.exists():
        return []
    return json.loads(rules_path.read_text()).get("rules", [])


def _analysis_with(rule: dict) -> dict:
    return {
        "summary": "fixture postmortem",
        "root_causes": [
            {
                "finding_category": "cq",
                "root_cause": "fixture root cause",
                "immediate_cause": "fixture immediate cause",
                "detection_failure": "fixture detection failure",
                "affected_executor": "all",
                "severity": "medium",
                "evidence": ["fixture evidence"],
            }
        ],
        "prevention_rules": [rule],
        "repair_failures": [],
        "model_suggestions": [],
        "hard_truth": "fixture hard truth",
    }


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "dynos-home"
    home.mkdir()
    monkeypatch.setenv("DYNOS_HOME", str(home))
    return home


# (a) Rule with template=every_name_in_X_satisfies_Y but missing predicate
#     → rejected, NOT merged, rejected_count == 1.
def test_missing_required_predicate_is_rejected(tmp_path, env, capsys):
    task_dir = _make_task_dir(tmp_path, env)
    project = task_dir.parent.parent
    bad_rule = {
        "rule": "every name in __all__ must be callable",
        "executor": "backend-executor",
        "category": "cq",
        "source_finding": "F1",
        "rationale": "prevent dead exports",
        "enforcement": "static-check",
        "template": "every_name_in_X_satisfies_Y",
        "params": {
            "module": "hooks.lib_receipts",
            "container": "__all__",
            # predicate omitted on purpose
        },
    }
    result = apply_analysis(task_dir, _analysis_with(bad_rule))
    assert result["rejected_count"] == 1, result
    assert result["rules_added"] == 0, result
    persisted = _read_persisted_rules(env, project)
    assert persisted == [], f"rejected rule must NOT be merged; got {persisted!r}"
    err = capsys.readouterr().err
    assert "REJECT" in err
    assert "every_name_in_X_satisfies_Y" in err


# (b) Rule with template=no_such_template → rejected.
def test_unknown_template_is_rejected(tmp_path, env, capsys):
    task_dir = _make_task_dir(tmp_path, env)
    project = task_dir.parent.parent
    bad_rule = {
        "rule": "this rule has a fictional template",
        "executor": "backend-executor",
        "category": "cq",
        "source_finding": "F2",
        "rationale": "should never be merged",
        "enforcement": "prompt-constraint",
        "template": "no_such_template",
        "params": {},
    }
    result = apply_analysis(task_dir, _analysis_with(bad_rule))
    assert result["rejected_count"] == 1
    assert result["rules_added"] == 0
    assert _read_persisted_rules(env, project) == []
    err = capsys.readouterr().err
    assert "REJECT" in err
    assert "no_such_template" in err


# (c) Valid rule → accepted and merged.
def test_valid_rule_is_accepted_and_merged(tmp_path, env):
    task_dir = _make_task_dir(tmp_path, env)
    project = task_dir.parent.parent
    good_rule = {
        "rule": "do not call time.time() in throttled paths",
        "executor": "backend-executor",
        "category": "cq",
        "source_finding": "F3",
        "rationale": "monotonic clock required",
        "enforcement": "static-check",
        "template": "pattern_must_not_appear",
        "params": {"regex": r"\btime\.time\(\)", "scope": "hooks/*.py"},
    }
    result = apply_analysis(task_dir, _analysis_with(good_rule))
    assert result["rejected_count"] == 0
    assert result["rules_added"] == 1
    persisted = _read_persisted_rules(env, project)
    assert len(persisted) == 1
    p = persisted[0]
    assert p["template"] == "pattern_must_not_appear"
    assert p["params"]["regex"] == r"\btime\.time\(\)"
    assert p["params"]["scope"] == "hooks/*.py"
    assert p["rule"] == good_rule["rule"]


# (d) Rule with template=advisory always accepted regardless of params shape.
def test_advisory_rule_accepted_with_any_params_shape(tmp_path, env):
    task_dir = _make_task_dir(tmp_path, env)
    project = task_dir.parent.parent
    # Empty params dict — accepted.
    rule_a = {
        "rule": "be more careful around CPU-bound work",
        "executor": "all",
        "category": "process",
        "source_finding": "F4a",
        "rationale": "process-level reminder",
        "enforcement": "review-checklist",
        "template": "advisory",
        "params": {},
    }
    r1 = apply_analysis(task_dir, _analysis_with(rule_a))
    assert r1["rejected_count"] == 0
    assert r1["rules_added"] == 1
    persisted = _read_persisted_rules(env, project)
    assert len(persisted) == 1
    assert persisted[0]["template"] == "advisory"

    # Now add a SECOND advisory rule with wholly arbitrary keys in params.
    # Advisory has no required keys; arbitrary params must not cause
    # rejection.
    rule_b = {
        "rule": "review db migrations against rollback plan",
        "executor": "db-executor",
        "category": "db",
        "source_finding": "F4b",
        "rationale": "process-level db reminder",
        "enforcement": "review-checklist",
        "template": "advisory",
        "params": {"freeform_note": "ad hoc bag", "complexity": 9000},
    }
    r2 = apply_analysis(task_dir, _analysis_with(rule_b))
    assert r2["rejected_count"] == 0
    assert r2["rules_added"] == 1
    persisted = _read_persisted_rules(env, project)
    assert len(persisted) == 2
    assert {p["template"] for p in persisted} == {"advisory"}
