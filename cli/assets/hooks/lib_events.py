#!/usr/bin/env python3
"""File-based event bus for dynos-work pipeline decoupling.

Events are JSON files in .dynos/events/. Each pipeline emits events
when it completes work; other pipelines subscribe via the drain runner.
This module depends only on lib_core.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lib_core import load_json, now_iso, write_json

# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------

EVENT_TYPES: set[str] = {
    "task-completed",
}

VALID_PIPELINES: set[str] = {"task", "memory", "observability", "eventbus"}

# Retention: processed events older than this (seconds) are deleted on cleanup
RETENTION_SECONDS: int = 7 * 24 * 3600  # 7 days


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------

def _events_dir(root: Path) -> Path:
    """Return the events directory, creating it if needed."""
    d = root / ".dynos" / "events"
    d.mkdir(parents=True, exist_ok=True)
    return d


def emit_event(
    root: Path,
    event_type: str,
    source_pipeline: str,
    payload: dict[str, Any] | None = None,
) -> Path:
    """Emit an event by writing a JSON file to .dynos/events/.

    Returns the path to the created event file.
    """
    if event_type not in EVENT_TYPES:
        raise ValueError(f"Unknown event type: {event_type}. Valid: {sorted(EVENT_TYPES)}")

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    event = {
        "event_type": event_type,
        "emitted_at": now_iso(),
        "source_pipeline": source_pipeline,
        "payload": payload or {},
        "processed_by": [],
    }

    event_path = _events_dir(root) / f"{ts}-{event_type}.json"
    write_json(event_path, event)
    return event_path


def consume_events(
    root: Path,
    event_type: str,
    consumer_name: str,
) -> list[tuple[Path, dict]]:
    """Read all unprocessed events of a given type for a consumer.

    Returns list of (path, event_data) tuples for events not yet
    processed by consumer_name.
    """
    events_dir = _events_dir(root)
    results: list[tuple[Path, dict]] = []

    for event_path in sorted(events_dir.glob(f"*-{event_type}.json")):
        try:
            event = load_json(event_path)
        except (json.JSONDecodeError, FileNotFoundError, OSError):
            continue

        if consumer_name not in event.get("processed_by", []):
            results.append((event_path, event))

    return results


def mark_processed(event_path: Path, consumer_name: str) -> None:
    """Mark an event as processed by a consumer."""
    try:
        event = load_json(event_path)
    except (json.JSONDecodeError, FileNotFoundError, OSError):
        return

    processed = event.get("processed_by", [])
    if consumer_name not in processed:
        processed.append(consumer_name)
        event["processed_by"] = processed
        write_json(event_path, event)


def cleanup_old_events(root: Path) -> int:
    """Delete fully-processed events older than RETENTION_SECONDS.

    An event is fully processed when ALL registered consumers for its
    event type have marked it. Partially-processed events are kept
    regardless of age so slow/offline consumers don't lose work.

    Returns count of deleted files.
    """
    events_dir = _events_dir(root)
    cutoff = time.time() - RETENTION_SECONDS
    deleted = 0

    # Import HANDLERS lazily to get the consumer list per event type
    try:
        from eventbus import HANDLERS
    except ImportError:
        HANDLERS = {}

    for event_path in events_dir.glob("*.json"):
        try:
            event = load_json(event_path)
        except (json.JSONDecodeError, FileNotFoundError, OSError):
            continue

        processed_by = set(event.get("processed_by", []))
        if not processed_by:
            continue

        # Check that ALL consumers for this event type have processed it
        event_type = event.get("event_type", "")
        expected_consumers = {name for name, _ in HANDLERS.get(event_type, [])}
        if expected_consumers and not expected_consumers.issubset(processed_by):
            continue  # partially processed — keep it

        # Check file age
        try:
            if event_path.stat().st_mtime < cutoff:
                event_path.unlink()
                deleted += 1
        except OSError:
            continue

    return deleted


# ---------------------------------------------------------------------------
# CLI (for use from bash hooks)
# ---------------------------------------------------------------------------

def _cmd_emit(args: Any) -> int:
    """CLI: emit an event."""
    root = Path(args.root).resolve()
    payload = {}
    if args.payload:
        try:
            payload = json.loads(args.payload)
        except json.JSONDecodeError:
            print(f"Invalid JSON payload: {args.payload}", file=__import__("sys").stderr)
            return 1

    path = emit_event(root, args.type, args.source, payload)
    print(json.dumps({"emitted": str(path)}))
    return 0


def _cmd_list(args: Any) -> int:
    """CLI: list pending events."""
    root = Path(args.root).resolve()
    events_dir = _events_dir(root)
    events = []
    for event_path in sorted(events_dir.glob("*.json")):
        try:
            event = load_json(event_path)
            events.append({
                "file": event_path.name,
                "type": event.get("event_type"),
                "emitted_at": event.get("emitted_at"),
                "processed_by": event.get("processed_by", []),
            })
        except (json.JSONDecodeError, FileNotFoundError, OSError):
            continue
    print(json.dumps(events, indent=2))
    return 0


def _cmd_cleanup(args: Any) -> int:
    """CLI: clean up old events."""
    root = Path(args.root).resolve()
    deleted = cleanup_old_events(root)
    print(f"Deleted {deleted} old event(s)")
    return 0


def build_parser() -> "argparse.ArgumentParser":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    emit_p = sub.add_parser("emit", help="Emit an event")
    emit_p.add_argument("--root", default=".")
    emit_p.add_argument("--type", required=True, choices=sorted(EVENT_TYPES))
    emit_p.add_argument("--source", default="task", choices=sorted(VALID_PIPELINES))
    emit_p.add_argument("--payload", default=None, help="JSON payload string")
    emit_p.set_defaults(func=_cmd_emit)

    list_p = sub.add_parser("list", help="List pending events")
    list_p.add_argument("--root", default=".")
    list_p.set_defaults(func=_cmd_list)

    clean_p = sub.add_parser("cleanup", help="Delete old processed events")
    clean_p.add_argument("--root", default=".")
    clean_p.set_defaults(func=_cmd_cleanup)

    return parser


if __name__ == "__main__":
    from cli_base import cli_main
    raise SystemExit(cli_main(build_parser))
