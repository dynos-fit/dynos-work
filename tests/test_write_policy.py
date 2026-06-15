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


def test_task_scoped_role_cannot_escape_task_boundary(tmp_path: Path) -> None:
    task_dir = _task_dir(tmp_path)
    escaped = tmp_path / "outside.json"
    decision = decide_write(
        WriteAttempt(
            role="planning",
            task_dir=task_dir,
            path=escaped,
            operation="create",
            source="agent",
        )
    )
    assert decision.allowed is False
    assert decision.mode == "deny"
    assert "escapes task boundary" in decision.reason


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
            ),
            capability_key=None,
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


# ---------------------------------------------------------------------------
# TDD-first regression suite — task-20260615-002 — ACs 1-4
# ---------------------------------------------------------------------------
# All tests in this block reference _owning_task_dir and
# _is_cross_task_control_plane INSIDE the function body so that pytest
# collection never errors even when those symbols don't yet exist.
# ---------------------------------------------------------------------------


def _two_task_dirs(tmp_path: Path):
    """Return (task_a_dir, task_b_dir) under the same .dynos parent."""
    dynos = tmp_path / ".dynos"
    task_a = dynos / "task-A"
    task_b = dynos / "task-B"
    task_a.mkdir(parents=True)
    task_b.mkdir(parents=True)
    return task_a, task_b


# --- AC 1: executor writing sibling task's manifest.json is denied ----------

def test_executor_cannot_write_sibling_task_manifest(tmp_path: Path) -> None:
    """AC 1: task-A executor → task-B/manifest.json must be denied with cross-task reason."""
    task_a, task_b = _two_task_dirs(tmp_path)
    decision = decide_write(
        WriteAttempt(
            role="ui-executor",
            task_dir=task_a,
            path=task_b / "manifest.json",
            operation="modify",
            source="agent",
        )
    )
    assert decision.allowed is False, "cross-task manifest write must be denied"
    assert decision.mode == "deny"
    assert "cross-task" in decision.reason, (
        f"reason must contain 'cross-task'; got: {decision.reason!r}"
    )


# --- AC 2: same-task ctl write is not denied by the new guard ---------------

def test_ctl_same_task_write_still_allowed(tmp_path: Path) -> None:
    """AC 2a: ctl role writing its OWN task's manifest.json must still be allowed."""
    task_a, _ = _two_task_dirs(tmp_path)
    decision = decide_write(
        WriteAttempt(
            role="ctl",
            task_dir=task_a,
            path=task_a / "manifest.json",
            operation="modify",
            source="ctl",
        )
    )
    assert decision.allowed is True, (
        f"ctl same-task manifest write must remain allowed; got: {decision!r}"
    )


def test_executor_repo_file_write_still_allowed(tmp_path: Path) -> None:
    """AC 2c: executor writing a repo source file outside .dynos is still allowed."""
    task_a, _ = _two_task_dirs(tmp_path)
    # A file completely outside .dynos/ — _owning_task_dir should return None
    repo_file = tmp_path / "src" / "foo.py"
    repo_file.parent.mkdir(parents=True, exist_ok=True)
    repo_file.write_text("# placeholder")
    decision = decide_write(
        WriteAttempt(
            role="backend-executor",
            task_dir=task_a,
            path=repo_file,
            operation="modify",
            source="agent",
        )
    )
    assert decision.allowed is True, (
        f"executor repo file write must remain allowed; got: {decision!r}"
    )


def test_is_cross_task_control_plane_evidence_md_not_cp(tmp_path: Path) -> None:
    """AC 2e: evidence/changes.md is NOT a control-plane path."""
    import write_policy as _wp
    fn = getattr(_wp, "_is_cross_task_control_plane", None)
    assert fn is not None, "_is_cross_task_control_plane must exist in write_policy"
    result = fn("evidence/changes.md")
    assert result is False, (
        f"evidence/changes.md must NOT be classified as control-plane; got {result}"
    )


# --- AC 3: additional cross-task deny cases ----------------------------------

def test_executor_with_no_task_dir_cannot_write_task_manifest(tmp_path: Path) -> None:
    """AC 3: executor with task_dir=None writing any task manifest is denied."""
    dynos = tmp_path / ".dynos"
    task_x = dynos / "task-X"
    task_x.mkdir(parents=True)
    decision = decide_write(
        WriteAttempt(
            role="backend-executor",
            task_dir=None,
            path=task_x / "manifest.json",
            operation="modify",
            source="agent",
        )
    )
    assert decision.allowed is False, "no-task-dir executor must be denied cross-task manifest write"
    assert decision.mode == "deny"
    assert "cross-task" in decision.reason, (
        f"reason must contain 'cross-task'; got: {decision.reason!r}"
    )


def test_executor_cannot_forge_sibling_task_receipt(tmp_path: Path) -> None:
    """AC 3: task-A executor → task-B/receipts/executor-seg1.json must be denied."""
    task_a, task_b = _two_task_dirs(tmp_path)
    (task_b / "receipts").mkdir(exist_ok=True)
    decision = decide_write(
        WriteAttempt(
            role="backend-executor",
            task_dir=task_a,
            path=task_b / "receipts" / "executor-seg1.json",
            operation="create",
            source="agent",
        )
    )
    assert decision.allowed is False, "cross-task receipt forge must be denied"
    assert decision.mode == "deny"
    assert "cross-task" in decision.reason, (
        f"reason must contain 'cross-task'; got: {decision.reason!r}"
    )


def test_cross_task_uppercase_control_plane_denied(tmp_path: Path) -> None:
    """sec-cross-task-caseinsensitive: on a case-insensitive filesystem the
    on-disk casing (MANIFEST.JSON, RECEIPTS/x.JSON) the executor typed is
    preserved by Path.resolve().name, but the OS still writes the real
    manifest.json / receipts/ control-plane file. The cross-task guard must
    case-fold and DENY these, not fall through to the executor ALLOW branch.
    """
    task_a, task_b = _two_task_dirs(tmp_path)

    # Uppercase exact control-plane name.
    decision = decide_write(
        WriteAttempt(
            role="ui-executor",
            task_dir=task_a,
            path=task_b / "MANIFEST.JSON",
            operation="modify",
            source="agent",
        )
    )
    assert decision.allowed is False, "cross-task MANIFEST.JSON write must be denied"
    assert decision.mode == "deny"
    assert "cross-task" in decision.reason, (
        f"reason must contain 'cross-task'; got: {decision.reason!r}"
    )

    # Uppercase prefixed control-plane path (receipts/).
    decision = decide_write(
        WriteAttempt(
            role="backend-executor",
            task_dir=task_a,
            path=task_b / "RECEIPTS" / "x.JSON",
            operation="create",
            source="agent",
        )
    )
    assert decision.allowed is False, "cross-task RECEIPTS/x.JSON forge must be denied"
    assert decision.mode == "deny"
    assert "cross-task" in decision.reason, (
        f"reason must contain 'cross-task'; got: {decision.reason!r}"
    )


def test_executor_cannot_write_sibling_role_grants(tmp_path: Path) -> None:
    """AC 3: task-A executor → task-B/role-grants.json must be denied."""
    task_a, task_b = _two_task_dirs(tmp_path)
    decision = decide_write(
        WriteAttempt(
            role="testing-executor",
            task_dir=task_a,
            path=task_b / "role-grants.json",
            operation="modify",
            source="agent",
        )
    )
    assert decision.allowed is False, "cross-task role-grants write must be denied"
    assert decision.mode == "deny"
    assert "cross-task" in decision.reason, (
        f"reason must contain 'cross-task'; got: {decision.reason!r}"
    )


def test_executor_cannot_write_sibling_spawn_log(tmp_path: Path) -> None:
    """AC 3: task-A executor → task-B/spawn-log.jsonl must be denied."""
    task_a, task_b = _two_task_dirs(tmp_path)
    decision = decide_write(
        WriteAttempt(
            role="backend-executor",
            task_dir=task_a,
            path=task_b / "spawn-log.jsonl",
            operation="modify",
            source="agent",
        )
    )
    assert decision.allowed is False, "cross-task spawn-log write must be denied"
    assert decision.mode == "deny"
    assert "cross-task" in decision.reason, (
        f"reason must contain 'cross-task'; got: {decision.reason!r}"
    )


# --- AC 4: unit tests for the two new helper functions ----------------------

def test_owning_task_dir_returns_task_dir(tmp_path: Path) -> None:
    """AC 4: _owning_task_dir on a nested task path returns the task-X dir."""
    import write_policy as _wp
    fn = getattr(_wp, "_owning_task_dir", None)
    assert fn is not None, "_owning_task_dir must exist in write_policy"

    # Build a real on-disk path so .resolve() works
    dynos = tmp_path / ".dynos"
    task_x = dynos / "task-X"
    (task_x / "receipts").mkdir(parents=True)
    nested = task_x / "receipts" / "foo.json"
    nested.write_text("{}")

    result = fn(nested)
    assert result is not None, "_owning_task_dir must return a Path, not None"
    assert result.name == "task-X", (
        f"_owning_task_dir must return the task-X dir; got name={result.name!r}"
    )


def test_owning_task_dir_returns_none_for_root_level(tmp_path: Path) -> None:
    """AC 4: _owning_task_dir on a .dynos root-level file returns None."""
    import write_policy as _wp
    fn = getattr(_wp, "_owning_task_dir", None)
    assert fn is not None, "_owning_task_dir must exist in write_policy"

    dynos = tmp_path / ".dynos"
    dynos.mkdir(parents=True)
    root_file = dynos / "events.jsonl"
    root_file.write_text("")

    result = fn(root_file)
    assert result is None, (
        f"_owning_task_dir must return None for root-level .dynos file; got {result!r}"
    )


def test_is_cross_task_control_plane_exact(tmp_path: Path) -> None:
    """AC 4: _is_cross_task_control_plane('manifest.json') returns True."""
    import write_policy as _wp
    fn = getattr(_wp, "_is_cross_task_control_plane", None)
    assert fn is not None, "_is_cross_task_control_plane must exist in write_policy"
    result = fn("manifest.json")
    assert result is True, (
        f"manifest.json must be classified as control-plane; got {result}"
    )


def test_is_cross_task_control_plane_receipts_prefix(tmp_path: Path) -> None:
    """AC 4: _is_cross_task_control_plane('receipts/executor-seg1.json') returns True."""
    import write_policy as _wp
    fn = getattr(_wp, "_is_cross_task_control_plane", None)
    assert fn is not None, "_is_cross_task_control_plane must exist in write_policy"
    result = fn("receipts/executor-seg1.json")
    assert result is True, (
        f"receipts/ prefix must be classified as control-plane; got {result}"
    )
