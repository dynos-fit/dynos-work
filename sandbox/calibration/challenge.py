#!/usr/bin/env python3
"""Run challenger vs baseline task rollouts from real task artifacts."""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent)); _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent.parent / "hooks"))

import argparse
import json
from pathlib import Path

from rollout import cmd_run as rollout_cmd_run
from lib_core import load_json, now_iso
from lib_registry import ensure_learned_registry


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


def synthesize_task_rollout(root: Path, task_id: str, entry: dict, baseline_commands: list[list[str]], candidate_commands: list[list[str]]) -> Path:
    task_dir = root / ".dynos" / task_id
    graph = load_json(task_dir / "execution-graph.json")
    files = sorted(
        {
            file_path
            for segment in graph.get("segments", [])
            if isinstance(segment, dict)
            for file_path in segment.get("files_expected", [])
            if isinstance(file_path, str)
        }
    )
    fixture = {
        "fixture_id": f"rollout-{entry.get('item_kind', 'agent')}-{entry.get('agent_name')}-{task_id}",
        "item_kind": entry.get("item_kind", "agent"),
        "target_name": entry.get("agent_name"),
        "role": entry.get("role"),
        "task_type": entry.get("task_type"),
        "source_tasks": [task_id],
        "synthesis": {
            "strategy": "task_snapshot_rollout",
            "task_id": task_id,
            "generated_at": now_iso(),
        },
        "cases": [
            {
                "case_id": task_id,
                "category": "task-rollout",
                "source_task_id": task_id,
                "sandbox": {
                    "copy_repo_paths": files,
                },
                "baseline": {"commands": baseline_commands},
                "candidate": {"commands": candidate_commands},
            }
        ],
    }
    output = root / "benchmarks" / "generated" / f"{fixture['fixture_id']}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(fixture, indent=2) + "\n")
    return output


def parse_commands(raw: list[str]) -> list[list[str]]:
    commands: list[list[str]] = []
    for chunk in raw:
        parsed = json.loads(chunk)
        if not isinstance(parsed, list) or not parsed or not all(isinstance(item, str) for item in parsed):
            raise SystemExit("command arguments must be JSON arrays of strings")
        commands.append(parsed)
    return commands


def cmd_challenge(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    entry = _find_entry(root, args.agent_name, args.role, args.task_type, args.item_kind)
    fixture_path = synthesize_task_rollout(
        root,
        args.task_id,
        entry,
        parse_commands(args.baseline_command),
        parse_commands(args.candidate_command),
    )
    rollout_args = argparse.Namespace(
        fixture_json=str(fixture_path),
        root=str(root),
        update_registry=args.update_registry,
    )
    return rollout_cmd_run(rollout_args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_id")
    parser.add_argument("agent_name")
    parser.add_argument("role")
    parser.add_argument("task_type")
    parser.add_argument("--item-kind", choices=["agent", "skill"], default="agent")
    parser.add_argument("--baseline-command", action="append", required=True)
    parser.add_argument("--candidate-command", action="append", required=True)
    parser.add_argument("--root", default=".")
    parser.add_argument("--update-registry", action="store_true")
    parser.set_defaults(func=cmd_challenge)
    return parser


if __name__ == "__main__":
    from cli_base import cli_main
    raise SystemExit(cli_main(build_parser))
