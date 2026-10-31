"""Postmortem inputs use the findings gate's report selection.

Field report (7.6.0): fast-tier findings a binding deep-tier verdict
overruled, and an attempt a re-audit replaced, kept counting after
``run-audit-findings-gate`` returned ``clear``:

- ``postmortem_analysis build-prompt`` listed the overruled findings as
  blocking and rendered the repair log as ``PERF-1 -> ? status=? route=generic``
  (it read ``executor``/``status``/per-batch ``repair_cycle``, which
  ``run-repair-log-build`` never writes).
- ``compute_reward`` scored ``quality_score: 0.0`` on a DONE task, because
  every blocking id in the stale report files counted as "surviving".
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))
sys.path.insert(0, str(ROOT))

from lib_core import _write_pre_repair_blocking_snapshot  # noqa: E402
from lib_receipts import receipt_audit_routing, write_receipt  # noqa: E402
from lib_validate import compute_reward  # noqa: E402
from memory import postmortem_analysis as pa  # noqa: E402

FAST = "haiku"  # noqa: model-literal
DEEP = "fable"  # noqa: model-literal


def _entry(name: str, *, ensemble: bool) -> dict:
    entry = {
        "name": name, "action": "spawn", "model": DEEP, "ensemble": ensemble,
        "route_mode": "generic", "agent_path": None, "injected_agent_sha256": None,
    }
    if ensemble:
        entry["ensemble_voting_models"] = [FAST]
        entry["ensemble_escalation_model"] = DEEP
    return entry


def _report(td: Path, auditor: str, model: str, attempt: int, findings: list[dict]) -> None:
    (td / "audit-reports" / f"{auditor}-{model}-attempt-{attempt}.json").write_text(json.dumps({
        "auditor_name": auditor, "status": "complete", "findings": findings,
    }))


def _shard(td: Path, auditor: str, model: str, blocking: int, ts: str) -> None:
    path = write_receipt(
        td, f"audit-{auditor}-{model}",
        auditor_name=auditor, model_used=model, finding_count=blocking, blocking_count=blocking,
        report_path=None, report_sha256=None, tokens_used=1, route_mode="generic",
        agent_path=None, injected_agent_sha256=None,
    )
    data = json.loads(path.read_text())
    data["ts"] = ts
    path.write_text(json.dumps(data))


def _task(tmp_path: Path, *, perf_fixed: bool = True) -> Path:
    """A11Y-1/API-1 overruled by the deep tier; PERF-1 repaired in cycle 1."""
    os.environ["DYNOS_HOME"] = str(tmp_path / "dynos-home")
    td = tmp_path / "project" / ".dynos" / "task-20261030-001"
    (td / "receipts").mkdir(parents=True)
    (td / "audit-reports").mkdir()
    (td / "manifest.json").write_text(json.dumps({
        "task_id": td.name, "created_at": "2026-10-30T00:00:00Z", "raw_input": "x",
        "stage": "DONE", "title": "field report",
        "classification": {"type": "feature", "risk_level": "high", "domains": ["backend", "ui"]},
    }))
    entries = [_entry("ui-auditor", ensemble=True), _entry("performance-auditor", ensemble=False)]
    (td / "audit-plan.json").write_text(json.dumps({"auditors": entries}))
    receipt_audit_routing(td, entries)

    _report(td, "ui-auditor", FAST, 1, [
        {"id": "A11Y-1", "severity": "major", "blocking": True, "location": "web/a.tsx:3", "category": "a11y"},
        {"id": "API-1", "severity": "major", "blocking": True, "location": "web/api.ts:9", "category": "api"},
    ])
    _report(td, "ui-auditor", DEEP, 1, [])
    _shard(td, "ui-auditor", FAST, blocking=2, ts="2026-10-30T10:00:00Z")
    _shard(td, "ui-auditor", DEEP, blocking=0, ts="2026-10-30T10:05:00Z")
    _report(td, "performance-auditor", DEEP, 1, [
        {"id": "PERF-1", "severity": "major", "blocking": True, "location": "src/q.py:40", "category": "perf"},
    ])

    # REPAIR_PLANNING: the snapshot and the repair log, in the shape
    # ``ctl run-repair-log-build`` writes.
    _write_pre_repair_blocking_snapshot(td)
    (td / "repair-log.json").write_text(json.dumps({
        "repair_cycle": 1, "phase": "repair", "source": "deterministic_ctl",
        "q_learning_source": "default",
        "batches": [{"batch_id": "batch-1", "parallel": False, "tasks": [{
            "finding_id": "PERF-1", "auditor": "performance-auditor",
            "assigned_executor": "backend-executor",
            "instruction": "Resolve finding PERF-1: N+1 query",
            "files_to_modify": ["src/q.py"], "retry_count": 0, "severity": "high",
            "route_mode": "generic", "route_source": "default", "model_override": "sonnet",  # noqa: model-literal
        }]}],
    }))
    # Re-audit after repair.
    _report(td, "performance-auditor", DEEP, 2, [] if perf_fixed else [
        {"id": "PERF-1", "severity": "major", "blocking": True, "location": "src/q.py:40", "category": "perf"},
    ])
    (td / "task-retrospective.json").write_text(json.dumps({
        "task_id": td.name, "task_outcome": "DONE", "repair_cycle_count": 1, "quality_score": 0.9,
    }))
    return td


def test_pre_repair_snapshot_holds_binding_blocking_only(tmp_path):
    td = _task(tmp_path)
    assert json.loads((td / "repair" / "pre-repair-blocking.json").read_text()) == ["PERF-1"]


def test_build_prompt_excludes_overruled_findings_and_reads_repair_log(tmp_path):
    td = _task(tmp_path)
    out = pa.build_analysis_prompt(td)
    assert out["has_findings"] is True
    prompt = out["prompt"]
    assert "id=A11Y-1" not in prompt
    assert "id=API-1" not in prompt
    assert "## Superseded Audit Reports (2;" in prompt
    assert f"ui-auditor-{FAST}-attempt-1.json: blocking=2 superseded_by=escalated cascade" in prompt
    assert f"performance-auditor-{DEEP}-attempt-1.json: blocking=1 superseded_by=attempt-2" in prompt
    assert (
        "- Cycle 1, batch-1: PERF-1 (performance-auditor) -> backend-executor status=resolved "
        "route=generic model=sonnet"  # noqa: model-literal
    ) in prompt
    assert "  files: src/q.py" in prompt
    assert "-> ?" not in prompt
    assert "status=?" not in prompt


def test_build_prompt_marks_repair_that_did_not_hold(tmp_path):
    td = _task(tmp_path, perf_fixed=False)
    prompt = pa.build_analysis_prompt(td)["prompt"]
    assert "PERF-1 (performance-auditor) -> backend-executor status=still_blocking" in prompt
    assert "id=PERF-1 category=perf severity=major blocking=True" in prompt


def test_quality_score_ignores_superseded_blocking(tmp_path):
    td = _task(tmp_path)
    result = compute_reward(td)
    # Findings were raised but none still binds: the informational ceiling,
    # not 0.0 ("every blocking finding survived").
    assert result["quality_score"] == 0.9
    assert result["repair_cycle_count"] == 1


def test_quality_score_counts_binding_survivor(tmp_path):
    td = _task(tmp_path, perf_fixed=False)
    # PERF-1 was blocking before repair and still binds after it: 1/1 survived.
    assert compute_reward(td)["quality_score"] == 0.0
