#!/usr/bin/env python3
"""Trajectory store and similarity search for dynos-work."""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent)); _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent.parent / "hooks"))

from pathlib import Path
from typing import Optional

from lib_core import (
    COMPOSITE_WEIGHTS,
    TOKEN_ESTIMATES,
    _safe_float,
    collect_retrospectives,
    load_json,
    now_iso,
    trajectories_store_path,
    write_json,
)


def ensure_trajectory_store(root: Path) -> dict:
    """Ensure the trajectory store exists and return its contents."""
    path = trajectories_store_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or not path.read_text().strip():
        store: dict = {"version": 1, "updated_at": now_iso(), "trajectories": []}
        write_json(path, store)
        return store
    data = load_json(path)
    if not isinstance(data, dict) or "trajectories" not in data:
        store = {"version": 1, "updated_at": now_iso(), "trajectories": []}
        write_json(path, store)
        return store
    return data


def compute_quality_score(findings_by_auditor: dict, repair_cycle_count: int) -> float:
    """Deterministic quality score from structured audit data.

    Never trust LLM-written quality_score -- always recompute from findings.
    """
    if not isinstance(findings_by_auditor, dict):
        return 0.9
    total_findings = sum(v for v in findings_by_auditor.values() if isinstance(v, (int, float)))
    if total_findings == 0:
        return 0.9
    repairs = max(0, int(repair_cycle_count or 0))
    if repairs > 0:
        surviving = max(0, total_findings - repairs * 2)
        return max(0.0, 1 - (surviving / total_findings))
    return 1 / (1 + total_findings)


def estimate_token_usage(subagent_spawn_count: int, model_used_by_agent: dict) -> int:
    """Estimate total token usage when real data is unavailable."""
    if isinstance(model_used_by_agent, dict) and model_used_by_agent:
        return sum(
            TOKEN_ESTIMATES.get(m, TOKEN_ESTIMATES["default"])
            for m in model_used_by_agent.values()
            if isinstance(m, str)
        ) or subagent_spawn_count * TOKEN_ESTIMATES["default"]
    return max(1, int(subagent_spawn_count or 0)) * TOKEN_ESTIMATES["default"]


def load_token_usage(task_dir: Path) -> dict:
    """Read token-usage.json written by the audit/execute skills during subagent spawns.

    Returns a dict with keys: agents, by_agent, by_model, total,
    total_input_tokens, total_output_tokens.
    """
    path = task_dir / "token-usage.json"
    empty: dict = {
        "agents": {}, "by_agent": {}, "by_model": {},
        "total": 0, "total_input_tokens": 0, "total_output_tokens": 0,
    }
    if path.exists():
        data = load_json(path)
        if isinstance(data, dict) and "agents" in data:
            # Backfill missing fields for older format files
            if "by_agent" not in data:
                data["by_agent"] = {}
            if "by_model" not in data:
                data["by_model"] = {}
            if "total_input_tokens" not in data:
                data["total_input_tokens"] = sum(
                    v.get("input_tokens", 0) for v in data.get("by_agent", {}).values()
                    if isinstance(v, dict)
                )
            if "total_output_tokens" not in data:
                data["total_output_tokens"] = sum(
                    v.get("output_tokens", 0) for v in data.get("by_agent", {}).values()
                    if isinstance(v, dict)
                )
            return data
    return empty


def validate_retrospective_scores(retro: dict, task_dir: Optional[Path] = None) -> dict:
    """Recompute quality/cost/efficiency scores deterministically, overwriting LLM values."""
    retro = dict(retro)
    findings_by_auditor = retro.get("findings_by_auditor", {})
    repair_cycles = int(retro.get("repair_cycle_count", 0) or 0)
    spec_iterations = int(retro.get("spec_review_iterations", 0) or 0)
    subagent_spawns = int(retro.get("subagent_spawn_count", 0) or 0)
    risk_level = str(retro.get("task_risk_level", "medium"))
    agent_source = retro.get("agent_source", {})
    model_used = retro.get("model_used_by_agent", {})

    retro["quality_score"] = compute_quality_score(findings_by_auditor, repair_cycles)
    retro["efficiency_score"] = max(0.0, 1 - (repair_cycles / 3) - (max(0, spec_iterations - 1) * 0.1))

    total_tokens = 0.0
    token_estimated = False
    if task_dir is not None:
        token_data = load_token_usage(task_dir)
        total_tokens = float(token_data.get("total", 0))
        if total_tokens > 0:
            retro["token_usage_by_agent"] = token_data["agents"]
            retro["total_token_usage"] = int(total_tokens)
    if total_tokens == 0:
        total_tokens = _safe_float(retro.get("total_token_usage"), 0.0)
    if total_tokens == 0 and subagent_spawns > 0:
        total_tokens = float(estimate_token_usage(subagent_spawns, model_used))
        token_estimated = True
    budget = {"low": 8000, "medium": 12000, "high": 18000, "critical": 25000}.get(risk_level, 12000)
    avg_tokens = total_tokens / max(1, subagent_spawns)
    cost_score = max(0.0, min(1.0, 1 / (1 + (avg_tokens / budget))))
    if isinstance(agent_source, dict) and agent_source and all(v == "generic" for v in agent_source.values()):
        cost_score = max(0.0, cost_score - 0.05)
    retro["cost_score"] = cost_score
    if token_estimated:
        retro["token_usage_estimated"] = True

    return retro


def make_trajectory_entry(retrospective: dict) -> dict:
    """Create a trajectory entry from a retrospective."""
    findings = retrospective.get("findings_by_category", {})
    categories = findings if isinstance(findings, dict) else {}
    task_domains = retrospective.get("task_domains", "")
    domains = [part.strip() for part in task_domains.split(",") if part.strip()]

    findings_by_auditor = retrospective.get("findings_by_auditor", {})
    repair_cycles = int(retrospective.get("repair_cycle_count", 0) or 0)
    quality_score = compute_quality_score(findings_by_auditor, repair_cycles)

    spec_iterations = int(retrospective.get("spec_review_iterations", 0) or 0)
    efficiency_score = max(0.0, 1 - (repair_cycles / 3) - (max(0, spec_iterations - 1) * 0.1))

    total_tokens = _safe_float(retrospective.get("total_token_usage"), 0.0)
    subagent_spawns = int(retrospective.get("subagent_spawn_count", 0) or 0)
    token_estimated = False
    if total_tokens == 0 and subagent_spawns > 0:
        model_used = retrospective.get("model_used_by_agent", {})
        total_tokens = float(estimate_token_usage(subagent_spawns, model_used))
        token_estimated = True
    risk_level = str(retrospective.get("task_risk_level", "medium"))
    budget_per_spawn = {"low": 8000, "medium": 12000, "high": 18000, "critical": 25000}.get(risk_level, 12000)
    avg_tokens = total_tokens / max(1, subagent_spawns)
    cost_score = max(0.0, min(1.0, 1 / (1 + (avg_tokens / budget_per_spawn))))
    agent_source = retrospective.get("agent_source", {})
    if isinstance(agent_source, dict) and agent_source and all(v == "generic" for v in agent_source.values()):
        cost_score = max(0.0, cost_score - 0.05)

    wq, we, wc = COMPOSITE_WEIGHTS
    reward = round(wq * quality_score + we * efficiency_score + wc * cost_score, 6)
    return {
        "trajectory_id": retrospective["task_id"],
        "source_task_id": retrospective["task_id"],
        "version": 1,
        "created_at": now_iso(),
        "state": {
            "task_type": retrospective.get("task_type"),
            "task_domains": domains,
            "task_risk_level": retrospective.get("task_risk_level"),
            "findings_by_category": categories,
            "spec_review_iterations": int(retrospective.get("spec_review_iterations", 0) or 0),
            "repair_cycle_count": int(retrospective.get("repair_cycle_count", 0) or 0),
            "subagent_spawn_count": int(retrospective.get("subagent_spawn_count", 0) or 0),
            "wasted_spawns": int(retrospective.get("wasted_spawns", 0) or 0),
        },
        "action_summary": {
            "executor_repair_frequency": retrospective.get("executor_repair_frequency", {}),
            "auditor_zero_finding_streaks": retrospective.get("auditor_zero_finding_streaks", {}),
        },
        "reward": {
            "quality_score": quality_score,
            "cost_score": cost_score,
            "efficiency_score": efficiency_score,
            "composite_reward": reward,
            "token_usage_estimated": token_estimated,
        },
        "outcome": retrospective.get("task_outcome", "UNKNOWN"),
    }


def rebuild_trajectory_store(root: Path) -> dict:
    """Rebuild the trajectory store from all retrospectives."""
    store = ensure_trajectory_store(root)
    trajectories = [make_trajectory_entry(item) for item in collect_retrospectives(root)]
    store["version"] = 1
    store["updated_at"] = now_iso()
    store["trajectories"] = sorted(trajectories, key=lambda item: item["trajectory_id"])
    write_json(trajectories_store_path(root), store)
    return store


def _domain_overlap(a: list[str], b: list[str]) -> float:
    """Compute Jaccard similarity between two domain lists."""
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def trajectory_similarity(query_state: dict, candidate: dict) -> float:
    """Compute similarity between a query state and a trajectory candidate."""
    candidate_state = candidate.get("state", {})
    score = 0.0
    if query_state.get("task_type") == candidate_state.get("task_type"):
        score += 0.35
    if query_state.get("task_risk_level") == candidate_state.get("task_risk_level"):
        score += 0.2
    score += 0.25 * _domain_overlap(
        query_state.get("task_domains", []), candidate_state.get("task_domains", [])
    )
    numeric_keys = ["repair_cycle_count", "subagent_spawn_count", "wasted_spawns", "spec_review_iterations"]
    numeric_score = 0.0
    for key in numeric_keys:
        qv = float(query_state.get(key, 0))
        cv = float(candidate_state.get(key, 0))
        numeric_score += 1 / (1 + abs(qv - cv))
    score += 0.2 * (numeric_score / len(numeric_keys))
    return round(score, 6)


def search_trajectories(root: Path, query_state: dict, limit: int = 3) -> list[dict]:
    """Search for similar trajectories given a query state."""
    store = ensure_trajectory_store(root)
    ranked: list[dict] = []
    for entry in store.get("trajectories", []):
        similarity = trajectory_similarity(query_state, entry)
        ranked.append({"similarity": similarity, "trajectory": entry})
    ranked.sort(key=lambda item: (-item["similarity"], item["trajectory"]["trajectory_id"]))
    return ranked[:limit]


def collect_task_summaries(root: Path) -> list[dict]:
    """Collect task summaries from retrospectives with benchmark scores."""
    summaries: list[dict] = []
    for retrospective in collect_retrospectives(root):
        task_id = retrospective.get("task_id")
        if not isinstance(task_id, str):
            continue
        task_dir = root / ".dynos" / task_id
        manifest: dict = {}
        if (task_dir / "manifest.json").exists():
            try:
                manifest = load_json(task_dir / "manifest.json")
            except Exception:
                manifest = {}
        task_domains = retrospective.get("task_domains", "")
        domains = [part.strip() for part in str(task_domains).split(",") if part.strip()]
        summaries.append(
            {
                "task_id": task_id,
                "task_type": retrospective.get("task_type"),
                "domains": domains,
                "risk_level": retrospective.get("task_risk_level"),
                "title": manifest.get("title") or manifest.get("raw_input") or task_id,
                "score": retrospective_benchmark_score(retrospective),
                "retrospective_path": str(task_dir / "task-retrospective.json"),
            }
        )
    summaries.sort(key=lambda item: item["task_id"])
    return summaries


def retrospective_benchmark_score(retrospective: dict) -> dict:
    """Compute benchmark score from a retrospective."""
    quality = _safe_float(retrospective.get("quality_score"), 0.0)
    cost = _safe_float(retrospective.get("cost_score"), 0.0)
    efficiency = _safe_float(retrospective.get("efficiency_score"), 0.0)
    wq, we, wc = COMPOSITE_WEIGHTS
    composite = wq * quality + we * efficiency + wc * cost
    return {
        "quality_score": round(quality, 6),
        "cost_score": round(cost, 6),
        "efficiency_score": round(efficiency, 6),
        "composite_score": round(composite, 6),
    }
