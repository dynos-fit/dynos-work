"""Tests for AC 10: write-policy invariants after seg-3 changes.

These tests are RED by design until seg-3 adds:
- 'tool-call-counters.json' to _CONTROL_PLANE_EXACT in write_policy.py:83
- ctl create-only allowance for audit-reports/ skeletons at write_policy.py:479

All tests call decide_write from hooks.write_policy with real WriteAttempt objects.
None are fabricated dict tests.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

from write_policy import WriteAttempt, decide_write  # noqa: E402

# Use a dummy task dir path — tests don't need real files on disk;
# write_policy evaluates the path structure, not file existence.
TASK_DIR = Path("/tmp/dynos-test-task-20260612-001")


# ---------------------------------------------------------------------------
# AC 10(a): tool-call-counters.json denied to all agent roles
# ---------------------------------------------------------------------------


def test_write_policy_counter_file_denied_to_agents() -> None:
    """tool-call-counters.json is in _CONTROL_PLANE_EXACT; all agent roles denied.

    After seg-3 adds 'tool-call-counters.json' to _CONTROL_PLANE_EXACT,
    every agent role attempting to write it must be denied.
    """
    agent_roles = [
        "audit-code-quality",
        "backend-executor",
        "orchestrator",
        "receipt-writer",
        "audit-security",
        "ui-executor",
    ]
    counter_path = TASK_DIR / "tool-call-counters.json"
    for role in agent_roles:
        decision = decide_write(
            WriteAttempt(
                role=role,
                task_dir=TASK_DIR,
                path=counter_path,
                operation="modify",
                source="agent",
            )
        )
        assert not decision.allowed, (
            f"role={role!r} must be DENIED write to tool-call-counters.json, "
            f"but got allowed=True (reason: {decision.reason!r})"
        )


# ---------------------------------------------------------------------------
# AC 10(b): ctl create of audit-reports skeleton IS allowed
# ---------------------------------------------------------------------------


def test_write_policy_ctl_skeleton_create_allowed() -> None:
    """ctl + create of audit-reports/ skeleton is allowed (narrow allowance).

    After seg-3 inserts the ctl create-only allowance BEFORE the audit-* check,
    ctl can create skeleton files in audit-reports/.
    """
    skeleton_path = TASK_DIR / "audit-reports" / "x-haiku-attempt-1.json"  # noqa: model-literal
    decision = decide_write(
        WriteAttempt(
            role="ctl",
            task_dir=TASK_DIR,
            path=skeleton_path,
            operation="create",
            source="ctl",
        )
    )
    assert decision.allowed, (
        f"ctl + create of audit-reports skeleton must be ALLOWED, "
        f"but got denied (reason: {decision.reason!r})"
    )


# ---------------------------------------------------------------------------
# AC 10(c): ctl modify of audit-reports/ file is DENIED
# ---------------------------------------------------------------------------


def test_write_policy_ctl_modify_denied() -> None:
    """ctl with operation != 'create' for audit-reports/ is denied.

    The narrow allowance is create-only; ctl cannot modify existing audit reports.
    This verifies the operation gating logic: role='ctl' AND operation='modify'
    must fall through to the general audit-* check, which denies non-audit- roles.
    """
    report_path = TASK_DIR / "audit-reports" / "cq-sonnet-attempt-1.json"  # noqa: model-literal
    decision = decide_write(
        WriteAttempt(
            role="ctl",
            task_dir=TASK_DIR,
            path=report_path,
            operation="modify",
            source="ctl",
        )
    )
    assert not decision.allowed, (
        f"ctl + modify of audit-reports/ must be DENIED, "
        f"but got allowed=True (reason: {decision.reason!r})"
    )


# ---------------------------------------------------------------------------
# AC 10(d): audit-code-quality write to its own report IS allowed
# ---------------------------------------------------------------------------


def test_write_policy_audit_role_modify_allowed() -> None:
    """audit-code-quality write to its own audit report is allowed (existing behavior).

    This is pre-existing behavior that must not regress. An audit-* role
    can always write to audit-reports/.
    """
    report_path = TASK_DIR / "audit-reports" / "cq-sonnet-attempt-1.json"  # noqa: model-literal
    decision = decide_write(
        WriteAttempt(
            role="audit-code-quality",
            task_dir=TASK_DIR,
            path=report_path,
            operation="modify",
            source="agent",
        )
    )
    assert decision.allowed, (
        f"audit-code-quality must be ALLOWED to write its own audit report, "
        f"but got denied (reason: {decision.reason!r})"
    )


# ---------------------------------------------------------------------------
# AC 10(e): no agent role can write control-plane files directly
# ---------------------------------------------------------------------------


def test_write_policy_no_agent_writes_spawn_log() -> None:
    """No agent role can write spawn-log.jsonl directly."""
    spawn_log = TASK_DIR / "spawn-log.jsonl"
    for role in ["orchestrator", "backend-executor", "audit-security"]:
        decision = decide_write(
            WriteAttempt(
                role=role,
                task_dir=TASK_DIR,
                path=spawn_log,
                operation="modify",
                source="agent",
            )
        )
        assert not decision.allowed, (
            f"role={role!r} must be DENIED write to spawn-log.jsonl"
        )


def test_write_policy_no_agent_writes_role_grants() -> None:
    """No agent role can write role-grants.json directly."""
    role_grants = TASK_DIR / "role-grants.json"
    for role in ["orchestrator", "backend-executor", "audit-security"]:
        decision = decide_write(
            WriteAttempt(
                role=role,
                task_dir=TASK_DIR,
                path=role_grants,
                operation="modify",
                source="agent",
            )
        )
        assert not decision.allowed, (
            f"role={role!r} must be DENIED write to role-grants.json"
        )


def test_write_policy_no_agent_writes_receipts() -> None:
    """No agent role can write to receipts/ directly."""
    receipt_path = TASK_DIR / "receipts" / "audit-security.json"
    for role in ["orchestrator", "backend-executor", "audit-security"]:
        decision = decide_write(
            WriteAttempt(
                role=role,
                task_dir=TASK_DIR,
                path=receipt_path,
                operation="modify",
                source="agent",
            )
        )
        assert not decision.allowed, (
            f"role={role!r} must be DENIED write to receipts/"
        )
