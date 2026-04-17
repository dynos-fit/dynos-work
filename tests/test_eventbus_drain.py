"""Integration tests for eventbus.drain() behavior.

Flat chain: all handlers fire on task-completed directly.
No intermediate events.
"""
from __future__ import annotations

import json
import os
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


def _count_events(root: Path, event_type: str) -> int:
    return len(list((root / ".dynos" / "events").glob(f"*-{event_type}.json")))


def _processed_by(event_path: Path) -> list[str]:
    return json.loads(event_path.read_text()).get("processed_by", [])


def _make_handlers(overrides: dict):
    import eventbus
    def _make_fn(result):
        if callable(result):
            return result
        return lambda root, payload: result
    test_handlers = {}
    for event_type, handlers in eventbus.HANDLERS.items():
        test_handlers[event_type] = []
        for name, _ in handlers:
            fn = _make_fn(overrides.get(name, True))
            test_handlers[event_type].append((name, fn))
    return test_handlers


class TestFlatChain:
    def test_all_handlers_fire_on_task_completed(self, tmp_path: Path):
        root = _setup(tmp_path)
        _emit(root, "task-completed", {"task_id": "t1", "task_dir": str(tmp_path)})

        called = set()
        def track(name):
            def fn(root, payload):
                called.add(name)
                return True
            return fn

        handlers = _make_handlers({
            "policy_engine": track("policy_engine"),
            "postmortem": track("postmortem"),
            "dashboard": track("dashboard"),
            "register": track("register"),
        })
        with mock.patch("eventbus.HANDLERS", handlers), \
             mock.patch("lib_core.is_learning_enabled", return_value=True), \
             mock.patch("eventbus.log_event"):
            from eventbus import drain
            drain(root, max_iterations=1)

        assert called == {"policy_engine", "postmortem", "dashboard", "register"}

    def test_no_follow_on_events(self, tmp_path: Path):
        root = _setup(tmp_path)
        _emit(root, "task-completed", {"task_id": "t1", "task_dir": str(tmp_path)})

        handlers = _make_handlers({})
        with mock.patch("eventbus.HANDLERS", handlers), \
             mock.patch("lib_core.is_learning_enabled", return_value=True), \
             mock.patch("eventbus.log_event"):
            from eventbus import drain
            drain(root, max_iterations=5)

        # No intermediate events should exist
        events_dir = root / ".dynos" / "events"
        all_events = list(events_dir.glob("*.json"))
        event_types = {json.loads(f.read_text()).get("event_type") for f in all_events}
        assert event_types == {"task-completed"}


class TestFailedHandlerRetry:
    def test_failed_handler_not_marked_processed(self, tmp_path: Path):
        root = _setup(tmp_path)
        ep = _emit(root, "task-completed", {"task_id": "t1", "task_dir": str(tmp_path)})

        handlers = _make_handlers({"policy_engine": False, "postmortem": True})
        with mock.patch("eventbus.HANDLERS", handlers), \
             mock.patch("lib_core.is_learning_enabled", return_value=True), \
             mock.patch("eventbus.log_event"):
            from eventbus import drain
            drain(root, max_iterations=1)

        processed = _processed_by(ep)
        assert "postmortem" in processed
        assert "policy_engine" not in processed

    def test_failed_handler_retried_on_second_drain(self, tmp_path: Path):
        root = _setup(tmp_path)
        _emit(root, "task-completed", {"task_id": "t1", "task_dir": str(tmp_path)})

        calls = {"n": 0}
        def fail_then_succeed(root, payload):
            calls["n"] += 1
            return calls["n"] > 1

        handlers = _make_handlers({"policy_engine": fail_then_succeed})
        with mock.patch("eventbus.HANDLERS", handlers), \
             mock.patch("lib_core.is_learning_enabled", return_value=True), \
             mock.patch("eventbus.log_event"):
            from eventbus import drain
            drain(root, max_iterations=1)
            assert calls["n"] == 1
            drain(root, max_iterations=1)
            assert calls["n"] == 2


class TestLearningDisabled:
    def test_policy_engine_skipped_when_learning_off(self, tmp_path: Path):
        root = _setup(tmp_path)
        _emit(root, "task-completed", {"task_id": "t1", "task_dir": str(tmp_path)})

        pe_called = {"v": False}
        dash_called = {"v": False}
        def track_pe(root, payload):
            pe_called["v"] = True
            return True
        def track_dash(root, payload):
            dash_called["v"] = True
            return True

        handlers = _make_handlers({"policy_engine": track_pe, "dashboard": track_dash})
        with mock.patch("eventbus.HANDLERS", handlers), \
             mock.patch("lib_core.is_learning_enabled", return_value=False), \
             mock.patch("eventbus.log_event"):
            from eventbus import drain
            drain(root, max_iterations=1)

        assert not pe_called["v"], "policy_engine should NOT run with learning off"
        assert dash_called["v"], "dashboard should run with learning off"


class TestNoTightRetry:
    def test_permanently_failing_handler_runs_once_per_drain(self, tmp_path: Path):
        root = _setup(tmp_path)
        _emit(root, "task-completed", {"task_id": "t1", "task_dir": str(tmp_path)})

        calls = {"n": 0}
        def always_fail(root, payload):
            calls["n"] += 1
            return False

        handlers = _make_handlers({"policy_engine": always_fail})
        with mock.patch("eventbus.HANDLERS", handlers), \
             mock.patch("lib_core.is_learning_enabled", return_value=True), \
             mock.patch("eventbus.log_event"):
            from eventbus import drain
            drain(root, max_iterations=10)

        assert calls["n"] == 1


class TestMultiTaskReceipts:
    def test_receipts_for_multiple_tasks(self, tmp_path: Path):
        root = _setup(tmp_path)
        task1 = tmp_path / ".dynos" / "task-1"
        task2 = tmp_path / ".dynos" / "task-2"
        task1.mkdir(parents=True)
        task2.mkdir(parents=True)

        _emit(root, "task-completed", {"task_id": "task-1", "task_dir": str(task1)})
        _emit(root, "task-completed", {"task_id": "task-2", "task_dir": str(task2)})

        handlers = _make_handlers({})
        with mock.patch("eventbus.HANDLERS", handlers), \
             mock.patch("lib_core.is_learning_enabled", return_value=True), \
             mock.patch("eventbus.log_event"), \
             mock.patch("lib_receipts.receipt_post_completion") as mock_receipt:
            from eventbus import drain
            drain(root, max_iterations=5)

        call_dirs = [str(call.args[0]) for call in mock_receipt.call_args_list]
        assert str(task1) in call_dirs
        assert str(task2) in call_dirs
