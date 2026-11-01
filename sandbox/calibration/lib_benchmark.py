#!/usr/bin/env python3
"""Benchmark evaluation and fixture management for dynos-work."""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent)); _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent.parent / "hooks"))

import json
from pathlib import Path
from typing import Optional

from lib_core import (
    COMPOSITE_WEIGHTS,
    _persistent_project_dir,
    _safe_float,
    collect_retrospectives,
    benchmark_history_path,
    benchmark_index_path,
    load_json,
    now_iso,
    write_json,
)
from lib_trajectory import retrospective_benchmark_score

MAX_BENCHMARK_HISTORY_RUNS: int = 200


def ensure_benchmark_history(root: Path) -> dict:
    """Ensure the benchmark history file exists and return its contents."""
    path = benchmark_history_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or not path.read_text().strip():
        history: dict = {"version": 1, "updated_at": now_iso(), "runs": []}
        write_json(path, history)
        return history
    data = load_json(path)
    if not isinstance(data, dict) or "runs" not in data:
        history = {"version": 1, "updated_at": now_iso(), "runs": []}
        write_json(path, history)
        return history
    return data


def ensure_benchmark_index(root: Path) -> dict:
    """Ensure the benchmark index file exists and return its contents."""
    path = benchmark_index_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or not path.read_text().strip():
        index: dict = {"version": 1, "updated_at": now_iso(), "fixtures": []}
        write_json(path, index)
        return index
    data = load_json(path)
    if not isinstance(data, dict) or "fixtures" not in data:
        index = {"version": 1, "updated_at": now_iso(), "fixtures": []}
        write_json(path, index)
        return index
    return data


def _core_benchmark_summary(benchmarks: list[dict]) -> dict:
    """Core summary stats without per_model (used by per-model grouping to avoid recursion)."""
    if not benchmarks:
        return {
            "sample_count": 0,
            "mean_quality": 0.0,
            "mean_cost": 0.0,
            "mean_efficiency": 0.0,
            "mean_composite": 0.0,
        }
    quality = sum(_safe_float(item.get("quality_score")) for item in benchmarks) / len(benchmarks)
    cost = sum(_safe_float(item.get("cost_score")) for item in benchmarks) / len(benchmarks)
    efficiency = sum(_safe_float(item.get("efficiency_score")) for item in benchmarks) / len(benchmarks)
    wq, we, wc = COMPOSITE_WEIGHTS
    composite = sum(
        wq * _safe_float(item.get("quality_score"))
        + we * _safe_float(item.get("efficiency_score"))
        + wc * _safe_float(item.get("cost_score"))
        for item in benchmarks
    ) / len(benchmarks)
    return {
        "sample_count": len(benchmarks),
        "mean_quality": round(quality, 6),
        "mean_cost": round(cost, 6),
        "mean_efficiency": round(efficiency, 6),
        "mean_composite": round(composite, 6),
    }


def compute_benchmark_summary(benchmarks: list[dict]) -> dict:
    """Compute summary statistics from a list of benchmark results.

    Returns aggregate stats plus per_model breakdown for runs that have a model field.
    """
    summary = _core_benchmark_summary(benchmarks)
    # Per-model breakdown (uses _core to avoid recursion)
    grouped: dict[str, list[dict]] = {}
    for item in benchmarks:
        model = item.get("model")
        if model:
            grouped.setdefault(model, []).append(item)
    summary["per_model"] = {
        model: _core_benchmark_summary(items)
        for model, items in grouped.items()
    }
    return summary


def compute_benchmark_summary_by_model(benchmarks: list[dict]) -> dict[str, dict]:
    """Group benchmark results by model and compute summary stats per model.

    Each benchmark item should have an optional 'model' field (str or None).
    Returns {model: summary_dict} for models with at least one sample.
    """
    grouped: dict[str, list[dict]] = {}
    for item in benchmarks:
        model = item.get("model")
        if model:
            grouped.setdefault(model, []).append(item)
    return {model: _core_benchmark_summary(items) for model, items in grouped.items()}


def _category_summaries(results: list[dict]) -> dict[str, dict]:
    """Group results by category and compute summaries for each."""
    grouped: dict[str, list[dict]] = {}
    for item in results:
        category = str(item.get("category", "default"))
        grouped.setdefault(category, []).append(item)
    return {category: compute_benchmark_summary(items) for category, items in grouped.items()}


def evaluate_candidate(candidate_results: list[dict], baseline_results: list[dict], policy: Optional[dict] = None) -> dict:
    """Evaluate a candidate agent against a baseline."""
    policy = policy or {}
    candidate = compute_benchmark_summary(candidate_results)
    baseline = compute_benchmark_summary(baseline_results)
    delta_quality = round(candidate["mean_quality"] - baseline["mean_quality"], 6)
    delta_composite = round(candidate["mean_composite"] - baseline["mean_composite"], 6)
    min_samples = int(policy.get("min_samples", 3) or 3)
    min_quality_delta = float(policy.get("min_quality_delta", 0.03) or 0.03)
    min_composite_delta = float(policy.get("min_composite_delta", 0.02) or 0.02)
    must_pass_categories = [str(item) for item in policy.get("must_pass_categories", [])]
    candidate_by_category = _category_summaries(candidate_results)
    baseline_by_category = _category_summaries(baseline_results)
    category_regressions: list[dict] = []
    for category in must_pass_categories:
        c_summary = candidate_by_category.get(category, compute_benchmark_summary([]))
        b_summary = baseline_by_category.get(category, compute_benchmark_summary([]))
        quality_delta = round(c_summary["mean_quality"] - b_summary["mean_quality"], 6)
        composite_delta = round(c_summary["mean_composite"] - b_summary["mean_composite"], 6)
        regressed = quality_delta < 0 or composite_delta < 0
        category_regressions.append(
            {
                "category": category,
                "candidate": c_summary,
                "baseline": b_summary,
                "delta_quality": quality_delta,
                "delta_composite": composite_delta,
                "regressed": regressed,
            }
        )
    blocked_by_category = any(item["regressed"] for item in category_regressions)
    recommendation = "reject"
    mode = "shadow"
    if blocked_by_category:
        recommendation = "reject"
        mode = "shadow"
    elif candidate["sample_count"] >= min_samples and delta_quality >= min_quality_delta and delta_composite >= min_composite_delta:
        recommendation = "promote_replace"
        mode = "replace"
    elif candidate["sample_count"] >= min_samples and delta_quality >= 0 and delta_composite >= 0:
        recommendation = "promote_alongside"
        mode = "alongside"
    elif candidate["sample_count"] >= 1:
        recommendation = "keep_shadow"
        mode = "shadow"
    return {
        "candidate": candidate,
        "baseline": baseline,
        "policy": {
            "min_samples": min_samples,
            "min_quality_delta": min_quality_delta,
            "min_composite_delta": min_composite_delta,
            "must_pass_categories": must_pass_categories,
        },
        "candidate_by_category": candidate_by_category,
        "baseline_by_category": baseline_by_category,
        "category_regressions": category_regressions,
        "blocked_by_category": blocked_by_category,
        "delta_quality": delta_quality,
        "delta_composite": delta_composite,
        "recommendation": recommendation,
        "target_mode": mode,
    }


def resolve_model_for_benchmark_run(root: Path, fixture: dict, role: str) -> str | None:
    """Resolve the dominant model used for *role* across benchmark cases' source tasks.

    Args:
        root: Project root path.
        fixture: Fixture dict containing a 'cases' list with source_task_id entries.
        role: The agent role to match in token-usage.json by_agent entries.

    Returns the most common model string, or None if no model data is found.
    """
    from collections import Counter
    cases = fixture.get("cases", [])
    models: list[str] = []
    for case in cases:
        task_id = case.get("source_task_id") or case.get("case_id")
        if not task_id:
            continue
        token_path = root / ".dynos" / task_id / "token-usage.json"
        if not token_path.exists():
            continue
        try:
            token_data = load_json(token_path)
        except (json.JSONDecodeError, OSError):
            continue
        by_agent = token_data.get("by_agent", {})
        # Try exact role match first, then prefix match (agent names can have segment suffixes)
        for agent_name, info in by_agent.items():
            if not isinstance(info, dict):
                continue
            m = info.get("model")
            if not m or m in ("none", "n/a", "", "unknown", "default"):
                continue
            # Match: exact role, or agent_name starts with role
            if agent_name == role or agent_name.startswith(role):
                models.append(m)
                break
    if not models:
        return None
    return Counter(models).most_common(1)[0][0]


def append_benchmark_run(root: Path, run: dict) -> dict:
    """Append a benchmark run to the history."""
    history = ensure_benchmark_history(root)
    runs = history.setdefault("runs", [])
    runs.append(run)
    if len(runs) > MAX_BENCHMARK_HISTORY_RUNS:
        history["runs"] = runs[-MAX_BENCHMARK_HISTORY_RUNS:]
    history["updated_at"] = now_iso()
    write_json(benchmark_history_path(root), history)
    return history


def upsert_fixture_trace(root: Path, fixture_record: dict) -> dict:
    """Insert or update a fixture trace in the benchmark index."""
    index = ensure_benchmark_index(root)
    fixtures = index.setdefault("fixtures", [])
    matched = None
    for item in fixtures:
        if item.get("fixture_id") == fixture_record.get("fixture_id"):
            matched = item
            break
    if matched is None:
        fixtures.append(fixture_record)
    else:
        matched.update(fixture_record)
    index["updated_at"] = now_iso()
    write_json(benchmark_index_path(root), index)
    return index


def benchmark_fixtures_dir(root: Path) -> Path:
    """Return the path to the benchmark fixtures directory."""
    return root / "benchmarks" / "fixtures"


def generated_fixtures_dir(root: Path) -> Path:
    """Directory for machine-generated fixtures (task-derived and rollout).

    Lives in the persistent project dir, not the repo: generated fixtures are
    learning state, and writing them into ``root/benchmarks/generated`` put
    untracked files into the user's project. Fixtures still in that old repo
    location are no longer read.
    """
    return _persistent_project_dir(root) / "benchmarks" / "generated"


def iter_benchmark_fixtures(root: Path) -> list[Path]:
    """Iterate over all benchmark fixture JSON files: hand-written ones in
    ``root/benchmarks/fixtures`` and generated ones in
    ``generated_fixtures_dir(root)``."""
    candidates = [benchmark_fixtures_dir(root), generated_fixtures_dir(root)]
    fixtures: list[Path] = []
    for directory in candidates:
        if directory.exists():
            fixtures.extend(path for path in directory.rglob("*.json") if path.is_file() and not path.is_symlink())
    return sorted(fixtures)


def matching_fixtures_for_registry_entry(root: Path, entry: dict) -> list[Path]:
    """Find benchmark fixtures that match a registry entry."""
    matches: list[Path] = []
    for fixture_path in iter_benchmark_fixtures(root):
        try:
            fixture = load_json(fixture_path)
        except (json.JSONDecodeError, FileNotFoundError, OSError):
            continue
        if fixture.get("item_kind", "agent") != entry.get("item_kind", "agent"):
            continue
        if fixture.get("target_name") != entry.get("agent_name"):
            continue
        if fixture.get("role") != entry.get("role"):
            continue
        if fixture.get("task_type") != entry.get("task_type"):
            continue
        matches.append(fixture_path)
    return matches


def benchmark_fixture_score(result: dict) -> dict:
    """Compute a benchmark score from a fixture result."""
    if all(key in result for key in ("quality_score", "cost_score", "efficiency_score")):
        quality = _safe_float(result.get("quality_score"))
        cost = _safe_float(result.get("cost_score"))
        efficiency = _safe_float(result.get("efficiency_score"))
        composite = result.get("composite_score")
        wq, we, wc = COMPOSITE_WEIGHTS
        if not isinstance(composite, (int, float)):
            composite = wq * quality + we * efficiency + wc * cost
        return {
            "quality_score": round(quality, 6),
            "efficiency_score": round(efficiency, 6),
            "cost_score": round(cost, 6),
            "composite_score": round(float(composite), 6),
        }
    tests_passed = int(result.get("tests_passed", 0) or 0)
    tests_total = int(result.get("tests_total", 0) or 0)
    findings = int(result.get("findings", 0) or 0)
    files_touched = int(result.get("files_touched", 0) or 0)
    duration_seconds = float(result.get("duration_seconds", 0) or 0)
    tokens_used = float(result.get("tokens_used", 0) or 0)
    quality = 1.0 if tests_total == 0 else max(0.0, min(1.0, tests_passed / max(1, tests_total)))
    quality *= 1 / (1 + findings)
    efficiency = 1 / (1 + max(0.0, duration_seconds / 300))
    cost = 1 / (1 + max(0.0, tokens_used / 50000) + max(0.0, files_touched / 40))
    wq, we, wc = COMPOSITE_WEIGHTS
    composite = wq * quality + we * efficiency + wc * cost
    return {
        "quality_score": round(quality, 6),
        "efficiency_score": round(efficiency, 6),
        "cost_score": round(cost, 6),
        "composite_score": round(composite, 6),
    }


def _learned_agent_ran(retro: dict, role: str, agent_name: str) -> bool:
    """True when the retrospective records *agent_name* running as *role*,
    in replace mode (``agent_source[role]``) or alongside mode
    (``agent_source[f"{role}:learned"]``)."""
    sources = retro.get("agent_source")
    if not isinstance(sources, dict):
        return False
    claim = f"learned:{agent_name}"
    return sources.get(role) == claim or sources.get(f"{role}:learned") == claim


def _ran_generic_only(retro: dict, role: str) -> bool:
    """True when *role* took part in the task and no learned agent ran for it."""
    models = retro.get("model_used_by_agent")
    if not isinstance(models, dict) or role not in models:
        return False
    sources = retro.get("agent_source")
    if not isinstance(sources, dict):
        return True
    return sources.get(role, "generic") == "generic" and f"{role}:learned" not in sources


def _task_case_summary(retro: dict) -> dict:
    domains = [part.strip() for part in str(retro.get("task_domains", "")).split(",") if part.strip()]
    return {
        "task_id": retro["task_id"],
        "domains": domains,
        "score": retrospective_benchmark_score(retro),
    }


def synthesize_fixture_for_entry(root: Path, entry: dict, *, limit: int = 5) -> Optional[dict]:
    """Synthesize a benchmark fixture from task retrospectives for a registry entry.

    Candidate cases are the most recent (up to *limit*) tasks of the entry's
    task_type in which this learned agent actually ran for its role, per the
    retrospective's ``agent_source``. The baseline is every task of that
    task_type in which the role ran as the generic agent. Tasks where another
    learned agent ran for the role are in neither group.

    Returns None (and removes any earlier generated fixture for the entry)
    when there are no candidate tasks or no baseline tasks. A learned agent in
    shadow mode is routed to the generic agent, so it gets no task-derived
    fixture; it needs a rollout fixture (``challenge.py``) to be benchmarked.

    This replaces a synthesis that ranked all tasks of the type by score,
    took the top five as the "candidate" and the rest as the baseline. Neither
    group had run the learned agent, and the top of a ranking beats the rest
    by construction, so every learned agent passed promotion.
    """
    task_type = entry.get("task_type")
    role = entry.get("role")
    agent_name = entry.get("agent_name")
    slug = f"{entry.get('item_kind', 'agent')}-{agent_name}-{task_type}".replace("/", "-")
    fixture_dir = generated_fixtures_dir(root)
    fixture_path = fixture_dir / f"{slug}.json"

    candidate_tasks: list[dict] = []
    baseline_pool: list[dict] = []
    if isinstance(role, str) and role and isinstance(agent_name, str) and agent_name:
        for retro in collect_retrospectives(root):
            if retro.get("task_type") != task_type or not isinstance(retro.get("task_id"), str):
                continue
            if _learned_agent_ran(retro, role, agent_name):
                candidate_tasks.append(_task_case_summary(retro))
            elif _ran_generic_only(retro, role):
                baseline_pool.append(_task_case_summary(retro))
    candidate_tasks.sort(key=lambda item: item["task_id"])
    candidate_tasks = candidate_tasks[-max(1, limit):]
    baseline_pool.sort(key=lambda item: item["task_id"])
    if not candidate_tasks or not baseline_pool:
        try:
            fixture_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        return None

    baseline_quality = sum(item["score"]["quality_score"] for item in baseline_pool) / len(baseline_pool)
    baseline_cost = sum(item["score"]["cost_score"] for item in baseline_pool) / len(baseline_pool)
    baseline_efficiency = sum(item["score"]["efficiency_score"] for item in baseline_pool) / len(baseline_pool)
    baseline_summary: dict = {
        "quality_score": round(baseline_quality, 6),
        "cost_score": round(baseline_cost, 6),
        "efficiency_score": round(baseline_efficiency, 6),
    }
    wq, we, wc = COMPOSITE_WEIGHTS
    baseline_summary["composite_score"] = round(
        wq * baseline_summary["quality_score"]
        + we * baseline_summary["efficiency_score"]
        + wc * baseline_summary["cost_score"],
        6,
    )
    fixture_cases: list[dict] = []
    for item in candidate_tasks:
        fixture_cases.append(
            {
                "case_id": item["task_id"],
                "category": item["domains"][0] if item["domains"] else "default",
                "source_task_id": item["task_id"],
                "baseline": dict(baseline_summary),
                "candidate": dict(item["score"]),
            }
        )
    fixture_dir.mkdir(parents=True, exist_ok=True)
    fixture: dict = {
        "fixture_id": slug,
        "item_kind": entry.get("item_kind", "agent"),
        "target_name": entry.get("agent_name"),
        "role": entry.get("role"),
        "task_type": task_type,
        "source_tasks": [item["task_id"] for item in candidate_tasks],
        "baseline_tasks": [item["task_id"] for item in baseline_pool],
        "synthesis": {
            "synthesized_at": now_iso(),
            "strategy": "learned_agent_tasks_vs_generic_tasks",
            "candidate_limit": len(candidate_tasks),
        },
        "policy": {
            "min_samples": 3,
            "min_quality_delta": 0.03,
            "min_composite_delta": 0.02,
        },
        "cases": fixture_cases,
    }
    fixture_path.write_text(json.dumps(fixture, indent=2) + "\n")
    upsert_fixture_trace(
        root,
        {
            "fixture_id": fixture["fixture_id"],
            "fixture_path": str(fixture_path),
            "item_kind": fixture["item_kind"],
            "target_name": fixture["target_name"],
            "role": fixture["role"],
            "task_type": fixture["task_type"],
            "source_tasks": fixture["source_tasks"],
            "baseline_tasks": fixture["baseline_tasks"],
            "synthesized_at": fixture["synthesis"]["synthesized_at"],
            "strategy": fixture["synthesis"]["strategy"],
        },
    )
    return fixture
