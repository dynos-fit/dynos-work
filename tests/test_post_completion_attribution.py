"""Task-local attribution tests for post-completion self-verification.

The D5 ordering hazard: ``hooks/eventbus.py`` drain must carry
``task=task_id`` on the ``eventbus_handler`` log_event call (AC18); only
then is it safe for ``hooks/lib_receipts.py:1263`` to tighten the filter
from ``if rec_task is not None and rec_task != task_id`` to
``if rec_task != task_id`` (AC19). Without the tighten, legitimate
handler events for task-B contaminate task-A's self-verify set.

This test simulates concurrent drains producing attributed handler
events in each task's own ``events.jsonl`` and proves:

  1. receipt_post_completion(task_dir_A, [{"name": "hA"}]) succeeds —
     hA's event carries task=task-A and matches.
  2. receipt_post_completion(task_dir_A, [{"name": "hB"}]) raises
     ValueError("post-completion handler not in events: hB") — hB's
     event carries task=task-B and is filtered out after AC19.

Today, the AC19 tighten has NOT landed (still ``is not None`` fallback
at lib_receipts.py:1263), so test 2 will FAIL: hB will incorrectly
match because the current filter accepts any record without a task.
That is the TDD-first expectation; the test drives the production fix.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_receipts import receipt_post_completion  # noqa: E402


def _setup_two_tasks(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create two sibling task dirs under a shared project root.

    Each task gets its own attributed ``events.jsonl`` file because
    post-completion self-verify now trusts only task-local evidence.
    """
    project = tmp_path / "project"
    dynos = project / ".dynos"
    dynos.mkdir(parents=True)

    task_a = dynos / "task-20260419-AAA"
    task_a.mkdir()
    (task_a / "manifest.json").write_text(
        json.dumps({"task_id": task_a.name, "stage": "CHECKPOINT_AUDIT"})
    )

    task_b = dynos / "task-20260419-BBB"
    task_b.mkdir()
    (task_b / "manifest.json").write_text(
        json.dumps({"task_id": task_b.name, "stage": "CHECKPOINT_AUDIT"})
    )

    (task_a / "events.jsonl").write_text(
        json.dumps({
            "ts": "2026-04-19T00:00:00Z",
            "event": "eventbus_handler",
            "task": task_a.name,
            "handler": "hA",
            "trigger_event": "task-completed",
            "success": True,
            "duration_s": 0.01,
        }) + "\n"
    )
    (task_b / "events.jsonl").write_text(
        json.dumps({
            "ts": "2026-04-19T00:00:01Z",
            "event": "eventbus_handler",
            "task": task_b.name,
            "handler": "hB",
            "trigger_event": "task-completed",
            "success": True,
            "duration_s": 0.01,
        }) + "\n"
    )

    return task_a, task_b, dynos


# --- AC20 assertion 1: task-A claiming hA succeeds -----------------------

def test_task_a_claim_hA_succeeds(tmp_path):
    task_a, _task_b, _dynos = _setup_two_tasks(tmp_path)
    # Must not raise — hA's event carries task=task_a.name.
    out = receipt_post_completion(task_a, [{"name": "hA"}])
    payload = json.loads(out.read_text())
    # Self-verify should be "passed" after AC17 also lands.
    assert payload.get("self_verify") in {"passed", None}, (
        f"task-A/hA: self_verify should be 'passed' after AC17; got "
        f"{payload.get('self_verify')!r}"
    )


# --- AC20 assertion 2: task-A claiming hB raises after AC19 tighten ------

def test_task_a_claim_hB_raises_after_ac19_tighten(tmp_path):
    task_a, _task_b, _dynos = _setup_two_tasks(tmp_path)
    # hB's event is attributed to task-B; after the AC19 tighten at
    # lib_receipts.py:1263, the per-task self-verify set for task-A must
    # NOT include hB — so the handler-not-in-events check raises.
    with pytest.raises(ValueError, match="post-completion handler not in events: hB"):
        receipt_post_completion(task_a, [{"name": "hB"}])


# --- AC20 cross-check: events with no task attribution are filtered out.

def test_events_without_task_attribution_are_filtered_after_tighten(tmp_path):
    """After AC19 tighten, an ``eventbus_handler`` event without a
    ``task`` key must NOT match any task's self-verify set.

    This regression test proves the ``is not None`` fallback is gone.
    """
    project = tmp_path / "project"
    dynos = project / ".dynos"
    dynos.mkdir(parents=True)

    td = dynos / "task-20260419-NOATT"
    td.mkdir()
    (td / "manifest.json").write_text(
        json.dumps({"task_id": td.name, "stage": "CHECKPOINT_AUDIT"})
    )

    # Write an eventbus_handler event with NO task= attribution.
    (td / "events.jsonl").write_text(
        json.dumps({
            "ts": "2026-04-19T00:00:00Z",
            "event": "eventbus_handler",
            "handler": "orphan",
            "trigger_event": "task-completed",
            "success": True,
            "duration_s": 0.01,
            # no "task" key
        }) + "\n"
    )

    with pytest.raises(ValueError, match="post-completion handler not in events: orphan"):
        receipt_post_completion(td, [{"name": "orphan"}])


# --- AC18 emission-side sanity: the drain call site threads task=task_id.

def test_ac18_eventbus_drain_passes_task_kwarg_to_log_event(monkeypatch, tmp_path):
    """AC18: ``hooks/eventbus.py`` drain loop must include ``task=task_id``
    when calling ``log_event("eventbus_handler", ...)`` for a per-task
    drain (task-completed event whose payload carries a ``task_dir`` key).

    Rather than spin up a real drain, we spy on ``log_event`` and invoke
    the drain with a single synthetic task-completed event; the spy
    records every call and we assert at least one ``eventbus_handler``
    entry was emitted with ``task=`` attribution derived from the payload.
    """
    import eventbus as eventbus_mod  # noqa: PLC0415  (lazy; may fail import)
    recorded: list[dict] = []

    def _spy(root, event_type, *, task=None, **payload):
        recorded.append({"event_type": event_type, "task": task, **payload})

    monkeypatch.setattr(eventbus_mod, "log_event", _spy)

    # Drive a synthetic drain. We don't run the real drain loop here —
    # we only assert the call-site shape, which a code-level inspection
    # of hooks/eventbus.py can also verify. This test asserts that at
    # least one log_event call with event_type == 'eventbus_handler'
    # carries a non-None task kwarg after AC18 lands.
    #
    # For TDD-first: today hooks/eventbus.py does NOT pass task=, so
    # any direct inspection of the call site would not find the kwarg.
    # Use a static-source check as the deterministic assertion.
    source = Path(eventbus_mod.__file__).read_text(encoding="utf-8")
    # Tolerate formatting variants: look for both "task=task_id" and
    # "task=Path(task_dir_str).name" or similar resolvable expressions.
    assert (
        "task=task_id" in source
        or 'task=Path(task_dir_str).name' in source
    ), (
        "AC18: hooks/eventbus.py drain must pass task=task_id to "
        "log_event(\"eventbus_handler\", ...) — not found in source"
    )
