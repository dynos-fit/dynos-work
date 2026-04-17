#!/usr/bin/env python3
"""Automatic postmortem generator for dynos-work tasks."""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent)); _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "hooks"))

import argparse
import json
from pathlib import Path

from lib_core import (
    collect_retrospectives,
    load_json,
    now_iso,
    write_json,
    _safe_float,
    _safe_int,
    _persistent_project_dir,
    project_policy,
    project_dir,
)
from lib_log import log_event
from lib_defaults import (
    DEFAULT_TOKEN_BUDGET,
    GENERIC_ROUTING_PATTERN_THRESHOLD,
    OVERRUN_PATTERN_THRESHOLD,
    OVERRUN_RATIO_HIGH,
    OVERRUN_RATIO_MEDIUM,
    PATTERN_DETECTION_WINDOW,
    QUALITY_REGRESSION_THRESHOLD,
    QUALITY_THRESHOLD_ANOMALY,
    REPAIR_CYCLES_THRESHOLD,
    REPAIR_PATTERN_THRESHOLD,
    SIMILAR_TASKS_LIMIT,
    SIMILARITY_WEIGHT_DOMAIN,
    SIMILARITY_WEIGHT_RISK,
    SIMILARITY_WEIGHT_TASKTYPE,
    TASK_TOKEN_BUDGETS,
    WASTED_SPAWN_RATIO_THRESHOLD,
    ZERO_QUALITY_PATTERN_THRESHOLD,
)


# Token budgets by (risk_level, task_type) — derived from centralized defaults
TOKEN_BUDGETS = {
    (risk, ttype): budget
    for risk, type_budgets in TASK_TOKEN_BUDGETS.items()
    for ttype, budget in type_budgets.items()
}
DEFAULT_BUDGET = DEFAULT_TOKEN_BUDGET


def postmortems_dir(root: Path) -> Path:
    return project_dir(root) / "postmortems"


def _expected_budget(risk_level: str, task_type: str, multiplier: float = 1.0) -> int:
    base = TOKEN_BUDGETS.get((risk_level, task_type), DEFAULT_BUDGET)
    return int(base * max(0.5, multiplier))


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


def _find_similar_tasks(retro: dict, all_retros: list[dict], limit: int = SIMILAR_TASKS_LIMIT) -> list[dict]:
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
            score += SIMILARITY_WEIGHT_TASKTYPE
        if past.get("task_domains") == task_domains:
            score += SIMILARITY_WEIGHT_DOMAIN
        if past.get("task_risk_level") == retro.get("task_risk_level"):
            score += SIMILARITY_WEIGHT_RISK
        if score > 0:
            similar.append({"task_id": past.get("task_id"), "similarity": score, **past})
    similar.sort(key=lambda x: (-x["similarity"], x.get("task_id", "")))
    return similar[:limit]


def _detect_anomalies(retro: dict, similar_tasks: list[dict], budget_multiplier: float = 1.0) -> list[dict]:
    """Detect anomalies by comparing to similar past tasks."""
    anomalies = []
    risk = retro.get("task_risk_level", "medium")
    task_type = retro.get("task_type", "feature")
    total_tokens = _safe_float(retro.get("total_token_usage"), 0)
    budget = _expected_budget(risk, task_type, budget_multiplier)

    # Token overrun
    if total_tokens > budget * OVERRUN_RATIO_MEDIUM:
        anomalies.append({
            "type": "token_overrun",
            "severity": "high" if total_tokens > budget * OVERRUN_RATIO_HIGH else "medium",
            "detail": f"Used {int(total_tokens)} tokens vs {budget} budget ({total_tokens/budget:.1f}x)",
        })

    # Quality anomaly
    quality = _safe_float(retro.get("quality_score"), 0)
    if quality < QUALITY_THRESHOLD_ANOMALY:
        anomalies.append({
            "type": "low_quality",
            "severity": "high",
            "detail": f"quality_score={quality:.2f} is below 0.5 threshold",
        })

    # All-generic routing
    sources = retro.get("agent_source", {})
    if isinstance(sources, dict) and sources and all(v == "generic" for v in sources.values()):
        anomalies.append({
            "type": "all_generic_routing",
            "severity": "low",
            "detail": "All agents used generic routing. No learned specialization applied.",
        })

    # Repair cycles
    repairs = _safe_int(retro.get("repair_cycle_count", 0) or 0)
    if repairs >= REPAIR_CYCLES_THRESHOLD:
        anomalies.append({
            "type": "high_repair_cycles",
            "severity": "medium",
            "detail": f"{repairs} repair cycles indicates spec/execution gaps",
        })

    # Wasted spawns ratio
    spawns = _safe_int(retro.get("subagent_spawn_count", 0) or 0)
    wasted = _safe_int(retro.get("wasted_spawns", 0) or 0)
    if spawns > 0 and wasted / spawns > WASTED_SPAWN_RATIO_THRESHOLD:
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
        if quality < avg_similar_quality - QUALITY_REGRESSION_THRESHOLD:
            anomalies.append({
                "type": "quality_regression_vs_similar",
                "severity": "high",
                "detail": f"quality={quality:.2f} vs avg similar={avg_similar_quality:.2f}",
            })

    return anomalies


def _detect_recurring_patterns(all_retros: list[dict], window: int = PATTERN_DETECTION_WINDOW, budget_multiplier: float = 1.0) -> list[dict]:
    """Detect recurring issues across recent tasks."""
    recent = all_retros[-window:]
    patterns = []

    # Recurring all-generic routing
    generic_count = sum(
        1 for r in recent
        if r.get("agent_source") and all(v == "generic" for v in r.get("agent_source", {}).values())
    )
    if generic_count >= GENERIC_ROUTING_PATTERN_THRESHOLD:
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
        if tokens > _expected_budget(risk, tt, budget_multiplier) * OVERRUN_RATIO_MEDIUM:
            overrun_count += 1
    if overrun_count >= OVERRUN_PATTERN_THRESHOLD:
        patterns.append({
            "pattern": "recurring_token_overrun",
            "occurrences": overrun_count,
            "window": window,
            "recommendation": "Multiple tasks exceeded token budget. Consider: haiku for low-risk auditors, inline execution for simple segments, tighter spec to reduce planning tokens.",
        })

    # Recurring repair cycles
    repair_count = sum(1 for r in recent if _safe_int(r.get("repair_cycle_count", 0) or 0) > 0)
    if repair_count >= REPAIR_PATTERN_THRESHOLD:
        patterns.append({
            "pattern": "recurring_repair_cycles",
            "occurrences": repair_count,
            "window": window,
            "recommendation": "Most tasks require repair. Spec/planning quality may need improvement. Consider stronger code grounding in spec normalization.",
        })

    # Recurring scoring issues (quality_score exactly 0)
    zero_quality = sum(1 for r in recent if _safe_float(r.get("quality_score"), -1) == 0)
    if zero_quality >= ZERO_QUALITY_PATTERN_THRESHOLD:
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
    policy = project_policy(root)
    budget_multiplier = float(policy.get("token_budget_multiplier", 1.0))
    similar = _find_similar_tasks(retro, all_retros)
    anomalies = _detect_anomalies(retro, similar, budget_multiplier)
    recurring = _detect_recurring_patterns(all_retros, budget_multiplier=budget_multiplier)

    risk = retro.get("task_risk_level", "medium")
    task_type = retro.get("task_type", "feature")
    budget = _expected_budget(risk, task_type, budget_multiplier)
    total_tokens = _safe_float(retro.get("total_token_usage"), 0)

    # Cost summary
    cost_summary = {
        "total_tokens": int(total_tokens),
        "expected_budget": budget,
        "budget_ratio": round(total_tokens / max(1, budget), 2),
        "subagent_spawns": _safe_int(retro.get("subagent_spawn_count", 0) or 0),
        "wasted_spawns": _safe_int(retro.get("wasted_spawns", 0) or 0),
    }

    # Quality summary
    findings_by_auditor = retro.get("findings_by_auditor", {})
    total_findings = sum(v for v in findings_by_auditor.values() if isinstance(v, (int, float)))
    quality_summary = {
        "quality_score": _safe_float(retro.get("quality_score"), 0),
        "total_findings": int(total_findings),
        "repair_cycles": _safe_int(retro.get("repair_cycle_count", 0) or 0),
        "findings_by_auditor": findings_by_auditor,
    }

    # Policy usage
    policy_usage = {
        "agent_sources": retro.get("agent_source", {}),
        "all_generic": all(v == "generic" for v in retro.get("agent_source", {}).values()) if isinstance(retro.get("agent_source"), dict) and retro.get("agent_source") else False,
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
            "repair_cycles": _safe_int(s.get("repair_cycle_count", 0) or 0),
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

    log_event(root, "postmortem_written", task=task_id, anomaly_count=len(postmortem.get("anomalies", [])), recurring_pattern_count=len(postmortem.get("recurring_patterns", [])), json_path=str(json_path))
    return {
        "task_id": task_id,
        "json_path": str(json_path),
        "md_path": str(md_path),
        "anomaly_count": len(postmortem.get("anomalies", [])),
        "recurring_pattern_count": len(postmortem.get("recurring_patterns", [])),
    }


def _render_markdown(pm: dict) -> str:
    cost = pm.get("cost_summary", {})
    quality = pm.get("quality_summary", {})
    policy = pm.get("policy_usage", {})
    lines = [
        f"# Postmortem: {pm.get('task_id', 'unknown')}",
        f"Generated: {pm.get('generated_at', 'unknown')}",
        "",
        f"**Type:** {pm.get('task_type', '')} | **Risk:** {pm.get('task_risk_level', '')} | **Domains:** {pm.get('task_domains', '')} | **Outcome:** {pm.get('task_outcome', '')}",
        "",
        "## Cost",
        f"- Tokens: {cost.get('total_tokens', 0):,} / {cost.get('expected_budget', 0):,} budget ({cost.get('budget_ratio', 0)}x)",
        f"- Spawns: {cost.get('subagent_spawns', 0)} total, {cost.get('wasted_spawns', 0)} wasted",
        "",
        "## Quality",
        f"- Score: {quality.get('quality_score', 0):.2f}",
        f"- Findings: {quality.get('total_findings', 0)} total, {quality.get('repair_cycles', 0)} repair cycles",
    ]

    findings_by_auditor = quality.get("findings_by_auditor", {})
    if findings_by_auditor:
        for auditor, count in findings_by_auditor.items():
            lines.append(f"  - {auditor}: {count}")

    lines.extend([
        "",
        "## Policy Usage",
        f"- All generic routing: {'yes' if policy.get('all_generic') else 'no'}",
        f"- Fast-track: {'yes' if policy.get('fast_track') else 'no'}",
        f"- Inline execution: {'yes' if policy.get('inline_execution') else 'no'}",
        f"- Discovery skipped: {'yes' if policy.get('discovery_skipped') else 'no'}",
    ])

    if pm.get("anomalies"):
        lines.extend(["", "## Anomalies"])
        for a in pm["anomalies"]:
            lines.append(f"- **[{a.get('severity', 'unknown')}] {a.get('type', 'unknown')}**: {a.get('detail', '')}")

    if pm.get("similar_tasks"):
        lines.extend(["", "## Similar Past Tasks"])
        for s in pm["similar_tasks"]:
            lines.append(f"- {s.get('task_id', 'unknown')}: quality={s.get('quality_score', 0):.2f}, tokens={s.get('total_tokens', 0):,}, repairs={s.get('repair_cycles', 0)}")

    if pm.get("recurring_patterns"):
        lines.extend(["", "## Recurring Patterns (across recent tasks)"])
        for p in pm["recurring_patterns"]:
            lines.append(f"- **{p.get('pattern', 'unknown')}** ({p.get('occurrences', 0)}/{p.get('window', 0)} tasks): {p.get('recommendation', '')}")

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Auto-improvement engine (delegated to postmortem_improve)
# ---------------------------------------------------------------------------
from postmortem_improve import (  # noqa: E402
    apply_improvement,
    cmd_approve,
    cmd_improve,
    cmd_list_pending,
    cmd_propose,
)


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

    pending = subparsers.add_parser("list-pending", help="List improvement proposals not yet applied")
    pending.add_argument("--root", default=".")
    pending.set_defaults(func=cmd_list_pending)

    approve = subparsers.add_parser("approve", help="Approve and apply a specific improvement by ID")
    approve.add_argument("improvement_id", help="The proposal ID to approve (e.g. imp-prevent-cq)")
    approve.add_argument("--root", default=".")
    approve.set_defaults(func=cmd_approve)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
