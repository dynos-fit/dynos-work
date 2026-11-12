"""`ctl reopen`: a defect found after DONE/CALIBRATED/FAILED goes back through
audit + repair inside the same task.

Field report: DONE and CALIBRATED had no outgoing edges, so agents that
found a defect after completion (a merge gate's first e2e run) stopped and
waited for a human, and the only workaround was a hand-built new task.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

from lib_core import transition_task  # noqa: E402
from lib_receipts import read_receipt, receipt_executor_routing  # noqa: E402
from router import _build_reopen_block  # noqa: E402
from test_gate_checkpoint_audit_per_segment import _setup_task, _write_exec_receipt  # noqa: E402


def _finished_task(tmp_path: Path, stage: str = "DONE") -> Path:
    td = _setup_task(tmp_path, "TEST_EXECUTION")
    receipt_executor_routing(td, [{"segment_id": "seg-1", "executor": "backend-executor"}])
    _write_exec_receipt(td, "seg-1")
    _set_stage(td, stage)
    return td


def _set_stage(td: Path, stage: str) -> None:
    manifest = json.loads((td / "manifest.json").read_text())
    manifest["stage"] = stage
    (td / "manifest.json").write_text(json.dumps(manifest))


def _manifest(td: Path) -> dict:
    return json.loads((td / "manifest.json").read_text())


def _reopen(td: Path, *extra: str) -> tuple[int, dict, str]:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "ctl.py"), "reopen", str(td), *extra],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = {}
    return proc.returncode, payload, proc.stderr


@pytest.mark.parametrize("stage", ["DONE", "CALIBRATED", "FAILED"])
def test_reopen_returns_terminal_task_to_checkpoint_audit(tmp_path: Path, stage: str) -> None:
    td = _finished_task(tmp_path, stage)
    code, payload, err = _reopen(td, "--reason", "integration test fails at merge gate")

    assert code == 0, (payload, err)
    assert payload["status"] == "reopened"
    assert payload["from_stage"] == stage
    manifest = _manifest(td)
    assert manifest["stage"] == "CHECKPOINT_AUDIT"
    assert manifest["reopen_count"] == 1
    assert manifest["reopens"][0]["reason"] == "integration test fails at merge gate"
    receipt = read_receipt(td, "reopen-1")
    assert receipt is not None and receipt["from_stage"] == stage and receipt["approver"] == "human"
    assert "[REOPEN]" in (td / "execution-log.md").read_text()


def test_reopen_edge_requires_the_receipt(tmp_path: Path) -> None:
    td = _finished_task(tmp_path, "DONE")
    with pytest.raises(ValueError, match="reopen-1"):
        transition_task(td, "CHECKPOINT_AUDIT")
    assert _manifest(td)["stage"] == "DONE"


def test_stale_reopen_receipt_cannot_reopen_twice(tmp_path: Path) -> None:
    td = _finished_task(tmp_path, "DONE")
    assert _reopen(td, "--reason", "first defect")[0] == 0
    _set_stage(td, "DONE")
    # reopen-1 exists, but the next reopen must carry reopen-2.
    with pytest.raises(ValueError, match="reopen-2"):
        transition_task(td, "CHECKPOINT_AUDIT")
    code, payload, err = _reopen(td, "--reason", "second defect", "--approver", "residual-auto")
    assert code == 0, (payload, err)
    assert _manifest(td)["reopen_count"] == 2
    assert read_receipt(td, "reopen-2")["approver"] == "residual-auto"


def test_reopen_refuses_active_task_and_blank_reason(tmp_path: Path) -> None:
    td = _finished_task(tmp_path, "REPAIR_EXECUTION")
    code, payload, _ = _reopen(td, "--reason", "x")
    assert code == 1 and payload["status"] == "blocked"
    assert _manifest(td)["stage"] == "REPAIR_EXECUTION"

    td2 = _finished_task(tmp_path / "b", "DONE")
    code, _, err = _reopen(td2, "--reason", "   ")
    assert code == 1 and "--reason" in err
    assert _manifest(td2)["stage"] == "DONE"


def test_reauditors_are_told_why_the_task_was_reopened(tmp_path: Path) -> None:
    td = _finished_task(tmp_path, "CALIBRATED")
    assert _build_reopen_block(td) == ""
    details = tmp_path / "details.txt"
    details.write_text("plan_today_loaded_flow_test.dart: expected 3 tiles, found 0")
    assert _reopen(td, "--reason", "e2e fails on first run", "--details-file", str(details))[0] == 0

    block = _build_reopen_block(td)
    assert "Task Reopened" in block
    assert "e2e fails on first run" in block
    assert "expected 3 tiles" in block
