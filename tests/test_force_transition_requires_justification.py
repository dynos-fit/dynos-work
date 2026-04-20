"""TDD-first tests for AC1 (task-20260419-009).

Exercises the post-fix signature of ``hooks/lib_core.transition_task``:

  transition_task(
      task_dir,
      next_stage,
      *,
      force: bool = False,
      force_reason: str | None = None,
      force_approver: str | None = None,
  ) -> tuple[str, dict]

When ``force=True``, both ``force_reason`` and ``force_approver`` MUST be
non-empty ``str`` instances; ``None``, ``""``, or any non-string raises
``ValueError`` whose message names the specific offending arg.

These tests also cover AC13 / AC21 fail-open invariants by monkeypatching
``log_event`` to raise; the validation happens before any logging, so a
broken logger must not mask a kwargs error and a valid call must still
complete.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_core import transition_task  # noqa: E402


def _setup_pre_exec_task(tmp_path: Path, slug: str = "FRJ") -> Path:
    """Task at PRE_EXECUTION_SNAPSHOT with no plan-validated receipt.

    Without force, the EXECUTION edge refuses; with force it succeeds.
    """
    project = tmp_path / "project"
    td = project / ".dynos" / f"task-20260419-{slug}"
    td.mkdir(parents=True)
    (td / "spec.md").write_text("# s\n")
    (td / "plan.md").write_text("# p\n")
    (td / "execution-graph.json").write_text("{}\n")
    (td / "manifest.json").write_text(
        json.dumps(
            {
                "task_id": td.name,
                "stage": "PRE_EXECUTION_SNAPSHOT",
                "classification": {"risk_level": "medium"},
            }
        )
    )
    return td


# --- AC1: five parametrized bad-arg cases --------------------------------

@pytest.mark.parametrize(
    "reason,approver,expected_arg",
    [
        # 1. both missing (None) — any arg may be named, but one MUST appear
        (None, None, "force_reason"),
        # 2. reason missing, approver OK
        (None, "alice", "force_reason"),
        # 3. approver missing, reason OK
        ("because we needed to", None, "force_approver"),
        # 4. reason empty string
        ("", "alice", "force_reason"),
        # 5. approver empty string
        ("because we needed to", "", "force_approver"),
        # 6. non-string reason
        (123, "alice", "force_reason"),
        # 7. non-string approver
        ("because", ["alice"], "force_approver"),
        # 8. whitespace-only reason (spaces) — caught post-audit by
        #    ensemble security opus: `not X` accepts whitespace, defeating
        #    the break-glass audit purpose. Reject via `.strip()`.
        ("   ", "alice", "force_reason"),
        # 9. whitespace-only reason (tab)
        ("\t\t", "alice", "force_reason"),
        # 10. whitespace-only reason (newline)
        ("\n", "alice", "force_reason"),
        # 11. whitespace-only approver (spaces)
        ("valid reason", "   ", "force_approver"),
        # 12. whitespace-only approver (tab)
        ("valid reason", "\t", "force_approver"),
    ],
)
def test_force_without_reason_raises(tmp_path, reason, approver, expected_arg):
    td = _setup_pre_exec_task(tmp_path, slug=f"FRJ-{expected_arg[:5]}")
    with pytest.raises(ValueError) as exc_info:
        transition_task(
            td,
            "EXECUTION",
            force=True,
            force_reason=reason,
            force_approver=approver,
        )
    msg = str(exc_info.value)
    assert expected_arg in msg, (
        f"ValueError must name the offending arg {expected_arg!r}; "
        f"got message: {msg!r}"
    )


def test_force_true_with_valid_reason_and_approver_proceeds(tmp_path):
    """A well-formed force=True call completes and mutates the manifest."""
    td = _setup_pre_exec_task(tmp_path, slug="FRJ-OK")
    result = transition_task(
        td,
        "EXECUTION",
        force=True,
        force_reason="deferred-findings stale; break-glass",
        force_approver="alice",
    )
    assert result is not None
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == "EXECUTION"


def test_force_false_ignores_reason_and_approver(tmp_path):
    """When force=False, reason/approver are accepted as kwargs but ignored.

    The transition still refuses on a would-refuse edge for non-reason
    reasons — we prove the kwargs don't force a short-circuit.
    """
    td = _setup_pre_exec_task(tmp_path, slug="FRJ-NF")
    # force=False against a gated edge still raises ValueError for the
    # gate (not for missing reason).
    with pytest.raises(ValueError) as exc:
        transition_task(
            td,
            "EXECUTION",
            force=False,
            force_reason=None,
            force_approver=None,
        )
    msg = str(exc.value)
    # Must NOT complain about force_reason/force_approver when force=False.
    assert "force_reason" not in msg, (
        f"force=False with None reason must not raise reason error: {msg!r}"
    )
    assert "force_approver" not in msg, (
        f"force=False with None approver must not raise approver error: {msg!r}"
    )


def test_validation_runs_before_gate_replay_or_receipt_work(tmp_path, monkeypatch):
    """AC1 demands fast-fail: kwargs validation must happen BEFORE any
    gate-replay or receipt-write work.

    We test this indirectly: monkeypatch ``receipt_force_override`` to
    raise a sentinel exception. A valid force call would hit it; an
    invalid-kwargs call must raise ``ValueError`` about the kwargs FIRST,
    not the sentinel.
    """
    import lib_receipts

    class _ShouldNotBeReached(RuntimeError):
        pass

    def _sentinel(*a, **kw):
        raise _ShouldNotBeReached("receipt_force_override should not have been called")

    monkeypatch.setattr(lib_receipts, "receipt_force_override", _sentinel)

    td = _setup_pre_exec_task(tmp_path, slug="FRJ-FASTFAIL")
    with pytest.raises(ValueError) as exc_info:
        transition_task(
            td,
            "EXECUTION",
            force=True,
            force_reason=None,
            force_approver="alice",
        )
    # Must be a ValueError about force_reason, NOT a RuntimeError from
    # the sentinel (which would indicate the receipt writer was reached).
    assert "force_reason" in str(exc_info.value)
    assert not isinstance(exc_info.value, _ShouldNotBeReached)


# --- AC3 / AC13 / AC14 / AC21 fail-open: log_event failures don't wedge
# the forced transition path.

def test_forced_transition_succeeds_even_if_log_event_raises(
    tmp_path, monkeypatch
):
    """AC3 + D3: if log_event itself raises during the force_override
    emission, the forced transition MUST still complete (fail-open).
    """
    import lib_log

    def _explode(*a, **kw):
        raise Exception("forced log_event failure")

    monkeypatch.setattr(lib_log, "log_event", _explode)

    td = _setup_pre_exec_task(tmp_path, slug="FRJ-LOG")
    # Must not raise — the log_event failure is swallowed by the
    # best-effort try/except around the force_override emit.
    transition_task(
        td,
        "EXECUTION",
        force=True,
        force_reason="break-glass",
        force_approver="alice",
    )
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == "EXECUTION", (
        "force=True transition must succeed even if log_event explodes"
    )
