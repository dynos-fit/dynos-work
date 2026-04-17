#!/usr/bin/env python3
"""Learned-agent generator for dynos-work.

Discovers (role, task_type) slots with sufficient observations in
retrospectives and generates learned agent .md files in shadow mode.
Shadow agents don't route — they wait for benchmarking and promotion.
"""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent)); _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "hooks"))

import argparse
import json
from pathlib import Path

from lib_core import collect_retrospectives, load_json, now_iso, _persistent_project_dir
from lib_log import log_event
from lib_registry import ensure_learned_registry, register_learned_agent


# Fallback imperative instructions for each known finding category.
# Used when no specific prevention rules exist for a category.
CATEGORY_INSTRUCTIONS: dict[str, str] = {
    "sec": "ALWAYS validate authentication and authorization before accessing protected resources.",
    "cq": "BEFORE submitting, verify that all code meets quality standards including lint, type checks, and consistent style.",
    "dc": "ALWAYS ensure documentation is accurate and matches the implemented behavior.",
    "perf": "DO NOT introduce O(n^2) or worse algorithms without explicit justification. ALWAYS consider performance impact of loops and queries.",
    "comp": "ALWAYS verify backward compatibility before modifying public interfaces or data formats.",
    "ui": "BEFORE submitting, verify that UI changes render correctly across supported viewports and do not break existing layouts.",
    "db": "ALWAYS use parameterized queries. DO NOT modify schema without migration scripts.",
    "test": "ALWAYS include tests for new behavior and verify existing tests pass before submitting.",
    "process": "ALWAYS follow the established workflow steps. DO NOT skip required review or validation stages.",
    "unknown": "BEFORE submitting, review changes for correctness and verify no regressions are introduced.",
}


def _matching_retrospectives(
    retrospectives: list[dict], role: str, task_type: str, root: Path
) -> list[dict]:
    """Return retrospectives where *role* participated per the execution graph and task_type matches."""
    matched: list[dict] = []
    for retro in retrospectives:
        if retro.get("task_type") != task_type:
            continue
        if "_path" not in retro:
            continue
        try:
            task_dir = Path(retro["_path"]).parent
            graph_path = task_dir / "execution-graph.json"
            graph = json.loads(graph_path.read_text())
            executors = {
                seg.get("executor")
                for seg in graph.get("segments", [])
                if seg.get("executor")
            }
            if role in executors:
                matched.append(retro)
        except (json.JSONDecodeError, OSError, KeyError):
            continue
    return matched


def _aggregate_finding_categories(retros: list[dict]) -> dict[str, int]:
    """Sum findings_by_category across retrospectives."""
    totals: dict[str, int] = {}
    for retro in retros:
        for cat, count in retro.get("findings_by_category", {}).items():
            totals[cat] = totals.get(cat, 0) + count
    return dict(sorted(totals.items(), key=lambda kv: kv[1], reverse=True))


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
    """Build structured markdown content for a learned agent file.

    Emits imperative failure-prevention instructions derived from finding
    patterns and prevention rules, not descriptive telemetry.
    """
    generation_date = now_iso()
    observation_count = len(matched_retros)

    finding_categories = _aggregate_finding_categories(matched_retros)

    prevention_rules = _load_prevention_rules(root)
    # Filter rules: category must match AND executor must be this role or "all"
    relevant_rules: dict[str, list[dict]] = {}
    for r in prevention_rules:
        cat = r.get("category")
        if cat not in finding_categories:
            continue
        rule_executor = r.get("executor", "all")
        if rule_executor not in (role, "all"):
            continue
        relevant_rules.setdefault(cat, []).append(r)

    lines: list[str] = []

    # Heading
    lines.append(f"# {agent_name}")
    lines.append("")

    # Context metadata section (brief)
    lines.append("## Context")
    lines.append("")
    lines.append(f"- **Role**: {role}")
    lines.append(f"- **Task type**: {task_type}")
    lines.append(f"- **Observations**: {observation_count}")
    lines.append(f"- **Generated**: {generation_date}")
    lines.append(f"- **Generated from**: {generated_from}")
    lines.append("")

    # Failure Prevention Rules section (imperative instructions)
    lines.append("## Failure Prevention Rules")
    lines.append("")

    if finding_categories:
        for cat in finding_categories:
            cat_rules = relevant_rules.get(cat, [])
            if cat_rules:
                for rule in cat_rules:
                    lines.append(f"- [{cat}] {rule.get('rule', '')}")
            else:
                # Fallback to category-level imperative instruction
                fallback = CATEGORY_INSTRUCTIONS.get(cat, CATEGORY_INSTRUCTIONS["unknown"])
                lines.append(f"- [{cat}] {fallback}")
    else:
        lines.append("No failure patterns observed yet. Follow standard best practices.")
    lines.append("")

    return "\n".join(lines)


def cmd_auto(args: argparse.Namespace) -> int:
    """Deterministic post-task evolve: check generation gates and register if warranted."""
    root = Path(args.root).resolve()
    retrospectives = collect_retrospectives(root)
    registry = ensure_learned_registry(root)
    min_tasks = int(args.min_tasks)
    result: dict = {"checked_at": now_iso(), "generated": [], "skipped_reasons": [], "steps": []}

    # Step 1: Gate check
    if len(retrospectives) < min_tasks:
        result["skipped_reasons"].append(f"insufficient retrospectives: {len(retrospectives)} < {min_tasks}")
        result["steps"].append({"step": "gate_check", "passed": False, "reason": f"{len(retrospectives)} < {min_tasks}"})
        log_event(root, "agent_generator_step", step="gate_check", passed=False, retrospective_count=len(retrospectives))
        print(json.dumps(result, indent=2))
        return 0
    result["steps"].append({"step": "gate_check", "passed": True, "retrospective_count": len(retrospectives)})
    log_event(root, "agent_generator_step", step="gate_check", passed=True, retrospective_count=len(retrospectives))

    # Step 2: Discover uncovered (role, task_type) slots
    # Scan execution graphs for ALL executors that ran, not just those with repairs.
    role_type_counts: dict[tuple[str, str], int] = {}
    dynos_dir = root / ".dynos"
    for task_dir in sorted(dynos_dir.iterdir()) if dynos_dir.exists() else []:
        if not task_dir.name.startswith("task-"):
            continue
        graph_path = task_dir / "execution-graph.json"
        retro_path = task_dir / "task-retrospective.json"
        if not (graph_path.exists() and retro_path.exists()):
            continue
        try:
            retro = load_json(retro_path)
            graph = load_json(graph_path)
            task_type = retro.get("task_type", "")
            if not task_type:
                continue
            for seg in graph.get("segments", []):
                role = seg.get("executor", "")
                if role and isinstance(role, str):
                    role_type_counts[(role, task_type)] = role_type_counts.get((role, task_type), 0) + 1
        except (json.JSONDecodeError, OSError):
            continue

    existing = {
        (a.get("role"), a.get("task_type"))
        for a in registry.get("agents", [])
    }

    candidates = [(r, t, c) for (r, t), c in role_type_counts.items() if c >= 3 and (r, t) not in existing]
    result["steps"].append({"step": "discover_slots", "total_slots": len(role_type_counts), "uncovered": len(candidates), "existing": len(existing)})
    log_event(root, "agent_generator_step", step="discover_slots", total_slots=len(role_type_counts), uncovered=len(candidates), existing=len(existing))

    # Step 3: Generate agents for uncovered slots
    for role, task_type, count in candidates:
        matched_retros = _matching_retrospectives(retrospectives, role, task_type, root)

        # Skip slot if no matching retrospectives (no observational data)
        if not matched_retros:
            log_event(root, "agent_generator_step", step="skip_no_match", role=role, task_type=task_type)
            result["skipped_reasons"].append(f"no matching retrospectives for {role}/{task_type}")
            continue

        agent_name = f"auto-{role.replace('-executor', '')}-{task_type}"
        agent_dir = _persistent_project_dir(root) / "learned-agents" / "executors"
        agent_dir.mkdir(parents=True, exist_ok=True)
        agent_path = agent_dir / f"{agent_name}.md"

        # Provenance: use latest matched retro, not global latest
        latest_task = matched_retros[-1].get("task_id", "unknown")

        if not agent_path.exists():
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
        log_event(root, "agent_generator_step", step="agent_generated", agent_name=agent_name, role=role, task_type=task_type, sample_count=count)

    result["steps"].append({"step": "generate", "generated_count": len(result["generated"])})
    log_event(root, "agent_generator_auto", generated_count=len(result.get("generated", [])), skipped_reasons=result.get("skipped_reasons", []), retrospective_count=len(retrospectives), steps=result["steps"])
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


if __name__ == "__main__":
    from cli_base import cli_main
    raise SystemExit(cli_main(build_parser))
