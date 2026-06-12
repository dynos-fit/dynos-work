"""Tests for the control-plane.json write-policy deny rule (sec-003 repair).

control-plane.json under <root>/.dynos/ is hook-owned actor/host identity. It
drives host resolution for receipt anti-forgery validation
(hooks/receipts/stage.py) and token capture (hooks/lib_tokens_hook.py). An
agent that writes {"host": "codex"} can blind the model cross-check, so ALL
agent roles must be denied direct writes to it. The framework write path is
lib_host.persist_host invoked from the SessionStart hook subprocess, which does
not pass through write_policy.

These tests live in a task-created file so they do not touch any pre-existing
test module.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

from write_policy import WriteAttempt, decide_write  # noqa: E402


# Roles spanning the privileged-most agent (orchestrator) down through
# planning, executor variants, and auditors. None of them may write the
# hook-owned control-plane.json.
_DENIED_ROLES = [
    "orchestrator",
    "planning",
    "backend-executor",
    "execute-inline",
    "audit-security",
]

_HOOK_OWNED_REASON_FRAGMENT = "hook-owned actor/host identity"


def _project_root(tmp_path: Path) -> Path:
    root = tmp_path / ".dynos"
    root.mkdir(parents=True)
    return tmp_path


def _task_dir(tmp_path: Path) -> Path:
    task_dir = tmp_path / ".dynos" / "task-20260611-001"
    task_dir.mkdir(parents=True)
    return task_dir


@pytest.mark.parametrize("role", _DENIED_ROLES)
def test_control_plane_json_denied_for_agent_roles(role: str, tmp_path: Path) -> None:
    """Every agent role is denied writing <root>/.dynos/control-plane.json with
    the hook-owned identity reason."""
    _project_root(tmp_path)
    task_dir = tmp_path / ".dynos" / "task-20260611-001"
    task_dir.mkdir(parents=True, exist_ok=True)
    cp_path = tmp_path / ".dynos" / "control-plane.json"

    decision = decide_write(
        WriteAttempt(
            role=role,
            task_dir=task_dir,
            path=cp_path,
            operation="modify",
            source="agent",
        )
    )

    assert decision.allowed is False, (
        f"role={role!r} must NOT be allowed to write control-plane.json"
    )
    assert decision.mode == "deny"
    assert _HOOK_OWNED_REASON_FRAGMENT in decision.reason, (
        f"deny reason must cite hook-owned identity, got {decision.reason!r}"
    )


@pytest.mark.parametrize("role", _DENIED_ROLES)
def test_control_plane_json_denied_without_active_task(role: str, tmp_path: Path) -> None:
    """The deny applies even when there is no active task dir (the rule keys on
    the path's .dynos parent, not on task membership)."""
    _project_root(tmp_path)
    cp_path = tmp_path / ".dynos" / "control-plane.json"

    decision = decide_write(
        WriteAttempt(
            role=role,
            task_dir=None,
            path=cp_path,
            operation="create",
            source="agent",
        )
    )

    assert decision.allowed is False
    assert decision.mode == "deny"
    assert _HOOK_OWNED_REASON_FRAGMENT in decision.reason


def test_control_plane_json_in_scratch_dir_not_affected(tmp_path: Path) -> None:
    """A control-plane.json inside a task _scratch dir is ordinary scratch space
    and stays allowed for a recognized scratch role. The deny rule keys on the
    parent being a .dynos directory, so scratch (parent name != '.dynos') is
    untouched."""
    task_dir = _task_dir(tmp_path)
    scratch_cp = task_dir / "_scratch" / "control-plane.json"
    scratch_cp.parent.mkdir(parents=True, exist_ok=True)

    decision = decide_write(
        WriteAttempt(
            role="execute-inline",
            task_dir=task_dir,
            path=scratch_cp,
            operation="create",
            source="agent",
        )
    )

    assert decision.allowed is True, (
        "control-plane.json inside _scratch/ must stay allowed for a recognized "
        f"scratch role, got reason={decision.reason!r}"
    )
    assert decision.mode == "direct"
    assert "scratch" in decision.reason.lower()


def test_control_plane_json_scratch_denied_for_unrecognized_role(tmp_path: Path) -> None:
    """Scratch namespace still gates unrecognized roles — the scratch exemption
    is role-scoped, not a blanket bypass of the control-plane deny."""
    task_dir = _task_dir(tmp_path)
    scratch_cp = task_dir / "_scratch" / "control-plane.json"
    scratch_cp.parent.mkdir(parents=True, exist_ok=True)

    decision = decide_write(
        WriteAttempt(
            role="bogus-role",
            task_dir=task_dir,
            path=scratch_cp,
            operation="create",
            source="agent",
        )
    )

    assert decision.allowed is False
    assert decision.mode == "deny"
