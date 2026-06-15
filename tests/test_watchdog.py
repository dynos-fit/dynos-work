"""Tests for AC 5: Per-session write-first watchdog in pre_tool_use.py.

These tests are RED by design until seg-4 adds the watchdog (_run_watchdog)
to hooks/pre_tool_use.py.

CRITICAL (Finding B): session_id is injected via the payload dict (stdin JSON).
There is NO DYNOS_SESSION_ID env var.

All tests drive the real watchdog logic in pre_tool_use.py via main().
Fixtures place role-grants.json with expected_artifact + budget.
"""

from __future__ import annotations

import contextlib
import io
import json
import math
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

import pre_tool_use  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

SESSION_ID = "session-watchdog-test-0001"
AUDITOR_ROLE = "audit-security"


def _setup_task_dir(tmp_path: Path) -> Path:
    """Create a minimal task dir structure."""
    project = tmp_path / "project"
    task_dir = project / ".dynos" / "task-20260612-watchdog"
    task_dir.mkdir(parents=True)
    (task_dir / "audit-reports").mkdir(exist_ok=True)
    return task_dir


def _write_grants(
    task_dir: Path,
    role: str,
    expected_artifact: str | None,
    budget: int = 21,
    session_id: str = SESSION_ID,
) -> None:
    """Write a role-grants.json and matching role-bindings.json for the session."""
    import actor_identity  # noqa: PLC0415
    now = time.time()
    grant: dict = {
        "role": role,
        "granted_at": now,
        "expires_at": now + 3600,
        "consumed_by": session_id,
        "consumed_at": now,
    }
    if expected_artifact is not None:
        grant["expected_artifact"] = expected_artifact
    grant["budget"] = budget
    ledger = {"grants": [grant]}
    grants_path = task_dir / "role-grants.json"
    grants_path.write_text(json.dumps(ledger), encoding="utf-8")

    # Write role-bindings.json so lookup_binding returns the correct role
    bindings = {
        "bindings": {
            session_id: {
                "role": role,
                "bound_at": now,
            }
        }
    }
    bindings_path = task_dir / "role-bindings.json"
    bindings_path.write_text(json.dumps(bindings), encoding="utf-8")


def _write_pin(task_dir: Path) -> None:
    """Write a pin file so the orchestrator session != our subagent session."""
    project = task_dir.parent.parent
    pin = {"session_id": "session-orchestrator-MAIN"}
    pin_path = project / ".dynos" / "orchestrator-pin.json"
    pin_path.write_text(json.dumps(pin), encoding="utf-8")


def _write_counter(task_dir: Path, session_id: str, call_count: int, deny_count: int = 0, cooldown: int = 0) -> None:
    """Write a tool-call-counters.json file with given state."""
    counters = {
        "sessions": {
            session_id: {
                "call_count": call_count,
                "deny_count": deny_count,
                "last_deny_at_call": call_count if deny_count > 0 else None,
                "cooldown_remaining": cooldown,
            }
        }
    }
    counter_path = task_dir / "tool-call-counters.json"
    counter_path.write_text(json.dumps(counters), encoding="utf-8")


def _write_skeleton(task_dir: Path, artifact_relpath: str, status: str = "in_progress", findings: list | None = None, ledger: bool = False) -> Path:
    """Write a skeleton artifact file."""
    artifact_path = task_dir / artifact_relpath
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    content: dict = {
        "status": status,
        "findings": findings or [],
    }
    if ledger:
        content["notes"] = "## Progress Ledger\n### Done\n- nothing\n### In-Flight\n### Next"
    artifact_path.write_text(json.dumps(content), encoding="utf-8")
    return artifact_path


def _invoke_watchdog(
    monkeypatch: pytest.MonkeyPatch,
    task_dir: Path,
    tool_name: str,
    tool_input: dict,
    session_id: str = SESSION_ID,
    env_role: str | None = None,
) -> tuple[int, str]:
    """Invoke pre_tool_use.main() with a payload dict (session_id in payload)."""
    payload = {
        "tool_name": tool_name,
        "tool_input": tool_input,
        "cwd": str(task_dir),
        "session_id": session_id,
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setenv("DYNOS_TASK_DIR", str(task_dir))
    if env_role is not None:
        monkeypatch.setenv("DYNOS_ROLE", env_role)
    else:
        monkeypatch.delenv("DYNOS_ROLE", raising=False)
    # Reset module-level cached imports so each test gets a fresh main()
    monkeypatch.setattr(pre_tool_use, "decide_write", None)
    monkeypatch.setattr(pre_tool_use, "_emit_policy_event", None)
    monkeypatch.setattr(pre_tool_use, "WriteAttempt", None)
    monkeypatch.setattr(pre_tool_use, "decide_read", None)
    monkeypatch.setattr(pre_tool_use, "ReadAttempt_RP", None)
    monkeypatch.setattr(pre_tool_use, "_emit_read_policy_event", None)
    monkeypatch.setattr(pre_tool_use, "log_event", None)
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        code = pre_tool_use.main()
    return code, stderr.getvalue()


# ---------------------------------------------------------------------------
# AC 5: Watchdog deny-once-with-instruction
# ---------------------------------------------------------------------------


def test_watchdog_deny_once_then_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Watchdog denies exactly once at ceil(budget/3); subsequent call allowed.

    AC 5: When call_count >= ceil(budget/3) and artifact has no content,
    the watchdog DENIES with exit code 2 and an exact stderr message.
    A subsequent non-artifact call is ALLOWED (deny_count >= 1 → ALLOW).
    """
    task_dir = _setup_task_dir(tmp_path)
    _write_pin(task_dir)
    artifact_relpath = "audit-reports/security-haiku-attempt-1.json"  # noqa: model-literal
    _write_grants(task_dir, AUDITOR_ROLE, expected_artifact=artifact_relpath, budget=21)
    # Write an empty in_progress skeleton (no findings, no ledger)
    _write_skeleton(task_dir, artifact_relpath, status="in_progress", findings=[])
    # Simulate call_count already at ceil(21/3)=7 — this is the checkpoint
    _write_counter(task_dir, SESSION_ID, call_count=6, deny_count=0)

    # Call with a non-artifact, non-Bash write tool to trigger the watchdog
    # Use a Write tool targeting a different file (not the expected artifact)
    other_path = task_dir / "evidence" / "notes.md"
    other_path.parent.mkdir(parents=True, exist_ok=True)
    code, stderr = _invoke_watchdog(
        monkeypatch,
        task_dir,
        tool_name="Write",
        tool_input={"file_path": str(other_path), "content": "notes"},
        env_role=AUDITOR_ROLE,
    )
    # First call at checkpoint: DENY
    assert code == 2, f"Expected deny (exit 2) at watchdog checkpoint, got {code}"
    artifact_path = task_dir / artifact_relpath
    assert str(artifact_path) in stderr or artifact_relpath in stderr, (
        f"Deny message must reference artifact path, stderr: {stderr!r}"
    )
    assert "write-first checkpoint" in stderr, (
        f"Deny message must contain 'write-first checkpoint', stderr: {stderr!r}"
    )

    # Second call (deny_count=1 now): ALLOW — watchdog silent
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "tool_name": "Write",
        "tool_input": {"file_path": str(other_path), "content": "updated"},
        "cwd": str(task_dir),
        "session_id": SESSION_ID,
    })))
    monkeypatch.setenv("DYNOS_TASK_DIR", str(task_dir))
    monkeypatch.setenv("DYNOS_ROLE", AUDITOR_ROLE)
    monkeypatch.setattr(pre_tool_use, "decide_write", None)
    monkeypatch.setattr(pre_tool_use, "_emit_policy_event", None)
    monkeypatch.setattr(pre_tool_use, "WriteAttempt", None)
    monkeypatch.setattr(pre_tool_use, "decide_read", None)
    monkeypatch.setattr(pre_tool_use, "ReadAttempt_RP", None)
    monkeypatch.setattr(pre_tool_use, "_emit_read_policy_event", None)
    monkeypatch.setattr(pre_tool_use, "log_event", None)
    stderr2 = io.StringIO()
    with contextlib.redirect_stderr(stderr2):
        code2 = pre_tool_use.main()
    # SEC-001: after its single deny the watchdog is SILENT (one deny per session) —
    # it must NOT emit a second write-first deny. Crucially it returns None and defers
    # to write_policy rather than short-circuiting main() with a 0 (which would let the
    # session bypass the write-boundary policy entirely). The watchdog's job is to nudge
    # write-first, never to GRANT a write the policy would deny.
    assert "write-first checkpoint" not in stderr2.getvalue(), (
        f"Watchdog must be silent after its one deny (no second nudge), "
        f"got stderr: {stderr2.getvalue()!r}"
    )


def test_watchdog_artifact_write_always_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Write targeting the expected artifact is always allowed at checkpoint.

    AC 5 (D6): Artifact-path Write tool calls are NEVER denied by the watchdog,
    even when call_count >= ceil(budget/3) and artifact has no content.
    """
    task_dir = _setup_task_dir(tmp_path)
    _write_pin(task_dir)
    artifact_relpath = "audit-reports/security-haiku-attempt-1.json"  # noqa: model-literal
    artifact_path = task_dir / artifact_relpath
    _write_grants(task_dir, AUDITOR_ROLE, expected_artifact=artifact_relpath, budget=21)
    _write_skeleton(task_dir, artifact_relpath, status="in_progress", findings=[])
    # Put call_count at exactly the checkpoint
    _write_counter(task_dir, SESSION_ID, call_count=6, deny_count=0)

    # Write targeting the EXPECTED artifact itself — must always be allowed
    code, stderr = _invoke_watchdog(
        monkeypatch,
        task_dir,
        tool_name="Write",
        tool_input={"file_path": str(artifact_path), "content": json.dumps({
            "status": "partial",
            "findings": [{"id": "F001"}],
        })},
        env_role=AUDITOR_ROLE,
    )
    assert code == 0, (
        f"Write to expected artifact must be ALLOWED (exit 0) at checkpoint, "
        f"got code={code}, stderr={stderr!r}"
    )
    assert "write-first checkpoint" not in stderr, (
        f"No watchdog deny message expected for artifact write, stderr: {stderr!r}"
    )


def test_watchdog_skips_when_no_expected_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Watchdog skips entirely when the grant has no expected_artifact.

    AC 5: If expected_artifact is absent or null in the grant, the watchdog
    skips entirely (ALLOW) regardless of call count.
    """
    task_dir = _setup_task_dir(tmp_path)
    _write_pin(task_dir)
    # Grant WITHOUT expected_artifact
    _write_grants(task_dir, AUDITOR_ROLE, expected_artifact=None, budget=21)
    # Simulate late call count — watchdog checkpoint would normally fire
    _write_counter(task_dir, SESSION_ID, call_count=100, deny_count=0)

    other_path = task_dir / "evidence" / "notes.md"
    other_path.parent.mkdir(parents=True, exist_ok=True)
    code, stderr = _invoke_watchdog(
        monkeypatch,
        task_dir,
        tool_name="Write",
        tool_input={"file_path": str(other_path), "content": "notes"},
        env_role=AUDITOR_ROLE,
    )
    # No expected_artifact → watchdog skips → ALLOW (policy may still deny unrelated)
    # The key invariant: watchdog must NOT deny (no "write-first checkpoint" in stderr)
    assert "write-first checkpoint" not in stderr, (
        f"Watchdog must skip when no expected_artifact, got stderr: {stderr!r}"
    )


def test_watchdog_cooldown_k5(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After one deny, 5 subsequent non-artifact calls are all ALLOWED.

    AC 5 (K=5): After the single deny, cooldown_remaining=5 is set.
    Each of the next 5 non-artifact calls must be allowed (cooldown decrements).
    After cooldown, deny_count=1 means the 6th call is also allowed (one deny max).
    """
    task_dir = _setup_task_dir(tmp_path)
    _write_pin(task_dir)
    artifact_relpath = "audit-reports/security-haiku-attempt-1.json"  # noqa: model-literal
    _write_grants(task_dir, AUDITOR_ROLE, expected_artifact=artifact_relpath, budget=21)
    _write_skeleton(task_dir, artifact_relpath, status="in_progress", findings=[])
    # Simulate state AFTER a deny: deny_count=1, cooldown_remaining=5
    _write_counter(task_dir, SESSION_ID, call_count=7, deny_count=1, cooldown=5)

    other_path = task_dir / "evidence" / "notes.md"
    other_path.parent.mkdir(parents=True, exist_ok=True)

    for i in range(6):
        # Each of 6 subsequent calls (5 cooldown + 1 post-cooldown) must be allowed
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(other_path), "content": f"call {i}"},
            "cwd": str(task_dir),
            "session_id": SESSION_ID,
        }
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        monkeypatch.setenv("DYNOS_TASK_DIR", str(task_dir))
        monkeypatch.setenv("DYNOS_ROLE", AUDITOR_ROLE)
        monkeypatch.setattr(pre_tool_use, "decide_write", None)
        monkeypatch.setattr(pre_tool_use, "_emit_policy_event", None)
        monkeypatch.setattr(pre_tool_use, "WriteAttempt", None)
        monkeypatch.setattr(pre_tool_use, "decide_read", None)
        monkeypatch.setattr(pre_tool_use, "ReadAttempt_RP", None)
        monkeypatch.setattr(pre_tool_use, "_emit_read_policy_event", None)
        monkeypatch.setattr(pre_tool_use, "log_event", None)
        stderr_buf = io.StringIO()
        with contextlib.redirect_stderr(stderr_buf):
            code = pre_tool_use.main()
        stderr_text = stderr_buf.getvalue()
        assert "write-first checkpoint" not in stderr_text, (
            f"Call {i} after initial deny must NOT receive another watchdog deny, "
            f"stderr: {stderr_text!r}"
        )
