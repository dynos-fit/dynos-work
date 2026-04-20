"""Tests for the force_override observability layer (F7 + F8,
CRITERIA 7 and 8).

When `transition_task` is called with `force=True`:

  (F7) Before mutating the manifest, compute what the gate block WOULD
       have refused on — `bypassed_gates: list[str]` — then
         (a) emit a `force_override` event via `log_event` with
             `task_id`, `from_stage`, `to_stage`, `bypassed_gates`.
         (b) write `receipts/force-override-{from}-{to}.json` via the
             new `receipt_force_override` writer.

  (F8) `receipt_force_override(task_dir, from_stage, to_stage,
       bypassed_gates)` validates its inputs and writes a well-formed
       receipt payload. Bad args → ValueError naming the arg.

Neither (F7)'s observability write nor (F8)'s validation must ever
block a forced transition — force is a break-glass door by design.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_core import transition_task  # noqa: E402
from lib_receipts import receipt_force_override  # noqa: E402


def _read_events(td: Path) -> list[dict]:
    """Read all task-scoped events.jsonl entries as a list of dicts.

    The F7 event is emitted via `log_event(root, "force_override",
    task=task_id, ...)`. `log_event` routes to the task-scoped file
    when the task dir exists, which it does in these tests.
    """
    events_path = td / "events.jsonl"
    if not events_path.exists():
        return []
    events: list[dict] = []
    for line in events_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        events.append(json.loads(line))
    return events


def _setup_pre_exec_task(tmp_path: Path, slug: str = "FO") -> Path:
    """A task at PRE_EXECUTION_SNAPSHOT with NO plan-validated receipt.
    PRE_EXECUTION_SNAPSHOT → EXECUTION refuses by default (plan was
    never validated). force=True turns that into a bypassed gate."""
    project = tmp_path / "project"
    td = project / ".dynos" / f"task-20260419-{slug}"
    td.mkdir(parents=True)
    (td / "spec.md").write_text("# s\n")
    (td / "plan.md").write_text("# p\n")
    (td / "execution-graph.json").write_text("{}\n")
    (td / "manifest.json").write_text(json.dumps({
        "task_id": td.name,
        "stage": "PRE_EXECUTION_SNAPSHOT",
        "classification": {"risk_level": "medium"},
    }))
    return td


def _setup_noop_task(tmp_path: Path, slug: str = "NOOP") -> Path:
    """A task at FOUNDRY_INITIALIZED advancing to SPEC_NORMALIZATION.
    That edge has no receipt gates (see ALLOWED_STAGE_TRANSITIONS and
    the gate block in transition_task), so a force=True call emits a
    `force_override` event/receipt with an EMPTY `bypassed_gates` list.
    This pins the "every force produces a receipt" invariant (ADR /
    Plan Open Question #2 — empty list still records)."""
    project = tmp_path / "project"
    td = project / ".dynos" / f"task-20260419-{slug}"
    td.mkdir(parents=True)
    (td / "manifest.json").write_text(json.dumps({
        "task_id": td.name,
        "stage": "FOUNDRY_INITIALIZED",
        "classification": {"risk_level": "medium"},
    }))
    return td


# ---------------------------------------------------------------------------
# F7: force_override receipt + event
# ---------------------------------------------------------------------------


def test_forced_transition_writes_receipt(tmp_path: Path) -> None:
    """force=True across a would-refuse edge → the
    `force-override-{from}-{to}.json` receipt is created by
    `transition_task`."""
    td = _setup_pre_exec_task(tmp_path, slug="FO-RCPT")
    # Sanity check: without force the gate refuses (proves there IS a
    # gate to bypass on this edge).
    with pytest.raises(ValueError):
        transition_task(td, "EXECUTION")

    transition_task(
        td,
        "EXECUTION",
        force=True,
        force_reason="test: force-override receipt writer contract",
        force_approver="test-suite",
    )

    receipt_path = td / "receipts" / "force-override-PRE_EXECUTION_SNAPSHOT-EXECUTION.json"
    assert receipt_path.exists(), (
        f"force_override receipt must exist at {receipt_path}"
    )
    payload = json.loads(receipt_path.read_text())
    assert payload["from_stage"] == "PRE_EXECUTION_SNAPSHOT"
    assert payload["to_stage"] == "EXECUTION"
    assert isinstance(payload["bypassed_gates"], list)
    # The plan-validated missing gate should have been captured.
    joined = " ".join(payload["bypassed_gates"])
    assert "plan-validated" in joined or "plan was never validated" in joined, (
        f"bypassed_gates did not capture the plan-validated gate: {payload['bypassed_gates']!r}"
    )


def test_forced_transition_emits_force_override_event(tmp_path: Path) -> None:
    """force=True → a `force_override` entry is appended to events.jsonl,
    carrying the task_id and a non-empty `bypassed_gates` list."""
    td = _setup_pre_exec_task(tmp_path, slug="FO-EVT")
    transition_task(
        td,
        "EXECUTION",
        force=True,
        force_reason="test: force-override event emission",
        force_approver="test-suite",
    )

    events = _read_events(td)
    force_events = [e for e in events if e.get("event") == "force_override"]
    assert len(force_events) >= 1, (
        f"no force_override event found in events.jsonl — got events: {[e.get('event') for e in events]}"
    )
    ev = force_events[0]
    assert ev.get("task_id") == td.name, f"task_id mismatch: {ev!r}"
    assert ev.get("from_stage") == "PRE_EXECUTION_SNAPSHOT"
    assert ev.get("to_stage") == "EXECUTION"
    assert isinstance(ev.get("bypassed_gates"), list)
    assert len(ev["bypassed_gates"]) >= 1, (
        f"bypassed_gates must be non-empty for a refused edge: {ev!r}"
    )


def test_forced_transition_with_no_bypassed_gates_still_records(tmp_path: Path) -> None:
    """force=True on an edge that would NOT have refused still writes a
    receipt + event, but with an empty `bypassed_gates` list. The
    invariant is "every force leaves a trace", regardless of whether
    force was strictly necessary."""
    td = _setup_noop_task(tmp_path, slug="FO-NOOP")
    transition_task(
        td,
        "SPEC_NORMALIZATION",
        force=True,
        force_reason="test: every-force-leaves-a-trace invariant on no-gate edge",
        force_approver="test-suite",
    )

    receipt_path = td / "receipts" / "force-override-FOUNDRY_INITIALIZED-SPEC_NORMALIZATION.json"
    assert receipt_path.exists()
    payload = json.loads(receipt_path.read_text())
    assert payload["bypassed_gates"] == [], (
        f"no-gate edge must record an empty bypassed_gates list: {payload!r}"
    )

    events = _read_events(td)
    force_events = [e for e in events if e.get("event") == "force_override"]
    assert len(force_events) == 1, (
        f"exactly one force_override event expected — got {force_events!r}"
    )
    assert force_events[0].get("bypassed_gates") == []


# ---------------------------------------------------------------------------
# F8: receipt_force_override writer validation
# ---------------------------------------------------------------------------


def test_receipt_writer_signature_valid_args(tmp_path: Path) -> None:
    """Direct call with valid args → a receipt file exists, and the
    payload carries the exact values we passed in (including the v5
    ``reason`` + ``approver`` justification fields)."""
    td = tmp_path / ".dynos" / "task-FO-DIRECT"
    td.mkdir(parents=True)
    out = receipt_force_override(
        td,
        from_stage="X",
        to_stage="Y",
        bypassed_gates=["gate1"],
        reason="test: direct receipt writer call",
        approver="test-suite",
    )
    assert out.exists()
    payload = json.loads(out.read_text())
    assert payload["from_stage"] == "X"
    assert payload["to_stage"] == "Y"
    assert payload["bypassed_gates"] == ["gate1"]
    assert payload["reason"] == "test: direct receipt writer call"
    assert payload["approver"] == "test-suite"


def test_receipt_writer_rejects_path_traversal_stage(tmp_path: Path) -> None:
    """SEC-002 regression: crafted stage values must not escape receipts/.
    Stage names must match ^[A-Z][A-Z0-9_]*$ to prevent path injection."""
    td = _setup_pre_exec_task(tmp_path, slug="SEC2")
    with pytest.raises(ValueError, match="from_stage must match"):
        receipt_force_override(
            td, "../../etc/x", "DONE", [],
            reason="test: sec2 traversal probe", approver="test-suite",
        )
    with pytest.raises(ValueError, match="to_stage must match"):
        receipt_force_override(
            td, "EXECUTION", "../evil", [],
            reason="test: sec2 traversal probe", approver="test-suite",
        )
    with pytest.raises(ValueError, match="from_stage must match"):
        receipt_force_override(
            td, "execution", "DONE", [],
            reason="test: sec2 lowercase probe", approver="test-suite",
        )  # lowercase rejected
    with pytest.raises(ValueError, match="to_stage must match"):
        receipt_force_override(
            td, "EXECUTION", "done/x", [],
            reason="test: sec2 slash probe", approver="test-suite",
        )


def test_receipt_writer_rejects_bad_args(tmp_path: Path) -> None:
    """Every bad-arg combination → ValueError naming the offending arg.
    This is a guardrail against silent "" / None / non-list slipping
    into the receipt chain. Extended for the v4→v5 bump: empty or
    non-string ``reason`` / ``approver`` must also be rejected."""
    td = tmp_path / ".dynos" / "task-FO-BAD"
    td.mkdir(parents=True)

    # Empty from_stage
    with pytest.raises(ValueError, match="from_stage"):
        receipt_force_override(
            td, from_stage="", to_stage="Y", bypassed_gates=[],
            reason="test: bad-arg probe", approver="test-suite",
        )

    # Empty to_stage
    with pytest.raises(ValueError, match="to_stage"):
        receipt_force_override(
            td, from_stage="X", to_stage="", bypassed_gates=[],
            reason="test: bad-arg probe", approver="test-suite",
        )

    # Non-list bypassed_gates
    with pytest.raises(ValueError, match="bypassed_gates"):
        receipt_force_override(
            td, from_stage="X", to_stage="Y",
            bypassed_gates="not a list",  # type: ignore[arg-type]
            reason="test: bad-arg probe", approver="test-suite",
        )

    # List with non-string entry
    with pytest.raises(ValueError, match="bypassed_gates"):
        receipt_force_override(
            td, from_stage="X", to_stage="Y",
            bypassed_gates=[1, 2, 3],  # type: ignore[list-item]
            reason="test: bad-arg probe", approver="test-suite",
        )

    # v5 floor: empty reason MUST be rejected by the receipt writer even
    # when every other arg is valid. Mirrors the contract bump at AC24 —
    # no silent empty-justification slipping past the writer.
    with pytest.raises(ValueError, match="reason"):
        receipt_force_override(
            td, from_stage="X", to_stage="Y", bypassed_gates=[],
            reason="", approver="test-suite",
        )

    # v5 floor: None reason (non-string) MUST be rejected.
    with pytest.raises(ValueError, match="reason"):
        receipt_force_override(
            td, from_stage="X", to_stage="Y", bypassed_gates=[],
            reason=None,  # type: ignore[arg-type]
            approver="test-suite",
        )

    # v5 floor: empty approver MUST be rejected.
    with pytest.raises(ValueError, match="approver"):
        receipt_force_override(
            td, from_stage="X", to_stage="Y", bypassed_gates=[],
            reason="test: bad-arg probe", approver="",
        )

    # v5 floor: None approver MUST be rejected.
    with pytest.raises(ValueError, match="approver"):
        receipt_force_override(
            td, from_stage="X", to_stage="Y", bypassed_gates=[],
            reason="test: bad-arg probe",
            approver=None,  # type: ignore[arg-type]
        )

    # v5 floor: non-string reason (int) MUST be rejected.
    with pytest.raises(ValueError, match="reason"):
        receipt_force_override(
            td, from_stage="X", to_stage="Y", bypassed_gates=[],
            reason=123,  # type: ignore[arg-type]
            approver="test-suite",
        )

    # v5 floor: non-string approver (int) MUST be rejected.
    with pytest.raises(ValueError, match="approver"):
        receipt_force_override(
            td, from_stage="X", to_stage="Y", bypassed_gates=[],
            reason="test: bad-arg probe",
            approver=42,  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# Dry-run isolation — force=True must NOT leak state into force=False.
# ---------------------------------------------------------------------------


def test_forced_transition_does_not_corrupt_normal_gate_state(tmp_path: Path) -> None:
    """ADR Risk-Note: the dry-run gate pass used to compute
    `bypassed_gates` runs on a LOCAL `errs` list that must NOT
    contaminate a subsequent force=False call's gate block.

    Scenario:
      1. Task A: force=True across a would-refuse edge. A's dry-run
         builds a non-empty bypassed_gates list.
      2. Task B (separate task): force=False across the SAME edge
         (with all gates satisfied). B MUST pass cleanly — no leaked
         errs from Task A.
    """
    # --- Task A: force=True on a would-refuse edge --------------------
    td_a = _setup_pre_exec_task(tmp_path, slug="ISO-A")
    transition_task(
        td_a,
        "EXECUTION",
        force=True,
        force_reason="test: dry-run-gate isolation probe (task A)",
        force_approver="test-suite",
    )
    manifest_a = json.loads((td_a / "manifest.json").read_text())
    assert manifest_a["stage"] == "EXECUTION"

    # --- Task B: a fresh task on the SAME edge, force=False ----------
    # Write a valid plan-validated receipt so the gate passes legitimately.
    td_b = _setup_pre_exec_task(tmp_path, slug="ISO-B")
    from lib_receipts import receipt_plan_validated
    import os; os.environ["DYNOS_ALLOW_TEST_OVERRIDE"]="1"; receipt_plan_validated(td_b, validation_passed_override=True)
    # No force this time — the gate must evaluate clean with no leak
    # from the prior force=True call in the same process.
    transition_task(td_b, "EXECUTION")
    manifest_b = json.loads((td_b / "manifest.json").read_text())
    assert manifest_b["stage"] == "EXECUTION"
    # Task B must NOT have produced a force_override receipt (it was
    # not forced).
    force_rcpt = td_b / "receipts" / "force-override-PRE_EXECUTION_SNAPSHOT-EXECUTION.json"
    assert not force_rcpt.exists(), (
        f"Task B (force=False) must not emit a force_override receipt: {force_rcpt}"
    )
