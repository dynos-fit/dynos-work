"""Regression tests for drain iteration short-circuit and telemetry summary.

Two related fixes verified here:

M1 — short-circuit drain iterations when no follow-on events were emitted.
The drain previously kept iterating until `not processed_any`, meaning even
a single-iteration successful drain would always run a wasted second
iteration that re-globbed the events directory once per consumer just to
confirm nothing was left. Since the FOLLOW_ON dict is currently empty,
no handler emits new events, so iteration > 1 is pure overhead.

M3 — drain returns per-handler duration aggregates so callers can see
actual cost without scraping events.jsonl.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))


def _setup(tmp_path: Path) -> Path:
    (tmp_path / ".dynos" / "events").mkdir(parents=True)
    (tmp_path / ".dynos" / "events.jsonl").touch()
    return tmp_path


def _emit(root: Path, event_type: str, payload: dict | None = None) -> Path:
    from lib_events import emit_event
    return emit_event(root, event_type, "task", payload)


def _track_iterations(monkeypatch):
    """Patch consume_events to count how many times it's called."""
    counts = {"calls": 0}
    from lib_events import consume_events as orig
    def counting(root, event_type, consumer_name):
        counts["calls"] += 1
        return orig(root, event_type, consumer_name)
    monkeypatch.setattr("eventbus.consume_events", counting)
    return counts


class TestIterationShortCircuit:
    def test_single_iteration_when_no_follow_on(self, tmp_path: Path, monkeypatch):
        """With FOLLOW_ON empty, the drain must complete in exactly one
        full iteration. Previously it ran two iterations: one to process,
        one to confirm nothing else exists. The second iteration's
        consume_events calls (1 per consumer) are pure waste."""
        root = _setup(tmp_path)
        _emit(root, "task-completed", {"task_id": "t1", "task_dir": str(tmp_path)})

        counts = _track_iterations(monkeypatch)
        test_handlers = {"task-completed": [
            ("h1", lambda r, p: True),
            ("h2", lambda r, p: True),
        ]}

        with mock.patch("eventbus.HANDLERS", test_handlers), \
             mock.patch("lib_core.is_learning_enabled", return_value=True), \
             mock.patch("eventbus.log_event"):
            from eventbus import drain
            drain(root, max_iterations=10)

        # 2 consumers × 1 iteration = 2 calls. Without the short-circuit,
        # iteration 2 would add 2 more (each consumer re-globs to confirm).
        assert counts["calls"] == 2, (
            f"expected 2 consume_events calls (1 iteration × 2 consumers); "
            f"got {counts['calls']} (drain still running extra iterations)"
        )

    def test_no_op_drain_short_circuits(self, tmp_path: Path, monkeypatch):
        """Drain with no events should not iterate past the first pass."""
        root = _setup(tmp_path)
        counts = _track_iterations(monkeypatch)
        test_handlers = {"task-completed": [("h1", lambda r, p: True)]}
        with mock.patch("eventbus.HANDLERS", test_handlers), \
             mock.patch("lib_core.is_learning_enabled", return_value=True), \
             mock.patch("eventbus.log_event"):
            from eventbus import drain
            drain(root, max_iterations=10)
        assert counts["calls"] == 1, (
            f"empty drain should make exactly 1 consume_events call; got {counts['calls']}"
        )


class TestDurationSummary:
    def test_durations_aggregate_in_summary(self, tmp_path: Path):
        """Summary must include per-handler duration stats."""
        root = _setup(tmp_path)
        _emit(root, "task-completed", {"task_id": "t1", "task_dir": str(tmp_path)})

        test_handlers = {"task-completed": [
            ("fast_handler", lambda r, p: True),
            ("slow_handler", lambda r, p: True),
        ]}

        with mock.patch("eventbus.HANDLERS", test_handlers), \
             mock.patch("lib_core.is_learning_enabled", return_value=True), \
             mock.patch("eventbus.log_event"):
            from eventbus import drain
            summary = drain(root, max_iterations=2)

        assert "_durations" in summary, "summary must include _durations key"
        durations = summary["_durations"]
        assert "fast_handler" in durations
        assert "slow_handler" in durations
        for handler_stats in durations.values():
            assert set(handler_stats.keys()) >= {"count", "min_s", "median_s", "max_s", "total_s"}
            assert handler_stats["count"] == 1

    def test_post_completion_receipt_loop_skips_duration_key(self, tmp_path: Path):
        """The post-completion receipt loop iterates summary.items().
        It must skip the new `_durations` key (which is a dict, not a
        list of 'name:status' strings)."""
        root = _setup(tmp_path)
        task = tmp_path / ".dynos" / "task-1"
        task.mkdir(parents=True)
        _emit(root, "task-completed", {"task_id": "task-1", "task_dir": str(task)})

        test_handlers = {"task-completed": [("h1", lambda r, p: True)]}
        with mock.patch("eventbus.HANDLERS", test_handlers), \
             mock.patch("lib_core.is_learning_enabled", return_value=True), \
             mock.patch("eventbus.log_event"), \
             mock.patch("lib_receipts.receipt_post_completion") as mock_receipt:
            from eventbus import drain
            # Should not raise (the bug was: ValueError unpacking _durations dict)
            drain(root, max_iterations=2)
        assert mock_receipt.called, "post-completion receipt should still fire"
