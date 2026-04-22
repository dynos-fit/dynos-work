from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

from ctl import _write_ctl_json  # noqa: E402
from lib_log import log_event  # noqa: E402
from lib_tokens import get_summary, record_tokens  # noqa: E402
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


def test_record_tokens_emits_policy_event(tmp_path: Path) -> None:
    task_dir = _task_dir(tmp_path)
    record_tokens(
        task_dir=task_dir,
        agent="planner",
        model="sonnet",
        input_tokens=10,
        output_tokens=5,
        phase="planning",
        stage="PLAN_REVIEW",
        event_type="spawn",
    )
    data = json.loads((task_dir / "token-usage.json").read_text())
    assert data["total"] == 15
    events = [json.loads(line) for line in (task_dir / "events.jsonl").read_text().splitlines() if line.strip()]
    assert any(
        line.get("event") == "write_policy_allowed"
        and line.get("path") == f".dynos/{task_dir.name}/token-usage.json"
        for line in events
    )


def test_get_summary_emits_policy_event(tmp_path: Path) -> None:
    task_dir = _task_dir(tmp_path)
    (task_dir / "token-usage.json").write_text(
        json.dumps(
            {
                "agents": {},
                "by_agent": {},
                "by_model": {},
                "total": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "events": [],
            }
        )
    )
    get_summary(task_dir)
    events = [json.loads(line) for line in (task_dir / "events.jsonl").read_text().splitlines() if line.strip()]
    assert any(
        line.get("event") == "write_policy_allowed"
        and line.get("path") == f".dynos/{task_dir.name}/token-usage.json"
        for line in events
    )


def test_log_event_task_scoped_writes_under_policy(tmp_path: Path) -> None:
    task_dir = _task_dir(tmp_path)
    log_event(tmp_path, "example_event", task=task_dir.name, detail="ok")
    lines = [json.loads(line) for line in (task_dir / "events.jsonl").read_text().splitlines() if line.strip()]
    assert lines[-1]["event"] == "example_event"


def test_log_event_global_fallback_writes_under_policy(tmp_path: Path) -> None:
    log_event(tmp_path, "global_example", detail="ok")
    global_events = tmp_path / ".dynos" / "events.jsonl"
    lines = [json.loads(line) for line in global_events.read_text().splitlines() if line.strip()]
    assert lines[-1]["event"] == "global_example"


def test_ctl_json_write_emits_policy_event(tmp_path: Path) -> None:
    task_dir = _task_dir(tmp_path)
    _write_ctl_json(task_dir, task_dir / "manifest.json", {"task_id": task_dir.name, "stage": "EXECUTION"})
    events = [json.loads(line) for line in (task_dir / "events.jsonl").read_text().splitlines() if line.strip()]
    assert any(
        line.get("event") == "write_policy_allowed"
        and line.get("path") == f".dynos/{task_dir.name}/manifest.json"
        for line in events
    )
