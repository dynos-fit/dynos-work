"""Integration tests for eventbus.drain() behavior.

Covers the drain semantics that were previously untested:
  - Failed handlers leave events available for retry
  - Follow-on events only fire when ALL handlers succeed
  - learning_enabled=false still runs non-learning downstream handlers
  - Multiple queued events: failure on one blocks follow-on (AND semantics)
  - Duplicate follow-on prevention across iterations
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup(tmp_path: Path) -> Path:
    """Create minimal .dynos/events/ for drain testing."""
    (tmp_path / ".dynos" / "events").mkdir(parents=True)
    (tmp_path / ".dynos" / "events.jsonl").touch()
    return tmp_path


def _emit(root: Path, event_type: str, payload: dict | None = None) -> Path:
    from lib_events import emit_event
    return emit_event(root, event_type, "task", payload)


def _count_events(root: Path, event_type: str) -> int:
    return len(list((root / ".dynos" / "events").glob(f"*-{event_type}.json")))


def _processed_by(event_path: Path) -> list[str]:
    return json.loads(event_path.read_text()).get("processed_by", [])


def _make_handlers(overrides: dict):
    """Build a test HANDLERS dict with controllable return values.

    overrides: {consumer_name: bool_or_callable}
    Missing consumers default to True.
    """
    import eventbus

    def _make_fn(result):
        if callable(result):
            return result
        return lambda root, payload: result

    # Start from the real handler structure but replace functions
    test_handlers = {}
    for event_type, handlers in eventbus.HANDLERS.items():
        test_handlers[event_type] = []
        for name, _ in handlers:
            fn = _make_fn(overrides.get(name, True))
            test_handlers[event_type].append((name, fn))
    return test_handlers


# ---------------------------------------------------------------------------
# Failed handlers: retry on next drain
# ---------------------------------------------------------------------------

class TestFailedHandlerRetry:
    def test_failed_handler_event_not_marked_processed(self, tmp_path: Path):
        root = _setup(tmp_path)
        ep = _emit(root, "task-completed", {"task_id": "t1", "task_dir": str(tmp_path)})

        handlers = _make_handlers({"memory": False, "trajectory": True})
        with mock.patch("eventbus.HANDLERS", handlers), \
             mock.patch("lib_core.is_learning_enabled", return_value=True), \
             mock.patch("eventbus.log_event"):
            from eventbus import drain
            drain(root, max_iterations=1)

        processed = _processed_by(ep)
        assert "trajectory" in processed
        assert "memory" not in processed

    def test_failed_handler_retried_on_second_drain(self, tmp_path: Path):
        root = _setup(tmp_path)
        _emit(root, "task-completed", {"task_id": "t1", "task_dir": str(tmp_path)})

        calls = {"n": 0}
        def fail_then_succeed(root, payload):
            calls["n"] += 1
            return calls["n"] > 1

        handlers = _make_handlers({"memory": fail_then_succeed, "trajectory": True})
        with mock.patch("eventbus.HANDLERS", handlers), \
             mock.patch("lib_core.is_learning_enabled", return_value=True), \
             mock.patch("eventbus.log_event"):
            from eventbus import drain
            drain(root, max_iterations=1)
            assert calls["n"] == 1
            drain(root, max_iterations=1)
            assert calls["n"] == 2


# ---------------------------------------------------------------------------
# Follow-on gating: ALL handlers must succeed
# ---------------------------------------------------------------------------

class TestFollowOnGating:
    def test_blocked_when_one_handler_fails(self, tmp_path: Path):
        root = _setup(tmp_path)
        _emit(root, "task-completed", {"task_id": "t1", "task_dir": str(tmp_path)})

        handlers = _make_handlers({"memory": False, "trajectory": True})
        with mock.patch("eventbus.HANDLERS", handlers), \
             mock.patch("lib_core.is_learning_enabled", return_value=True), \
             mock.patch("eventbus.log_event"):
            from eventbus import drain
            drain(root, max_iterations=1)

        assert _count_events(root, "memory-completed") == 0

    def test_emitted_when_all_succeed(self, tmp_path: Path):
        root = _setup(tmp_path)
        _emit(root, "task-completed", {"task_id": "t1", "task_dir": str(tmp_path)})

        handlers = _make_handlers({})  # all default to True
        with mock.patch("eventbus.HANDLERS", handlers), \
             mock.patch("lib_core.is_learning_enabled", return_value=True), \
             mock.patch("eventbus.log_event"):
            from eventbus import drain
            drain(root, max_iterations=1)

        assert _count_events(root, "memory-completed") == 1

    def test_no_duplicates_across_iterations(self, tmp_path: Path):
        root = _setup(tmp_path)
        _emit(root, "task-completed", {"task_id": "t1", "task_dir": str(tmp_path)})

        handlers = _make_handlers({})
        with mock.patch("eventbus.HANDLERS", handlers), \
             mock.patch("lib_core.is_learning_enabled", return_value=True), \
             mock.patch("eventbus.log_event"):
            from eventbus import drain
            drain(root, max_iterations=5)

        assert _count_events(root, "memory-completed") == 1
        assert _count_events(root, "calibration-completed") == 1
        assert _count_events(root, "benchmark-completed") == 1


# ---------------------------------------------------------------------------
# learning_enabled=false: non-learning handlers still fire
# ---------------------------------------------------------------------------

class TestLearningDisabled:
    def test_skipped_handlers_mark_events_processed(self, tmp_path: Path):
        root = _setup(tmp_path)
        ep = _emit(root, "task-completed", {"task_id": "t1", "task_dir": str(tmp_path)})

        handlers = _make_handlers({})
        with mock.patch("eventbus.HANDLERS", handlers), \
             mock.patch("lib_core.is_learning_enabled", return_value=False), \
             mock.patch("eventbus.log_event"):
            from eventbus import drain
            drain(root, max_iterations=1)

        processed = _processed_by(ep)
        assert "memory" in processed
        assert "trajectory" in processed

    def test_chain_reaches_dashboard(self, tmp_path: Path):
        root = _setup(tmp_path)
        _emit(root, "task-completed", {"task_id": "t1", "task_dir": str(tmp_path)})

        dashboard_called = {"v": False}
        def track_dashboard(root, payload):
            dashboard_called["v"] = True
            return True

        handlers = _make_handlers({"dashboard": track_dashboard})
        with mock.patch("eventbus.HANDLERS", handlers), \
             mock.patch("lib_core.is_learning_enabled", return_value=False), \
             mock.patch("eventbus.log_event"):
            from eventbus import drain
            drain(root, max_iterations=5)

        assert dashboard_called["v"], "dashboard should run with learning off"

    def test_learning_handlers_not_executed(self, tmp_path: Path):
        root = _setup(tmp_path)
        _emit(root, "task-completed", {"task_id": "t1", "task_dir": str(tmp_path)})

        memory_called = {"v": False}
        def track_memory(root, payload):
            memory_called["v"] = True
            return True

        handlers = _make_handlers({"memory": track_memory})
        with mock.patch("eventbus.HANDLERS", handlers), \
             mock.patch("lib_core.is_learning_enabled", return_value=False), \
             mock.patch("eventbus.log_event"):
            from eventbus import drain
            drain(root, max_iterations=5)

        assert not memory_called["v"], "memory should NOT execute with learning off"


# ---------------------------------------------------------------------------
# No tight-loop retry within a single drain call
# ---------------------------------------------------------------------------

class TestNoTightRetry:
    def test_permanently_failing_handler_runs_once_per_drain(self, tmp_path: Path):
        """A handler that always fails should run exactly once per drain(),
        not once per iteration (which would be 10 times with max_iterations=10)."""
        root = _setup(tmp_path)
        _emit(root, "task-completed", {"task_id": "t1", "task_dir": str(tmp_path)})

        calls = {"n": 0}
        def always_fail(root, payload):
            calls["n"] += 1
            return False

        handlers = _make_handlers({"memory": always_fail, "trajectory": True})
        with mock.patch("eventbus.HANDLERS", handlers), \
             mock.patch("lib_core.is_learning_enabled", return_value=True), \
             mock.patch("eventbus.log_event"):
            from eventbus import drain
            drain(root, max_iterations=10)

        assert calls["n"] == 1, f"permanently failing handler ran {calls['n']} times, expected 1"


# ---------------------------------------------------------------------------
# Multiple queued events: AND semantics
# ---------------------------------------------------------------------------

class TestMultiEventAND:
    def test_failure_on_one_event_blocks_follow_on(self, tmp_path: Path):
        root = _setup(tmp_path)
        _emit(root, "task-completed", {"task_id": "t1", "task_dir": str(tmp_path)})
        _emit(root, "task-completed", {"task_id": "t2", "task_dir": str(tmp_path)})

        calls = {"n": 0}
        def fail_first(root, payload):
            calls["n"] += 1
            return calls["n"] > 1

        handlers = _make_handlers({"memory": fail_first, "trajectory": True})
        with mock.patch("eventbus.HANDLERS", handlers), \
             mock.patch("lib_core.is_learning_enabled", return_value=True), \
             mock.patch("eventbus.log_event"):
            from eventbus import drain
            drain(root, max_iterations=1)

        assert _count_events(root, "memory-completed") == 0
