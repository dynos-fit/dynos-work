#!/usr/bin/env python3
"""Automation queue management for dynos-work."""

from __future__ import annotations

from pathlib import Path

from lib_core import (
    automation_queue_path,
    load_json,
    now_iso,
    write_json,
)


def ensure_automation_queue(root: Path) -> dict:
    """Ensure the automation queue file exists and return its contents."""
    path = automation_queue_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or not path.read_text().strip():
        queue: dict = {"version": 1, "updated_at": now_iso(), "items": []}
        write_json(path, queue)
        return queue
    data = load_json(path)
    if not isinstance(data, dict) or "items" not in data:
        queue = {"version": 1, "updated_at": now_iso(), "items": []}
        write_json(path, queue)
        return queue
    return data


def enqueue_automation_item(root: Path, item: dict) -> dict:
    """Add an item to the automation queue."""
    queue = ensure_automation_queue(root)
    queue.setdefault("items", []).append(item)
    queue["updated_at"] = now_iso()
    write_json(automation_queue_path(root), queue)
    return queue


def replace_automation_queue(root: Path, items: list[dict]) -> dict:
    """Replace the entire automation queue with new items."""
    queue = ensure_automation_queue(root)
    queue["items"] = items
    queue["updated_at"] = now_iso()
    write_json(automation_queue_path(root), queue)
    return queue


def queue_identity(item: dict) -> tuple[str, str]:
    """Return a unique identity tuple for a queue item."""
    return (str(item.get("agent_name", "")), str(item.get("fixture_path", "")))
