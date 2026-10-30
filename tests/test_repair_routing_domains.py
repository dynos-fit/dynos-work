"""Repair routing follows the files to modify and the finding's severity.

Field cases:
- A blocking .tsx defect from code-quality-auditor went to refactor-executor
  on the fast tier: the "cq" action space has no ui-executor, unseen states
  picked at random, and the model was drawn over every tier.
- Q-values learned on unrelated tasks (``perf:major:feature:0`` preferring
  observability-executor) steered routing because the state key ignored
  which files were being repaired.
- A merged repair task kept its lead finding's executor and model.
- A docs-only finding was scoped to a migration file its evidence cited.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))
sys.path.insert(0, str(ROOT / "memory"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

from ctl import _merge_overlapping_repair_tasks, _repair_files_for_finding  # noqa: E402
from memory import lib_qlearn  # noqa: E402
from memory.lib_qlearn import (  # noqa: E402
    allowed_executors,
    build_repair_plan,
    encode_repair_state,
    file_domain,
    files_domain,
)
from test_field_report_7_5_31 import _project  # noqa: E402
from test_repair_findings_ensemble import DEEP, _report, _run_ctl, _setup, _single_entry  # noqa: E402

FAST_MODEL = "haiku"  # noqa: model-literal


# ---------------------------------------------------------------------------
# File domains and the allowed executor set
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path, domain", [
    ("web/app.tsx", "ui"),
    ("src/components/Button.jsx", "ui"),
    ("styles/site.css", "ui"),
    ("app/migrations/0004_add_index.py", "db"),
    ("db/schema.sql", "db"),
    ("docs/guide.md", "docs"),
    ("README.md", "docs"),
    ("tests/test_a.py", "test"),
    ("web/app.test.tsx", "test"),
    ("src/a.py", "code"),
    ("uv.lock", "infra"),
    (".github/workflows/ci.yml", "infra"),
    ("assets/logo.png", "other"),
])
def test_file_domain(path: str, domain: str) -> None:
    assert file_domain(path) == domain


def test_files_domain_is_order_independent() -> None:
    assert files_domain(["web/app.tsx", "src/a.py"]) == files_domain(["src/a.py", "web/app.tsx"]) == "code+ui"
    assert files_domain([]) is None


def test_ui_file_restricts_code_quality_to_ui_executor() -> None:
    # cq's action space has no ui-executor; the files win.
    assert allowed_executors("cq", ["web/app.tsx"]) == ["ui-executor"]


def test_code_file_intersects_with_category() -> None:
    assert allowed_executors("cq", ["src/a.py"]) == ["backend-executor", "refactor-executor"]
    assert allowed_executors("cq", ["tests/test_a.py"]) == ["testing-executor"]
    assert allowed_executors("db", ["app/migrations/0001.py"]) == ["db-executor", "data-executor"]


def test_no_files_or_unknown_files_keep_the_category_space() -> None:
    cq_space = lib_qlearn._DEFAULT_ACTION_SPACE["cq"]
    assert allowed_executors("cq", []) == cq_space
    assert allowed_executors("cq", ["assets/logo.png"]) == cq_space


def test_state_key_carries_the_domain_after_severity() -> None:
    assert encode_repair_state("cq", "high", "feature", 0, "ui") == "cq:high:feature:0:ui"
    assert encode_repair_state("cq", "high", "feature", 0) == "cq:high:feature:0"


# ---------------------------------------------------------------------------
# build_repair_plan: domain, determinism, severity floor
# ---------------------------------------------------------------------------

def _finding(fid: str, severity: str, auditor: str, files: list[str], blocking: bool, retry: int = 0) -> dict:
    return {"id": fid, "severity": severity, "auditor": auditor, "files": files,
            "blocking": blocking, "retry_count": retry}


def _plan(tmp_path: Path, findings: list[dict], epsilon: float, tables: dict | None = None) -> list[dict]:
    tables = tables or {}
    with mock.patch.object(lib_qlearn, "project_policy",
                           return_value={"repair_qlearning": True, "repair_epsilon": epsilon}), \
         mock.patch.object(lib_qlearn, "_has_learned_agent", return_value=(False, None, None)), \
         mock.patch.object(lib_qlearn, "_resolve_host", return_value="claude"), \
         mock.patch.object(lib_qlearn, "load_q_table",
                           side_effect=lambda root, name: tables.get(name, {"entries": {}})), \
         mock.patch.dict(os.environ, {"DYNOS_HOME": str(tmp_path)}):
        return build_repair_plan(tmp_path, findings, "feature")["assignments"]


def test_blocking_tsx_finding_is_deterministic_ui_on_a_non_fast_tier(tmp_path: Path) -> None:
    finding = _finding("CQ-1", "major", "code-quality-auditor", ["web/app.tsx"], blocking=True)
    seen = set()
    for _ in range(30):
        (a,) = _plan(tmp_path, [finding], epsilon=1.0)
        seen.add((a["assigned_executor"], a.get("model_override"), a["executor_source"], a["model_source"]))
    assert seen == {("ui-executor", "sonnet", "q-default", "q-default")}  # noqa: model-literal
    assert a["state"] == "cq:high:feature:0:ui"


def test_non_blocking_finding_still_explores_within_the_allowed_set(tmp_path: Path) -> None:
    finding = _finding("CQ-1", "minor", "code-quality-auditor", ["src/a.py"], blocking=False)
    executors = {_plan(tmp_path, [finding], epsilon=1.0)[0]["assigned_executor"] for _ in range(40)}
    assert executors <= {"backend-executor", "refactor-executor"}


def test_serious_finding_never_gets_the_fast_tier_even_when_learned(tmp_path: Path) -> None:
    state = "cq:high:feature:0:code:backend-executor:generic"
    tables = {
        "executor": {"entries": {"cq:high:feature:0:code": {"backend-executor": 0.9}}},
        "model": {"entries": {state: {"fast": 0.99, "balanced": 0.1}}},
    }
    for severity, blocking in (("major", False), ("critical", False), ("high", True)):
        for _ in range(20):
            (a,) = _plan(tmp_path, [_finding("CQ-1", severity, "code-quality-auditor", ["src/a.py"], blocking)],
                         epsilon=1.0 if not blocking else 0.15, tables=tables)
            assert a.get("model_override") != FAST_MODEL


def test_minor_finding_may_use_the_fast_tier(tmp_path: Path) -> None:
    tables = {
        "executor": {"entries": {"cq:low:feature:0:code": {"backend-executor": 0.9}}},
        "model": {"entries": {"cq:low:feature:0:code:backend-executor:generic": {"fast": 0.9}}},
    }
    (a,) = _plan(tmp_path, [_finding("CQ-1", "minor", "code-quality-auditor", ["src/a.py"], False)],
                 epsilon=0.0, tables=tables)
    assert a["model_override"] == FAST_MODEL


def test_q_values_from_another_domain_do_not_steer(tmp_path: Path) -> None:
    # Learned on an unrelated task without a domain: prefers observability.
    tables = {"executor": {"entries": {"perf:high:feature:0": {"observability-executor": 0.9}}}}
    (a,) = _plan(tmp_path, [_finding("PERF-1", "major", "performance-auditor", ["src/a.py"], True)],
                 epsilon=0.0, tables=tables)
    assert a["assigned_executor"] == "backend-executor"
    assert a["state"] == "perf:high:feature:0:code"


def test_learned_values_within_the_domain_are_used(tmp_path: Path) -> None:
    tables = {"executor": {"entries": {"perf:high:feature:0:code": {"observability-executor": 0.9}}}}
    (a,) = _plan(tmp_path, [_finding("PERF-1", "major", "performance-auditor", ["src/a.py"], True)],
                 epsilon=0.0, tables=tables)
    assert a["assigned_executor"] == "observability-executor"
    assert a["executor_source"] == "q-learning"


# ---------------------------------------------------------------------------
# Merging drops the lead's routing
# ---------------------------------------------------------------------------

def test_merge_drops_inherited_routing_and_keeps_blocking() -> None:
    lead = {"finding_id": "SEC-1", "auditor": "security-auditor", "severity": "high",
            "assigned_executor": "backend-executor", "instruction": "a", "files_to_modify": ["src/a.py"],
            "retry_count": 0, "model_override": FAST_MODEL, "model_source": "q-learning",
            "state": "sec:high:feature:0", "_blocking": False}
    other = {"finding_id": "CQ-1", "auditor": "code-quality-auditor", "severity": "low",
             "assigned_executor": "refactor-executor", "instruction": "b",
             "files_to_modify": ["src/a.py", "web/app.tsx"], "retry_count": 1, "_blocking": True}
    (merged,) = _merge_overlapping_repair_tasks([lead, other])
    assert merged["finding_id"] == "SEC-1"
    assert "model_override" not in merged and "state" not in merged
    assert merged["_blocking"] is True
    assert merged["files_to_modify"] == ["src/a.py", "web/app.tsx"]


# ---------------------------------------------------------------------------
# run-repair-log-build end to end
# ---------------------------------------------------------------------------

def _build(td: Path, tmp_path: Path) -> tuple[dict, list[dict]]:
    env = {"DYNOS_HOME": str(tmp_path / "dynos-home")}
    assert _run_ctl("run-audit-repair-cycle-plan", str(td), env=env).returncode == 0
    build = _run_ctl("run-repair-log-build", str(td), env=env)
    assert build.returncode == 0, build.stdout + build.stderr
    tasks = [t for b in json.loads((td / "repair-log.json").read_text())["batches"] for t in b["tasks"]]
    return json.loads(build.stdout), tasks


def test_build_routes_a_blocking_tsx_quality_finding_to_ui(tmp_path: Path) -> None:
    td = _setup(tmp_path, [_single_entry("code-quality-auditor")], stage="REPAIR_PLANNING")
    _project(td)
    _report(td, "code-quality-auditor", DEEP, [
        {"id": "CQ-1", "severity": "major", "blocking": True, "location": "web/app.tsx:4",
         "description": "effect re-subscribes on every render"},
    ])
    for _ in range(5):
        _, tasks = _build(td, tmp_path)
        (task,) = tasks
        assert task["assigned_executor"] == "ui-executor"
        assert task.get("model_override") != FAST_MODEL
        assert task["state"].endswith(":ui")


def test_build_routes_merged_cluster_on_union_files_and_reports_merges(tmp_path: Path) -> None:
    td = _setup(tmp_path, [_single_entry("code-quality-auditor"), _single_entry("dead-code-auditor")],
                stage="REPAIR_PLANNING")
    _project(td)
    # The dead-code finding leads (critical) and its own action space is
    # refactor-only; the cluster also touches a .tsx file.
    _report(td, "dead-code-auditor", DEEP, [
        {"id": "DC-1", "severity": "critical", "blocking": True, "location": "src/a.py:1"},
    ])
    _report(td, "code-quality-auditor", DEEP, [
        {"id": "CQ-1", "severity": "minor", "blocking": True, "location": "src/a.py:2, web/app.tsx:3"},
    ])
    payload, tasks = _build(td, tmp_path)
    (task,) = tasks
    assert task["finding_id"] == "DC-1"
    assert sorted(task["files_to_modify"]) == ["src/a.py", "web/app.tsx"]
    assert task["state"].endswith(":code+ui")
    assert task.get("model_override") != FAST_MODEL
    assert payload["merged_finding_ids"] == {"code-quality-auditor:CQ-1": "dead-code-auditor:DC-1"}


# ---------------------------------------------------------------------------
# Prose paths: evidence is not a repair target
# ---------------------------------------------------------------------------

def test_path_cited_only_in_evidence_is_not_a_target(tmp_path: Path) -> None:
    td = _setup(tmp_path, [])
    root = _project(td)
    (root / "docs").mkdir()
    (root / "docs" / "guide.md").write_text("x\n")
    (root / "app" / "migrations").mkdir(parents=True)
    (root / "app" / "migrations" / "0004_add_index.py").write_text("x\n")
    finding = {
        "id": "DOC-1",
        "location": "",
        "description": "The guide documents the wrong default.",
        "evidence": "The default is set in app/migrations/0004_add_index.py line 12.",
        "recommendation": "Update docs/guide.md to state the real default.",
    }
    files, _ = _repair_files_for_finding(td, finding, None, "docs-executor")
    assert files == ["docs/guide.md"]


def test_evidence_only_path_falls_back_instead_of_scoping_to_it(tmp_path: Path) -> None:
    td = _setup(tmp_path, [])
    root = _project(td)
    (root / "app" / "migrations").mkdir(parents=True)
    (root / "app" / "migrations" / "0004_add_index.py").write_text("x\n")
    finding = {
        "id": "DOC-2",
        "location": "",
        "description": "The setup guide is out of date.",
        "evidence": "See app/migrations/0004_add_index.py.",
    }
    files, _ = _repair_files_for_finding(td, finding, None, "ui-executor")
    assert "app/migrations/0004_add_index.py" not in files
