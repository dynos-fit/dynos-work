#!/usr/bin/env python3
"""Learned-agent registry management for dynos-work."""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

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


def _matching_retrospectives(
    retrospectives: list[dict], role: str, task_type: str
) -> list[dict]:
    """Return retrospectives where *role* appears in executor_repair_frequency and task_type matches."""
    matched: list[dict] = []
    for retro in retrospectives:
        if retro.get("task_type") != task_type:
            continue
        if role in retro.get("executor_repair_frequency", {}):
            matched.append(retro)
    return matched


def _aggregate_finding_categories(retros: list[dict]) -> dict[str, int]:
    """Sum findings_by_category across retrospectives."""
    totals: dict[str, int] = {}
    for retro in retros:
        for cat, count in retro.get("findings_by_category", {}).items():
            totals[cat] = totals.get(cat, 0) + count
    return dict(sorted(totals.items(), key=lambda kv: kv[1], reverse=True))


def _aggregate_repair_frequency(retros: list[dict], role: str) -> dict[str, int]:
    """Compute total and per-task repair counts for *role*."""
    total_repairs = 0
    tasks_with_repairs = 0
    for retro in retros:
        repairs = retro.get("executor_repair_frequency", {}).get(role, 0)
        total_repairs += repairs
        if repairs > 0:
            tasks_with_repairs += 1
    return {"total_repairs": total_repairs, "tasks_with_repairs": tasks_with_repairs}


def _load_prevention_rules(root: Path) -> list[dict]:
    """Load prevention rules from persistent project storage, returning [] on failure."""
    rules_path = _persistent_project_dir(root) / "prevention-rules.json"
    if not rules_path.exists():
        return []
    try:
        data = json.loads(rules_path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    return data.get("rules", []) if isinstance(data, dict) else []


def _build_agent_content(
    agent_name: str,
    role: str,
    task_type: str,
    matched_retros: list[dict],
    generated_from: str,
    root: Path,
) -> str:
    """Build structured markdown content for a learned agent file."""
    generation_date = now_iso()
    observation_count = len(matched_retros)

    finding_categories = _aggregate_finding_categories(matched_retros)
    repair_stats = _aggregate_repair_frequency(matched_retros, role)

    prevention_rules = _load_prevention_rules(root)
    relevant_rules = [
        r for r in prevention_rules
        if r.get("category") in finding_categories
    ]

    lines: list[str] = []

    # Heading
    lines.append(f"# {agent_name}")
    lines.append("")

    # Context section
    lines.append("## Context")
    lines.append("")
    lines.append(f"- **Role**: {role}")
    lines.append(f"- **Task type**: {task_type}")
    lines.append(f"- **Observations**: {observation_count}")
    lines.append(f"- **Generated**: {generation_date}")
    lines.append(f"- **Generated from**: {generated_from}")
    lines.append("")

    # Patterns section
    lines.append("## Patterns")
    lines.append("")

    # Finding categories
    lines.append("### Finding categories")
    lines.append("")
    if finding_categories:
        for cat, count in finding_categories.items():
            lines.append(f"- `{cat}`: {count} findings")
    else:
        lines.append("No finding categories recorded.")
    lines.append("")

    # Repair frequency
    lines.append("### Repair frequency")
    lines.append("")
    lines.append(f"- Total repairs: {repair_stats['total_repairs']}")
    lines.append(f"- Tasks requiring repairs: {repair_stats['tasks_with_repairs']} / {observation_count}")
    if observation_count > 0:
        avg = repair_stats["total_repairs"] / observation_count
        lines.append(f"- Average repairs per task: {avg:.1f}")
    lines.append("")

    # Prevention rules
    lines.append("### Prevention rules")
    lines.append("")
    if relevant_rules:
        for rule in relevant_rules:
            lines.append(f"- [{rule.get('category', '?')}] {rule.get('rule', '')}")
    else:
        lines.append("No prevention rules applicable to observed finding categories.")
    lines.append("")

    return "\n".join(lines)


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
        agent_name = f"auto-{role.replace('-executor', '')}-{task_type}"
        agent_dir = _persistent_project_dir(root) / "learned-agents" / "executors"
        agent_dir.mkdir(parents=True, exist_ok=True)
        agent_path = agent_dir / f"{agent_name}.md"
        latest_task = retrospectives[-1].get("task_id", "unknown")
        if not agent_path.exists():
            matched_retros = _matching_retrospectives(retrospectives, role, task_type)
            content = _build_agent_content(
                agent_name, role, task_type, matched_retros, latest_task, root
            )
            agent_path.write_text(content)
        rel_path = str(agent_path.relative_to(root))
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
