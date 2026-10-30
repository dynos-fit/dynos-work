"""Field report 7.5.34: finishing an audit after a repair cycle.

After a repair cycle the stage stayed REPAIR_EXECUTION through the re-audit,
so `run-audit-finish` refused with "unexpected stage for audit finish" even
when the findings gate was clear. And `run-audit-finish` copied
audit-summary.json into completion.json without checking that the summary
was written after the last audit report.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

from lib_receipts import receipt_executor_routing  # noqa: E402
from test_ctl import _run_ctl, _setup_task_dir  # noqa: E402


def _set_stage(task_dir: Path, stage: str) -> None:
    # A task in REPAIR_EXECUTION already passed execution: no segments are
    # left without a receipt.
    (task_dir / "execution-graph.json").write_text(json.dumps({"task_id": task_dir.name, "segments": []}))
    receipt_executor_routing(task_dir, [])
    path = task_dir / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["stage"] = stage
    path.write_text(json.dumps(manifest, indent=2) + "\n")


def _stage(task_dir: Path) -> str:
    return json.loads((task_dir / "manifest.json").read_text())["stage"]


def _report(task_dir: Path, findings: list[dict]) -> Path:
    audit_dir = task_dir / "audit-reports"
    audit_dir.mkdir(parents=True, exist_ok=True)
    path = audit_dir / "performance-auditor.json"
    path.write_text(json.dumps({"auditor_name": "performance-auditor", "findings": findings}))
    return path


def test_clear_gate_returns_repaired_task_to_checkpoint_audit(tmp_path):
    task_dir = _setup_task_dir(tmp_path)
    _set_stage(task_dir, "REPAIR_EXECUTION")
    _report(task_dir, [{"id": "PERF-1", "blocking": False, "severity": "low"}])

    result = _run_ctl("run-audit-findings-gate", str(task_dir))
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "clear"
    assert payload["transitioned_to"] == "CHECKPOINT_AUDIT"
    assert _stage(task_dir) == "CHECKPOINT_AUDIT"


def test_blocking_gate_leaves_repair_execution_alone(tmp_path):
    task_dir = _setup_task_dir(tmp_path)
    _set_stage(task_dir, "REPAIR_EXECUTION")
    _report(task_dir, [{"id": "PERF-1", "blocking": True, "severity": "high"}])

    result = _run_ctl("run-audit-findings-gate", str(task_dir))
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["transitioned_to"] is None
    assert _stage(task_dir) == "REPAIR_EXECUTION"


def test_gate_does_not_move_other_stages(tmp_path):
    task_dir = _setup_task_dir(tmp_path)
    _set_stage(task_dir, "CHECKPOINT_AUDIT")
    _report(task_dir, [])
    result = _run_ctl("run-audit-findings-gate", str(task_dir))
    assert json.loads(result.stdout)["transitioned_to"] is None
    assert _stage(task_dir) == "CHECKPOINT_AUDIT"


def _write_summary_and_retro(task_dir: Path) -> None:
    (task_dir / "audit-summary.json").write_text(json.dumps({"audit_result": "fail", "total_blocking": 1}))
    (task_dir / "task-retrospective.json").write_text(json.dumps({"task_id": task_dir.name}))


def test_finish_accepts_clear_repair_execution_and_refuses_stale_summary(tmp_path):
    task_dir = _setup_task_dir(tmp_path)
    _set_stage(task_dir, "REPAIR_EXECUTION")
    _write_summary_and_retro(task_dir)
    report = _report(task_dir, [])
    later = time.time() + 5
    os.utime(report, (later, later))

    result = _run_ctl("run-audit-finish", str(task_dir))
    assert result.returncode == 1
    error = json.loads(result.stdout)["error"]
    assert "unexpected stage" not in error
    assert "predates audit report performance-auditor.json" in error
    assert _stage(task_dir) == "CHECKPOINT_AUDIT"
    assert not (task_dir / "completion.json").exists()


def test_finish_still_refuses_repair_execution_with_blocking_findings(tmp_path):
    task_dir = _setup_task_dir(tmp_path)
    _set_stage(task_dir, "REPAIR_EXECUTION")
    _write_summary_and_retro(task_dir)
    _report(task_dir, [{"id": "PERF-1", "blocking": True, "severity": "high"}])

    result = _run_ctl("run-audit-finish", str(task_dir))
    assert result.returncode == 1
    assert "unexpected stage for audit finish: REPAIR_EXECUTION" in json.loads(result.stdout)["error"]
