#!/usr/bin/env python3
"""Compact runtime observability report for dynos-work."""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import argparse
import json
from pathlib import Path

from dynoslib import (
    ensure_automation_queue,
    ensure_benchmark_history,
    ensure_benchmark_index,
    ensure_learned_registry,
)


def build_report(root: Path) -> dict:
    registry = ensure_learned_registry(root)
    queue = ensure_automation_queue(root)
    history = ensure_benchmark_history(root)
    index = ensure_benchmark_index(root)
    agents = registry.get("agents", [])
    active = [item for item in agents if item.get("route_allowed")]
    shadow = [item for item in agents if item.get("mode") == "shadow"]
    demoted = [item for item in agents if item.get("status") == "demoted_on_regression"]
    queue_items = queue.get("items", [])
    runs = history.get("runs", [])
    fixture_ids = {item.get("fixture_id") for item in index.get("fixtures", [])}
    uncovered = [
        {
            "target_name": item.get("agent_name"),
            "role": item.get("role"),
            "task_type": item.get("task_type"),
            "item_kind": item.get("item_kind", "agent"),
        }
        for item in shadow
        if f"{item.get('item_kind', 'agent')}-{item.get('agent_name')}-{item.get('task_type')}" not in fixture_ids
    ]
    report = {
        "registry_updated_at": registry.get("updated_at"),
        "summary": {
            "learned_components": len(agents),
            "active_routes": len(active),
            "shadow_components": len(shadow),
            "demoted_components": len(demoted),
            "queued_automation_jobs": len(queue_items),
            "benchmark_runs": len(runs),
            "tracked_fixtures": len(index.get("fixtures", [])),
            "coverage_gaps": len(uncovered),
        },
        "active_routes": [
            {
                "agent_name": item.get("agent_name"),
                "role": item.get("role"),
                "task_type": item.get("task_type"),
                "item_kind": item.get("item_kind", "agent"),
                "mode": item.get("mode"),
                "composite": item.get("benchmark_summary", {}).get("mean_composite", 0.0),
            }
            for item in active
        ],
        "demotions": [
            {
                "agent_name": item.get("agent_name"),
                "role": item.get("role"),
                "task_type": item.get("task_type"),
                "last_evaluation": item.get("last_evaluation", {}),
            }
            for item in demoted
        ],
        "automation_queue": queue_items,
        "coverage_gaps": uncovered,
        "recent_runs": runs[-5:],
    }
    return report


def cmd_report(args: argparse.Namespace) -> int:
    report = build_report(Path(args.root).resolve())
    print(json.dumps(report, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.set_defaults(func=cmd_report)
    return parser


if __name__ == "__main__":
    from dyno_cli_base import cli_main
    raise SystemExit(cli_main(build_parser))
