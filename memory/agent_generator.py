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

from lib_core import collect_retrospectives, count_unverified_retrospectives, load_json, now_iso, _persistent_project_dir
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


def _retro_participants(retro: dict) -> set[str]:
    """Roles that took part in the task behind *retro*.

    Read primarily from the retrospective's own ``model_used_by_agent``
    mapping (keyed by role). That mapping lives inside the flushed
    retrospective JSON, so it SURVIVES worktree removal. For completeness /
    backward-compat it is unioned with the executors declared in a sibling
    ``execution-graph.json`` when that file is present (the in-worktree case).

    Reading the execution graph ALONE was a bug: persistent retrospectives
    (the post-worktree-removal case the DONE-edge flush exists for) have no
    graph sibling — their ``_path`` points at ``.../retrospectives/{task}.json``
    — so participation was never found for them.
    """
    participants: set[str] = set()
    mua = retro.get("model_used_by_agent")
    if isinstance(mua, dict):
        participants.update(k for k in mua if isinstance(k, str) and k)
    path = retro.get("_path")
    if path:
        try:
            graph_path = Path(path).parent / "execution-graph.json"
            if graph_path.exists():
                graph = json.loads(graph_path.read_text())
                participants.update(
                    seg.get("executor")
                    for seg in graph.get("segments", [])
                    if isinstance(seg, dict) and isinstance(seg.get("executor"), str) and seg.get("executor")
                )
        except (json.JSONDecodeError, OSError, KeyError, AttributeError):
            pass
    return participants


def _matching_retrospectives(
    retrospectives: list[dict], role: str, task_type: str, root: Path
) -> list[dict]:
    """Return retrospectives where *role* participated and task_type matches
    (participation per ``_retro_participants``)."""
    return [
        retro for retro in retrospectives
        if retro.get("task_type") == task_type and role in _retro_participants(retro)
    ]


def _discover_executor_slots(retrospectives: list[dict]) -> dict[tuple[str, str], int]:
    """Count, per (executor role, task_type), the TASKS in which that executor
    ran, from the collected retrospectives.

    Slots used to be discovered by scanning ``root/.dynos/task-*/
    execution-graph.json``. Those exist only in the checkout that ran the task,
    so on main (after worktree removal) no slot was ever found. It also counted
    segments rather than tasks, so one task with three backend segments met the
    three-sample threshold on its own.
    """
    counts: dict[tuple[str, str], int] = {}
    for retro in retrospectives:
        task_type = retro.get("task_type")
        if not isinstance(task_type, str) or not task_type:
            continue
        for role in _retro_participants(retro):
            if role.endswith("-executor"):
                counts[(role, task_type)] = counts.get((role, task_type), 0) + 1
    return counts


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
    # Filter rules: category must match AND executor must be this role or "all".
    # `auditor-only` rules (aggregate category-trend signals from
    # memory/postmortem_improve.py) are excluded from every executor agent
    # file — they are audit-review signals, not per-line code constraints.
    relevant_rules: dict[str, list[dict]] = {}
    for r in prevention_rules:
        cat = r.get("category")
        if cat not in finding_categories:
            continue
        rule_executor = r.get("executor", "all")
        if rule_executor == "auditor-only":
            continue
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
        reason = f"insufficient retrospectives: {len(retrospectives)} < {min_tasks}"
        unverified = count_unverified_retrospectives(root)
        if unverified:
            reason += (
                f" ({unverified} persistent retrospectives excluded as unverified;"
                " see ctl.py check-retro-integrity / backfill-retro-flush-ledger)"
            )
            result["unverified_retrospectives"] = unverified
        result["skipped_reasons"].append(reason)
        result["steps"].append({"step": "gate_check", "passed": False, "reason": f"{len(retrospectives)} < {min_tasks}"})
        log_event(root, "agent_generator_step", step="gate_check", passed=False, retrospective_count=len(retrospectives))
        print(json.dumps(result, indent=2))
        return 0
    result["steps"].append({"step": "gate_check", "passed": True, "retrospective_count": len(retrospectives)})
    log_event(root, "agent_generator_step", step="gate_check", passed=True, retrospective_count=len(retrospectives))

    # Step 2: Discover uncovered (role, task_type) slots from the retrospectives
    # themselves (they survive worktree removal), counting tasks per slot.
    role_type_counts = _discover_executor_slots(retrospectives)

    existing = {
        (a.get("role"), a.get("task_type"))
        for a in registry.get("agents", [])
    }

    candidates = [(r, t, c) for (r, t), c in role_type_counts.items() if c >= 3]
    new_count = sum(1 for (r, t, _) in candidates if (r, t) not in existing)
    refresh_count = len(candidates) - new_count
    result["steps"].append({"step": "discover_slots", "total_slots": len(role_type_counts), "new": new_count, "refresh": refresh_count, "existing": len(existing)})
    log_event(root, "agent_generator_step", step="discover_slots", total_slots=len(role_type_counts), new=new_count, refresh=refresh_count, existing=len(existing))

    result.setdefault("refreshed", [])

    # Step 3: Generate (new) or refresh (existing) agents for all role/type pairs with enough samples.
    # Refresh rewrites the .md file from current retros + prevention rules but does NOT touch the
    # registry entry — preserving accumulated mode (alongside/replace), benchmark_summary, and
    # last_evaluation. The .md content is a derived artifact; the registry holds promotion state.
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

        content = _build_agent_content(
            agent_name, role, task_type, matched_retros, latest_task, root
        )
        agent_path.write_text(content)

        is_new = (role, task_type) not in existing
        if is_new:
            register_learned_agent(
                root,
                agent_name=agent_name,
                role=role,
                task_type=task_type,
                path=str(agent_path),
                generated_from=latest_task,
            )
            result["generated"].append({"agent_name": agent_name, "role": role, "task_type": task_type})
            log_event(root, "agent_generator_step", step="agent_generated", agent_name=agent_name, role=role, task_type=task_type, sample_count=count)
        else:
            result["refreshed"].append({"agent_name": agent_name, "role": role, "task_type": task_type, "sample_count": count})
            log_event(root, "agent_generator_step", step="agent_refreshed", agent_name=agent_name, role=role, task_type=task_type, sample_count=count)

    result["steps"].append({"step": "generate", "generated_count": len(result["generated"]), "refreshed_count": len(result["refreshed"])})
    log_event(root, "agent_generator_auto", generated_count=len(result.get("generated", [])), refreshed_count=len(result.get("refreshed", [])), skipped_reasons=result.get("skipped_reasons", []), retrospective_count=len(retrospectives), steps=result["steps"])
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
