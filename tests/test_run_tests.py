"""`ctl run-tests`: test failures route to audit, never to REPAIR_PLANNING.

TEST_EXECUTION -> REPAIR_PLANNING was a dead end for a failing suite: the
repair log can only cite audit-report findings and none exist before the
audit, so write-repair-log refused and only FAILED remained. run-tests now
records the outcome, always advances to CHECKPOINT_AUDIT, the spec-completion
auditor is handed the failure, and the DONE gate refuses until a re-run passes.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

from lib_core import (  # noqa: E402
    _compute_bypassed_gates_for_force,
    _failing_test_results_err,
    transition_task,
)
from router import _build_failing_tests_block  # noqa: E402
from write_policy import _CONTROL_PLANE_EXACT  # noqa: E402
from test_gate_checkpoint_audit_per_segment import (  # noqa: E402
    _setup_task,
    _write_exec_receipt,
)
from lib_receipts import receipt_executor_routing  # noqa: E402


def _ready_task(tmp_path: Path, stage: str = "TEST_EXECUTION") -> Path:
    td = _setup_task(tmp_path, stage)
    receipt_executor_routing(td, [{"segment_id": "seg-1", "executor": "backend-executor"}])
    _write_exec_receipt(td, "seg-1")
    return td


def _run_tests(td: Path, *commands: str) -> tuple[int, dict]:
    argv = [sys.executable, str(ROOT / "hooks" / "ctl.py"), "run-tests", str(td)]
    for command in commands:
        argv += ["--command", command]
    proc = subprocess.run(argv, cwd=ROOT, text=True, capture_output=True, check=False)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = {"stderr": proc.stderr}
    return proc.returncode, payload


def _stage(td: Path) -> str:
    return json.loads((td / "manifest.json").read_text())["stage"]


def test_failing_suite_advances_to_checkpoint_audit(tmp_path: Path) -> None:
    td = _ready_task(tmp_path)
    code, payload = _run_tests(td, "exit 0", "echo conftest AttributeError; exit 3")

    assert code == 0, payload
    assert payload["status"] == "tests_failed"
    assert payload["failed_commands"] == ["echo conftest AttributeError; exit 3"]
    assert _stage(td) == "CHECKPOINT_AUDIT"

    results = json.loads((td / "test-results.json").read_text())
    assert results["status"] == "failed"
    assert results["stage"] == "TEST_EXECUTION"
    assert [r["exit_code"] for r in results["results"]] == [0, 3]
    assert "conftest AttributeError" in results["results"][1]["stdout_tail"]
    assert "[FAIL] tests" in (td / "execution-log.md").read_text()


def test_passing_suite_advances_and_records_pass(tmp_path: Path) -> None:
    td = _ready_task(tmp_path)
    code, payload = _run_tests(td, "true")

    assert code == 0, payload
    assert payload["status"] == "tests_passed"
    assert _stage(td) == "CHECKPOINT_AUDIT"
    assert json.loads((td / "test-results.json").read_text())["status"] == "passed"


def test_rerun_after_repair_records_without_transition(tmp_path: Path) -> None:
    td = _ready_task(tmp_path, stage="CHECKPOINT_AUDIT")
    _run_tests(td, "exit 1")
    assert _failing_test_results_err(td) is not None

    code, payload = _run_tests(td, "exit 0")
    assert code == 0, payload
    assert payload["stage"] == "CHECKPOINT_AUDIT"
    assert payload["next_command"] is None
    assert _failing_test_results_err(td) is None


def test_refused_outside_test_stages(tmp_path: Path) -> None:
    td = _ready_task(tmp_path, stage="EXECUTION")
    code, payload = _run_tests(td, "true")
    assert code == 1
    assert "unexpected stage for run-tests: EXECUTION" in payload["error"]
    assert not (td / "test-results.json").exists()


def test_test_execution_to_repair_planning_is_illegal(tmp_path: Path) -> None:
    td = _ready_task(tmp_path)
    with pytest.raises(ValueError, match="Illegal stage transition"):
        transition_task(td, "REPAIR_PLANNING")


def test_done_gate_blocks_only_on_recorded_failure(tmp_path: Path) -> None:
    td = _ready_task(tmp_path, stage="CHECKPOINT_AUDIT")
    assert _failing_test_results_err(td) is None  # never ran through ctl

    def done_gaps() -> list[str]:
        manifest = json.loads((td / "manifest.json").read_text())
        return _compute_bypassed_gates_for_force(
            task_dir=td, manifest=manifest,
            current_stage="CHECKPOINT_AUDIT", next_stage="DONE",
        )

    (td / "test-results.json").write_text(json.dumps({"status": "failed"}))
    assert any("test-results.json" in gap for gap in done_gaps())

    (td / "test-results.json").write_text(json.dumps({"status": "passed"}))
    assert not any("test-results.json" in gap for gap in done_gaps())


def test_failing_tests_block_goes_to_spec_completion_only(tmp_path: Path) -> None:
    td = _ready_task(tmp_path, stage="CHECKPOINT_AUDIT")
    assert _build_failing_tests_block(td, "spec-completion-auditor") == ""

    _run_tests(td, "echo boom >&2; exit 2")
    block = _build_failing_tests_block(td, "spec-completion-auditor")
    assert "## Known Failing Tests" in block
    assert "exit 2" in block and "boom" in block
    assert _build_failing_tests_block(td, "security-auditor") == ""

    _run_tests(td, "true")
    assert _build_failing_tests_block(td, "spec-completion-auditor") == ""


def test_test_results_is_control_plane() -> None:
    assert "test-results.json" in _CONTROL_PLANE_EXACT
