"""Repair-log history across cycles, and rescoring stale retrospectives.

- ``repair-log.json`` is overwritten every cycle; the ctl writers now also
  record each cycle in the ctl-owned ``repair-log-history.json`` and the
  postmortem prompt renders every cycle, with a finding repaired again in a
  later cycle shown as re-raised.
- Retrospectives scored before ``REWARD_SCORING_VERSION`` 2 could carry
  ``quality_score: 0.0`` on a task the gate cleared. Postmortem recomputes
  them from the task dir (and persists the result); when the task dir is gone
  an old-formula 0.0 on a DONE task no longer raises ``low_quality``.
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
sys.path.insert(0, str(ROOT / "memory"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import lib_retro_rescore  # noqa: E402
from lib_core import _flush_retrospective_on_done, collect_retrospectives  # noqa: E402
from lib_repair_history import HISTORY_FILENAME, load_repair_cycles, record_repair_log  # noqa: E402
from lib_validate import REWARD_SCORING_VERSION, compute_reward  # noqa: E402
from memory import postmortem_analysis as pa  # noqa: E402
from write_policy import WriteAttempt, decide_write  # noqa: E402

import postmortem  # noqa: E402
import test_postmortem_binding_findings as fx  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_dynos_home(monkeypatch, tmp_path):
    """The tasks below set DYNOS_HOME; restore it after each test."""
    monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "dynos-home"))


def _log(cycle: int, fid: str = "PERF-1", retry: int = 0, auditor: str = "performance-auditor") -> dict:
    return {
        "repair_cycle": cycle, "phase": f"phase_{cycle}", "source": "deterministic_ctl",
        "batches": [{"batch_id": "batch-1", "parallel": False, "tasks": [{
            "finding_id": fid, "auditor": auditor, "assigned_executor": "backend-executor",
            "instruction": f"Resolve finding {fid}", "files_to_modify": ["src/q.py"],
            "retry_count": retry, "severity": "high", "route_mode": "generic",
        }]}],
    }


def _plain_task(tmp_path: Path) -> Path:
    os.environ["DYNOS_HOME"] = str(tmp_path / "dynos-home")
    td = tmp_path / "project" / ".dynos" / "task-20261031-001"
    (td / "audit-reports").mkdir(parents=True)
    (td / "manifest.json").write_text(json.dumps({
        "task_id": td.name, "created_at": "2026-10-31T00:00:00Z", "raw_input": "x", "stage": "DONE",
        "classification": {"type": "feature", "risk_level": "medium", "domains": ["backend"]},
    }))
    (td / "audit-reports" / "performance-auditor.json").write_text(json.dumps({
        "auditor_name": "performance-auditor", "status": "complete", "findings": [],
    }))
    (td / "task-retrospective.json").write_text(json.dumps({
        "task_id": td.name, "task_outcome": "DONE", "repair_cycle_count": 2,
        "quality_score": 0.9, "reward_scoring_version": REWARD_SCORING_VERSION,
    }))
    return td


# --- (1) repair-log history -------------------------------------------------

def test_record_keeps_every_cycle_and_replaces_a_rebuilt_one(tmp_path):
    td = _plain_task(tmp_path)
    record_repair_log(td, _log(1))
    record_repair_log(td, _log(2, retry=1), previous=_log(1))
    record_repair_log(td, _log(2, fid="PERF-9", retry=1), previous=_log(2, retry=1))
    history = json.loads((td / HISTORY_FILENAME).read_text())
    assert [c["repair_cycle"] for c in history["cycles"]] == [1, 2]
    assert history["cycles"][1]["repair_log"]["batches"][0]["tasks"][0]["finding_id"] == "PERF-9"


def test_previous_log_is_archived_for_tasks_that_predate_history(tmp_path):
    td = _plain_task(tmp_path)
    record_repair_log(td, _log(2, retry=1), previous=_log(1))
    assert [c["repair_cycle"] for c in load_repair_cycles(td)] == [1, 2]


def test_ctl_write_repair_log_records_history(tmp_path):
    td = _plain_task(tmp_path)
    env = {**os.environ, "DYNOS_HOME": str(tmp_path / "dynos-home")}
    for cycle in (1, 2):
        payload = tmp_path / f"log-{cycle}.json"
        log = _log(cycle, retry=cycle - 1)
        for task in log["batches"][0]["tasks"]:
            task["affected_files"] = task.pop("files_to_modify")
        payload.write_text(json.dumps(log))
        res = subprocess.run(
            ["python3", str(ROOT / "hooks" / "ctl.py"), "write-repair-log", str(td), "--from", str(payload)],
            cwd=ROOT, text=True, capture_output=True, env=env, check=False,
        )
        assert res.returncode == 0, res.stdout + res.stderr
    history = json.loads((td / HISTORY_FILENAME).read_text())
    assert [c["repair_cycle"] for c in history["cycles"]] == [1, 2]


def test_history_file_is_ctl_owned(tmp_path):
    td = _plain_task(tmp_path)
    for role in ("repair-coordinator", "orchestrator", "backend-executor"):
        decision = decide_write(WriteAttempt(
            role=role, task_dir=td, path=td / HISTORY_FILENAME, operation="modify", source="agent",
        ))
        assert decision.allowed is False, role
    assert decide_write(WriteAttempt(
        role="ctl", task_dir=td, path=td / HISTORY_FILENAME, operation="modify", source="ctl",
    )).allowed is True


def test_build_prompt_renders_every_cycle_with_reraised_status(tmp_path):
    td = _plain_task(tmp_path)
    record_repair_log(td, _log(1))
    record_repair_log(td, _log(2, retry=1), previous=_log(1))
    (td / "repair-log.json").write_text(json.dumps(_log(2, retry=1)))
    prompt = pa.build_analysis_prompt(td)["prompt"]
    assert "## Repair Log (2 cycles)" in prompt
    first = prompt.index("- Cycle 1, batch-1: PERF-1 (performance-auditor) -> backend-executor "
                         "status=re-raised, repaired again in cycle 2")
    second = prompt.index("- Cycle 2, batch-1: PERF-1 (performance-auditor) -> backend-executor "
                          "status=resolved route=generic retry=1")
    assert first < second


def test_build_prompt_falls_back_to_cycle_plan_for_unarchived_cycles(tmp_path):
    td = _plain_task(tmp_path)
    (td / "repair-log.json").write_text(json.dumps(_log(2, retry=1)))
    (td / "repair-cycle-plan.json").write_text(json.dumps({
        "status": "clear", "repair_cycle": 0,
        "auditor_repair_cycles": {"performance-auditor": [1, 2]},
    }))
    prompt = pa.build_analysis_prompt(td)["prompt"]
    assert ("- Cycle 1: repair log not archived (task predates repair-log-history.json); "
            "repair-cycle-plan.json records findings from performance-auditor driving this cycle") in prompt
    assert "- Cycle 2, batch-1: PERF-1 (performance-auditor) -> backend-executor status=resolved" in prompt


# --- (2) stale quality scores ----------------------------------------------

def _stale(td: Path) -> None:
    retro = compute_reward(td)
    retro.pop("reward_scoring_version")
    retro["quality_score"] = 0.0
    (td / "task-retrospective.json").write_text(json.dumps(retro))


def test_new_retrospectives_carry_the_scoring_version(tmp_path):
    td = fx._task(tmp_path)
    assert compute_reward(td)["reward_scoring_version"] == REWARD_SCORING_VERSION


def test_rescore_recomputes_persists_and_keeps_persistent_copy_verified(tmp_path):
    td = fx._task(tmp_path)
    _stale(td)
    _flush_retrospective_on_done(task_dir=td, manifest={"task_id": td.name})
    root = td.parent.parent

    result = lib_retro_rescore.rescore_task(root, td.name)
    assert result["status"] == "rescored"
    assert (result["previous"], result["quality_score"]) == (0.0, 0.9)

    local = json.loads((td / "task-retrospective.json").read_text())
    assert local["quality_score"] == 0.9
    assert local["reward_scoring_version"] == REWARD_SCORING_VERSION
    assert local["quality_score_rescore"]["previous"] == 0.0
    [entry] = [r for r in collect_retrospectives(root) if r.get("task_id") == td.name]
    assert entry["_source"] == "persistent"
    assert entry["quality_score"] == 0.9
    # Idempotent: a current retrospective is left alone.
    assert lib_retro_rescore.rescore_task(root, td.name)["status"] == "current"


def test_rescore_dry_run_writes_nothing(tmp_path):
    td = fx._task(tmp_path)
    _stale(td)
    before = (td / "task-retrospective.json").read_bytes()
    [result] = lib_retro_rescore.rescore_all(td.parent.parent, persist=False)
    assert result["status"] == "rescored" and result["quality_score"] == 0.9
    assert (td / "task-retrospective.json").read_bytes() == before


def test_postmortem_generate_rescores_instead_of_raising_low_quality(tmp_path):
    td = fx._task(tmp_path)
    _stale(td)
    pm = postmortem.generate_postmortem(td.parent.parent, td.name)
    assert pm["quality_summary"]["quality_score"] == 0.9
    assert pm["quality_summary"]["quality_unverified"] is False
    assert "low_quality" not in {a["type"] for a in pm["anomalies"]}


def test_unrescorable_old_zero_on_done_is_not_low_quality():
    stale = {"task_id": "task-x", "task_outcome": "DONE", "quality_score": 0.0}
    assert lib_retro_rescore.effective_quality(stale) is None
    assert "low_quality" not in {a["type"] for a in postmortem._detect_anomalies(stale, [])}
    # A current-version 0.0 is a real score and still raises it.
    current = {**stale, "reward_scoring_version": REWARD_SCORING_VERSION}
    assert "low_quality" in {a["type"] for a in postmortem._detect_anomalies(current, [])}
    # So is an old-formula 0.0 on a task that did not finish DONE.
    failed = {**stale, "task_outcome": "FAILED"}
    assert "low_quality" in {a["type"] for a in postmortem._detect_anomalies(failed, [])}


def test_rescore_reports_unavailable_when_task_dir_is_gone(tmp_path):
    root = tmp_path / "project"
    td = root / ".dynos" / "task-20261031-009"
    td.mkdir(parents=True)
    (td / "task-retrospective.json").write_text(json.dumps({
        "task_id": td.name, "task_outcome": "DONE", "quality_score": 0.0,
        "findings_by_auditor": {"sec": 2},
    }))
    result = lib_retro_rescore.rescore_task(root, td.name)
    assert result == {"task_id": td.name, "status": "unavailable", "quality_score": 0.0,
                      "quality_unverified": True}


def test_run_repair_log_build_records_history(tmp_path):
    import test_repair_routing_domains as rr  # noqa: PLC0415

    td = rr._setup(tmp_path, [rr._single_entry("code-quality-auditor")], stage="REPAIR_PLANNING")
    rr._project(td)
    rr._report(td, "code-quality-auditor", rr.DEEP, [
        {"id": "CQ-1", "severity": "major", "blocking": True, "location": "web/app.tsx:4",
         "description": "effect re-subscribes on every render"},
    ])
    rr._build(td, tmp_path)
    history = json.loads((td / HISTORY_FILENAME).read_text())
    [entry] = history["cycles"]
    assert entry["repair_cycle"] == 1
    assert entry["repair_log"] == json.loads((td / "repair-log.json").read_text())
