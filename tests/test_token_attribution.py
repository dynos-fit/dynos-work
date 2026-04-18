"""Regression tests for token-attribution drift in lib_tokens_hook._find_active_task.

Before the fix, the function returned the highest task ID among non-terminal
tasks. A task that stalled mid-pipeline (e.g., never reached DONE because
TEST_EXECUTION crashed) would remain "active" forever and silently absorb
every subsequent SubagentStop token recording — including manual
/dynos-work:investigate runs unrelated to that task. Concrete observed
case: task-20260417-012 stalled at TEST_EXECUTION on 2026-04-17 and
accumulated 30,463,455 phantom investigator tokens over the next 7 hours
from unrelated investigation work.

The fix uses manifest.json mtime as the freshness signal (transition_task
rewrites the manifest atomically on every stage advance) and refuses
attribution when the freshest non-terminal task is older than a configurable
window (default 1 hour, env DYNOS_TASK_ATTRIBUTION_WINDOW_SECONDS).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))


def _make_task(root: Path, task_id: str, stage: str, *, manifest_age_seconds: float = 0.0) -> Path:
    """Create a minimal task dir with a manifest at the given stage and mtime."""
    task_dir = root / ".dynos" / task_id
    task_dir.mkdir(parents=True)
    manifest = task_dir / "manifest.json"
    manifest.write_text(json.dumps({
        "task_id": task_id,
        "stage": stage,
        "created_at": "2026-04-17T00:00:00Z",
        "raw_input": "",
    }))
    if manifest_age_seconds > 0:
        new_mtime = time.time() - manifest_age_seconds
        os.utime(manifest, (new_mtime, new_mtime))
    return task_dir


class TestFindActiveTask:
    def test_returns_none_when_no_dynos_dir(self, tmp_path: Path):
        from lib_tokens_hook import _find_active_task
        assert _find_active_task(tmp_path) is None

    def test_returns_none_when_only_terminal_tasks(self, tmp_path: Path):
        _make_task(tmp_path, "task-20260417-001", "DONE")
        _make_task(tmp_path, "task-20260417-002", "FAILED")
        from lib_tokens_hook import _find_active_task
        assert _find_active_task(tmp_path) is None

    def test_picks_freshest_manifest_not_highest_id(self, tmp_path: Path):
        """The bug was: highest task ID won, even if its manifest was hours old.
        The fix: most recently modified manifest wins."""
        # Older ID, fresh manifest (current work)
        fresh = _make_task(tmp_path, "task-20260417-001", "EXECUTION", manifest_age_seconds=10)
        # Newer ID, stale manifest (the abandoned task)
        _make_task(tmp_path, "task-20260417-099", "TEST_EXECUTION", manifest_age_seconds=3600 * 8)

        from lib_tokens_hook import _find_active_task
        result = _find_active_task(tmp_path)
        assert result == fresh, (
            "freshest-manifest task should win over highest task ID"
        )

    def test_returns_none_when_all_active_tasks_are_stale(self, tmp_path: Path):
        """If every non-terminal task has a stale manifest (older than
        the attribution window), refuse attribution rather than mis-attribute
        to a stalled task. This is the core fix for the 30M phantom-token bug."""
        _make_task(tmp_path, "task-20260417-012", "TEST_EXECUTION", manifest_age_seconds=3600 * 8)
        _make_task(tmp_path, "task-20260417-099", "EXECUTION", manifest_age_seconds=3600 * 2)

        from lib_tokens_hook import _find_active_task
        assert _find_active_task(tmp_path) is None, (
            "all-stale tasks should produce None, not silent mis-attribution"
        )

    def test_window_is_configurable_via_env(self, tmp_path: Path, monkeypatch):
        """A long-running stage might genuinely take more than the default
        window. Operators can override via DYNOS_TASK_ATTRIBUTION_WINDOW_SECONDS."""
        # Default window is 1h — make a 2h-old task
        task = _make_task(tmp_path, "task-20260417-001", "EXECUTION", manifest_age_seconds=3600 * 2)

        from lib_tokens_hook import _find_active_task
        # Default: rejected as stale
        assert _find_active_task(tmp_path) is None

        # Override to 4h: accepted
        monkeypatch.setenv("DYNOS_TASK_ATTRIBUTION_WINDOW_SECONDS", str(3600 * 4))
        assert _find_active_task(tmp_path) == task

    def test_cancelled_tasks_excluded(self, tmp_path: Path):
        _make_task(tmp_path, "task-20260417-001", "CANCELLED", manifest_age_seconds=10)
        from lib_tokens_hook import _find_active_task
        assert _find_active_task(tmp_path) is None

    def test_skips_dirs_without_manifest(self, tmp_path: Path):
        """A task dir that exists but has no manifest yet (e.g., partial init)
        should not crash or be selected."""
        (tmp_path / ".dynos" / "task-20260417-001").mkdir(parents=True)  # no manifest
        fresh = _make_task(tmp_path, "task-20260417-002", "EXECUTION", manifest_age_seconds=10)

        from lib_tokens_hook import _find_active_task
        assert _find_active_task(tmp_path) == fresh

    def test_skips_dirs_with_invalid_manifest_json(self, tmp_path: Path):
        bad = tmp_path / ".dynos" / "task-20260417-001"
        bad.mkdir(parents=True)
        (bad / "manifest.json").write_text("{not json")
        fresh = _make_task(tmp_path, "task-20260417-002", "EXECUTION", manifest_age_seconds=10)

        from lib_tokens_hook import _find_active_task
        assert _find_active_task(tmp_path) == fresh
