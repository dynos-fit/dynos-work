#!/usr/bin/env python3
"""Learned-agent registry management for dynos-work."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from dynoslib import (
    collect_retrospectives,
    ensure_learned_registry,
    register_learned_agent,
    now_iso,
    _persistent_project_dir,
)


def cmd_auto(args: argparse.Namespace) -> int:
    """Deterministic post-task evolve: check generation gates and register if warranted."""
    root = Path(args.root).resolve()
    retrospectives = collect_retrospectives(root)
    registry = ensure_learned_registry(root)
    min_tasks = int(args.min_tasks)
    result: dict = {"checked_at": now_iso(), "generated": [], "skipped_reasons": []}

    if len(retrospectives) < min_tasks:
        result["skipped_reasons"].append(f"insufficient retrospectives: {len(retrospectives)} < {min_tasks}")
        print(json.dumps(result, indent=2))
        return 0

    # Find (role, task_type) combos from retrospectives that have enough data
    role_type_counts: dict[tuple[str, str], int] = {}
    for retro in retrospectives:
        for role in retro.get("executor_repair_frequency", {}):
            task_type = retro.get("task_type", "")
            if isinstance(role, str) and isinstance(task_type, str) and role and task_type:
                role_type_counts[(role, task_type)] = role_type_counts.get((role, task_type), 0) + 1

    # Check existing registry to avoid duplicates
    existing = {
        (a.get("role"), a.get("task_type"))
        for a in registry.get("agents", [])
    }

    for (role, task_type), count in role_type_counts.items():
        if count < 3:
            continue
        if (role, task_type) in existing:
            continue
        # Generate a simple learned agent placeholder
        agent_name = f"auto-{role.replace('-executor', '')}-{task_type}"
        agent_dir = _persistent_project_dir(root) / "learned-agents" / "executors"
        agent_dir.mkdir(parents=True, exist_ok=True)
        agent_path = agent_dir / f"{agent_name}.md"
        if not agent_path.exists():
            agent_path.write_text(
                f"# {agent_name}\n\n"
                f"Auto-generated learned executor for {role} on {task_type} tasks.\n"
                f"Based on {count} retrospective observations.\n"
                f"Generated: {now_iso()}\n"
            )
        rel_path = str(agent_path.relative_to(root))
        latest_task = retrospectives[-1].get("task_id", "unknown")
        register_learned_agent(
            root,
            agent_name=agent_name,
            role=role,
            task_type=task_type,
            path=rel_path,
            generated_from=latest_task,
        )
        result["generated"].append({"agent_name": agent_name, "role": role, "task_type": task_type})

    print(json.dumps(result, indent=2))
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    registry = ensure_learned_registry(Path(args.root).resolve())
    print(json.dumps(registry, indent=2))
    return 0


def cmd_register(args: argparse.Namespace) -> int:
    registry = register_learned_agent(
        Path(args.root).resolve(),
        agent_name=args.agent_name,
        role=args.role,
        task_type=args.task_type,
        path=args.path,
        generated_from=args.generated_from,
        item_kind=args.item_kind,
    )
    print(json.dumps(registry, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    auto_parser = subparsers.add_parser("auto", help="Deterministic post-task evolve check")
    auto_parser.add_argument("--root", default=".")
    auto_parser.add_argument("--min-tasks", type=int, default=5)
    auto_parser.set_defaults(func=cmd_auto)

    init_parser = subparsers.add_parser("init-registry", help="Create learned-agent registry if missing")
    init_parser.add_argument("--root", default=".")
    init_parser.set_defaults(func=cmd_init)

    register_parser = subparsers.add_parser("register-agent", help="Register a learned agent in shadow mode")
    register_parser.add_argument("agent_name")
    register_parser.add_argument("role")
    register_parser.add_argument("task_type")
    register_parser.add_argument("path")
    register_parser.add_argument("generated_from")
    register_parser.add_argument("--item-kind", choices=["agent", "skill"], default="agent")
    register_parser.add_argument("--root", default=".")
    register_parser.set_defaults(func=cmd_register)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
