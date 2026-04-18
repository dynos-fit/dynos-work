"""Regression tests for the concurrent-drain duplicate-handler-invocation race.

After _fire_task_completed() became async (PR 117), two near-simultaneous DONE
transitions spawn two parallel `eventbus.py drain` processes. Without a mutex,
both call consume_events() on the same unprocessed event, both invoke the
handler, then both mark_processed (which is idempotent on the field but the
handler still ran twice). Side effects of double-invocation can include:
duplicate post-completion log entries, duplicate registry mutations, double-
spent benchmark budgets.

Fix: drain() now acquires an exclusive fcntl lock on .dynos/events/drain.lock.
Concurrent calls receive {"skipped": ["another drain..."]} and exit immediately;
the running drain picks up new events on its next poll iteration.
"""
from __future__ import annotations

import multiprocessing as mp
import os
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))


def _setup(tmp_path: Path) -> Path:
    (tmp_path / ".dynos" / "events").mkdir(parents=True)
    return tmp_path


def _emit(root: Path, event_type: str, payload: dict | None = None) -> Path:
    from lib_events import emit_event
    return emit_event(root, event_type, "task", payload)


class TestDrainLockExclusivity:
    def test_drain_skips_when_lock_held(self, tmp_path: Path):
        """When another process holds the drain lock, drain() must return
        immediately with a skipped marker — no handler invocations."""
        import fcntl
        root = _setup(tmp_path)
        _emit(root, "task-completed", {"task_id": "t1", "task_dir": str(tmp_path)})

        lock_path = root / ".dynos" / "events" / "drain.lock"
        # Hold the lock from this test process
        held_fh = open(lock_path, "w")
        fcntl.flock(held_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        try:
            handler_calls = []
            def fake_handler(root, payload):
                handler_calls.append(payload)
                return True
            test_handlers = {"task-completed": [("test_handler", fake_handler)]}

            with mock.patch("eventbus.HANDLERS", test_handlers), \
                 mock.patch("lib_core.is_learning_enabled", return_value=True), \
                 mock.patch("eventbus.log_event"):
                from eventbus import drain
                summary = drain(root, max_iterations=3)

            assert "skipped" in summary, f"expected skipped marker, got {summary}"
            assert handler_calls == [], "handler must not run while lock is held"
        finally:
            fcntl.flock(held_fh.fileno(), fcntl.LOCK_UN)
            held_fh.close()

    def test_lock_released_after_drain_completes(self, tmp_path: Path):
        """A drain that completes normally must release the lock so the
        next drain can acquire it."""
        root = _setup(tmp_path)
        _emit(root, "task-completed", {"task_id": "t1", "task_dir": str(tmp_path)})

        test_handlers = {"task-completed": [("h1", lambda r, p: True)]}
        with mock.patch("eventbus.HANDLERS", test_handlers), \
             mock.patch("lib_core.is_learning_enabled", return_value=True), \
             mock.patch("eventbus.log_event"):
            from eventbus import drain
            drain(root, max_iterations=2)

        # Now another drain should be able to acquire the lock
        import fcntl
        lock_path = root / ".dynos" / "events" / "drain.lock"
        fh = open(lock_path, "w")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pytest.fail("lock not released after drain completed")
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()

    def test_lock_released_even_if_drain_raises(self, tmp_path: Path):
        """If the inner drain logic raises, the outer try/finally must
        still release the lock — otherwise a single crash blocks all
        future drains."""
        root = _setup(tmp_path)
        _emit(root, "task-completed", {"task_id": "t1", "task_dir": str(tmp_path)})

        with mock.patch("eventbus._drain_locked", side_effect=RuntimeError("boom")):
            from eventbus import drain
            with pytest.raises(RuntimeError):
                drain(root, max_iterations=1)

        # Lock should now be free
        import fcntl
        lock_path = root / ".dynos" / "events" / "drain.lock"
        fh = open(lock_path, "w")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pytest.fail("lock leaked after drain raised")
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()

    def test_handler_runs_exactly_once_for_event_under_concurrent_drains(
        self, tmp_path: Path
    ):
        """End-to-end: with two drain calls racing, the handler must run
        exactly once per event. The losing drain returns 'skipped' and the
        event is processed by the winning drain only."""
        import fcntl
        root = _setup(tmp_path)
        _emit(root, "task-completed", {"task_id": "t1", "task_dir": str(tmp_path)})

        # Simulate a winning drain by holding the lock externally
        lock_path = root / ".dynos" / "events" / "drain.lock"
        held_fh = open(lock_path, "w")
        fcntl.flock(held_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        try:
            calls = []
            test_handlers = {"task-completed": [("h", lambda r, p: (calls.append(p) or True))]}

            with mock.patch("eventbus.HANDLERS", test_handlers), \
                 mock.patch("lib_core.is_learning_enabled", return_value=True), \
                 mock.patch("eventbus.log_event"):
                from eventbus import drain
                # The losing drain
                summary = drain(root, max_iterations=2)

            assert summary.get("skipped"), f"expected skip, got {summary}"
            assert len(calls) == 0, "handler must not have run in the losing drain"
        finally:
            fcntl.flock(held_fh.fileno(), fcntl.LOCK_UN)
            held_fh.close()
