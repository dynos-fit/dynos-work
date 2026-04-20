"""TDD-first tests for AC17 (task-20260419-009).

Verifies the new ``self_verify`` enum field on
``hooks/lib_receipts.receipt_post_completion``:

  * ``"skipped-no-events-log"`` — no task-scoped events.jsonl AND no
    repo-level ``.dynos/events.jsonl`` exists or both are unreadable.
  * ``"skipped-handlers-empty"`` — ``handlers_run`` list is empty.
  * ``"passed"`` — events.jsonl readable AND every handler name in
    ``handlers_run`` matched a corresponding ``eventbus_handler`` event.

The field is written at top level of the receipt payload by
``write_receipt(..., self_verify=<enum>)``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_receipts import receipt_post_completion  # noqa: E402


def _make_task(tmp_path: Path, slug: str = "PCV") -> Path:
    td = tmp_path / ".dynos" / f"task-20260419-{slug}"
    td.mkdir(parents=True)
    (td / "manifest.json").write_text(
        json.dumps({"task_id": td.name, "stage": "CHECKPOINT_AUDIT"})
    )
    return td


def _read_self_verify(receipt_path: Path) -> str | None:
    payload = json.loads(receipt_path.read_text())
    return payload.get("self_verify")


# --- AC17 Test 1: no events.jsonl --> "skipped-no-events-log" ------------

def test_self_verify_skipped_no_events_log(tmp_path):
    td = _make_task(tmp_path, slug="SKIP-NO-LOG")
    # Explicitly ensure neither task-scoped nor repo-level events.jsonl
    # exists.
    assert not (td / "events.jsonl").exists()
    assert not (td.parent / "events.jsonl").exists()

    out = receipt_post_completion(td, [{"name": "h1"}])
    val = _read_self_verify(out)
    assert val == "skipped-no-events-log", (
        f"self_verify must be 'skipped-no-events-log' when both events.jsonl "
        f"are absent; got {val!r}"
    )


# --- AC17 Test 2: handlers_run=[] --> "skipped-handlers-empty" -----------

def test_self_verify_skipped_handlers_empty(tmp_path):
    td = _make_task(tmp_path, slug="SKIP-EMPTY")
    # Even with events.jsonl present, empty handlers_run must short-circuit
    # to the 'skipped-handlers-empty' enum.
    (td / "events.jsonl").write_text("")

    out = receipt_post_completion(td, [])
    val = _read_self_verify(out)
    assert val == "skipped-handlers-empty", (
        f"self_verify must be 'skipped-handlers-empty' when handlers_run is "
        f"empty; got {val!r}"
    )


# --- AC17 Test 3: valid events.jsonl --> "passed" -----------------------

def test_self_verify_passed_with_matching_events(tmp_path):
    td = _make_task(tmp_path, slug="PASSED")
    # Write a handler event that matches the requested handler name.
    handler_event = {
        "ts": "2026-04-19T00:00:00Z",
        "event": "eventbus_handler",
        "handler": "h1",
        "trigger_event": "task-completed",
        "success": True,
        "duration_s": 0.01,
        "task": td.name,
    }
    (td / "events.jsonl").write_text(json.dumps(handler_event) + "\n")

    out = receipt_post_completion(td, [{"name": "h1"}])
    val = _read_self_verify(out)
    assert val == "passed", (
        f"self_verify must be 'passed' when every handler matches an event; "
        f"got {val!r}"
    )


def test_self_verify_enum_is_one_of_three_values(tmp_path):
    """Field must be exactly one of the three enum strings. Anything else
    is a regression."""
    allowed = {"passed", "skipped-no-events-log", "skipped-handlers-empty"}
    td = _make_task(tmp_path, slug="ENUM-A")
    out = receipt_post_completion(td, [])
    assert _read_self_verify(out) in allowed

    td_b = _make_task(tmp_path, slug="ENUM-B")
    out_b = receipt_post_completion(td_b, [{"name": "h1"}])
    assert _read_self_verify(out_b) in allowed
