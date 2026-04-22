"""Regression tests for the synchronous post-completion drain bug.

Before the fix, transition_task() called _fire_task_completed() inline,
and that function used subprocess.run for the eventbus drain — blocking
the user's "task complete" return on improvement, agent generation,
policy refresh, dashboard refresh, and registry updates. The drain
took anywhere from seconds to the 120s timeout cap.

After the fix, the drain is dispatched via subprocess.Popen with
start_new_session=True so the parent returns immediately and the
drain runs in a detached background process. Output is redirected to
.dynos/events/drain.log for post-mortem inspection.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "hooks"))


def _make_task_dir(tmp_path: Path) -> Path:
    """Create a minimal task dir under tmp_path/.dynos/."""
    task_dir = tmp_path / ".dynos" / "task-async"
    task_dir.mkdir(parents=True)
    return task_dir


class TestFireTaskCompletedAsync:
    def test_drain_dispatched_via_popen_not_run(self, tmp_path: Path):
        """The drain must be a Popen (fire-and-forget), not a run (sync)."""
        task_dir = _make_task_dir(tmp_path)
        from lib_core import _fire_task_completed

        with mock.patch.dict("os.environ", {"PLUGIN_HOOKS": str(ROOT / "hooks")}, clear=False):
            with mock.patch("subprocess.run") as mock_run, \
                 mock.patch("subprocess.Popen") as mock_popen:
                _fire_task_completed(task_dir)

        # The emit step is still sync (must precede drain), so subprocess.run
        # is allowed exactly once and only for the emit command.
        run_commands = [call.args[0] for call in mock_run.call_args_list]
        assert all("lib_events.py" in c[1] for c in run_commands), \
            f"expected only emit calls in subprocess.run, got: {run_commands}"

        # The drain MUST be Popen (async).
        popen_commands = [call.args[0] for call in mock_popen.call_args_list]
        assert any("eventbus.py" in c[1] and "drain" in c for c in popen_commands), \
            f"drain must be dispatched via Popen, got Popen calls: {popen_commands}"

    def test_drain_popen_uses_start_new_session(self, tmp_path: Path):
        """The drain Popen must use start_new_session=True so the child
        survives the parent's exit and is detached from the parent's
        process group."""
        task_dir = _make_task_dir(tmp_path)
        from lib_core import _fire_task_completed

        with mock.patch.dict("os.environ", {"PLUGIN_HOOKS": str(ROOT / "hooks")}, clear=False):
            with mock.patch("subprocess.run"), \
                 mock.patch("subprocess.Popen") as mock_popen:
                _fire_task_completed(task_dir)

        drain_calls = [
            call for call in mock_popen.call_args_list
            if any("drain" in str(arg) for arg in call.args[0])
        ]
        assert drain_calls, "no drain Popen call observed"
        kwargs = drain_calls[0].kwargs
        assert kwargs.get("start_new_session") is True, (
            "drain Popen must set start_new_session=True for detachment"
        )
        assert kwargs.get("stdin") is not None, "stdin must be redirected"

    def test_returns_quickly_even_if_drain_is_slow(self, tmp_path: Path):
        """The function must return in well under a second even if the drain
        command (if it actually ran sync) would take many seconds."""
        task_dir = _make_task_dir(tmp_path)
        from lib_core import _fire_task_completed

        # Make subprocess.Popen no-op so we don't actually spawn anything.
        # We're testing that _fire_task_completed itself doesn't block.
        with mock.patch("subprocess.run") as mock_run, \
             mock.patch("subprocess.Popen") as mock_popen:
            mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            mock_popen.return_value = mock.Mock()
            t0 = time.monotonic()
            _fire_task_completed(task_dir)
            elapsed = time.monotonic() - t0

        assert elapsed < 1.0, (
            f"_fire_task_completed took {elapsed:.2f}s — must return well "
            f"under a second to keep DONE off the user's wait path"
        )

    def test_creates_drain_log_directory(self, tmp_path: Path):
        """A drain log file is required so post-completion failures remain
        inspectable. The directory must exist and the log path must be
        passed as stdout to Popen."""
        task_dir = _make_task_dir(tmp_path)
        from lib_core import _fire_task_completed

        with mock.patch("subprocess.run"), \
             mock.patch("subprocess.Popen") as mock_popen:
            _fire_task_completed(task_dir)

        log_dir = tmp_path / ".dynos" / "events"
        assert log_dir.exists(), "drain log directory must be created"
        assert (log_dir / "drain.log").exists(), \
            "drain.log must be created (with the dispatch header line written before Popen)"

    def test_emit_failure_does_not_prevent_drain_dispatch(self, tmp_path: Path):
        """If the emit subprocess raises, the function should still attempt
        to dispatch the drain (and at minimum not crash transition_task)."""
        task_dir = _make_task_dir(tmp_path)
        from lib_core import _fire_task_completed

        with mock.patch.dict("os.environ", {"PLUGIN_HOOKS": str(ROOT / "hooks")}, clear=False):
            with mock.patch("subprocess.run", side_effect=OSError("boom")), \
                 mock.patch("subprocess.Popen") as mock_popen:
                # Must not raise.
                _fire_task_completed(task_dir)

        # Drain dispatch should still have been attempted.
        assert mock_popen.called, "drain dispatch must run even when emit fails"
