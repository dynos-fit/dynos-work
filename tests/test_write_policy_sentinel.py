"""Tests for .dynos/.rules_corrupt sentinel gate in write_policy (task-20260504-003).

Covers ACs 4 and 5 (sec-r2-001):
  AC 4 — decide_write with path resolving to .dynos/.rules_corrupt and
          role='execute-inline' must be refused.
  AC 5 — same path with role='daemon' must NOT be refused by the sentinel.

The sentinel check must operate on the already-resolved absolute path so a
symlink pointing at .dynos/.rules_corrupt cannot bypass it.
"""
from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

from write_policy import WriteAttempt, decide_write  # noqa: E402


def _make_dynos_dir(tmp_path: Path) -> Path:
    """Return tmp_path/.dynos, created on disk."""
    dynos_dir = tmp_path / ".dynos"
    dynos_dir.mkdir(parents=True, exist_ok=True)
    return dynos_dir


def test_sentinel_refused_for_non_daemon_role(tmp_path: Path) -> None:
    """execute-inline must be refused when writing .dynos/.rules_corrupt (AC 4)."""
    dynos_dir = _make_dynos_dir(tmp_path)
    sentinel_path = dynos_dir / ".rules_corrupt"

    decision = decide_write(
        WriteAttempt(
            role="execute-inline",
            task_dir=None,
            path=sentinel_path,
            operation="create",
            source="agent",
        )
    )

    assert decision.allowed is False, (
        "sentinel gate must refuse non-daemon role writing .dynos/.rules_corrupt"
    )
    # Reason must identify what is being protected — any of these substrings suffice.
    reason_lower = decision.reason.lower()
    assert (
        "rules_corrupt" in reason_lower
        or "kill-switch" in reason_lower
        or "kill switch" in reason_lower
        or "daemon-owned" in reason_lower
        or "daemon" in reason_lower
    ), f"reason should reference the sentinel or daemon ownership; got: {decision.reason!r}"


def test_sentinel_refused_for_backend_executor_role(tmp_path: Path) -> None:
    """backend-executor must also be refused (AC 4 — any non-daemon role)."""
    dynos_dir = _make_dynos_dir(tmp_path)
    sentinel_path = dynos_dir / ".rules_corrupt"

    decision = decide_write(
        WriteAttempt(
            role="backend-executor",
            task_dir=None,
            path=sentinel_path,
            operation="modify",
            source="agent",
        )
    )

    assert decision.allowed is False, (
        "backend-executor must not be allowed to write .dynos/.rules_corrupt"
    )


def test_sentinel_allowed_for_daemon_role(tmp_path: Path) -> None:
    """daemon role must not be refused by the sentinel check (AC 5)."""
    dynos_dir = _make_dynos_dir(tmp_path)
    sentinel_path = dynos_dir / ".rules_corrupt"

    decision = decide_write(
        WriteAttempt(
            role="daemon",
            task_dir=None,
            path=sentinel_path,
            operation="create",
            source="system",
        )
    )

    assert decision.allowed is True, (
        "daemon role must be permitted to write .dynos/.rules_corrupt; "
        f"got allowed={decision.allowed}, reason={decision.reason!r}"
    )


def test_sentinel_via_symlink_still_blocked(tmp_path: Path) -> None:
    """A symlink whose resolve() lands on .dynos/.rules_corrupt must still be blocked.

    This verifies that the check operates on the resolved absolute path, not the
    raw caller-supplied path — closing the symlink bypass vector described in AC 1
    and the implicit requirements section.
    """
    dynos_dir = _make_dynos_dir(tmp_path)
    # The symlink target does not need to exist for resolve() on POSIX; Path.resolve()
    # resolves symlinks in the prefix and returns the real path of the target name.
    # Create the target so resolve() can fully canonicalize it.
    target = dynos_dir / ".rules_corrupt"
    target.touch()

    symlink_path = dynos_dir / "sneaky"
    symlink_path.symlink_to(target)

    # Confirm the symlink resolves to .rules_corrupt
    assert symlink_path.resolve().name == ".rules_corrupt", (
        "test setup error: symlink should resolve to .rules_corrupt"
    )

    decision = decide_write(
        WriteAttempt(
            role="execute-inline",
            task_dir=None,
            path=symlink_path,  # caller passes the symlink path
            operation="modify",
            source="agent",
        )
    )

    assert decision.allowed is False, (
        "symlink whose resolved path is .dynos/.rules_corrupt must be blocked; "
        f"got allowed={decision.allowed}, reason={decision.reason!r}"
    )
