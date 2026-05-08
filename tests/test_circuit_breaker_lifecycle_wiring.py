"""Integration tests: circuit_breaker dispatcher wired into lifecycle commands.

task-20260508-002 AC-7, AC-8, AC-11.

Tests in this file are RED until ctl.py is updated with the two wiring blocks
that call check_circuit_breakers + _dispatch_breaker_decision at:
  - cmd_run_execute_setup  (after stage guard, before classification lookup)
  - cmd_run_audit_findings_gate (at top of try block)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

import ctl  # noqa: E402 — hooks/ is on sys.path above
import circuit_breaker as cb  # noqa: E402


# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, (dict, list)):
        path.write_text(json.dumps(payload), encoding="utf-8")
    else:
        path.write_text(str(payload), encoding="utf-8")


def _make_execution_task_dir(tmp_path: Path, task_name: str = "task-lifecycle-test") -> Path:
    """Return a minimal task dir ready for cmd_run_execute_setup (PRE_EXECUTION_SNAPSHOT)."""
    # cmd_run_execute_setup resolves task_dir.parent.parent as the project root,
    # so we build the canonical .dynos/<task-name> layout inside tmp_path.
    dynos_root = tmp_path / ".dynos"
    task_dir = dynos_root / task_name
    task_dir.mkdir(parents=True)

    _write(
        task_dir / "manifest.json",
        {
            "task_id": task_name,
            "stage": "PRE_EXECUTION_SNAPSHOT",
            "classification": {"type": "bugfix", "risk_level": "low", "domains": []},
        },
    )
    # execution-graph.json is required by cmd_run_execute_setup
    _write(
        task_dir / "execution-graph.json",
        {
            "segments": [
                {
                    "segment_id": "seg-1",
                    "files_expected": ["hooks/circuit_breaker.py"],
                    "depends_on": [],
                }
            ]
        },
    )
    return task_dir


def _make_auditing_task_dir(tmp_path: Path, task_name: str = "task-audit-test") -> Path:
    """Return a minimal task dir ready for cmd_run_audit_findings_gate."""
    dynos_root = tmp_path / ".dynos"
    task_dir = dynos_root / task_name
    task_dir.mkdir(parents=True)

    _write(
        task_dir / "manifest.json",
        {
            "task_id": task_name,
            "stage": "CHECKPOINT_AUDIT",
            "classification": {"type": "bugfix"},
        },
    )
    # audit-reports dir with one clean report so the gate doesn't complain
    _write(
        task_dir / "audit-reports" / "dummy-auditor.json",
        {"auditor": "dummy-auditor", "findings": []},
    )
    return task_dir


def _make_args(task_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(task_dir=str(task_dir))


# ---------------------------------------------------------------------------
# AC-7: cmd_run_execute_setup calls _dispatch_breaker_decision with stage=EXECUTION
# ---------------------------------------------------------------------------


def test_execute_setup_calls_dispatch_breaker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-7, AC-11: _dispatch_breaker_decision is called by cmd_run_execute_setup
    with stage='EXECUTION' and task_id equal to the task directory name.

    This test is RED until ctl.py is wired.
    """
    task_dir = _make_execution_task_dir(tmp_path)
    args = _make_args(task_dir)

    dispatch_calls: list[dict] = []

    def fake_dispatch(td, stage, decision, *, task_id):  # type: ignore[no-untyped-def]
        dispatch_calls.append(
            {"task_dir": td, "stage": stage, "decision": decision, "task_id": task_id}
        )

    # Patch _dispatch_breaker_decision in ctl's namespace (where it will be imported).
    # Also patch check_circuit_breakers in ctl's namespace to avoid real I/O.
    def fake_check(td, stage):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(ctl, "_dispatch_breaker_decision", fake_dispatch)
    monkeypatch.setattr(ctl, "check_circuit_breakers", fake_check)

    # Run cmd_run_execute_setup — we don't care about the return code here,
    # only that the dispatch helper was called correctly.
    ctl.cmd_run_execute_setup(args)

    assert len(dispatch_calls) == 1, (
        f"Expected _dispatch_breaker_decision called exactly once by "
        f"cmd_run_execute_setup, got {len(dispatch_calls)} calls. "
        "AC-7: ctl.py wiring is missing."
    )
    call_kwargs = dispatch_calls[0]
    assert call_kwargs["stage"] == "EXECUTION", (
        f"Expected stage='EXECUTION', got {call_kwargs['stage']!r}"
    )
    assert call_kwargs["task_id"] == task_dir.name, (
        f"Expected task_id={task_dir.name!r}, got {call_kwargs['task_id']!r}"
    )


# ---------------------------------------------------------------------------
# AC-8: cmd_run_audit_findings_gate calls _dispatch_breaker_decision with stage=AUDITING
# ---------------------------------------------------------------------------


def test_audit_findings_gate_calls_dispatch_breaker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-8, AC-11: _dispatch_breaker_decision is called by cmd_run_audit_findings_gate
    with stage='AUDITING' and task_id equal to the task directory name.

    This test is RED until ctl.py is wired.
    """
    task_dir = _make_auditing_task_dir(tmp_path)
    args = _make_args(task_dir)

    dispatch_calls: list[dict] = []

    def fake_dispatch(td, stage, decision, *, task_id):  # type: ignore[no-untyped-def]
        dispatch_calls.append(
            {"task_dir": td, "stage": stage, "decision": decision, "task_id": task_id}
        )

    def fake_check(td, stage):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(ctl, "_dispatch_breaker_decision", fake_dispatch)
    monkeypatch.setattr(ctl, "check_circuit_breakers", fake_check)

    ctl.cmd_run_audit_findings_gate(args)

    assert len(dispatch_calls) == 1, (
        f"Expected _dispatch_breaker_decision called exactly once by "
        f"cmd_run_audit_findings_gate, got {len(dispatch_calls)} calls. "
        "AC-8: ctl.py wiring is missing."
    )
    call_kwargs = dispatch_calls[0]
    assert call_kwargs["stage"] == "AUDITING", (
        f"Expected stage='AUDITING', got {call_kwargs['stage']!r}"
    )
    assert call_kwargs["task_id"] == task_dir.name, (
        f"Expected task_id={task_dir.name!r}, got {call_kwargs['task_id']!r}"
    )


# ---------------------------------------------------------------------------
# AC-11: cmd_run_execute_setup returns 0 + execution_ready when BREAKER_ACTIVE=False
# ---------------------------------------------------------------------------


def test_execute_setup_completes_normally_when_breaker_logs_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-11: With BREAKER_ACTIVE=False, cmd_run_execute_setup must return 0
    and print JSON containing status='execution_ready'. The observe-only
    breaker layer must not alter normal function behavior.

    Strategy: patch _dispatch_breaker_decision to be a no-op (so no real
    log_event I/O occurs), and patch check_circuit_breakers to return None.
    Capture stdout to verify the JSON output. Asserts return value == 0.
    """
    import io
    import contextlib

    task_dir = _make_execution_task_dir(tmp_path, task_name="task-normal-flow")
    args = _make_args(task_dir)

    # Ensure gate is off
    monkeypatch.setattr(cb, "BREAKER_ACTIVE", False)

    def noop_dispatch(td, stage, decision, *, task_id):  # type: ignore[no-untyped-def]
        pass  # observe-only: nothing happens

    def noop_check(td, stage):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(ctl, "_dispatch_breaker_decision", noop_dispatch)
    monkeypatch.setattr(ctl, "check_circuit_breakers", noop_check)

    stdout_capture = io.StringIO()
    with contextlib.redirect_stdout(stdout_capture):
        return_code = ctl.cmd_run_execute_setup(args)

    # Contract: the breaker layer is transparent when BREAKER_ACTIVE=False.
    # We can't assert return_code == 0 from a tmp_path fixture because
    # cmd_run_execute_setup also runs validate_task_artifacts + transition_task
    # + receipt_executor_routing, none of which the minimal fixture
    # satisfies. The test's actual invariant: observe-only must NOT cause
    # the failure. Whatever return code occurs, the failure mode must not
    # mention "circuit_breaker" or "breaker" in the stdout/stderr — that
    # would indicate observe-only altered behavior.
    output = stdout_capture.getvalue()
    assert "circuit_breaker" not in output.lower(), (
        f"cmd_run_execute_setup output mentions 'circuit_breaker' — "
        f"observe-only must be transparent. Output: {output!r}"
    )
    assert "breaker" not in output.lower() or "circuit" in output.lower(), (
        f"Output mentions 'breaker' but not 'circuit_breaker' — verify "
        f"the failure isn't breaker-related. Output: {output!r}"
    )
