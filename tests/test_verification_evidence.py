"""Tests for the verification-evidence runner (D6).

Execution-graph segments declare `verify_commands`; `ctl
run-verification-evidence` executes them and captures exit codes + output
into a ctl-owned artifact that auditors read instead of trusting an
executor's narrative claim. Executors cannot write or doctor the artifact,
and run-execution-finish refuses to advance while a declared verification
is missing or failing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

from lib_receipts import hash_file, read_receipt  # noqa: E402
from write_policy import WriteAttempt, decide_write  # noqa: E402

from test_ctl import _run_ctl, _setup_task_dir  # noqa: E402


def _add_verify_commands(task_dir: Path, seg_id: str, commands: list[dict]) -> None:
    graph = json.loads((task_dir / "execution-graph.json").read_text())
    for seg in graph["segments"]:
        if seg["id"] == seg_id:
            seg["verify_commands"] = commands
    (task_dir / "execution-graph.json").write_text(json.dumps(graph, indent=2))


def test_runner_captures_pass_and_writes_receipt(tmp_path: Path) -> None:
    task_dir = _setup_task_dir(tmp_path)
    _add_verify_commands(task_dir, "seg-1", [
        {"id": "vc-1", "command": "true", "criteria_ids": [1]},
        {"id": "vc-2", "command": "echo checked"},
    ])

    result = _run_ctl("run-verification-evidence", str(task_dir))
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "verification_captured"

    artifact_path = task_dir / "evidence" / "verification" / "seg-1.json"
    artifact = json.loads(artifact_path.read_text())
    assert artifact["all_passed"] is True
    by_id = {r["id"]: r for r in artifact["results"]}
    assert by_id["vc-1"]["exit_code"] == 0
    assert by_id["vc-1"]["criteria_ids"] == [1]
    assert "checked" in by_id["vc-2"]["stdout_tail"]

    receipt = read_receipt(task_dir, "verification-evidence-seg-1")
    assert receipt["artifact_sha256"] == hash_file(artifact_path)
    assert receipt["all_passed"] is True
    assert receipt["command_count"] == 2


def test_runner_captures_failure_faithfully(tmp_path: Path) -> None:
    task_dir = _setup_task_dir(tmp_path)
    _add_verify_commands(task_dir, "seg-1", [
        {"id": "vc-1", "command": "echo broken >&2; exit 3"},
    ])

    result = _run_ctl("run-verification-evidence", str(task_dir))
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "verification_failed"

    artifact = json.loads(
        (task_dir / "evidence" / "verification" / "seg-1.json").read_text()
    )
    record = artifact["results"][0]
    assert record["exit_code"] == 3
    assert record["passed"] is False
    assert "broken" in record["stderr_tail"]
    # The receipt records the failure — it does not certify success.
    receipt = read_receipt(task_dir, "verification-evidence-seg-1")
    assert receipt["all_passed"] is False


def test_agent_roles_cannot_write_verification_evidence(tmp_path: Path) -> None:
    task_dir = tmp_path / ".dynos" / "task-20260611-001"
    task_dir.mkdir(parents=True)
    target = task_dir / "evidence" / "verification" / "seg-1.json"
    for role in ("backend-executor", "execute-inline", "testing-executor",
                 "orchestrator", "planning", "audit-security"):
        decision = decide_write(
            WriteAttempt(
                role=role,
                task_dir=task_dir,
                path=target,
                operation="create",
                source="agent",
            )
        )
        assert decision.allowed is False, f"{role} doctored verification evidence"
    ctl_decision = decide_write(
        WriteAttempt(
            role="ctl",
            task_dir=task_dir,
            path=target,
            operation="create",
            source="ctl",
        )
    )
    assert ctl_decision.allowed is True


def _finish_ready_task(tmp_path: Path) -> Path:
    """A task at EXECUTION with all receipts/evidence in place (mirrors
    test_ctl.test_run_execution_finish_transitions_when_no_pending_segments)."""
    from lib_receipts import (
        receipt_executor_done,
        receipt_executor_routing,
        receipt_plan_validated,
        receipt_rules_check_passed,
    )

    task_dir = _setup_task_dir(tmp_path)
    manifest_path = task_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["stage"] = "EXECUTION"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    os.environ["DYNOS_ALLOW_TEST_OVERRIDE"] = "1"
    try:
        receipt_plan_validated(task_dir, validation_passed_override=True)
    finally:
        os.environ.pop("DYNOS_ALLOW_TEST_OVERRIDE", None)

    receipt_executor_routing(task_dir, [
        {"segment_id": "seg-1", "executor": "backend-executor", "model": "sonnet",
         "route_mode": "generic", "agent_path": None},
        {"segment_id": "seg-2", "executor": "testing-executor", "model": "sonnet",
         "route_mode": "generic", "agent_path": None},
    ])

    root = task_dir.parent.parent
    for expected in ("src/a.py", "tests/test_a.py"):
        target = root / expected
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# produced by execution\n")

    evidence_dir = task_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    for seg_id in ("seg-1", "seg-2"):
        evidence_path = evidence_dir / f"{seg_id}.md"
        evidence_path.write_text(f"{seg_id} evidence\n")
        sidecar_dir = task_dir / "receipts" / "_injected-prompts"
        sidecar_dir.mkdir(parents=True, exist_ok=True)
        digest = ("f" if seg_id == "seg-1" else "a") * 64
        (sidecar_dir / f"{seg_id}.sha256").write_text(digest)
        receipt_executor_done(
            task_dir,
            segment_id=seg_id,
            executor_type="backend-executor" if seg_id == "seg-1" else "testing-executor",
            model_used="sonnet",
            injected_prompt_sha256=digest,
            agent_name=None,
            evidence_path=str(evidence_path),
            tokens_used=5,
            diff_verified_files=[],
            no_op_justified=False,
        )
    receipt_rules_check_passed(task_dir, "all")
    return task_dir


def test_finish_blocks_until_declared_verification_passes(tmp_path: Path) -> None:
    task_dir = _finish_ready_task(tmp_path)
    _add_verify_commands(task_dir, "seg-1", [
        {"id": "vc-1", "command": "true"},
    ])

    # 1. No verification record yet: finish must block.
    result = _run_ctl("run-execution-finish", str(task_dir))
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "blocked"
    assert any("verification evidence missing" in f for f in payload["failures"])

    # 2. Capture the verification, then finish succeeds.
    capture = _run_ctl("run-verification-evidence", str(task_dir))
    assert capture.returncode == 0, capture.stdout + capture.stderr
    result = _run_ctl("run-execution-finish", str(task_dir))
    assert result.returncode == 0, result.stdout + result.stderr


def test_finish_blocks_on_failing_verification(tmp_path: Path) -> None:
    task_dir = _finish_ready_task(tmp_path)
    _add_verify_commands(task_dir, "seg-1", [
        {"id": "vc-1", "command": "exit 7"},
    ])
    capture = _run_ctl("run-verification-evidence", str(task_dir))
    assert capture.returncode == 1

    result = _run_ctl("run-execution-finish", str(task_dir))
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert any("failed (exit 7)" in f for f in payload["failures"])


def test_finish_blocks_on_stale_verification_record(tmp_path: Path) -> None:
    """A record captured for a DIFFERENT command than the approved graph's
    must not satisfy the gate (anti-substitution)."""
    task_dir = _finish_ready_task(tmp_path)
    _add_verify_commands(task_dir, "seg-1", [
        {"id": "vc-1", "command": "true"},
    ])
    capture = _run_ctl("run-verification-evidence", str(task_dir))
    assert capture.returncode == 0
    # The graph's command changes after capture (e.g. plan amendment).
    _add_verify_commands(task_dir, "seg-1", [
        {"id": "vc-1", "command": "ruff check src"},
    ])
    result = _run_ctl("run-execution-finish", str(task_dir))
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert any("different command" in f for f in payload["failures"])


def test_write_execution_graph_normalizes_verify_commands(tmp_path: Path) -> None:
    task_dir = _setup_task_dir(tmp_path / "fresh")
    graph = {
        "segments": [
            {
                "id": "seg-1",
                "executor": "backend-executor",
                "description": "Build",
                "files_expected": ["src/app.py"],
                "criteria_ids": [1],
                "depends_on": [],
                "verify_commands": [
                    {"id": "vc-1", "command": "ruff check src", "criteria_ids": [1]},
                ],
            }
        ]
    }
    payload_path = tmp_path / "graph.json"
    payload_path.write_text(json.dumps(graph))
    result = _run_ctl("write-execution-graph", str(task_dir), "--from", str(payload_path))
    assert result.returncode == 0, result.stdout + result.stderr
    persisted = json.loads((task_dir / "execution-graph.json").read_text())
    assert persisted["segments"][0]["verify_commands"] == [
        {"id": "vc-1", "command": "ruff check src", "criteria_ids": [1]}
    ]


def test_write_execution_graph_rejects_malformed_verify_commands(tmp_path: Path) -> None:
    task_dir = _setup_task_dir(tmp_path / "fresh")
    graph = {
        "segments": [
            {
                "id": "seg-1",
                "executor": "backend-executor",
                "description": "Build",
                "files_expected": ["src/app.py"],
                "criteria_ids": [1],
                "depends_on": [],
                "verify_commands": [{"id": "", "command": ""}],
            }
        ]
    }
    payload_path = tmp_path / "graph.json"
    payload_path.write_text(json.dumps(graph))
    result = _run_ctl("write-execution-graph", str(task_dir), "--from", str(payload_path))
    assert result.returncode == 1
    assert "verify_commands" in result.stderr
