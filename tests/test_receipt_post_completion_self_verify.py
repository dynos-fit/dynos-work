"""Tests for post-completion self-verification.

Verifies the new ``self_verify`` enum field on
``hooks/lib_receipts.receipt_post_completion``:

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


# --- Missing task events log is now a hard failure ------------------------

def test_self_verify_requires_task_events_log(tmp_path):
    td = _make_task(tmp_path, slug="SKIP-NO-LOG")
    # Explicitly ensure the task-scoped events.jsonl does not exist.
    assert not (td / "events.jsonl").exists()
    with pytest.raises(ValueError, match="post-completion task events log missing"):
        receipt_post_completion(td, [{"name": "h1"}])


def test_self_verify_rejects_unreadable_task_events_log(tmp_path, monkeypatch):
    """Adversarial cover for the OSError raise at lib_receipts.py:1334.

    The events.jsonl exists but reading it raises OSError (e.g., permission
    denied, filesystem I/O error). The receipt writer must surface the
    failure as a ValueError with the 'post-completion task events log
    unreadable' substring — NOT silently proceed with an empty handler set
    (which would falsely count the post-completion as 'passed').
    """
    td = _make_task(tmp_path, slug="UNREAD")
    events_path = td / "events.jsonl"
    events_path.write_text('{"event":"eventbus_handler","task":"' + td.name + '","handler":"h1"}\n')

    real_open = Path.open

    def explode(self, *args, **kwargs):
        if self.resolve() == events_path.resolve():
            raise OSError("simulated read failure")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", explode)

    with pytest.raises(ValueError, match="post-completion task events log unreadable"):
        receipt_post_completion(td, [{"name": "h1"}])


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
    allowed = {"passed", "skipped-handlers-empty"}
    td = _make_task(tmp_path, slug="ENUM-A")
    out = receipt_post_completion(td, [])
    assert _read_self_verify(out) in allowed

    td_b = _make_task(tmp_path, slug="ENUM-B")
    handler_event = {
        "ts": "2026-04-19T00:00:00Z",
        "event": "eventbus_handler",
        "handler": "h1",
        "trigger_event": "task-completed",
        "success": True,
        "duration_s": 0.01,
        "task": td_b.name,
    }
    (td_b / "events.jsonl").write_text(json.dumps(handler_event) + "\n")
    out_b = receipt_post_completion(td_b, [{"name": "h1"}])
    assert _read_self_verify(out_b) in allowed
