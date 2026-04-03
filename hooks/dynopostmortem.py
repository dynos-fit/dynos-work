#!/usr/bin/env python3
"""Automatic postmortem generator for dynos-work tasks."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from dynoslib import (
    collect_retrospectives,
    load_json,
    now_iso,
    write_json,
    _safe_float,
    COMPOSITE_WEIGHTS,
)


# Token budgets by (risk_level, task_type)
TOKEN_BUDGETS = {
    ("low", "feature"): 30000,
    ("low", "bugfix"): 15000,
    ("low", "refactor"): 20000,
    ("medium", "feature"): 80000,
    ("medium", "bugfix"): 40000,
    ("medium", "refactor"): 50000,
    ("high", "feature"): 200000,
    ("high", "bugfix"): 80000,
    ("high", "refactor"): 100000,
    ("critical", "feature"): 400000,
}
DEFAULT_BUDGET = 60000


def postmortems_dir(root: Path) -> Path:
    return root / ".dynos" / "postmortems"


def _expected_budget(risk_level: str, task_type: str) -> int:
    return TOKEN_BUDGETS.get((risk_level, task_type), DEFAULT_BUDGET)


def _read_execution_log(task_dir: Path) -> str:
    path = task_dir / "execution-log.md"
    if path.exists():
        return path.read_text()
    return ""


def _read_audit_reports(task_dir: Path) -> list[dict]:
    reports = []
    audit_dir = task_dir / "audit-reports"
    if not audit_dir.exists():
        return reports
    for path in sorted(audit_dir.glob("*.json")):
        try:
            reports.append(load_json(path))
        except (json.JSONDecodeError, FileNotFoundError, OSError):
            continue
    return reports


def _count_log_pattern(log_text: str, pattern: str) -> int:
    return sum(1 for line in log_text.splitlines() if pattern in line)


def _find_similar_tasks(retro: dict, all_retros: list[dict], limit: int = 3) -> list[dict]:
    """Find similar past tasks by type + domain match."""
    task_type = retro.get("task_type", "")
    task_domains = retro.get("task_domains", "")
    task_id = retro.get("task_id", "")
    similar = []
    for past in all_retros:
        if past.get("task_id") == task_id:
            continue
        score = 0.0
        if past.get("task_type") == task_type:
            score += 0.5
        if past.get("task_domains") == task_domains:
            score += 0.3
        if past.get("task_risk_level") == retro.get("task_risk_level"):
            score += 0.2
        if score > 0:
            similar.append({"task_id": past.get("task_id"), "similarity": score, **past})
    similar.sort(key=lambda x: (-x["similarity"], x.get("task_id", "")))
    return similar[:limit]


def _detect_anomalies(retro: dict, similar_tasks: list[dict]) -> list[dict]:
    """Detect anomalies by comparing to similar past tasks."""
    anomalies = []
    risk = retro.get("task_risk_level", "medium")
    task_type = retro.get("task_type", "feature")
    total_tokens = _safe_float(retro.get("total_token_usage"), 0)
    budget = _expected_budget(risk, task_type)

    # Token overrun
    if total_tokens > budget * 1.5:
        anomalies.append({
            "type": "token_overrun",
            "severity": "high" if total_tokens > budget * 3 else "medium",
            "detail": f"Used {int(total_tokens)} tokens vs {budget} budget ({total_tokens/budget:.1f}x)",
        })

    # Quality anomaly
    quality = _safe_float(retro.get("quality_score"), 0)
    if quality < 0.5:
        anomalies.append({
            "type": "low_quality",
            "severity": "high",
            "detail": f"quality_score={quality:.2f} is below 0.5 threshold",
        })

    # All-generic routing
    sources = retro.get("agent_source", {})
    if sources and all(v == "generic" for v in sources.values()):
        anomalies.append({
            "type": "all_generic_routing",
            "severity": "low",
            "detail": "All agents used generic routing. No learned specialization applied.",
        })

    # Repair cycles
    repairs = int(retro.get("repair_cycle_count", 0) or 0)
    if repairs >= 2:
        anomalies.append({
            "type": "high_repair_cycles",
            "severity": "medium",
            "detail": f"{repairs} repair cycles indicates spec/execution gaps",
        })

    # Wasted spawns ratio
    spawns = int(retro.get("subagent_spawn_count", 0) or 0)
    wasted = int(retro.get("wasted_spawns", 0) or 0)
    if spawns > 0 and wasted / spawns > 0.5:
        anomalies.append({
            "type": "high_waste_ratio",
            "severity": "medium",
            "detail": f"{wasted}/{spawns} spawns were wasted ({wasted/spawns:.0%})",
        })

    # Compare to similar tasks
    if similar_tasks:
        avg_similar_quality = sum(
            _safe_float(t.get("quality_score"), 0) for t in similar_tasks
        ) / len(similar_tasks)
        if quality < avg_similar_quality - 0.3:
            anomalies.append({
                "type": "quality_regression_vs_similar",
                "severity": "high",
                "detail": f"quality={quality:.2f} vs avg similar={avg_similar_quality:.2f}",
            })

    return anomalies


def _detect_recurring_patterns(all_retros: list[dict], window: int = 5) -> list[dict]:
    """Detect recurring issues across recent tasks."""
    recent = all_retros[-window:]
    patterns = []

    # Recurring all-generic routing
    generic_count = sum(
        1 for r in recent
        if r.get("agent_source") and all(v == "generic" for v in r.get("agent_source", {}).values())
    )
    if generic_count >= 3:
        patterns.append({
            "pattern": "persistent_generic_routing",
            "occurrences": generic_count,
            "window": window,
            "recommendation": "The system has not learned specialized routing after {0} tasks. Consider manually seeding learned agents or lowering the promotion threshold.".format(generic_count),
        })

    # Recurring token overruns
    overrun_count = 0
    for r in recent:
        risk = r.get("task_risk_level", "medium")
        tt = r.get("task_type", "feature")
        tokens = _safe_float(r.get("total_token_usage"), 0)
        if tokens > _expected_budget(risk, tt) * 1.5:
            overrun_count += 1
    if overrun_count >= 2:
        patterns.append({
            "pattern": "recurring_token_overrun",
            "occurrences": overrun_count,
            "window": window,
            "recommendation": "Multiple tasks exceeded token budget. Consider: haiku for low-risk auditors, inline execution for simple segments, tighter spec to reduce planning tokens.",
        })

    # Recurring repair cycles
    repair_count = sum(1 for r in recent if int(r.get("repair_cycle_count", 0) or 0) > 0)
    if repair_count >= 3:
        patterns.append({
            "pattern": "recurring_repair_cycles",
            "occurrences": repair_count,
            "window": window,
            "recommendation": "Most tasks require repair. Spec/planning quality may need improvement. Consider stronger code grounding in spec normalization.",
        })

    # Recurring scoring issues (quality_score exactly 0)
    zero_quality = sum(1 for r in recent if _safe_float(r.get("quality_score"), -1) == 0)
    if zero_quality >= 2:
        patterns.append({
            "pattern": "recurring_zero_quality_scores",
            "occurrences": zero_quality,
            "window": window,
            "recommendation": "Multiple tasks have quality_score=0. The scoring formula may not be computing correctly. Check make_trajectory_entry and inline audit scoring.",
        })

    return patterns


def generate_postmortem(root: Path, task_id: str) -> dict:
    """Generate a structured postmortem for a completed task."""
    task_dir = root / ".dynos" / task_id
    retro_path = task_dir / "task-retrospective.json"

    if not retro_path.exists():
        return {"error": f"no retrospective found for {task_id}"}

    retro = load_json(retro_path)
    log_text = _read_execution_log(task_dir)
    audit_reports = _read_audit_reports(task_dir)
    all_retros = collect_retrospectives(root)
    similar = _find_similar_tasks(retro, all_retros)
    anomalies = _detect_anomalies(retro, similar)
    recurring = _detect_recurring_patterns(all_retros)

    risk = retro.get("task_risk_level", "medium")
    task_type = retro.get("task_type", "feature")
    budget = _expected_budget(risk, task_type)
    total_tokens = _safe_float(retro.get("total_token_usage"), 0)

    # Cost summary
    cost_summary = {
        "total_tokens": int(total_tokens),
        "expected_budget": budget,
        "budget_ratio": round(total_tokens / max(1, budget), 2),
        "subagent_spawns": int(retro.get("subagent_spawn_count", 0) or 0),
        "wasted_spawns": int(retro.get("wasted_spawns", 0) or 0),
    }

    # Quality summary
    findings_by_auditor = retro.get("findings_by_auditor", {})
    total_findings = sum(v for v in findings_by_auditor.values() if isinstance(v, (int, float)))
    quality_summary = {
        "quality_score": _safe_float(retro.get("quality_score"), 0),
        "total_findings": int(total_findings),
        "repair_cycles": int(retro.get("repair_cycle_count", 0) or 0),
        "findings_by_auditor": findings_by_auditor,
    }

    # Policy usage
    policy_usage = {
        "agent_sources": retro.get("agent_source", {}),
        "all_generic": all(v == "generic" for v in retro.get("agent_source", {}).values()) if retro.get("agent_source") else True,
        "fast_track": "fast_track" in str(log_text),
        "inline_execution": "[INLINE]" in log_text,
        "discovery_skipped": "[SKIP] discovery" in log_text,
    }

    # Comparison to similar tasks
    comparison = []
    for s in similar:
        comparison.append({
            "task_id": s.get("task_id"),
            "similarity": s.get("similarity"),
            "quality_score": _safe_float(s.get("quality_score"), 0),
            "total_tokens": int(_safe_float(s.get("total_token_usage"), 0)),
            "repair_cycles": int(s.get("repair_cycle_count", 0) or 0),
        })

    postmortem = {
        "task_id": task_id,
        "generated_at": now_iso(),
        "task_type": task_type,
        "task_domains": retro.get("task_domains", ""),
        "task_risk_level": risk,
        "task_outcome": retro.get("task_outcome", "UNKNOWN"),
        "cost_summary": cost_summary,
        "quality_summary": quality_summary,
        "policy_usage": policy_usage,
        "anomalies": anomalies,
        "similar_tasks": comparison,
        "recurring_patterns": recurring,
        "efficiency_score": _safe_float(retro.get("efficiency_score"), 0),
    }

    return postmortem


def write_postmortem(root: Path, task_id: str) -> dict:
    """Generate and write postmortem files."""
    pm_dir = postmortems_dir(root)
    pm_dir.mkdir(parents=True, exist_ok=True)

    postmortem = generate_postmortem(root, task_id)
    if "error" in postmortem:
        return postmortem

    # Write JSON
    json_path = pm_dir / f"{task_id}.json"
    write_json(json_path, postmortem)

    # Write human-readable MD
    md_path = pm_dir / f"{task_id}.md"
    md = _render_markdown(postmortem)
    md_path.write_text(md)

    return {
        "task_id": task_id,
        "json_path": str(json_path),
        "md_path": str(md_path),
        "anomaly_count": len(postmortem.get("anomalies", [])),
        "recurring_pattern_count": len(postmortem.get("recurring_patterns", [])),
    }


def _render_markdown(pm: dict) -> str:
    lines = [
        f"# Postmortem: {pm['task_id']}",
        f"Generated: {pm['generated_at']}",
        "",
        f"**Type:** {pm['task_type']} | **Risk:** {pm['task_risk_level']} | **Domains:** {pm['task_domains']} | **Outcome:** {pm['task_outcome']}",
        "",
        "## Cost",
        f"- Tokens: {pm['cost_summary']['total_tokens']:,} / {pm['cost_summary']['expected_budget']:,} budget ({pm['cost_summary']['budget_ratio']}x)",
        f"- Spawns: {pm['cost_summary']['subagent_spawns']} total, {pm['cost_summary']['wasted_spawns']} wasted",
        "",
        "## Quality",
        f"- Score: {pm['quality_summary']['quality_score']:.2f}",
        f"- Findings: {pm['quality_summary']['total_findings']} total, {pm['quality_summary']['repair_cycles']} repair cycles",
    ]

    if pm["quality_summary"]["findings_by_auditor"]:
        for auditor, count in pm["quality_summary"]["findings_by_auditor"].items():
            lines.append(f"  - {auditor}: {count}")

    lines.extend([
        "",
        "## Policy Usage",
        f"- All generic routing: {'yes' if pm['policy_usage']['all_generic'] else 'no'}",
        f"- Fast-track: {'yes' if pm['policy_usage']['fast_track'] else 'no'}",
        f"- Inline execution: {'yes' if pm['policy_usage']['inline_execution'] else 'no'}",
        f"- Discovery skipped: {'yes' if pm['policy_usage']['discovery_skipped'] else 'no'}",
    ])

    if pm.get("anomalies"):
        lines.extend(["", "## Anomalies"])
        for a in pm["anomalies"]:
            lines.append(f"- **[{a['severity']}] {a['type']}**: {a['detail']}")

    if pm.get("similar_tasks"):
        lines.extend(["", "## Similar Past Tasks"])
        for s in pm["similar_tasks"]:
            lines.append(f"- {s['task_id']}: quality={s['quality_score']:.2f}, tokens={s['total_tokens']:,}, repairs={s['repair_cycles']}")

    if pm.get("recurring_patterns"):
        lines.extend(["", "## Recurring Patterns (across recent tasks)"])
        for p in pm["recurring_patterns"]:
            lines.append(f"- **{p['pattern']}** ({p['occurrences']}/{p['window']} tasks): {p['recommendation']}")

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Auto-improvement engine
# Produces project-local .dynos/ changes, NOT global plugin file mutations.
# ---------------------------------------------------------------------------

IMPROVEMENT_LOG_PATH = ".dynos/improvements"


def _improvements_dir(root: Path) -> Path:
    return root / ".dynos" / "improvements"


def propose_improvements(root: Path) -> list[dict]:
    """Analyze postmortems and propose project-local improvements.

    Improvements target .dynos/ state only:
    - policy.json tuning
    - learned agent generation
    - prevention rule additions
    - fast-track threshold changes

    Never proposes changes to global plugin source files.
    """
    all_retros = collect_retrospectives(root)
    if len(all_retros) < 3:
        return []  # Not enough data

    recent = all_retros[-5:]
    proposals = []

    # 1. Token budget tuning: if most recent tasks overrun, raise budget expectation in policy
    overrun_tasks = []
    for r in recent:
        risk = r.get("task_risk_level", "medium")
        tt = r.get("task_type", "feature")
        tokens = _safe_float(r.get("total_token_usage"), 0)
        budget = _expected_budget(risk, tt)
        if tokens > budget * 1.5:
            overrun_tasks.append({"task_id": r.get("task_id"), "tokens": int(tokens), "budget": budget})
    if len(overrun_tasks) >= 2:
        proposals.append({
            "id": "imp-token-budget",
            "type": "policy_tuning",
            "target": ".dynos/policy.json",
            "description": "Raise freshness_task_window or add token_budget_multiplier to reduce false overrun alerts",
            "evidence": overrun_tasks,
            "action": "adjust_policy",
            "field": "token_budget_multiplier",
            "suggested_value": 2.0,
        })

    # 2. Fast-track expansion: if low-risk tasks consistently pass without repair, widen fast-track
    low_risk_tasks = [r for r in recent if r.get("task_risk_level") == "low"]
    low_risk_clean = [r for r in low_risk_tasks if int(r.get("repair_cycle_count", 0) or 0) == 0]
    if len(low_risk_clean) >= 3:
        proposals.append({
            "id": "imp-fast-track-expand",
            "type": "policy_tuning",
            "target": ".dynos/policy.json",
            "description": "Low-risk tasks consistently pass without repair. Consider auto-skipping plan audit for fast-track tasks.",
            "evidence": [{"task_id": r.get("task_id")} for r in low_risk_clean],
            "action": "adjust_policy",
            "field": "fast_track_skip_plan_audit",
            "suggested_value": True,
        })

    # 3. Prevention rule generation: if same finding category appears 3+ times, add prevention rule
    category_counts: dict[str, int] = {}
    for r in all_retros:
        for cat, count in r.get("findings_by_category", {}).items():
            if isinstance(cat, str) and isinstance(count, (int, float)):
                category_counts[cat] = category_counts.get(cat, 0) + int(count)
    for cat, count in category_counts.items():
        if count >= 5:
            proposals.append({
                "id": f"imp-prevent-{cat}",
                "type": "prevention_rule",
                "target": ".dynos/dynos_patterns.md",
                "description": f"Finding category '{cat}' has {count} occurrences across all tasks. Add a prevention rule.",
                "action": "add_prevention_rule",
                "category": cat,
                "total_occurrences": count,
            })

    # 4. Model tier recommendation: if quality is consistently high with default model, recommend haiku
    default_high_quality = [
        r for r in recent
        if _safe_float(r.get("quality_score"), 0) >= 0.8
        and r.get("agent_source") and all(v == "generic" for v in r.get("agent_source", {}).values())
    ]
    if len(default_high_quality) >= 3:
        proposals.append({
            "id": "imp-model-haiku",
            "type": "model_recommendation",
            "target": ".dynos/policy.json",
            "description": "Default model produces high quality consistently. Safe to use haiku for non-security auditors to reduce cost.",
            "evidence": [{"task_id": r.get("task_id"), "quality": _safe_float(r.get("quality_score"), 0)} for r in default_high_quality],
            "action": "adjust_model_policy",
            "suggested_value": "haiku for spec-completion-auditor and code-quality-auditor on low-risk tasks",
        })

    # 5. Learned agent seeding: if a (role, task_type) combo has 3+ tasks, propose seeding
    role_type_counts: dict[tuple[str, str], int] = {}
    for r in all_retros:
        for role in r.get("executor_repair_frequency", {}):
            tt = r.get("task_type", "")
            if isinstance(role, str) and isinstance(tt, str):
                role_type_counts[(role, tt)] = role_type_counts.get((role, tt), 0) + 1
    for (role, tt), count in role_type_counts.items():
        if count >= 3:
            proposals.append({
                "id": f"imp-seed-{role}-{tt}",
                "type": "learned_agent_seed",
                "target": f".dynos/learned-agents/executors/auto-{role.replace('-executor', '')}-{tt}.md",
                "description": f"Role {role} on {tt} tasks has {count} observations. Seed a learned agent.",
                "action": "seed_learned_agent",
                "role": role,
                "task_type": tt,
                "observation_count": count,
            })

    return proposals


def apply_improvement(root: Path, proposal: dict) -> dict:
    """Apply a single improvement proposal to project-local state.

    Only modifies files under .dynos/. Never touches plugin source.
    Returns result dict with applied=True/False.
    """
    action = proposal.get("action", "")
    result = {"id": proposal["id"], "applied": False, "reason": ""}

    if action == "adjust_policy":
        policy_path = root / ".dynos" / "policy.json"
        policy = {}
        if policy_path.exists():
            try:
                policy = load_json(policy_path)
            except (json.JSONDecodeError, OSError):
                policy = {}
        field = proposal.get("field", "")
        if field and field not in policy:
            policy[field] = proposal.get("suggested_value")
            write_json(policy_path, policy)
            result["applied"] = True
            result["reason"] = f"Set {field}={proposal.get('suggested_value')}"
        else:
            result["reason"] = f"Field {field} already exists in policy"

    elif action == "seed_learned_agent":
        role = proposal.get("role", "")
        tt = proposal.get("task_type", "")
        agent_name = f"auto-{role.replace('-executor', '')}-{tt}"
        agent_dir = root / ".dynos" / "learned-agents" / "executors"
        agent_dir.mkdir(parents=True, exist_ok=True)
        agent_path = agent_dir / f"{agent_name}.md"
        if not agent_path.exists():
            agent_path.write_text(
                f"# {agent_name}\n\n"
                f"Auto-generated from postmortem analysis.\n"
                f"Role: {role}, Task type: {tt}\n"
                f"Observations: {proposal.get('observation_count', 0)}\n"
                f"Generated: {now_iso()}\n"
            )
            result["applied"] = True
            result["reason"] = f"Seeded {agent_path.name}"
        else:
            result["reason"] = f"Agent {agent_name} already exists"

    else:
        result["reason"] = f"Action '{action}' not auto-applicable (logged for manual review)"

    return result


def run_improvement_cycle(root: Path) -> dict:
    """Full improvement cycle: propose, log, apply safe improvements."""
    imp_dir = _improvements_dir(root)
    imp_dir.mkdir(parents=True, exist_ok=True)

    proposals = propose_improvements(root)
    if not proposals:
        return {"proposals": 0, "applied": 0, "results": []}

    # Log proposals
    log_path = imp_dir / f"proposals-{datetime.now(timezone.utc).strftime('%Y%m%d')}.json"
    write_json(log_path, {
        "generated_at": now_iso(),
        "proposals": proposals,
    })

    # Apply safe improvements (policy tuning and agent seeding only)
    safe_actions = {"adjust_policy", "seed_learned_agent"}
    results = []
    for p in proposals:
        if p.get("action") in safe_actions:
            r = apply_improvement(root, p)
            results.append(r)
        else:
            results.append({"id": p["id"], "applied": False, "reason": "Requires manual review"})

    # Log results
    result_path = imp_dir / f"results-{datetime.now(timezone.utc).strftime('%Y%m%d')}.json"
    write_json(result_path, {
        "executed_at": now_iso(),
        "results": results,
    })

    applied_count = sum(1 for r in results if r.get("applied"))
    return {
        "proposals": len(proposals),
        "applied": applied_count,
        "results": results,
    }


def cmd_improve(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    result = run_improvement_cycle(root)
    print(json.dumps(result, indent=2))
    return 0


def cmd_propose(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    proposals = propose_improvements(root)
    print(json.dumps({"proposals": proposals}, indent=2))
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    task_id = args.task_id
    if not task_id:
        # Find most recent completed task
        retros = collect_retrospectives(root)
        if not retros:
            print(json.dumps({"error": "no retrospectives found"}))
            return 1
        task_id = retros[-1].get("task_id")
    result = write_postmortem(root, task_id)
    print(json.dumps(result, indent=2))
    return 0


def cmd_generate_all(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    retros = collect_retrospectives(root)
    results = []
    for retro in retros:
        tid = retro.get("task_id")
        if tid:
            results.append(write_postmortem(root, tid))
    print(json.dumps({"generated": len(results), "results": results}, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    gen = subparsers.add_parser("generate", help="Generate postmortem for a task")
    gen.add_argument("--task-id", default=None, help="Task ID (default: most recent)")
    gen.add_argument("--root", default=".")
    gen.set_defaults(func=cmd_generate)

    gen_all = subparsers.add_parser("generate-all", help="Generate postmortems for all tasks")
    gen_all.add_argument("--root", default=".")
    gen_all.set_defaults(func=cmd_generate_all)

    propose = subparsers.add_parser("propose", help="Propose project-local improvements (dry run)")
    propose.add_argument("--root", default=".")
    propose.set_defaults(func=cmd_propose)

    improve = subparsers.add_parser("improve", help="Propose and apply safe project-local improvements")
    improve.add_argument("--root", default=".")
    improve.set_defaults(func=cmd_improve)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
