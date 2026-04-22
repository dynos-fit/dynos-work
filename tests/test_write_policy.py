from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

from write_policy import WriteAttempt, decide_write, require_write_allowed  # noqa: E402


def _task_dir(tmp_path: Path) -> Path:
    task_dir = tmp_path / ".dynos" / "task-20260421-001"
    task_dir.mkdir(parents=True)
    return task_dir


def test_planning_cannot_write_manifest(tmp_path: Path) -> None:
    task_dir = _task_dir(tmp_path)
    decision = decide_write(
        WriteAttempt(
            role="planning",
            task_dir=task_dir,
            path=task_dir / "manifest.json",
            operation="modify",
            source="agent",
        )
    )
    assert decision.allowed is False
    assert decision.mode == "deny"
    assert "control-plane" in decision.reason


def test_receipt_writer_can_write_receipts(tmp_path: Path) -> None:
    task_dir = _task_dir(tmp_path)
    decision = decide_write(
        WriteAttempt(
            role="receipt-writer",
            task_dir=task_dir,
            path=task_dir / "receipts" / "audit-x.json",
            operation="create",
            source="receipt-writer",
        )
    )
    assert decision.allowed is True
    assert decision.mode == "direct"


def test_executor_cannot_write_receipts(tmp_path: Path) -> None:
    task_dir = _task_dir(tmp_path)
    decision = decide_write(
        WriteAttempt(
            role="backend-executor",
            task_dir=task_dir,
            path=task_dir / "receipts" / "audit-x.json",
            operation="create",
            source="agent",
        )
    )
    assert decision.allowed is False
    assert decision.mode == "deny"


def test_repair_log_is_wrapper_required_for_agent(tmp_path: Path) -> None:
    task_dir = _task_dir(tmp_path)
    decision = decide_write(
        WriteAttempt(
            role="repair-coordinator",
            task_dir=task_dir,
            path=task_dir / "repair-log.json",
            operation="create",
            source="agent",
        )
    )
    assert decision.allowed is False
    assert decision.mode == "wrapper"
    assert "write-repair-log" in str(decision.wrapper_command)


def test_classification_is_wrapper_required_for_planning(tmp_path: Path) -> None:
    task_dir = _task_dir(tmp_path)
    decision = decide_write(
        WriteAttempt(
            role="planning",
            task_dir=task_dir,
            path=task_dir / "classification.json",
            operation="create",
            source="agent",
        )
    )
    assert decision.allowed is False
    assert decision.mode == "wrapper"
    assert "write-classification" in str(decision.wrapper_command)


def test_ctl_can_write_handoff(tmp_path: Path) -> None:
    task_dir = _task_dir(tmp_path)
    decision = decide_write(
        WriteAttempt(
            role="ctl",
            task_dir=task_dir,
            path=task_dir / "handoff-execute-audit.json",
            operation="create",
            source="ctl",
        )
    )
    assert decision.allowed is True
    assert decision.mode == "direct"


def test_require_write_allowed_emits_denial_event(tmp_path: Path) -> None:
    task_dir = _task_dir(tmp_path)
    events = task_dir / "events.jsonl"
    try:
        require_write_allowed(
            WriteAttempt(
                role="planning",
                task_dir=task_dir,
                path=task_dir / "manifest.json",
                operation="modify",
                source="agent",
            )
        )
    except ValueError:
        pass
    lines = [json.loads(line) for line in events.read_text().splitlines() if line.strip()]
    assert any(line.get("event") == "write_policy_denied" for line in lines)
