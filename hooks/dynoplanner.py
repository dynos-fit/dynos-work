#!/usr/bin/env python3
"""Deterministic planning decisions for dynos-work.

Three CLI subcommands:
  start-plan    — resolve deterministic start/spec/planning decisions for a task
  planning-mode — determine standard vs. hierarchical planning mode
  task-policy   — generate a complete policy-packet.json for a task
"""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import argparse
import json
from pathlib import Path

from dynoslib import (
    _persistent_project_dir,
    collect_retrospectives,
    load_json,
    now_iso,
    write_json,
)
from dynorouter import (
    build_audit_plan,
    load_prevention_rules,
    resolve_model,
    resolve_route,
    resolve_skip,
)


# ---------------------------------------------------------------------------
# Trajectory similarity (consistent with dynopostmortem._find_similar_tasks)
# ---------------------------------------------------------------------------

def _find_similar_trajectories(
    task_type: str,
    domains: list[str],
    risk_level: str,
    root: Path,
    limit: int = 3,
) -> list[dict]:
    """Find similar past tasks from retrospectives.

    Matching uses task_type + overlapping domains + risk_level, consistent
    with dynopostmortem._find_similar_tasks().
    """
    retros = collect_retrospectives(root)
    domains_str = ",".join(sorted(domains))
    similar: list[dict] = []
    for past in retros:
        score = 0.0
        if past.get("task_type") == task_type:
            score += 0.5
        past_domains = past.get("task_domains", "")
        if past_domains == domains_str:
            score += 0.3
        elif isinstance(past_domains, str):
            past_set = {d.strip() for d in past_domains.split(",") if d.strip()}
            overlap = past_set & set(domains)
            if overlap:
                score += 0.15 * min(len(overlap), 2)
        if past.get("task_risk_level") == risk_level:
            score += 0.2
        if score > 0:
            similar.append({
                "task_id": past.get("task_id", ""),
                "similarity": round(score, 2),
                "quality_score": past.get("quality_score", 0),
                "task_outcome": past.get("task_outcome", ""),
                "repair_cycle_count": past.get("repair_cycle_count", 0),
            })
    similar.sort(key=lambda x: (-x["similarity"], x.get("task_id", "")))
    return similar[:limit]


def _trajectory_adjustments(similar: list[dict]) -> list[str]:
    """Derive trajectory-conditioned policy hints from similar tasks."""
    hints: list[str] = []
    for s in similar:
        quality = float(s.get("quality_score", 0) or 0)
        repairs = int(s.get("repair_cycle_count", 0) or 0)
        outcome = s.get("task_outcome", "")
        tid = s.get("task_id", "unknown")
        if quality < 0.5:
            hints.append(f"Similar task {tid} had low quality ({quality:.2f}); increase scrutiny")
        if repairs >= 2:
            hints.append(f"Similar task {tid} needed {repairs} repair cycles; pre-validate carefully")
        if outcome == "FAILED":
            hints.append(f"Similar task {tid} failed; review root cause before proceeding")
    return hints


# ---------------------------------------------------------------------------
# Planning mode resolution
# ---------------------------------------------------------------------------

def _resolve_planning_mode(
    risk_level: str,
    ac_count: int = 0,
) -> dict:
    """Determine standard vs. hierarchical planning.

    Hierarchical if risk_level in (high, critical) or ac_count > 10.
    """
    hierarchical = risk_level in ("high", "critical") or ac_count > 10
    mode = "hierarchical" if hierarchical else "standard"
    reasons = []
    if risk_level in ("high", "critical"):
        reasons.append(f"risk_level={risk_level}")
    if ac_count > 10:
        reasons.append(f"ac_count={ac_count}")
    if not reasons:
        reasons.append("default standard planning")
    return {
        "mode": mode,
        "reason": "; ".join(reasons),
    }


# ---------------------------------------------------------------------------
# Policy packet generation
# ---------------------------------------------------------------------------

_KEY_ROLES = [
    "planner",
    "spec-writer",
    "backend-executor",
    "ui-executor",
    "security-auditor",
    "code-quality-auditor",
    "spec-completion-auditor",
]

_AUDITORS = [
    "spec-completion-auditor",
    "security-auditor",
    "code-quality-auditor",
    "dead-code-auditor",
]


def _build_policy_packet(root: Path, task_id: str) -> dict:
    """Build the complete policy packet for a task."""
    task_dir = root / ".dynos" / task_id

    # Load manifest
    manifest_path = task_dir / "manifest.json"
    try:
        manifest = load_json(manifest_path)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        manifest = {}

    classification = manifest.get("classification", {})
    task_type = classification.get("type", "feature")
    domains_raw = classification.get("domains", [])
    domains = domains_raw if isinstance(domains_raw, list) else [str(domains_raw)]
    risk_level = classification.get("risk_level", "medium")
    fast_track = bool(manifest.get("fast_track", False))

    # Load execution graph for AC count estimation
    graph_path = task_dir / "execution-graph.json"
    ac_count = 0
    segments = []
    try:
        graph = load_json(graph_path)
        segments = graph.get("segments", [])
        seen_criteria: set[int] = set()
        for seg in segments:
            for cid in seg.get("criteria_ids", []):
                seen_criteria.add(cid)
        ac_count = len(seen_criteria)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    # Planning mode
    planning = _resolve_planning_mode(risk_level, ac_count)

    # Trajectory similarity
    similar = _find_similar_trajectories(task_type, domains, risk_level, root)
    hints = _trajectory_adjustments(similar)

    # Model decisions
    models: dict[str, dict] = {}
    for role in _KEY_ROLES:
        decision = resolve_model(root, role, task_type)
        models[role] = {"model": decision.get("model"), "source": decision.get("source", "default")}

    # Skip decisions
    skip_decisions: dict[str, dict] = {}
    for auditor in _AUDITORS:
        decision = resolve_skip(root, auditor, task_type)
        skip_decisions[auditor] = {
            "skip": decision.get("skip", False),
            "reason": decision.get("reason", ""),
            "source": "learned_history" if decision.get("streak", 0) > 0 else "default",
        }

    # Route decisions
    route_decisions: dict[str, dict] = {}
    all_roles_for_routing = list(set(
        _KEY_ROLES + [seg.get("executor", "") for seg in segments if seg.get("executor")]
    ))
    for role in all_roles_for_routing:
        if not role:
            continue
        key = f"{role}:{task_type}"
        decision = resolve_route(root, role, task_type)
        route_decisions[key] = {
            "mode": decision.get("mode", "generic"),
            "agent_path": decision.get("agent_path"),
            "source": decision.get("source", "default"),
        }

    # Audit plan
    audit_plan = build_audit_plan(root, task_type, domains, fast_track=fast_track)

    # Prevention rules
    prevention_rules = load_prevention_rules(root)

    # Computed modes
    founder_mode = risk_level in ("high", "critical") and len(similar) == 0
    spec_mode = "concise" if fast_track else "full"
    discovery_mode = "skip" if fast_track else "full"
    execution_mode = "parallel" if len(segments) > 1 else "sequential"

    # Validation requirements
    validation_requirements = {
        "spec_review": not fast_track,
        "plan_audit": not fast_track,
        "checkpoint_audit": True,
        "security_audit": True,
    }

    # Dreaming and curiosity (AC 18)
    dreaming = len(similar) == 0 and risk_level in ("high", "critical")
    curiosity_targets: list[str] = []
    if similar:
        for s in similar:
            outcome = s.get("task_outcome", "")
            if outcome == "FAILED":
                curiosity_targets.append(f"Investigate failure pattern from {s.get('task_id', 'unknown')}")

    packet = {
        "task_id": task_id,
        "generated_at": now_iso(),
        "classification": classification,
        "fast_track": fast_track,
        "planning_mode": planning["mode"],
        "planning_mode_reason": planning["reason"],
        "discovery_mode": discovery_mode,
        "founder_mode": founder_mode,
        "spec_mode": spec_mode,
        "execution_mode": execution_mode,
        "validation_requirements": validation_requirements,
        "models": models,
        "skip_decisions": skip_decisions,
        "route_decisions": route_decisions,
        "audit_plan": audit_plan,
        "prevention_rules": prevention_rules,
        "trajectory_hints": hints,
        "dreaming": dreaming,
        "curiosity_targets": curiosity_targets,
    }

    return packet


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_start_plan(args: argparse.Namespace) -> int:
    """Resolve deterministic start/spec/planning decisions."""
    root = Path(args.root).resolve()
    task_type = str(args.task_type).strip()
    domains = [d.strip() for d in args.domains.split(",") if d.strip()]
    risk_level = str(args.risk_level).strip()

    if not task_type:
        print(json.dumps({"error": "task-type is required"}))
        return 1
    if not risk_level:
        print(json.dumps({"error": "risk-level is required"}))
        return 1

    # Planning mode
    planning = _resolve_planning_mode(risk_level)

    # Planner model
    planner_model_decision = resolve_model(root, "planner", task_type)
    planner_model = planner_model_decision.get("model")

    # Discovery skip: true if well-scoped (low risk, single domain, not full-stack)
    discovery_skip = (
        risk_level in ("low", "medium")
        and len(domains) <= 1
        and task_type not in ("full-stack", "migration")
    )

    # Trajectory adjustments
    similar = _find_similar_trajectories(task_type, domains, risk_level, root)
    adjustments = _trajectory_adjustments(similar)

    result = {
        "planning_mode": planning["mode"],
        "planning_mode_reason": planning["reason"],
        "planner_model": planner_model,
        "planner_model_source": planner_model_decision.get("source", "default"),
        "discovery_skip": discovery_skip,
        "trajectory_adjustments": adjustments,
        "similar_tasks_found": len(similar),
    }

    print(json.dumps(result, indent=2))
    return 0


def cmd_planning_mode(args: argparse.Namespace) -> int:
    """Determine standard vs. hierarchical planning mode."""
    risk_level = str(args.risk_level).strip()
    ac_count = int(args.ac_count) if args.ac_count else 0

    if not risk_level:
        print(json.dumps({"error": "risk-level is required"}))
        return 1

    result = _resolve_planning_mode(risk_level, ac_count)
    print(json.dumps(result, indent=2))
    return 0


def cmd_task_policy(args: argparse.Namespace) -> int:
    """Generate complete policy-packet.json for a task."""
    root = Path(args.root).resolve()
    task_id = str(args.task_id).strip()

    if not task_id:
        print(json.dumps({"error": "task-id is required"}))
        return 1

    task_dir = root / ".dynos" / task_id
    if not task_dir.is_dir():
        print(json.dumps({"error": f"task directory not found: {task_dir}"}))
        return 1

    packet = _build_policy_packet(root, task_id)

    # Write to disk
    packet_path = task_dir / "policy-packet.json"
    write_json(packet_path, packet)

    # Also print to stdout
    print(json.dumps(packet, indent=2))
    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deterministic planning decisions for dynos-work.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # start-plan
    sp = subparsers.add_parser("start-plan", help="Resolve start/spec/planning decisions")
    sp.add_argument("--root", default=".")
    sp.add_argument("--task-type", required=True, dest="task_type")
    sp.add_argument("--domains", default="")
    sp.add_argument("--risk-level", required=True, dest="risk_level")
    sp.set_defaults(func=cmd_start_plan)

    # planning-mode
    pm = subparsers.add_parser("planning-mode", help="Determine planning mode")
    pm.add_argument("--root", default=".")
    pm.add_argument("--task-type", default="feature", dest="task_type")
    pm.add_argument("--risk-level", required=True, dest="risk_level")
    pm.add_argument("--ac-count", default="0", dest="ac_count")
    pm.set_defaults(func=cmd_planning_mode)

    # task-policy
    tp = subparsers.add_parser("task-policy", help="Generate policy-packet.json")
    tp.add_argument("--root", default=".")
    tp.add_argument("--task-id", required=True, dest="task_id")
    tp.set_defaults(func=cmd_task_policy)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
