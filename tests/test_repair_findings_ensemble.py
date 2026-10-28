"""Repair planning over escalated ensemble cascades.

Regression for: ``run-repair-log-build`` wrote a repair log that failed its own
validation when an ensemble auditor's fast tier and deep tier both reported
the same finding ID. The findings gate, the repair-cycle plan, the repair-log
builder and the repair-log validator each derived "the findings" differently:

- the gate / plan / builder took every blocking finding from every report, so
  ``SC-1`` from the fast-tier and the deep-tier report were both queued;
- the validator kept only the first report's copy by filename order (``fable``
  sorts before ``haiku``), so severities disagreed, and its location dedup
  dropped findings the builder kept ("not found in live audit reports").

All four now read ``lib_validate.collect_repair_findings``, which applies the
cascade rule the audit skill documents: once an ensemble auditor's verdict is
``escalated``, the deep tier's report is final and binding and the voting-tier
reports are superseded.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

from lib_receipts import receipt_audit_routing, write_receipt  # noqa: E402
from lib_validate import collect_repair_findings, validate_repair_log  # noqa: E402

FAST = "haiku"  # noqa: model-literal
DEEP = "fable"  # noqa: model-literal


def _run_ctl(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(ROOT / "hooks" / "ctl.py"), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env={**os.environ, **env},
        check=False,
    )


def _ensemble_entry(name: str) -> dict:
    return {
        "name": name,
        "action": "spawn",
        "model": DEEP,
        "ensemble": True,
        "ensemble_voting_models": [FAST],
        "ensemble_escalation_model": DEEP,
        "route_mode": "generic",
        "agent_path": None,
        "injected_agent_sha256": None,
    }


def _single_entry(name: str) -> dict:
    return {
        "name": name,
        "action": "spawn",
        "model": DEEP,
        "ensemble": False,
        "route_mode": "generic",
        "agent_path": None,
        "injected_agent_sha256": None,
    }


def _setup(tmp_path: Path, entries: list[dict], stage: str = "CHECKPOINT_AUDIT") -> Path:
    td = tmp_path / "project" / ".dynos" / "task-20261027-001"
    (td / "receipts").mkdir(parents=True)
    (td / "audit-reports").mkdir()
    (td / "manifest.json").write_text(json.dumps({
        "task_id": td.name,
        "created_at": "2026-10-27T00:00:00Z",
        "raw_input": "x",
        "stage": stage,
        "classification": {"type": "feature", "risk_level": "medium", "domains": ["backend"]},
    }))
    (td / "audit-plan.json").write_text(json.dumps({"auditors": entries}))
    # A realistic graph: when a finding's files cannot be resolved, the old
    # builder fell back to every file here (the "64 files" repair task).
    (td / "execution-graph.json").write_text(json.dumps({"segments": [
        {"id": "seg-1", "executor": "backend-executor", "files_expected": ["src/auth.py", "src/a.py"]},
        {"id": "seg-2", "executor": "ui-executor", "files_expected": ["web/app.tsx"]},
    ]}))
    receipt_audit_routing(td, entries)
    return td


def _report(td: Path, auditor: str, model: str, findings: list[dict], attempt: int = 1) -> Path:
    path = td / "audit-reports" / f"{auditor}-{model}-attempt-{attempt}.json"
    path.write_text(json.dumps({
        "auditor_name": auditor,
        "status": "complete",
        "findings": findings,
    }))
    return path


def _shard(td: Path, auditor: str, model: str, blocking: int, ts: str) -> None:
    path = write_receipt(
        td,
        f"audit-{auditor}-{model}",
        auditor_name=auditor,
        model_used=model,
        finding_count=blocking,
        blocking_count=blocking,
        report_path=None,
        report_sha256=None,
        tokens_used=1,
        route_mode="generic",
        agent_path=None,
        injected_agent_sha256=None,
    )
    data = json.loads(path.read_text())
    data["ts"] = ts
    path.write_text(json.dumps(data))


def _escalated_task(tmp_path: Path) -> Path:
    """The reported shape: two ensemble auditors escalated to the deep tier,
    reusing finding IDs across tiers with different severities."""
    td = _setup(tmp_path, [
        _ensemble_entry("supply-chain-auditor"),
        _ensemble_entry("security-auditor"),
        _single_entry("ui-auditor"),
    ])
    sca = "supply-chain-auditor"
    _report(td, sca, FAST, [
        {"id": "SC-1", "severity": "high", "blocking": True, "location": "uv.lock:1", "category": "deps"},
        {"id": "SC-6", "severity": "high", "blocking": True, "location": "pyproject.toml:4", "category": "deps"},
    ])
    _report(td, sca, DEEP, [
        {"id": "SC-1", "severity": "medium", "blocking": True, "location": "uv.lock:1", "category": "deps"},
        {"id": "SC-6", "severity": "low", "blocking": False, "location": "pyproject.toml:4", "category": "deps"},
    ])
    _shard(td, sca, FAST, blocking=2, ts="2026-10-27T10:00:00Z")
    _shard(td, sca, DEEP, blocking=1, ts="2026-10-27T10:05:00Z")

    sec = "security-auditor"
    _report(td, sec, FAST, [
        {"id": "SEC-01", "severity": "high", "blocking": True, "location": "src/auth.py:10", "category": "authz"},
        {"id": "SEC-02", "severity": "high", "blocking": True, "location": "src/auth.py:20", "category": "authz"},
    ])
    # The deep tier ruled: nothing blocks. That verdict is final and binding.
    _report(td, sec, DEEP, [
        {"id": "SEC-01", "severity": "low", "blocking": False, "location": "src/auth.py:10", "category": "authz"},
    ])
    _shard(td, sec, FAST, blocking=2, ts="2026-10-27T10:00:00Z")
    _shard(td, sec, DEEP, blocking=0, ts="2026-10-27T10:06:00Z")

    _report(td, "ui-auditor", DEEP, [
        {"id": "UI-02", "severity": "major", "blocking": True, "location": "web/app.tsx:7", "category": "a11y"},
    ])
    return td


def test_collect_repair_findings_applies_deep_tier_verdict(tmp_path):
    td = _escalated_task(tmp_path)
    collected = collect_repair_findings(td)
    blocking = {(f["_auditor"], f["id"]): f for f in collected["findings"] if f.get("blocking")}
    # Deep-tier copy of SC-1 is the binding one (severity from the deep report).
    assert blocking[("supply-chain-auditor", "SC-1")]["severity"] == "medium"
    # SC-6 was downgraded to non-blocking by the deep tier; the security deep
    # tier found nothing blocking. Neither fast-tier copy may block.
    assert ("supply-chain-auditor", "SC-6") not in blocking
    assert not any(auditor == "security-auditor" for auditor, _ in blocking)
    assert ("ui-auditor", "UI-02") in blocking
    superseded = {r["report_path"].rsplit("/", 1)[-1] for r in collected["reports"] if r["superseded_by"]}
    assert superseded == {
        f"supply-chain-auditor-{FAST}-attempt-1.json",
        f"security-auditor-{FAST}-attempt-1.json",
    }


def test_repair_flow_builds_a_log_that_passes_its_own_validation(tmp_path):
    td = _escalated_task(tmp_path)
    env = {"DYNOS_HOME": str(tmp_path / "dynos-home")}

    gate = _run_ctl("run-audit-findings-gate", str(td), env=env)
    assert gate.returncode == 0, gate.stdout + gate.stderr
    gate_payload = json.loads(gate.stdout)
    assert gate_payload["status"] == "repair_required"
    assert sorted(gate_payload["blocking_finding_ids"]) == ["SC-1", "UI-02"]

    plan = _run_ctl("run-audit-repair-cycle-plan", str(td), env=env)
    assert plan.returncode == 0, plan.stdout + plan.stderr
    plan_payload = json.loads(plan.stdout)
    assert plan_payload["status"] == "repair_cycle_ready"
    ids = plan_payload["blocking_finding_ids"]
    assert sorted(ids) == ["SC-1", "UI-02"]
    assert len(ids) == len(set(ids)), f"duplicate finding ids queued: {ids}"

    build = _run_ctl("run-repair-log-build", str(td), env=env)
    assert build.returncode == 0, build.stdout + build.stderr
    assert json.loads(build.stdout)["status"] == "repair_log_built"
    assert validate_repair_log(td) == []

    tasks = {
        t["finding_id"]: t
        for batch in json.loads((td / "repair-log.json").read_text())["batches"]
        for t in batch["tasks"]
    }
    assert set(tasks) == {"SC-1", "UI-02"}
    assert tasks["SC-1"]["severity"] == "medium"
    # Severity alias (major -> high) is applied once, the way the validator reads it.
    assert tasks["UI-02"]["severity"] == "high"
    # Schema-conformant `location: "file:line"` resolves to the file.
    assert tasks["SC-1"]["files_to_modify"] == ["uv.lock"]
    assert tasks["UI-02"]["files_to_modify"] == ["web/app.tsx"]


def _tasks_without_policy_choices(td: Path) -> dict:
    """Repair-log tasks minus the fields Q-learning may explore (executor,
    route, model): those are epsilon-greedy by design."""
    explored = {"assigned_executor", "state", "route_mode", "route_source", "agent_path",
                "agent_name", "model_override", "model_source"}
    return {
        (t["auditor"], t["finding_id"]): {k: v for k, v in t.items() if k not in explored}
        for batch in json.loads((td / "repair-log.json").read_text())["batches"]
        for t in batch["tasks"]
    }


def test_rebuild_is_stable_and_executors_fit_the_auditor(tmp_path):
    td = _escalated_task(tmp_path)
    env = {"DYNOS_HOME": str(tmp_path / "dynos-home")}
    assert _run_ctl("run-audit-repair-cycle-plan", str(td), env=env).returncode == 0
    first = _run_ctl("run-repair-log-build", str(td), env=env)
    assert first.returncode == 0, first.stdout + first.stderr
    once = _tasks_without_policy_choices(td)
    for _ in range(5):
        again = _run_ctl("run-repair-log-build", str(td), env=env)
        assert again.returncode == 0, again.stdout + again.stderr
        assert _tasks_without_policy_choices(td) == once
        executors = {
            t["finding_id"]: t["assigned_executor"]
            for batch in json.loads((td / "repair-log.json").read_text())["batches"]
            for t in batch["tasks"]
        }
        # supply-chain -> infra action space; never a random pick from all executors.
        assert executors["SC-1"] in {"infra-executor", "release-executor"}
        assert executors["UI-02"] == "ui-executor"


def test_findings_on_pipeline_artifacts_escalate_instead_of_becoming_repairs(tmp_path):
    """No executor may write .dynos receipts or logs (write_policy), so a
    repair task targeting one can only fail. The build escalates instead."""
    td = _setup(tmp_path, [_single_entry("security-auditor")], stage="REPAIR_PLANNING")
    (td / "receipts" / "plan-validated.json").write_text("{}")
    _report(td, "security-auditor", DEEP, [
        {"id": "PI-1", "severity": "high", "blocking": True, "location": "receipts/plan-validated.json:1"},
        {"id": "PI-2", "severity": "high", "blocking": True,
         "location": ".dynos/task-20261027-001/events.jsonl:3"},
        {"id": "SEC-9", "severity": "high", "blocking": True, "location": "src/a.py:2"},
    ])
    env = {"DYNOS_HOME": str(tmp_path / "dynos-home")}
    plan = _run_ctl("run-audit-repair-cycle-plan", str(td), env=env)
    assert plan.returncode == 0, plan.stdout + plan.stderr
    build = _run_ctl("run-repair-log-build", str(td), env=env)
    assert build.returncode == 1
    payload = json.loads(build.stdout)
    assert payload["status"] == "escalation_required"
    assert payload["next_action"] == "write_escalation_and_fail"
    assert sorted(f["finding_id"] for f in payload["non_repairable_findings"]) == ["PI-1", "PI-2"]
    assert not (td / "repair-log.json").exists()


def test_pipeline_paths_are_stripped_from_mixed_findings(tmp_path):
    td = _setup(tmp_path, [_single_entry("code-quality-auditor")], stage="REPAIR_PLANNING")
    path = _report(td, "code-quality-auditor", DEEP, [
        {"id": "CQ-7", "severity": "minor", "blocking": True, "location": "src/a.py:4"},
    ])
    data = json.loads(path.read_text())
    data["repair_tasks"] = [{
        "finding_id": "CQ-7",
        "assigned_executor": "refactor-executor",
        "affected_files": ["src/a.py", "src/b.py", ".dynos/task-20261027-001/execution-log.md"],
    }]
    path.write_text(json.dumps(data))
    env = {"DYNOS_HOME": str(tmp_path / "dynos-home")}
    assert _run_ctl("run-audit-repair-cycle-plan", str(td), env=env).returncode == 0
    build = _run_ctl("run-repair-log-build", str(td), env=env)
    assert build.returncode == 0, build.stdout + build.stderr
    task = json.loads((td / "repair-log.json").read_text())["batches"][0]["tasks"][0]
    assert task["files_to_modify"] == ["src/a.py", "src/b.py"]
    assert task["severity"] == "low"


def test_build_with_same_id_from_two_auditors_validates(tmp_path):
    td = _setup(tmp_path, [_single_entry("security-auditor"), _single_entry("performance-auditor")], stage="REPAIR_PLANNING")
    _report(td, "security-auditor", DEEP, [
        {"id": "F-1", "severity": "high", "blocking": True, "location": "src/a.py:1"},
    ])
    _report(td, "performance-auditor", DEEP, [
        {"id": "F-1", "severity": "low", "blocking": True, "location": "src/b.py:9"},
    ])
    env = {"DYNOS_HOME": str(tmp_path / "dynos-home")}
    assert _run_ctl("run-audit-repair-cycle-plan", str(td), env=env).returncode == 0
    build = _run_ctl("run-repair-log-build", str(td), env=env)
    assert build.returncode == 0, build.stdout + build.stderr
    assert validate_repair_log(td) == []
    tasks = sorted(
        (t["auditor"], t["finding_id"], t["severity"])
        for batch in json.loads((td / "repair-log.json").read_text())["batches"]
        for t in batch["tasks"]
    )
    assert tasks == [("performance-auditor", "F-1", "low"), ("security-auditor", "F-1", "high")]


def test_validator_rejects_a_superseded_fast_tier_finding(tmp_path):
    """A hand-written log that repairs a finding the deep tier overruled fails."""
    td = _escalated_task(tmp_path)
    (td / "repair-log.json").write_text(json.dumps({"repair_cycle": 1, "batches": [{
        "batch_id": "batch-1",
        "tasks": [{
            "finding_id": "SEC-02", "auditor": "security-auditor", "severity": "high",
            "instruction": "fix", "assigned_executor": "backend-executor", "files_to_modify": ["src/auth.py"],
        }],
    }]}))
    errors = validate_repair_log(td)
    assert any("SEC-02" in e and "not found" in e for e in errors), errors


def test_fast_tier_still_blocks_when_escalation_report_is_missing(tmp_path):
    """Fail closed: a deep-tier receipt without its report must not erase the
    fast tier's blocking findings."""
    td = _setup(tmp_path, [_ensemble_entry("security-auditor")])
    _report(td, "security-auditor", FAST, [
        {"id": "SEC-01", "severity": "high", "blocking": True, "location": "src/a.py:1"},
    ])
    _shard(td, "security-auditor", FAST, blocking=1, ts="2026-10-27T10:00:00Z")
    _shard(td, "security-auditor", DEEP, blocking=0, ts="2026-10-27T10:05:00Z")
    blocking = [f for f in collect_repair_findings(td)["findings"] if f.get("blocking")]
    assert [f["id"] for f in blocking] == ["SEC-01"]


def test_fast_tier_still_blocks_while_cascade_is_pending(tmp_path):
    td = _setup(tmp_path, [_ensemble_entry("security-auditor")])
    _report(td, "security-auditor", FAST, [
        {"id": "SEC-01", "severity": "high", "blocking": True, "location": "src/a.py:1"},
    ])
    _shard(td, "security-auditor", FAST, blocking=1, ts="2026-10-27T10:00:00Z")
    blocking = [f for f in collect_repair_findings(td)["findings"] if f.get("blocking")]
    assert [f["id"] for f in blocking] == ["SEC-01"]


def test_later_attempt_supersedes_earlier_attempt_of_same_shard(tmp_path):
    """After repair + re-audit, attempt-1's (already repaired) findings must not
    be re-queued next to attempt-2's, which reused the same IDs."""
    td = _setup(tmp_path, [_single_entry("code-quality-auditor")])
    _report(td, "code-quality-auditor", DEEP, [
        {"id": "CQ-1", "severity": "high", "blocking": True, "location": "src/a.py:1"},
        {"id": "CQ-2", "severity": "high", "blocking": True, "location": "src/b.py:1"},
    ], attempt=1)
    _report(td, "code-quality-auditor", DEEP, [
        {"id": "CQ-1", "severity": "low", "blocking": True, "location": "src/c.py:1"},
    ], attempt=2)
    blocking = [f for f in collect_repair_findings(td)["findings"] if f.get("blocking")]
    assert [(f["id"], f["severity"]) for f in blocking] == [("CQ-1", "low")]


def test_location_duplicate_keeps_the_blocking_copy_and_records_the_merge(tmp_path):
    """Two auditors flag the same file:line:category. One repair task covers
    both, and a non-blocking duplicate can never shadow a blocking finding."""
    td = _setup(tmp_path, [_single_entry("code-quality-auditor"), _single_entry("infrastructure-auditor")])
    _report(td, "code-quality-auditor", DEEP, [
        {"id": "cq-001", "severity": "low", "blocking": False, "location": "Makefile:3", "category": "race"},
    ])
    _report(td, "infrastructure-auditor", DEEP, [
        {"id": "INFRA-01", "severity": "high", "blocking": True, "location": "Makefile:3", "category": "race"},
    ])
    findings = collect_repair_findings(td)["findings"]
    assert [(f["_auditor"], f["id"], f["blocking"]) for f in findings] == [
        ("infrastructure-auditor", "INFRA-01", True),
    ]
    assert findings[0]["_merged"] == [{"auditor": "code-quality-auditor", "id": "cq-001"}]


def test_same_id_from_two_auditors_are_distinct_findings(tmp_path):
    td = _setup(tmp_path, [_single_entry("security-auditor"), _single_entry("performance-auditor")], stage="REPAIR_PLANNING")
    _report(td, "security-auditor", DEEP, [
        {"id": "F-1", "severity": "high", "blocking": True, "location": "src/a.py:1"},
    ])
    _report(td, "performance-auditor", DEEP, [
        {"id": "F-1", "severity": "low", "blocking": True, "location": "src/b.py:9"},
    ])
    findings = collect_repair_findings(td)["findings"]
    assert sorted((f["_auditor"], f["id"]) for f in findings) == [
        ("performance-auditor", "F-1"), ("security-auditor", "F-1"),
    ]


def test_an_invalid_build_leaves_the_previous_repair_log_untouched(tmp_path):
    """The next cycle plan reads repair-log.json as the last completed cycle;
    a rejected build must not leave its invalid log there."""
    td = _setup(tmp_path, [_single_entry("security-auditor")], stage="REPAIR_PLANNING")
    _report(td, "security-auditor", DEEP, [
        {"id": "SEC-1", "severity": "urgent", "blocking": True, "location": "src/a.py:1"},
    ])
    env = {"DYNOS_HOME": str(tmp_path / "dynos-home")}
    assert _run_ctl("run-audit-repair-cycle-plan", str(td), env=env).returncode == 0

    build = _run_ctl("run-repair-log-build", str(td), env=env)
    assert build.returncode == 1
    assert json.loads(build.stdout)["status"] == "repair_log_invalid"
    assert not (td / "repair-log.json").exists()

    previous = '{"repair_cycle": 1, "batches": []}\n'
    (td / "repair-log.json").write_text(previous)
    assert _run_ctl("run-repair-log-build", str(td), env=env).returncode == 1
    assert (td / "repair-log.json").read_text() == previous
