#!/usr/bin/env python3
"""Anonymous cross-project statistics extraction for dynos-work.

Extracted from dynoglobal.py to reduce module size. The aggregate and
promote functions accept helper callables to avoid circular imports
back into dynoglobal.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from dynoslib import collect_retrospectives, now_iso, write_json


def extract_project_stats(project_root: Path) -> dict:
    """Extract anonymous abstract statistics from a single project.

    Returns counts by type, executor reliability rates, average quality
    scores, and prevention rule frequencies.  No file paths, task
    descriptions, or project-specific content is included.
    """
    project_root = project_root.resolve()
    retrospectives = collect_retrospectives(project_root)

    task_counts_by_type: dict[str, int] = {}
    quality_scores: list[float] = []
    executor_repair_totals: dict[str, list[float]] = {}
    prevention_rules: dict[str, int] = {}
    rule_executors: dict[str, str] = {}

    for retro in retrospectives:
        task_type = retro.get("task_type")
        if isinstance(task_type, str) and task_type:
            task_counts_by_type[task_type] = task_counts_by_type.get(task_type, 0) + 1

        qs = retro.get("quality_score")
        if isinstance(qs, (int, float)):
            quality_scores.append(float(qs))

        repair_freq = retro.get("executor_repair_frequency", {})
        if isinstance(repair_freq, dict):
            for role, count in repair_freq.items():
                if isinstance(role, str) and isinstance(count, (int, float)):
                    executor_repair_totals.setdefault(role, []).append(float(count))

        rules = retro.get("prevention_rules", [])
        if isinstance(rules, list):
            for rule in rules:
                rule_text = None
                rule_executor = "unknown"
                if isinstance(rule, str) and rule:
                    rule_text = rule
                elif isinstance(rule, dict):
                    candidate = rule.get("rule") or rule.get("text")
                    if isinstance(candidate, str) and candidate:
                        rule_text = candidate
                    rule_executor = str(rule.get("executor", "unknown"))
                if rule_text:
                    prevention_rules[rule_text] = prevention_rules.get(rule_text, 0) + 1
                    rule_executors[rule_text] = rule_executor

    total_tasks = sum(task_counts_by_type.values())
    avg_quality = (sum(quality_scores) / len(quality_scores)) if quality_scores else 0.0

    executor_reliability: dict[str, float] = {}
    for role, counts in executor_repair_totals.items():
        avg_repairs = sum(counts) / len(counts) if counts else 0.0
        executor_reliability[role] = round(max(0.0, 1.0 - avg_repairs * 0.1), 3)

    return {
        "total_tasks": total_tasks,
        "task_counts_by_type": task_counts_by_type,
        "average_quality_score": round(avg_quality, 3),
        "executor_reliability": executor_reliability,
        "prevention_rule_frequencies": prevention_rules,
        "prevention_rule_executors": rule_executors,
    }


def aggregate_cross_project_stats(
    *,
    list_projects_fn: Callable[[], list[dict]],
    ensure_global_dirs_fn: Callable[[], None],
    patterns_dir_fn: Callable[[], Path],
    log_global_fn: Callable[[str], None],
) -> dict:
    """Aggregate anonymous stats from all registered projects.

    Writes results to ~/.dynos/patterns/cross-project-stats.json keyed by
    metric name (not by project).  Returns the aggregated dict.
    """
    ensure_global_dirs_fn()
    projects = list_projects_fn()

    agg_task_counts: dict[str, int] = {}
    agg_quality_scores: list[float] = []
    agg_executor_reliability: dict[str, list[float]] = {}
    agg_prevention_rules: dict[str, int] = {}
    project_count = 0

    for proj in projects:
        proj_path = Path(proj.get("path", ""))
        if not proj_path.is_dir():
            continue
        try:
            stats = extract_project_stats(proj_path)
        except OSError:
            log_global_fn(f"failed to extract stats from project at {proj_path}")
            continue

        project_count += 1

        for task_type, count in stats.get("task_counts_by_type", {}).items():
            agg_task_counts[task_type] = agg_task_counts.get(task_type, 0) + count

        avg_q = stats.get("average_quality_score", 0.0)
        if isinstance(avg_q, (int, float)) and avg_q > 0:
            agg_quality_scores.append(float(avg_q))

        for role, rate in stats.get("executor_reliability", {}).items():
            if isinstance(rate, (int, float)):
                agg_executor_reliability.setdefault(role, []).append(float(rate))

        for rule, freq in stats.get("prevention_rule_frequencies", {}).items():
            agg_prevention_rules[rule] = agg_prevention_rules.get(rule, 0) + freq

    overall_quality = (
        round(sum(agg_quality_scores) / len(agg_quality_scores), 3)
        if agg_quality_scores
        else 0.0
    )
    reliability_means: dict[str, float] = {}
    for role, rates in agg_executor_reliability.items():
        reliability_means[role] = round(sum(rates) / len(rates), 3) if rates else 0.0

    result = {
        "aggregated_at": now_iso(),
        "project_count": project_count,
        "total_tasks": sum(agg_task_counts.values()),
        "task_counts_by_type": agg_task_counts,
        "average_quality_score": overall_quality,
        "executor_reliability": reliability_means,
        "prevention_rule_frequencies": agg_prevention_rules,
    }

    output_path = patterns_dir_fn() / "cross-project-stats.json"
    write_json(output_path, result)
    log_global_fn(json.dumps({
        "action": "aggregate_cross_project_stats",
        "projects_aggregated": project_count,
        "total_tasks": result["total_tasks"],
        "average_quality_score": result["average_quality_score"],
        "timestamp": now_iso(),
    }))
    return result


def promote_prevention_rules(
    *,
    list_projects_fn: Callable[[], list[dict]],
    ensure_global_dirs_fn: Callable[[], None],
    patterns_dir_fn: Callable[[], Path],
    log_global_fn: Callable[[str], None],
) -> dict:
    """Promote prevention rules appearing in 2+ distinct projects.

    Reads per-project stats (via extract_project_stats), finds rules that
    appear in at least 2 distinct projects, and writes them to
    ~/.dynos/patterns/global-prevention-rules.json.

    Returns the promoted rules dict.
    """
    ensure_global_dirs_fn()
    projects = list_projects_fn()

    rule_project_count: dict[str, int] = {}
    rule_executor_map: dict[str, str] = {}

    for proj in projects:
        proj_path = Path(proj.get("path", ""))
        if not proj_path.is_dir():
            continue
        try:
            stats = extract_project_stats(proj_path)
        except OSError:
            continue

        seen_in_project: set[str] = set()
        for rule in stats.get("prevention_rule_frequencies", {}):
            if isinstance(rule, str) and rule and rule not in seen_in_project:
                seen_in_project.add(rule)
                rule_project_count[rule] = rule_project_count.get(rule, 0) + 1
        for rule, executor in stats.get("prevention_rule_executors", {}).items():
            if isinstance(rule, str) and isinstance(executor, str):
                rule_executor_map.setdefault(rule, executor)

    promoted = {
        rule: count
        for rule, count in rule_project_count.items()
        if count >= 2
    }

    promotion_ts = now_iso()
    result = {
        "promoted_at": promotion_ts,
        "threshold": 2,
        "rules": [
            {
                "rule": rule,
                "executor": rule_executor_map.get(rule, "unknown"),
                "project_count": count,
                "promotion_timestamp": promotion_ts,
            }
            for rule, count in sorted(promoted.items(), key=lambda x: -x[1])
        ],
    }

    output_path = patterns_dir_fn() / "global-prevention-rules.json"
    write_json(output_path, result)
    log_global_fn(json.dumps({
        "action": "promote_prevention_rules",
        "rules_promoted": len(result["rules"]),
        "threshold": 2,
        "timestamp": promotion_ts,
    }))
    return result
