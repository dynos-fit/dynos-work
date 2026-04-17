#!/usr/bin/env python3
"""Auto-improvement engine for dynos-work postmortems.

Extracted from postmortem.py. Produces project-local .dynos/ changes,
NOT global plugin file mutations.
"""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent)); _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "hooks"))

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from lib_usage_telemetry import record_usage as _record_usage
_record_usage("postmortem_improve")

from lib_core import (
    _persistent_project_dir,
    _safe_float,
    collect_retrospectives,
    load_json,
    now_iso,
    project_dir,
    write_json,
)


# Token budgets by (risk_level, task_type)
TOKEN_BUDGETS: dict[tuple[str, str], int] = {
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
DEFAULT_BUDGET: int = 60000


def _expected_budget(risk_level: str, task_type: str, multiplier: float = 1.0) -> int:
    """Compute the expected token budget for a given risk/type combination."""
    base = TOKEN_BUDGETS.get((risk_level, task_type), DEFAULT_BUDGET)
    return int(base * max(0.5, multiplier))


def _improvements_dir(root: Path) -> Path:
    """Return the improvements directory for a project."""
    return project_dir(root) / "improvements"


def _load_applied_ids(root: Path) -> set:
    """Load the set of proposal IDs that have been applied."""
    path = _improvements_dir(root) / "applied-ids.json"
    if path.exists():
        data = load_json(path)
        if isinstance(data, list):
            return set(data)
    return set()


def _save_applied_id(root: Path, proposal_id: str) -> None:
    """Record a proposal ID as applied."""
    imp_dir = _improvements_dir(root)
    imp_dir.mkdir(parents=True, exist_ok=True)
    path = imp_dir / "applied-ids.json"
    applied = _load_applied_ids(root)
    applied.add(proposal_id)
    write_json(path, sorted(applied))


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
        return []

    applied_ids = _load_applied_ids(root)
    recent = all_retros[-5:]
    proposals: list[dict] = []

    # 1. Token budget tuning
    overrun_tasks: list[dict] = []
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

    # 2. Fast-track expansion
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

    # 3. Prevention rule generation
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
                "target": ".dynos/project_rules.md",
                "description": f"Finding category '{cat}' has {count} occurrences across all tasks. Add a prevention rule.",
                "action": "add_prevention_rule",
                "category": cat,
                "total_occurrences": count,
            })

    # 4. Model tier recommendation
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

    # 5. Learned agent seeding
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
                "target": str(_persistent_project_dir(root) / "learned-agents" / "executors" / f"auto-{role.replace('-executor', '')}-{tt}.md"),
                "description": f"Role {role} on {tt} tasks has {count} observations. Seed a learned agent.",
                "action": "seed_learned_agent",
                "role": role,
                "task_type": tt,
                "observation_count": count,
            })

    return [p for p in proposals if p.get("id") not in applied_ids]


def apply_improvement(root: Path, proposal: dict) -> dict:
    """Apply a single improvement proposal to project-local state.

    Only modifies files under .dynos/. Never touches plugin source.
    Returns result dict with applied=True/False.
    """
    action = proposal.get("action", "")
    result: dict = {"id": proposal["id"], "applied": False, "reason": ""}

    if action == "adjust_policy":
        policy_path = _persistent_project_dir(root) / "policy.json"
        policy: dict = {}
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

    elif action == "adjust_model_policy":
        persistent = _persistent_project_dir(root)
        policy_path = persistent / "policy.json"
        policy = {}
        if policy_path.exists():
            try:
                policy = load_json(policy_path)
            except (json.JSONDecodeError, OSError):
                policy = {}
        overrides = policy.get("model_overrides", {})
        changed = False
        for role in ("spec-completion-auditor", "code-quality-auditor", "dead-code-auditor"):
            for tt in ("feature", "bugfix", "refactor"):
                key = f"{role}:{tt}"
                if key not in overrides:
                    overrides[key] = "haiku"
                    changed = True
        if changed:
            policy["model_overrides"] = overrides
            write_json(policy_path, policy)
            result["applied"] = True
            result["reason"] = f"Set haiku for {len(overrides)} non-security auditor:task_type pairs"
        else:
            result["reason"] = "Model overrides already set"

        mp_path = persistent / "model-policy.json"
        model_policy: dict = {}
        if mp_path.exists():
            try:
                model_policy = load_json(mp_path)
            except (json.JSONDecodeError, OSError):
                model_policy = {}
        mp_changed = False
        for role in ("spec-completion-auditor", "code-quality-auditor", "dead-code-auditor"):
            for tt in ("feature", "bugfix", "refactor"):
                key = f"{role}:{tt}"
                existing = model_policy.get(key, {})
                if existing.get("source") == "explicit_policy":
                    continue
                model_policy[key] = {
                    "model": "haiku",
                    "source": "postmortem_recommendation",
                }
                mp_changed = True
        if mp_changed:
            write_json(mp_path, model_policy)
            result["applied"] = True

    elif action == "add_prevention_rule":
        rules_path = _persistent_project_dir(root) / "prevention-rules.json"
        rules: list[dict] = []
        if rules_path.exists():
            try:
                data = load_json(rules_path)
                rules = data.get("rules", [])
            except (json.JSONDecodeError, OSError):
                rules = []
        category = proposal.get("category", "")
        existing_cats = {r.get("category") for r in rules}
        if category and category not in existing_cats:
            rules.append({
                "category": category,
                "rule": f"Category '{category}' has {proposal.get('total_occurrences', 0)} findings across tasks. Add extra scrutiny for {category}-class issues.",
                "added_at": now_iso(),
            })
            write_json(rules_path, {"rules": rules, "updated_at": now_iso()})
            result["applied"] = True
            result["reason"] = f"Added prevention rule for category '{category}'"
        else:
            result["reason"] = f"Prevention rule for '{category}' already exists"

    elif action == "seed_learned_agent":
        role = proposal.get("role", "")
        tt = proposal.get("task_type", "")
        agent_name = f"auto-{role.replace('-executor', '')}-{tt}"
        agent_dir = _persistent_project_dir(root) / "learned-agents" / "executors"
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

    log_path = imp_dir / f"proposals-{datetime.now(timezone.utc).strftime('%Y%m%d')}.json"
    write_json(log_path, {
        "generated_at": now_iso(),
        "proposals": proposals,
    })

    safe_actions = {"adjust_policy", "seed_learned_agent", "adjust_model_policy", "add_prevention_rule"}
    results: list[dict] = []
    for p in proposals:
        if p.get("action") in safe_actions:
            r = apply_improvement(root, p)
            _save_applied_id(root, p["id"])
            results.append(r)
        else:
            results.append({"id": p["id"], "applied": False, "reason": "Requires manual review"})

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
    """Run the full improvement cycle."""
    root = Path(args.root).resolve()
    result = run_improvement_cycle(root)
    print(json.dumps(result, indent=2))
    return 0


def cmd_list_pending(args: argparse.Namespace) -> int:
    """List improvement proposals that were not auto-applied."""
    root = Path(args.root).resolve()
    imp_dir = _persistent_project_dir(root) / "improvements"
    if not imp_dir.exists():
        print(json.dumps({"pending": []}))
        return 0

    pending: list[dict] = []
    globally_applied = _load_applied_ids(root)
    seen_ids: set[str] = set()
    for pfile in sorted(imp_dir.glob("proposals-*.json")):
        pdata = load_json(pfile)
        if not isinstance(pdata, dict):
            continue
        for p in pdata.get("proposals", []):
            pid = p.get("id", "")
            if pid in globally_applied or pid in seen_ids:
                continue
            seen_ids.add(pid)
            pending.append({
                "id": pid,
                "type": p.get("type"),
                "action": p.get("action"),
                "description": p.get("description"),
                "source_file": str(pfile),
            })

    print(json.dumps({"pending": pending}, indent=2))
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    """Approve and apply a specific improvement proposal by ID."""
    root = Path(args.root).resolve()
    imp_dir = _persistent_project_dir(root) / "improvements"
    target_id = args.improvement_id

    found = None
    for pfile in sorted(imp_dir.glob("proposals-*.json")):
        pdata = load_json(pfile)
        if not isinstance(pdata, dict):
            continue
        for p in pdata.get("proposals", []):
            if p.get("id") == target_id:
                found = p
                break
        if found:
            break

    if not found:
        print(json.dumps({"error": f"proposal '{target_id}' not found"}))
        return 1

    result = apply_improvement(root, found)
    if result.get("applied"):
        _save_applied_id(root, target_id)
    date_key = datetime.now(timezone.utc).strftime("%Y%m%d")
    result_path = imp_dir / f"results-{date_key}.json"
    existing = load_json(result_path) if result_path.exists() else {}
    results_list = existing.get("results", []) if isinstance(existing, dict) else []
    results_list.append(result)
    write_json(result_path, {"executed_at": now_iso(), "results": results_list})

    print(json.dumps(result, indent=2))
    return 0


def cmd_propose(args: argparse.Namespace) -> int:
    """Propose improvements without applying them."""
    root = Path(args.root).resolve()
    proposals = propose_improvements(root)
    print(json.dumps({"proposals": proposals}, indent=2))
    return 0
