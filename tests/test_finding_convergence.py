"""Single-auditor re-audit convergence.

A single (non-ensemble) auditor reported a new variant of the same issue
class on every re-audit (board query -> deal list -> indexes -> migrations),
and each variant restarted the per-finding retry count, so repair never
converged. Covered here:

1. re-audit prompts carry the auditor's prior findings, their repairs and
   the accepted findings, and drop the "find something" output pressure;
2. ``ctl accept-finding`` takes a finding (and its reworded variants) out
   of the blocking set, and agents cannot forge the acceptance file;
3. reworded variants inherit retry counts, and a per-auditor repair-cycle
   cap makes ``run-repair-retry`` return ``escalation_required``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

AUDITOR = "performance-auditor"


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return {**os.environ, "DYNOS_HOME": str(tmp_path / "dynos-home"), "PYTHONPATH": str(ROOT / "hooks")}


def _task(tmp_path: Path, stage: str = "CHECKPOINT_AUDIT") -> Path:
    td = tmp_path / "project" / ".dynos" / "task-20261030-001"
    td.mkdir(parents=True)
    (td / "manifest.json").write_text(json.dumps({
        "task_id": td.name,
        "created_at": "2026-10-30T00:00:00Z",
        "raw_input": "Speed up the board",
        "stage": stage,
        "classification": {"type": "feature", "domains": ["backend"], "risk_level": "medium"},
        "retry_counts": {},
    }))
    return td


def _report(td: Path, findings: list[dict], auditor: str = AUDITOR) -> None:
    reports = td / "audit-reports"
    reports.mkdir(exist_ok=True)
    (reports / f"{auditor}.json").write_text(json.dumps({"auditor_name": auditor, "findings": findings}))


def _ctl(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "ctl.py"), *args],
        capture_output=True, text=True, env=env, cwd=str(ROOT), check=False,
    )


def _repair_log(td: Path, cycle: int, tasks: list[dict]) -> None:
    (td / "repair-log.json").write_text(json.dumps({
        "repair_cycle": cycle,
        "batches": [{"batch_id": "batch-1", "tasks": tasks}],
    }))


PERF_1 = {"id": "PERF-1", "blocking": True, "severity": "high", "category": "performance",
          "title": "N+1 query in board loader", "location": "app/board.py:40"}


# --- 1. re-audit context -----------------------------------------------------

def _inject(env: dict[str, str], td: Path) -> str:
    (td / "audit-plan.json").write_text(json.dumps({
        "auditors": [{"name": AUDITOR, "route_mode": "generic", "agent_path": None}],
    }))
    proc = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "router.py"), "audit-inject-prompt",
         "--root", str(td.parent.parent), "--task-type", "feature",
         "--audit-plan", str(td / "audit-plan.json"), "--auditor-name", AUDITOR],
        input="base prompt", capture_output=True, text=True, env=env, cwd=str(ROOT), check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


def test_first_audit_prompt_is_unchanged(tmp_path: Path, env):
    td = _task(tmp_path)
    out = _inject(env, td)
    assert "## Re-Audit Context" not in out
    assert "Output pressure: if you report no blocking findings" in out


def test_reaudit_prompt_lists_prior_findings_repairs_and_accepted(tmp_path: Path, env):
    td = _task(tmp_path)
    second = {"id": "PERF-2", "blocking": True, "severity": "medium", "category": "performance",
              "title": "Unbounded deal list", "location": "app/deals.py:12"}
    _report(td, [PERF_1, second])
    accepted = _ctl(env, "accept-finding", str(td), "--auditor", AUDITOR, "--finding-id", "PERF-2",
                    "--reason", "list is capped upstream", "--decision-ref", "ADR-7")
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    _repair_log(td, 1, [{"finding_id": "PERF-1", "auditor": AUDITOR, "assigned_executor": "db-executor",
                         "files_to_modify": ["app/board.py"], "retry_count": 0,
                         "instruction": "Resolve finding PERF-1: batch the board query"}])

    out = _inject(env, td)
    assert "## Re-Audit Context" in out
    assert "PERF-1 [high] app/board.py: N+1 query in board loader" in out
    assert "db-executor" in out and "batch the board query" in out
    assert "Accepted findings (do not re-raise)" in out
    assert "list is capped upstream" in out and "ADR-7" in out
    assert "WHOLE class of an issue as ONE finding" in out
    assert "only if the fix is actually wrong" in out
    assert "Output pressure: if you report no blocking findings" not in out
    assert "Re-audit focus" in out


# --- 2. accept-finding ------------------------------------------------------

def test_accept_finding_validates_input(tmp_path: Path, env):
    td = _task(tmp_path)
    _report(td, [PERF_1])
    no_reason = _ctl(env, "accept-finding", str(td), "--auditor", AUDITOR, "--finding-id", "PERF-1", "--reason", " ")
    assert no_reason.returncode == 1 and "--reason" in no_reason.stderr
    unknown = _ctl(env, "accept-finding", str(td), "--auditor", AUDITOR, "--finding-id", "PERF-9", "--reason", "x")
    assert unknown.returncode == 1 and "no finding" in unknown.stderr
    wrong_auditor = _ctl(env, "accept-finding", str(td), "--auditor", "security-auditor",
                         "--finding-id", "PERF-1", "--reason", "x")
    assert wrong_auditor.returncode == 1
    assert not (td / "accepted-findings.json").exists()


def test_accepted_finding_leaves_the_blocking_set(tmp_path: Path, env):
    td = _task(tmp_path)
    _report(td, [PERF_1])
    gate = json.loads(_ctl(env, "run-audit-findings-gate", str(td)).stdout)
    assert gate["status"] == "repair_required"

    ok = _ctl(env, "accept-finding", str(td), "--auditor", AUDITOR, "--finding-id", "PERF-1",
              "--reason", "query runs on a 50-row table")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    stored = json.loads((td / "accepted-findings.json").read_text())["accepted"][0]
    assert stored["file"] == "app/board.py" and stored["category"] == "performance"
    events = (td / "events.jsonl").read_text()
    assert "finding_accepted" in events

    gate = json.loads(_ctl(env, "run-audit-findings-gate", str(td)).stdout)
    assert gate["status"] == "clear"
    assert gate["accepted_findings"] == [{
        "auditor": AUDITOR, "id": "PERF-1", "reason": "query runs on a 50-row table",
        "decision_ref": None, "matched_by": "id",
    }]


def test_reworded_variant_of_accepted_finding_is_accepted(tmp_path: Path, env):
    from lib_validate import collect_repair_findings

    td = _task(tmp_path)
    _report(td, [PERF_1])
    assert _ctl(env, "accept-finding", str(td), "--auditor", AUDITOR, "--finding-id", "PERF-1",
                "--reason", "known trade-off").returncode == 0
    variant = {"id": "PERF-4", "blocking": True, "severity": "high", "category": "performance",
               "title": "Board loader issues one query per column", "location": "app/board.py:77"}
    other_file = {"id": "PERF-5", "blocking": True, "severity": "high", "category": "performance",
                  "title": "Missing index", "location": "db/migrations/0042.sql:3"}
    _report(td, [variant, other_file])

    by_id = {f["id"]: f for f in collect_repair_findings(td)["findings"]}
    assert by_id["PERF-4"]["accepted"]["matched_by"] == "class_and_file"
    assert by_id["PERF-4"]["blocking"] is False
    assert "accepted" not in by_id["PERF-5"]
    gate = json.loads(_ctl(env, "run-audit-findings-gate", str(td)).stdout)
    assert gate["blocking_finding_ids"] == ["PERF-5"]


def test_agents_cannot_write_the_acceptance_or_cycle_plan_files(tmp_path: Path):
    from write_policy import WriteAttempt, decide_write

    td = _task(tmp_path)
    for role in ("backend-executor", "audit-performance", "repair-coordinator", "orchestrator"):
        decision = decide_write(WriteAttempt(role=role, task_dir=td, path=td / "repair-cycle-plan.json",
                                             operation="create", source="agent"))
        assert decision.allowed is False, role
    for role in ("backend-executor", "audit-performance", "repair-coordinator"):
        decision = decide_write(WriteAttempt(role=role, task_dir=td, path=td / "accepted-findings.json",
                                             operation="create", source="agent"))
        assert decision.allowed is False
        assert "accept-finding" in (decision.wrapper_command or decision.reason)


# --- 3. convergence caps ----------------------------------------------------

def test_reworded_variant_inherits_retry_count(tmp_path: Path, env):
    td = _task(tmp_path, stage="REPAIR_PLANNING")
    # Cycle 1 repaired PERF-1 at retry 1; the plan that queued it is on disk.
    (td / "repair-cycle-plan.json").write_text(json.dumps({
        "repair_cycle": 1,
        "blocking_findings": [{**PERF_1, "_auditor": AUDITOR, "retry_count": 1}],
        "auditor_repair_cycles": {AUDITOR: [1]},
    }))
    _repair_log(td, 1, [{"finding_id": "PERF-1", "auditor": AUDITOR, "assigned_executor": "db-executor",
                         "files_to_modify": ["app/board.py"], "retry_count": 1}])
    variant = {"id": "PERF-2", "blocking": True, "severity": "high", "category": "performance",
               "title": "Board loader still N+1 for archived deals", "location": "app/board.py:88"}
    unrelated = {"id": "PERF-3", "blocking": True, "severity": "high", "category": "performance",
                 "title": "Slow export", "location": "app/export.py:5"}
    _report(td, [variant, unrelated])

    result = _ctl(env, "run-audit-repair-cycle-plan", str(td))
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    by_id = {f["id"]: f for f in payload["blocking_findings"]}
    assert by_id["PERF-2"]["retry_count"] == 2
    assert by_id["PERF-3"]["retry_count"] == 0
    assert payload["auditor_repair_cycles"] == {AUDITOR: [1, 2]}

    # Re-planning the same upcoming cycle is idempotent.
    again = json.loads(_ctl(env, "run-audit-repair-cycle-plan", str(td)).stdout)
    assert {f["id"]: f["retry_count"] for f in again["blocking_findings"]} == {"PERF-2": 2, "PERF-3": 0}
    assert again["auditor_repair_cycles"] == {AUDITOR: [1, 2]}


def _exhausted_cycles(td: Path, cycles: list[int], retry: int = 0) -> None:
    (td / "repair-cycle-plan.json").write_text(json.dumps({
        "repair_cycle": cycles[-1],
        "blocking_findings": [{**PERF_1, "_auditor": AUDITOR, "retry_count": retry}],
        "auditor_repair_cycles": {AUDITOR: cycles},
    }))
    _repair_log(td, cycles[-1], [{"finding_id": "PERF-1", "auditor": AUDITOR,
                                  "assigned_executor": "db-executor",
                                  "files_to_modify": ["app/board.py"], "retry_count": retry}])


def test_repair_retry_escalates_on_auditor_cycle_cap(tmp_path: Path, env):
    td = _task(tmp_path, stage="REPAIR_EXECUTION")
    _exhausted_cycles(td, [1, 2, 3])
    _report(td, [{"id": "PERF-7", "blocking": True, "severity": "high", "category": "migrations",
                  "title": "Migration locks table", "location": "db/migrations/0050.sql:1"}])

    result = _ctl(env, "run-repair-retry", str(td))
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "escalation_required"
    assert "auditor cycle cap" in payload["error"] and AUDITOR in payload["error"]
    assert "accept-finding" in payload["hint"]
    assert json.loads((td / "manifest.json").read_text())["stage"] == "REPAIR_EXECUTION"


def test_repair_retry_escalates_when_variant_exhausts_retry_cap(tmp_path: Path, env):
    td = _task(tmp_path, stage="REPAIR_EXECUTION")
    _exhausted_cycles(td, [1], retry=3)
    # The same defect, reworded under a new id: the old id's retry 3 applies.
    _report(td, [{"id": "PERF-8", "blocking": True, "severity": "high", "category": "performance",
                  "title": "Board N+1 again", "location": "app/board.py:41"}])

    payload = json.loads(_ctl(env, "run-repair-retry", str(td)).stdout)
    assert payload["status"] == "escalation_required"
    assert "retry cap" in payload["error"] and "PERF-8" in payload["error"]


def test_cycle_cap_policy_override_and_accepted_findings(tmp_path: Path, env, monkeypatch):
    from lib_core import _persistent_project_dir, max_repair_cycles_per_auditor

    monkeypatch.setenv("DYNOS_HOME", env["DYNOS_HOME"])
    td = _task(tmp_path, stage="REPAIR_EXECUTION")
    _exhausted_cycles(td, [1, 2, 3])
    _report(td, [PERF_1])
    root = td.parent.parent
    assert max_repair_cycles_per_auditor(root) == 3
    policy_dir = _persistent_project_dir(root)
    policy_dir.mkdir(parents=True, exist_ok=True)
    (policy_dir / "policy.json").write_text(json.dumps({"max_repair_cycles_per_auditor": 5}))
    assert max_repair_cycles_per_auditor(root) == 5

    payload = json.loads(_ctl(env, "run-repair-retry", str(td)).stdout)
    assert payload["status"] == "repair_retry_ready", payload

    # An accepted finding is not counted against either cap.
    td2 = _task(tmp_path / "second", stage="REPAIR_EXECUTION")
    _exhausted_cycles(td2, [1, 2, 3], retry=3)
    _report(td2, [PERF_1])
    accepted = _ctl(env, "accept-finding", str(td2), "--auditor", AUDITOR, "--finding-id", "PERF-1",
                    "--reason", "accepted after review")
    assert accepted.returncode == 0, accepted.stderr
    payload = json.loads(_ctl(env, "run-repair-retry", str(td2)).stdout)
    assert payload["status"] == "repair_retry_ready", payload
