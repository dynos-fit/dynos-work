#!/usr/bin/env python3
"""Learned agent registry management for dynos-work."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from lib_core import (
    benchmark_policy_config,
    learned_registry_path,
    load_json,
    now_iso,
    tasks_since,
    write_json,
)
MAX_REGISTRY_BENCHMARKS: int = 200

# Static empty summary — avoids cross-domain import from lib_benchmark.
_EMPTY_BENCHMARK_SUMMARY: dict = {
    "sample_count": 0,
    "mean_quality": 0.0,
    "mean_cost": 0.0,
    "mean_efficiency": 0.0,
    "mean_composite": 0.0,
}


def ensure_learned_registry(root: Path) -> dict:
    """Ensure the learned agent registry exists and return its contents."""
    registry_path = learned_registry_path(root)
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    for dirname in ("auditors", "executors", "skills", ".archive", ".staging"):
        (registry_path.parent / dirname).mkdir(parents=True, exist_ok=True)
    if not registry_path.exists():
        registry: dict = {"version": 1, "updated_at": now_iso(), "agents": [], "benchmarks": []}
        write_json(registry_path, registry)
        return registry
    data = load_json(registry_path)
    if not isinstance(data, dict) or "agents" not in data:
        registry = {"version": 1, "updated_at": now_iso(), "agents": [], "benchmarks": []}
        write_json(registry_path, registry)
        return registry
    return data


def register_learned_agent(
    root: Path,
    *,
    agent_name: str,
    role: str,
    task_type: str,
    path: str,
    generated_from: str,
    source: str = "learned",
    item_kind: str = "agent",
) -> dict:
    """Register a new learned agent in the registry."""
    registry = ensure_learned_registry(root)
    agents = registry.setdefault("agents", [])
    existing = next(
        (
            agent
            for agent in agents
            if agent.get("agent_name") == agent_name
            and agent.get("role") == role
            and agent.get("task_type") == task_type
            and agent.get("item_kind", "agent") == item_kind
        ),
        None,
    )
    record = {
        "item_kind": item_kind,
        "agent_name": agent_name,
        "role": role,
        "task_type": task_type,
        "source": source,
        "path": path,
        "generated_from": generated_from,
        "generated_at": now_iso(),
        "mode": "shadow",
        "status": "active",
        "benchmark_summary": dict(_EMPTY_BENCHMARK_SUMMARY),
    }
    if existing:
        existing.update(record)
    else:
        agents.append(record)
    registry["updated_at"] = now_iso()
    write_json(learned_registry_path(root), registry)
    return registry


def apply_evaluation_to_registry(
    root: Path,
    agent_name: str,
    role: str,
    task_type: str,
    evaluation: dict,
    *,
    item_kind: str = "agent",
    context: Optional[dict] = None,
) -> dict:
    """Apply an evaluation result to a registry entry."""
    registry = ensure_learned_registry(root)
    matched = None
    for agent in registry.get("agents", []):
        if (
            agent.get("agent_name") == agent_name
            and agent.get("role") == role
            and agent.get("task_type") == task_type
            and agent.get("item_kind", "agent") == item_kind
        ):
            matched = agent
            break
    if matched is None:
        raise ValueError(f"Registry entry not found: {agent_name} ({role}, {task_type}, {item_kind})")
    matched["benchmark_summary"] = evaluation["candidate"]
    matched["baseline_summary"] = evaluation["baseline"]
    matched["last_evaluation"] = {
        "evaluated_at": now_iso(),
        "delta_quality": evaluation["delta_quality"],
        "delta_composite": evaluation["delta_composite"],
        "recommendation": evaluation["recommendation"],
        "blocked_by_category": evaluation.get("blocked_by_category", False),
    }
    if context:
        matched["last_evaluation"].update(context)
    matched["last_benchmarked_task_offset"] = 0
    previous_mode = matched.get("mode", "shadow")
    matched["mode"] = evaluation["target_mode"]
    matched["route_allowed"] = matched["mode"] in {"alongside", "replace"}
    if evaluation["recommendation"] == "reject" and previous_mode in {"alongside", "replace"}:
        matched["status"] = "demoted_on_regression"
        matched["route_allowed"] = False
    elif matched["mode"] == "shadow":
        matched["status"] = "active_shadow"
    else:
        matched["status"] = "active"
    benchmarks = registry.setdefault("benchmarks", [])
    benchmarks.append(
        {
            "agent_name": agent_name,
            "item_kind": item_kind,
            "role": role,
            "task_type": task_type,
            "evaluated_at": now_iso(),
            "recommendation": evaluation["recommendation"],
            "delta_quality": evaluation["delta_quality"],
            "delta_composite": evaluation["delta_composite"],
            "blocked_by_category": evaluation.get("blocked_by_category", False),
            **(context or {}),
        }
    )
    if len(benchmarks) > MAX_REGISTRY_BENCHMARKS:
        registry["benchmarks"] = benchmarks[-MAX_REGISTRY_BENCHMARKS:]
    registry["updated_at"] = now_iso()
    write_json(learned_registry_path(root), registry)
    return registry


def entry_is_stale(root: Path, entry: dict) -> tuple[bool, Optional[int], int]:
    """Determine if a registry entry is stale based on freshness window."""
    policy = benchmark_policy_config(root)
    freshness_window = int(policy.get("freshness_task_window", 5) or 5)
    offset = tasks_since(root, entry.get("last_evaluation", {}).get("source_tasks", [None])[-1] if entry.get("last_evaluation", {}).get("source_tasks") else entry.get("generated_from"))
    if offset is None:
        offset = tasks_since(root, entry.get("generated_from"))
    return (offset is not None and offset > freshness_window, offset, freshness_window)


def resolve_registry_route(root: Path, role: str, task_type: str, *, item_kind: str = "agent") -> dict:
    """Resolve a route from the learned agent registry."""
    registry = ensure_learned_registry(root)
    candidates: list[dict] = []
    freshness_blocked = False
    for item in registry.get("agents", []):
        if item.get("item_kind", "agent") != item_kind:
            continue
        if item.get("role") != role or item.get("task_type") != task_type:
            continue
        if not item.get("route_allowed", item.get("mode") in {"alongside", "replace"}):
            continue
        if item.get("status") in {"demoted_on_regression", "archived"}:
            continue
        is_stale, stale_offset, freshness_window = entry_is_stale(root, item)
        if is_stale:
            freshness_blocked = True
            continue
        candidates.append(item)
    if not candidates:
        return {
            "role": role,
            "task_type": task_type,
            "item_kind": item_kind,
            "source": "generic",
            "mode": "default",
            "path": "built-in",
            "freshness_blocked": freshness_blocked,
        }
    candidates.sort(
        key=lambda item: (
            -float(item.get("benchmark_summary", {}).get("mean_composite", 0.0)),
            item.get("agent_name", ""),
        )
    )
    chosen = candidates[0]
    return {
        "role": role,
        "task_type": task_type,
        "item_kind": item_kind,
        "source": f"learned:{chosen['agent_name']}",
        "mode": chosen.get("mode", "shadow"),
        "path": chosen.get("path"),
        "route_allowed": chosen.get("route_allowed", False),
        "status": chosen.get("status", "active"),
        "composite": chosen.get("benchmark_summary", {}).get("mean_composite", 0.0),
        "freshness_blocked": False,
    }
