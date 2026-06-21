"""Regression tests for orphan-token capture when no fresh active task exists.

After the attribution-drift fix, _find_active_task() returns None when the
freshest non-terminal task is older than the attribution window. Without a
fallback, the SubagentStop hook's main() would silently drop the token data
— losing legitimate token usage from long-running subagents (e.g., a
benchmark that runs for >1h while no transitions happen) or from subagents
that finish after a long manual pause.

Fix: when _find_active_task returns None AND the transcript has token data,
write the record to .dynos/orphan-tokens.jsonl so the data is preserved for
later reconciliation.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
import actor_identity  # noqa: E402


def _make_transcript(tmp_path: Path, input_tokens: int, output_tokens: int) -> Path:
    """Write a minimal subagent transcript JSONL with the given token counts."""
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(json.dumps({
        "agentId": "test-agent-123",
        "message": {
            "model": "claude-opus-4",
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
        },
    }) + "\n")
    return transcript


def _make_stale_task(root: Path, task_id: str, age_hours: float = 8.0) -> Path:
    """Create a non-terminal task with a stale manifest to force orphan path."""
    task_dir = root / ".dynos" / task_id
    task_dir.mkdir(parents=True)
    manifest = task_dir / "manifest.json"
    manifest.write_text(json.dumps({
        "task_id": task_id,
        "stage": "EXECUTION",
        "raw_input": "",
        "created_at": "2026-04-17T00:00:00Z",
    }))
    new_mtime = time.time() - age_hours * 3600
    os.utime(manifest, (new_mtime, new_mtime))
    return task_dir


class TestOrphanTokenCapture:
    def test_orphan_file_written_when_no_active_task_and_tokens_nonzero(
        self, tmp_path: Path
    ):
        """If there are no active tasks at all and the transcript has tokens,
        the data must be preserved in orphan-tokens.jsonl."""
        (tmp_path / ".dynos").mkdir()
        transcript = _make_transcript(tmp_path, input_tokens=12345, output_tokens=678)

        from lib_tokens_hook import main
        with mock.patch("sys.argv", [
            "lib_tokens_hook.py",
            "--transcript", str(transcript),
            "--agent-type", "dynos-work:long-running-bench",
            "--agent-desc", "ran for 90 minutes",
            "--root", str(tmp_path),
        ]):
            assert main() == 0

        orphan_path = tmp_path / ".dynos" / "orphan-tokens.jsonl"
        assert orphan_path.exists(), "orphan-tokens.jsonl must be created"
        records = [json.loads(line) for line in orphan_path.read_text().splitlines() if line.strip()]
        assert len(records) == 1
        rec = records[0]
        assert rec["agent"] == "long-running-bench", "dynos-work: prefix must be stripped"
        assert rec["input_tokens"] == 12345
        assert rec["output_tokens"] == 678
        assert rec["agent_id"] == "test-agent-123"
        assert rec["model"] == "opus"
        assert rec["transcript_path"] == str(transcript)
        assert "no fresh active task" in rec["reason"]

    def test_orphan_file_written_when_freshest_task_is_stale(self, tmp_path: Path):
        """Concrete scenario: a long-running subagent finishes 8 hours after
        the most recent stage transition. The window-gated
        _find_active_task returns None — without the orphan capture, the
        subagent's tokens would silently disappear."""
        _make_stale_task(tmp_path, "task-20260417-001", age_hours=8.0)
        transcript = _make_transcript(tmp_path, input_tokens=500_000, output_tokens=20_000)

        from lib_tokens_hook import main
        with mock.patch("sys.argv", [
            "lib_tokens_hook.py",
            "--transcript", str(transcript),
            "--agent-type", "dynos-work:planning",
            "--root", str(tmp_path),
        ]):
            assert main() == 0

        orphan_path = tmp_path / ".dynos" / "orphan-tokens.jsonl"
        assert orphan_path.exists(), "orphan must catch the stale-window drop"
        records = [json.loads(line) for line in orphan_path.read_text().splitlines() if line.strip()]
        assert len(records) == 1
        assert records[0]["input_tokens"] == 500_000

    def test_no_orphan_record_when_transcript_has_zero_tokens(self, tmp_path: Path):
        """An empty transcript (no token data) should NOT pollute the orphan
        ledger — there's no data to preserve."""
        (tmp_path / ".dynos").mkdir()
        transcript = _make_transcript(tmp_path, input_tokens=0, output_tokens=0)

        from lib_tokens_hook import main
        with mock.patch("sys.argv", [
            "lib_tokens_hook.py",
            "--transcript", str(transcript),
            "--agent-type", "dynos-work:noop",
            "--root", str(tmp_path),
        ]):
            assert main() == 0

        orphan_path = tmp_path / ".dynos" / "orphan-tokens.jsonl"
        assert not orphan_path.exists(), \
            "orphan file must not be created for zero-token transcripts"

    def test_normal_attribution_unchanged_when_active_task_is_fresh(self, tmp_path: Path):
        """Sanity: a fresh active task gets normal attribution; orphan path
        is not triggered."""
        task_dir = _make_stale_task(tmp_path, "task-20260417-001", age_hours=0.001)
        transcript = _make_transcript(tmp_path, input_tokens=100, output_tokens=50)

        from lib_tokens_hook import main
        with mock.patch("sys.argv", [
            "lib_tokens_hook.py",
            "--transcript", str(transcript),
            "--agent-type", "dynos-work:executor",
            "--root", str(tmp_path),
        ]):
            assert main() == 0

        # No orphan file
        assert not (tmp_path / ".dynos" / "orphan-tokens.jsonl").exists()
        # But the task's token-usage.json should have the record
        token_log = task_dir / "token-usage.json"
        assert token_log.exists(), "fresh task should receive the attribution"
        data = json.loads(token_log.read_text())
        assert data["total"] == 150

    def test_multiple_active_tasks_without_session_binding_are_orphaned(self, tmp_path: Path):
        """A global active-task pointer must not steal tokens when another
        active concurrent task exists and no session binding is available."""
        task_a = _make_stale_task(tmp_path, "task-20260417-001", age_hours=0.001)
        task_b = _make_stale_task(tmp_path, "task-20260417-002", age_hours=0.001)
        (tmp_path / ".dynos" / "active-task.json").write_text(json.dumps({
            "task_id": task_b.name,
            "task_dir": str(task_b),
            "stage": "EXECUTION",
        }))
        transcript = _make_transcript(tmp_path, input_tokens=200, output_tokens=25)

        from lib_tokens_hook import main
        with mock.patch("sys.argv", [
            "lib_tokens_hook.py",
            "--transcript", str(transcript),
            "--agent-type", "dynos-work:executor",
            "--root", str(tmp_path),
        ]):
            assert main() == 0

        assert not (task_a / "token-usage.json").exists()
        assert not (task_b / "token-usage.json").exists()
        assert (tmp_path / ".dynos" / "orphan-tokens.jsonl").exists()

    def test_session_binding_attributes_tokens_to_bound_task(self, tmp_path: Path):
        task_a = _make_stale_task(tmp_path, "task-20260417-001", age_hours=0.001)
        task_b = _make_stale_task(tmp_path, "task-20260417-002", age_hours=0.001)
        (tmp_path / ".dynos" / "active-task.json").write_text(json.dumps({
            "task_id": task_b.name,
            "task_dir": str(task_b),
            "stage": "EXECUTION",
        }))
        session_id = "session-A"
        actor_identity.bind_session_task(tmp_path, session_id, task_a)
        transcript = _make_transcript(tmp_path, input_tokens=300, output_tokens=40)

        from lib_tokens_hook import main
        with mock.patch("sys.argv", [
            "lib_tokens_hook.py",
            "--transcript", str(transcript),
            "--agent-type", "dynos-work:executor",
            "--root", str(tmp_path),
            "--session-id", session_id,
        ]):
            assert main() == 0

        assert (task_a / "token-usage.json").exists()
        assert not (task_b / "token-usage.json").exists()
        assert not (tmp_path / ".dynos" / "orphan-tokens.jsonl").exists()
