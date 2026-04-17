"""Unified structured event logging for dynos-work.

Writes JSONL events to per-task log files at .dynos/task-{id}/events.jsonl.
When no task context is available, falls back to .dynos/events.jsonl (global).
Thread-safe via fcntl advisory locking.
"""

from __future__ import annotations

import fcntl
import json
import sys
from pathlib import Path
from typing import Any

from lib_core import now_iso


def _append_jsonl(path: Path, line: str) -> None:
    """Thread-safe append a single line to a JSONL file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(line)
            f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def log_event(root: Path, event_type: str, *, task: str | None = None, **payload: Any) -> None:
    """Append one structured JSONL event to the task's events.jsonl.

    If `task` is provided and the task directory exists, writes to
    .dynos/{task}/events.jsonl (task-scoped). Otherwise writes to
    .dynos/events.jsonl (global fallback for daemon/system events).

    Args:
        root: Project root directory (contains .dynos/).
        event_type: Event name (e.g. "stage_transition", "router_model_decision").
        task: Optional task ID. When provided, events go to that task's log.
        **payload: Arbitrary key-value pairs merged into the JSON line.
    """
    try:
        record: dict[str, Any] = {"ts": now_iso(), "event": event_type}
        if task is not None:
            record["task"] = task
        record.update(payload)

        line = json.dumps(record, default=str, ensure_ascii=False) + "\n"

        # Task-scoped log when task ID is known and dir exists
        if task is not None:
            task_dir = root / ".dynos" / task
            if task_dir.is_dir():
                _append_jsonl(task_dir / "events.jsonl", line)
                return

        # Global fallback
        _append_jsonl(root / ".dynos" / "events.jsonl", line)
    except Exception as exc:
        print(f"[dynos-log] WARNING: log_event failed: {exc}", file=sys.stderr)
