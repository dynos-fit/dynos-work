#!/usr/bin/env python3
"""Synthesize benchmark fixtures from completed task retrospectives."""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import argparse
import json
from pathlib import Path

from lib_registry import ensure_learned_registry
from lib_benchmark import synthesize_fixture_for_entry


def _find_entry(root: Path, agent_name: str, role: str, task_type: str, item_kind: str) -> dict:
    registry = ensure_learned_registry(root)
    for entry in registry.get("agents", []):
        if (
            entry.get("agent_name") == agent_name
            and entry.get("role") == role
            and entry.get("task_type") == task_type
            and entry.get("item_kind", "agent") == item_kind
        ):
            return entry
    raise SystemExit(f"registry entry not found: {agent_name} ({role}, {task_type}, {item_kind})")


def cmd_synthesize(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    entry = _find_entry(root, args.agent_name, args.role, args.task_type, args.item_kind)
    fixture = synthesize_fixture_for_entry(root, entry, limit=args.limit)
    if fixture is None:
        raise SystemExit("no matching retrospectives found for fixture synthesis")
    print(json.dumps(fixture, indent=2))
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    registry = ensure_learned_registry(root)
    synthesized = []
    for entry in registry.get("agents", []):
        fixture = synthesize_fixture_for_entry(root, entry, limit=args.limit)
        if fixture is not None:
            synthesized.append(
                {
                    "fixture_id": fixture["fixture_id"],
                    "target_name": fixture["target_name"],
                    "role": fixture["role"],
                    "task_type": fixture["task_type"],
                    "source_tasks": fixture["source_tasks"],
                }
            )
    print(json.dumps({"synthesized": synthesized}, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    synth_parser = subparsers.add_parser("synthesize", help="Generate a fixture for one registry entry")
    synth_parser.add_argument("agent_name")
    synth_parser.add_argument("role")
    synth_parser.add_argument("task_type")
    synth_parser.add_argument("--item-kind", choices=["agent", "skill"], default="agent")
    synth_parser.add_argument("--limit", type=int, default=5)
    synth_parser.add_argument("--root", default=".")
    synth_parser.set_defaults(func=cmd_synthesize)

    sync_parser = subparsers.add_parser("sync", help="Generate fixtures for all registry entries")
    sync_parser.add_argument("--limit", type=int, default=5)
    sync_parser.add_argument("--root", default=".")
    sync_parser.set_defaults(func=cmd_sync)
    return parser


if __name__ == "__main__":
    from cli_base import cli_main
    raise SystemExit(cli_main(build_parser))
