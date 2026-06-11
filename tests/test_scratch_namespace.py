"""Tests for the task-scoped _scratch/ namespace (D2).

_scratch/ is sanctioned temp space inside the task boundary, replacing the
/tmp staging the skills used to prescribe (which the policy denied — P0-a in
docs/permissions-on-design.md). Its safety property is proof-irrelevance:
nothing in the control plane reads from _scratch/, so a scratch write can
never certify a stage, forge evidence, or alter a receipt.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

from write_policy import WriteAttempt, decide_write  # noqa: E402


def _task_dir(tmp_path: Path) -> Path:
    task_dir = tmp_path / ".dynos" / "task-20260611-001"
    task_dir.mkdir(parents=True)
    return task_dir


@pytest.mark.parametrize(
    "role",
    [
        "orchestrator",
        "planning",
        "execute-inline",
        "backend-executor",
        "testing-executor",
        "repair-coordinator",
        "audit-security",
        "audit-spec-completion",
    ],
)
def test_recognized_actor_roles_may_write_scratch(tmp_path: Path, role: str) -> None:
    task_dir = _task_dir(tmp_path)
    decision = decide_write(
        WriteAttempt(
            role=role,
            task_dir=task_dir,
            path=task_dir / "_scratch" / "payload.json",
            operation="create",
            source="agent",
        )
    )
    assert decision.allowed is True, f"{role} denied scratch: {decision.reason}"


def test_unrecognized_role_denied_scratch(tmp_path: Path) -> None:
    task_dir = _task_dir(tmp_path)
    decision = decide_write(
        WriteAttempt(
            role="totally-made-up",
            task_dir=task_dir,
            path=task_dir / "_scratch" / "x.json",
            operation="create",
            source="agent",
        )
    )
    assert decision.allowed is False


def test_scratch_cannot_shadow_control_plane(tmp_path: Path) -> None:
    """A control-plane filename inside _scratch/ is just a scratch file —
    and the real control-plane path stays protected."""
    task_dir = _task_dir(tmp_path)
    in_scratch = decide_write(
        WriteAttempt(
            role="planning",
            task_dir=task_dir,
            path=task_dir / "_scratch" / "manifest.json",
            operation="create",
            source="agent",
        )
    )
    assert in_scratch.allowed is True
    real = decide_write(
        WriteAttempt(
            role="planning",
            task_dir=task_dir,
            path=task_dir / "manifest.json",
            operation="modify",
            source="agent",
        )
    )
    assert real.allowed is False


def test_no_control_plane_code_reads_scratch() -> None:
    """Proof-irrelevance: no hook/receipt/validator resolves _scratch paths.

    The only sanctioned mentions are in write_policy (the namespace rule
    itself) and pre_tool_use (denial-message hint text).
    """
    allowed_files = {"write_policy.py", "pre_tool_use.py"}
    offenders: list[str] = []
    for py in (ROOT / "hooks").rglob("*.py"):
        if py.name in allowed_files:
            continue
        text = py.read_text(encoding="utf-8", errors="ignore")
        if re.search(r"_scratch", text):
            offenders.append(str(py.relative_to(ROOT)))
    assert offenders == [], (
        f"_scratch/ must stay proof-irrelevant; control-plane code references "
        f"it in: {offenders}"
    )
