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
            "improve": track("improve"),
            "policy_engine": track("policy_engine"),
            "dashboard": track("dashboard"),
            "register": track("register"),
        })
        with mock.patch("eventbus.HANDLERS", handlers), \
             mock.patch("lib_core.is_learning_enabled", return_value=True), \
             mock.patch("eventbus.log_event"):
            from eventbus import drain
            drain(root, max_iterations=1)

        assert called == {"improve", "policy_engine", "dashboard", "register"}

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

        handlers = _make_handlers({"policy_engine": False, "improve": True})
        with mock.patch("eventbus.HANDLERS", handlers), \
             mock.patch("lib_core.is_learning_enabled", return_value=True), \
             mock.patch("eventbus.log_event"):
            from eventbus import drain
            drain(root, max_iterations=1)

        processed = _processed_by(ep)
        assert "improve" in processed
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


class TestPostCompletionReceiptFidelity:
    """Regression tests for the eventbus:295 bug where postmortem_written and
    patterns_updated were derived from `calibration-completed` and
    `memory-completed` summary buckets that no longer exist after the chain
    was flattened to task-completed only. Both fields were silently False
    even when the underlying handlers ran successfully.
    """

    def test_patterns_updated_true_when_policy_engine_succeeds(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("DYNOS_HOME", str(tmp_path))
        root = _setup(tmp_path)
        task = tmp_path / ".dynos" / "task-1"
        task.mkdir(parents=True)
        _emit(root, "task-completed", {"task_id": "task-1", "task_dir": str(task)})

        handlers = _make_handlers({"policy_engine": True, "improve": True,
                                    "dashboard": True, "register": True,
                                    "agent_generator": True,
                                    "benchmark_scheduler": True})
        with mock.patch("eventbus.HANDLERS", handlers), \
             mock.patch("lib_core.is_learning_enabled", return_value=True), \
             mock.patch("eventbus.log_event"), \
             mock.patch("lib_receipts.receipt_post_completion") as mock_receipt:
            from eventbus import drain
            drain(root, max_iterations=5)

        assert mock_receipt.call_count == 1
        kwargs = mock_receipt.call_args.kwargs
        assert kwargs["patterns_updated"] is True, \
            "patterns_updated should be True when policy_engine handler returns ok"

    def test_patterns_updated_false_when_policy_engine_fails(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("DYNOS_HOME", str(tmp_path))
        root = _setup(tmp_path)
        task = tmp_path / ".dynos" / "task-1"
        task.mkdir(parents=True)
        _emit(root, "task-completed", {"task_id": "task-1", "task_dir": str(task)})

        handlers = _make_handlers({"policy_engine": False, "improve": True,
                                    "dashboard": True, "register": True,
                                    "agent_generator": True,
                                    "benchmark_scheduler": True})
        with mock.patch("eventbus.HANDLERS", handlers), \
             mock.patch("lib_core.is_learning_enabled", return_value=True), \
             mock.patch("eventbus.log_event"), \
             mock.patch("lib_receipts.receipt_post_completion") as mock_receipt:
            from eventbus import drain
            drain(root, max_iterations=5)

        kwargs = mock_receipt.call_args.kwargs
        assert kwargs["patterns_updated"] is False, \
            "patterns_updated should be False when policy_engine returns failure"

    def test_postmortem_written_reflects_disk_state_per_task(self, tmp_path: Path, monkeypatch):
        """postmortem_written must check the persistent project dir per task,
        since the postmortem now runs in the audit skill (not as an eventbus
        handler) and writes per-task .json under postmortems/."""
        monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "dynos-home"))
        root = _setup(tmp_path)

        task_with_pm = tmp_path / ".dynos" / "task-WITH"
        task_without_pm = tmp_path / ".dynos" / "task-WITHOUT"
        task_with_pm.mkdir(parents=True)
        task_without_pm.mkdir(parents=True)

        # Seed only one of the two tasks' postmortems on disk in the
        # persistent project dir for `root`.
        from lib_core import _persistent_project_dir
        pm_dir = _persistent_project_dir(root) / "postmortems"
        pm_dir.mkdir(parents=True, exist_ok=True)
        (pm_dir / "task-WITH.json").write_text("{}")

        _emit(root, "task-completed", {"task_id": "task-WITH", "task_dir": str(task_with_pm)})
        _emit(root, "task-completed", {"task_id": "task-WITHOUT", "task_dir": str(task_without_pm)})

        handlers = _make_handlers({})
        with mock.patch("eventbus.HANDLERS", handlers), \
             mock.patch("lib_core.is_learning_enabled", return_value=True), \
             mock.patch("eventbus.log_event"), \
             mock.patch("lib_receipts.receipt_post_completion") as mock_receipt:
            from eventbus import drain
            drain(root, max_iterations=5)

        per_task = {
            str(call.args[0]): call.kwargs["postmortem_written"]
            for call in mock_receipt.call_args_list
        }
        assert per_task[str(task_with_pm)] is True, \
            "task with on-disk postmortem must report postmortem_written=True"
        assert per_task[str(task_without_pm)] is False, \
            "task without on-disk postmortem must report postmortem_written=False"
