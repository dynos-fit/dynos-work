"""Tests for per-actor role resolution (D3 in docs/permissions-on-design.md).

The orchestrator session is pinned at SessionStart and ALWAYS resolves to the
'orchestrator' role; subagent sessions consume single-use grants. The suite's
most important member is the self-elevation regression: granting an audit
role must NOT let the orchestrator write audit-reports/.
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

import actor_identity  # noqa: E402
import pre_tool_use  # noqa: E402
from write_policy import WriteAttempt, decide_write  # noqa: E402

ORCH_SESSION = "session-orchestrator-0001"
SUB_SESSION_A = "session-subagent-aaaa"
SUB_SESSION_B = "session-subagent-bbbb"


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Project root with a task dir and a pinned orchestrator session."""
    root = tmp_path / "proj"
    task_dir = root / ".dynos" / "task-20260611-001"
    task_dir.mkdir(parents=True)
    (task_dir / "manifest.json").write_text(json.dumps({
        "task_id": "task-20260611-001",
        "stage": "PLANNING",
        "fast_track": False,
    }))
    actor_identity.pin_orchestrator(root, {"session_id": ORCH_SESSION})
    monkeypatch.delenv("DYNOS_ROLE", raising=False)
    monkeypatch.setenv("DYNOS_TASK_DIR", str(task_dir))
    return root


def _task_dir(root: Path) -> Path:
    return root / ".dynos" / "task-20260611-001"


def _run_hook(
    monkeypatch: pytest.MonkeyPatch,
    *,
    session_id: str,
    tool_name: str,
    tool_input: dict,
    cwd: Path,
) -> tuple[int, str]:
    payload = {
        "tool_name": tool_name,
        "tool_input": tool_input,
        "cwd": str(cwd),
        "session_id": session_id,
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        code = pre_tool_use.main()
    return code, stderr.getvalue()


def _grant(task_dir: Path, role: str) -> None:
    result = subprocess.run(
        ["python3", str(ROOT / "hooks" / "ctl.py"), "grant-role", str(task_dir), "--role", role],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# The self-elevation regression test
# ---------------------------------------------------------------------------

def test_orchestrator_cannot_write_audit_reports_even_with_audit_grant(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Granting (or stamping) an audit role must never elevate the
    orchestrator's own session — the historical 'stuck role' behavior was
    exactly this leak, and it is the forgery-adjacent primitive."""
    task_dir = _task_dir(project)
    _grant(task_dir, "audit-security")
    code, err = _run_hook(
        monkeypatch,
        session_id=ORCH_SESSION,
        tool_name="Write",
        tool_input={
            "file_path": str(task_dir / "audit-reports" / "security-1.json"),
            "content": "{}",
        },
        cwd=project,
    )
    assert code == 2
    assert "role=orchestrator" in err
    # Degraded-mode diagnostic: a pending grant exists, so the denial says
    # how to recognize a mis-attributed subagent call.
    assert "degraded actor resolution" in err


def test_orchestrator_ignores_stamped_role_file(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The P1-a fix: stamping a subagent role no longer mutates the
    orchestrator's own rights."""
    task_dir = _task_dir(project)
    (task_dir / "active-segment-role").write_text("planning")
    # planning may write spec.md — the orchestrator must NOT inherit that.
    code, err = _run_hook(
        monkeypatch,
        session_id=ORCH_SESSION,
        tool_name="Write",
        tool_input={"file_path": str(task_dir / "spec.md"), "content": "# spec"},
        cwd=project,
    )
    assert code == 2
    assert "role=orchestrator" in err


def test_orchestrator_writes_its_own_coordination_files(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_dir = _task_dir(project)
    for filename in ("execution-log.md", "escalation.md", "discovery-notes.md"):
        code, err = _run_hook(
            monkeypatch,
            session_id=ORCH_SESSION,
            tool_name="Write",
            tool_input={"file_path": str(task_dir / filename), "content": "x"},
            cwd=project,
        )
        assert code == 0, f"{filename}: {err}"


def test_orchestrator_repo_write_denied_outside_inline_execution(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, err = _run_hook(
        monkeypatch,
        session_id=ORCH_SESSION,
        tool_name="Write",
        tool_input={"file_path": str(project / "src" / "app.py"), "content": "x"},
        cwd=project,
    )
    assert code == 2
    assert "inline fast-track" in err


def test_orchestrator_repo_write_allowed_during_inline_fast_track(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_dir = _task_dir(project)
    (task_dir / "manifest.json").write_text(json.dumps({
        "task_id": "task-20260611-001",
        "stage": "EXECUTION",
        "fast_track": True,
    }))
    code, err = _run_hook(
        monkeypatch,
        session_id=ORCH_SESSION,
        tool_name="Write",
        tool_input={"file_path": str(project / "src" / "app.py"), "content": "x"},
        cwd=project,
    )
    assert code == 0, err


# ---------------------------------------------------------------------------
# Subagent grant consumption and binding
# ---------------------------------------------------------------------------

def test_subagent_consumes_grant_and_writes_its_artifact(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_dir = _task_dir(project)
    _grant(task_dir, "planning")
    code, err = _run_hook(
        monkeypatch,
        session_id=SUB_SESSION_A,
        tool_name="Write",
        tool_input={"file_path": str(task_dir / "spec.md"), "content": "# spec"},
        cwd=project,
    )
    assert code == 0, err
    # Binding persisted and grant consumed.
    assert actor_identity.lookup_binding(task_dir, SUB_SESSION_A) == "planning"
    assert actor_identity.pending_grants(task_dir) == []


def test_binding_is_immutable_for_session_lifetime(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later grant (for the next spawn) must not re-role a bound session."""
    task_dir = _task_dir(project)
    _grant(task_dir, "planning")
    code, _ = _run_hook(
        monkeypatch,
        session_id=SUB_SESSION_A,
        tool_name="Write",
        tool_input={"file_path": str(task_dir / "spec.md"), "content": "# spec"},
        cwd=project,
    )
    assert code == 0
    _grant(task_dir, "audit-security")
    # Session A is still planning: audit-reports write denied.
    code, err = _run_hook(
        monkeypatch,
        session_id=SUB_SESSION_A,
        tool_name="Write",
        tool_input={
            "file_path": str(task_dir / "audit-reports" / "x.json"),
            "content": "{}",
        },
        cwd=project,
    )
    assert code == 2
    assert "role=planning" in err
    # The audit grant is still pending for the real auditor session.
    code, err = _run_hook(
        monkeypatch,
        session_id=SUB_SESSION_B,
        tool_name="Write",
        tool_input={
            "file_path": str(task_dir / "audit-reports" / "x.json"),
            "content": "{}",
        },
        cwd=project,
    )
    assert code == 0, err


def test_parallel_sessions_consume_distinct_grants(project: Path) -> None:
    task_dir = _task_dir(project)
    _grant(task_dir, "audit-security")
    _grant(task_dir, "audit-code-quality")
    role_a = actor_identity.consume_grant(task_dir, SUB_SESSION_A)
    role_b = actor_identity.consume_grant(task_dir, SUB_SESSION_B)
    assert {role_a, role_b} == {"audit-security", "audit-code-quality"}
    # Re-consume returns the existing binding, not a new grant.
    assert actor_identity.consume_grant(task_dir, SUB_SESSION_A) == role_a


def test_unknown_session_without_grant_falls_back_to_default(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_dir = _task_dir(project)
    # No grant, no role file: execute-inline default applies (repo writes OK,
    # planning artifacts denied).
    code, err = _run_hook(
        monkeypatch,
        session_id=SUB_SESSION_A,
        tool_name="Write",
        tool_input={"file_path": str(task_dir / "spec.md"), "content": "# spec"},
        cwd=project,
    )
    assert code == 2
    assert "role=execute-inline" in err


def test_no_pin_preserves_legacy_role_file_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "legacy"
    task_dir = root / ".dynos" / "task-20260611-009"
    task_dir.mkdir(parents=True)
    (task_dir / "active-segment-role").write_text("planning")
    monkeypatch.delenv("DYNOS_ROLE", raising=False)
    monkeypatch.setenv("DYNOS_TASK_DIR", str(task_dir))
    code, err = _run_hook(
        monkeypatch,
        session_id="any-session",
        tool_name="Write",
        tool_input={"file_path": str(task_dir / "spec.md"), "content": "# spec"},
        cwd=root,
    )
    assert code == 0, err


# ---------------------------------------------------------------------------
# Ledger / pin protection (no self-elevation writes)
# ---------------------------------------------------------------------------

def test_ctl_refuses_non_allowlisted_grant(project: Path) -> None:
    task_dir = _task_dir(project)
    result = subprocess.run(
        ["python3", str(ROOT / "hooks" / "ctl.py"), "grant-role", str(task_dir),
         "--role", "orchestrator"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "not in the role" in result.stderr


def test_clear_role_expires_grants_and_removes_role_file(project: Path) -> None:
    task_dir = _task_dir(project)
    _grant(task_dir, "planning")
    (task_dir / "active-segment-role").write_text("planning")
    result = subprocess.run(
        ["python3", str(ROOT / "hooks" / "ctl.py"), "clear-role", str(task_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert actor_identity.pending_grants(task_dir) == []
    assert not (task_dir / "active-segment-role").exists()
    # Existing bindings untouched: clear-role only reduces privilege.


@pytest.mark.parametrize(
    ("filename", "roles"),
    [
        ("role-grants.json", ["orchestrator", "planning", "backend-executor", "audit-security"]),
        ("role-bindings.json", ["orchestrator", "planning", "backend-executor", "audit-security", "ctl"]),
    ],
)
def test_ledger_files_not_agent_writable(project: Path, filename: str, roles: list[str]) -> None:
    task_dir = _task_dir(project)
    for role in roles:
        decision = decide_write(
            WriteAttempt(
                role=role,
                task_dir=task_dir,
                path=task_dir / filename,
                operation="modify",
                source="agent",
            )
        )
        assert decision.allowed is False, f"{role} wrote {filename}"


def test_orchestrator_pin_not_agent_writable(project: Path) -> None:
    pin = project / ".dynos" / "orchestrator-session.json"
    for role in ("orchestrator", "planning", "backend-executor", "execute-inline"):
        decision = decide_write(
            WriteAttempt(
                role=role,
                task_dir=_task_dir(project),
                path=pin,
                operation="modify",
                source="agent",
            )
        )
        assert decision.allowed is False, f"{role} rewrote the pin"
        assert "hook-owned" in decision.reason


def test_expired_grant_not_consumable(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task_dir = _task_dir(project)
    _grant(task_dir, "planning")
    ledger = actor_identity.load_grants(task_dir)
    ledger["grants"][0]["expires_at"] = 1.0  # long past
    actor_identity._atomic_write_json(actor_identity.grants_path(task_dir), ledger)
    assert actor_identity.consume_grant(task_dir, SUB_SESSION_A) is None
