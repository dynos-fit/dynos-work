#!/usr/bin/env python3
"""Policy engine — computes routing policies from task retrospectives."""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent)); _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "hooks"))

import argparse
import json
from pathlib import Path

from lib_core import collect_retrospectives, now_iso, _persistent_project_dir, load_json, write_json, VALID_EXECUTORS
from lib_log import log_event
from lib_registry import ensure_learned_registry

DEFAULT_TASK_TYPES = ["feature", "bugfix", "refactor", "migration", "ml", "full-stack"]
DEFAULT_EXECUTOR_ROLES = sorted(VALID_EXECUTORS)  # auto-discovered from agents/
DEFAULT_AUDITOR_ROLES = [
    "ui-auditor",
    "db-schema-auditor",
    "dead-code-auditor",
    "security-auditor",
    "spec-completion-auditor",
    "code-quality-auditor",
]
SKIP_EXEMPT_AUDITORS = {"security-auditor", "spec-completion-auditor", "code-quality-auditor"}
VALID_MODELS = {"haiku", "sonnet", "opus"}
from lib_defaults import (
    EMA_ALPHA,
    EMA_COLD_START_MINIMUM,
    EMA_MAX_EFFECTIVENESS_ROWS,
    EMA_TIE_BREAKING_THRESHOLD,
    ROUTER_WEIGHT_QUALITY,
    ROUTER_WEIGHT_COST,
    ROUTER_WEIGHT_EFFICIENCY,
    ROUTING_WEIGHT_QUALITY,
    ROUTING_WEIGHT_EFFICIENCY,
    ROUTING_WEIGHT_COST,
    SKIP_THRESHOLD_BASE,
    SKIP_THRESHOLD_SLOPE,
    SKIP_THRESHOLD_MIN,
    SKIP_THRESHOLD_MAX,
    CONFIDENCE_MAX_CLAMP,
    UCB_COLD_START_MINIMUM,
)
COMPOSITE_WEIGHTS = (ROUTER_WEIGHT_QUALITY, ROUTER_WEIGHT_COST, ROUTER_WEIGHT_EFFICIENCY)
MODEL_COST_ORDER = {"haiku": 0, "sonnet": 1, "opus": 2}
# Backwards compat aliases
COLD_START_MINIMUM = EMA_COLD_START_MINIMUM
MAX_EFFECTIVENESS_ROWS = EMA_MAX_EFFECTIVENESS_ROWS


def project_slug(root: Path) -> str:
    return str(root.resolve()).replace("/", "-")


def local_patterns_path(root: Path) -> Path:
    return _persistent_project_dir(root) / "project_rules.md"


def claude_patterns_path(root: Path) -> Path:
    return Path.home() / ".claude" / "projects" / project_slug(root) / "memory" / "project_rules.md"


def pattern_paths(root: Path) -> list[Path]:
    paths = [local_patterns_path(root), claude_patterns_path(root)]
    unique: list[Path] = []
    for path in paths:
        if path not in unique:
            unique.append(path)
    return unique


def _load_prevention_rules(root: Path) -> list[dict]:
    """Load learned prevention rules written by postmortem_improve."""
    rules_path = _persistent_project_dir(root) / "prevention-rules.json"
    try:
        data = load_json(rules_path)
        if isinstance(data, dict):
            return data.get("rules", [])
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return []


def _observed_task_types(retrospectives: list[dict], registry: dict) -> list[str]:
    task_types = {
        str(item.get("task_type"))
        for item in retrospectives
        if isinstance(item.get("task_type"), str) and item.get("task_type")
    }
    task_types.update(
        str(item.get("task_type"))
        for item in registry.get("agents", [])
        if isinstance(item.get("task_type"), str) and item.get("task_type")
    )
    if not task_types:
        task_types.update(DEFAULT_TASK_TYPES)
    return sorted(task_types)


def _observed_executor_roles(retrospectives: list[dict], registry: dict) -> list[str]:
    roles = set(DEFAULT_EXECUTOR_ROLES)
    for item in retrospectives:
        for role in item.get("executor_repair_frequency", {}):
            if isinstance(role, str) and role:
                roles.add(role)
    for item in registry.get("agents", []):
        role = item.get("role")
        if isinstance(role, str) and role.endswith("-executor"):
            roles.add(role)
    return sorted(roles)


def _observed_auditor_roles(retrospectives: list[dict], registry: dict) -> list[str]:
    roles = set(DEFAULT_AUDITOR_ROLES)
    for item in retrospectives:
        for role in item.get("auditor_zero_finding_streaks", {}):
            if isinstance(role, str) and role:
                roles.add(role)
        for role in item.get("findings_by_auditor", {}):
            if isinstance(role, str) and role.endswith("-auditor"):
                roles.add(role)
    for item in registry.get("agents", []):
        role = item.get("role")
        if isinstance(role, str) and role.endswith("-auditor"):
            roles.add(role)
    return sorted(roles)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


# ---------------------------------------------------------------------------
# EMA computation (deterministic, replaces inline SKILL.md math)
# ---------------------------------------------------------------------------

QuadKey = tuple[str, str, str, str]  # (role, model, task_type, source)


def _build_events_by_task(
    root: Path,
    task_ids: set[str] | None = None,
) -> dict[str, list[dict]]:
    """Build {task_id -> [event, ...]} by streaming `.dynos/events.jsonl`.

    When ``task_ids`` is provided, events whose ``task_id`` is NOT in the
    set are dropped during streaming. This caps memory at O(events-for-
    needed-tasks) instead of O(all-events) — essential on long-lived
    projects where events.jsonl grows unbounded (PERF-001).

    When ``task_ids`` is None, all events with a string task_id are
    retained (legacy behavior). Callers that know their retrospective set
    should always pass the filter.

    Malformed lines are silently skipped. Entries without a string
    ``task_id`` key are skipped. When the file does not exist, returns an
    empty dict — callers that want the legacy (no-cross-check) path of
    :func:`_extract_quads` should pass ``events_by_task=None`` explicitly;
    passing ``{}`` means "cross-check opted-in; no events; everything
    unmatched."
    """
    events_by_task: dict[str, list[dict]] = {}
    events_path = root / ".dynos" / "events.jsonl"
    if not events_path.exists():
        return events_by_task
    try:
        with events_path.open("r", encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                task_id = entry.get("task_id")
                if not isinstance(task_id, str) or not task_id:
                    continue
                if task_ids is not None and task_id not in task_ids:
                    continue
                events_by_task.setdefault(task_id, []).append(entry)
    except OSError:
        # File became unreadable after exists() — treat as "no events."
        return {}
    return events_by_task


def _extract_quads(
    retrospectives: list[dict],
    events_by_task: dict[str, list[dict]] | None = None,
    root: Path | None = None,
) -> list[tuple[QuadKey, float, float, float]]:
    """Extract (quad_key, quality, cost, efficiency) from validated retrospectives.

    Sorted by task_id ascending for deterministic replay.

    When ``events_by_task`` is not None, each quad whose
    ``agent_source[role]`` starts with ``"learned:"`` is cross-checked
    against the matching task's event stream. A quad is verified iff at
    least one entry in ``events_by_task.get(retro["task_id"], [])`` has
    ``event == "learned_agent_applied"`` AND ``agent_name == <claimed>``
    AND (``segment_id == role`` OR
    ``segment_id == role.removeprefix("audit-")``). Unmatched quads have
    their ``source`` rewritten to ``"generic"``; when ``root`` is also
    provided, a single ``agent_source_reclassified`` event is emitted via
    :func:`log_event` per rewritten quad. When ``events_by_task is None``,
    the cross-check is skipped entirely (legacy path).
    """
    # PERF-002: collect reclassification events during extraction and
    # flush them once after the loop completes. Prevents the synchronous
    # open+append+close per reclassified quad inside the hot path — which
    # at N retros * M roles (all learned-unmatched) would fire N*M serial
    # file writes before any quad is returned.
    _reclass_events: list[dict] = []
    # Filter and validate
    valid = []
    for retro in retrospectives:
        q = retro.get("quality_score")
        c = retro.get("cost_score")
        e = retro.get("efficiency_score")
        if not all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in (q, c, e) if v is not None):
            continue
        if q is None or c is None or e is None:
            continue
        models = retro.get("model_used_by_agent", {})
        if not isinstance(models, dict):
            continue
        task_type = retro.get("task_type")
        if not isinstance(task_type, str) or not task_type:
            continue
        valid.append(retro)

    valid.sort(key=lambda r: str(r.get("task_id", "")))

    quads: list[tuple[QuadKey, float, float, float]] = []
    for retro in valid:
        q = float(retro["quality_score"])
        c = float(retro["cost_score"])
        e = float(retro["efficiency_score"])
        task_type = retro["task_type"]
        models = retro.get("model_used_by_agent", {})
        sources = retro.get("agent_source", {})
        task_id = retro.get("task_id")
        task_events = (
            events_by_task.get(task_id, [])
            if events_by_task is not None and isinstance(task_id, str)
            else []
        )

        for role, model in models.items():
            if not isinstance(model, str) or model not in VALID_MODELS:
                continue
            source = sources.get(role, "generic") if isinstance(sources, dict) else "generic"

            # Cross-check learned:X claims against events.jsonl (fix E).
            # Only active when caller opted in via events_by_task != None.
            if (
                events_by_task is not None
                and isinstance(source, str)
                and source.startswith("learned:")
            ):
                claimed_agent = source[len("learned:"):]
                role_stripped = role[len("audit-"):] if role.startswith("audit-") else role
                matched = False
                for entry in task_events:
                    if not isinstance(entry, dict):
                        continue
                    if entry.get("event") != "learned_agent_applied":
                        continue
                    if entry.get("agent_name") != claimed_agent:
                        continue
                    seg_id = entry.get("segment_id")
                    if seg_id == role or seg_id == role_stripped:
                        matched = True
                        break
                if not matched:
                    original_source = source
                    source = "generic"
                    if root is not None:
                        _reclass_events.append({
                            "task_id": task_id,
                            "role": role,
                            "original_source": original_source,
                            "reclassified_to": "generic",
                            "reason": "no_matching_learned_agent_applied_event",
                        })

            quads.append(((role, model, task_type, source), q, c, e))

    # Flush batched reclassification events after the extraction loop.
    if root is not None and _reclass_events:
        for ev in _reclass_events:
            log_event(root, "agent_source_reclassified", **ev)

    return quads


def compute_effectiveness_scores(
    retrospectives: list[dict],
    baseline: dict[QuadKey, tuple[float, float, float]] | None = None,
    root: Path | None = None,
) -> list[dict]:
    """Compute EMA effectiveness scores from retrospectives.

    Returns list of dicts with: role, model, task_type, source,
    quality_ema, cost_ema, efficiency_ema, sample_count, updated.

    When ``root`` is provided, this function builds ``events_by_task`` once
    from ``<root>/.dynos/events.jsonl`` and passes it to
    :func:`_extract_quads` so that retrospective ``learned:X`` claims are
    cross-checked against the event stream. Unmatched claims are
    reclassified to ``"generic"`` with an ``agent_source_reclassified``
    event. When ``events.jsonl`` does not exist, an empty dict is still
    passed (cross-check runs with zero signal — every ``learned:X`` gets
    reclassified). Legacy callers that omit ``root`` skip the cross-check
    entirely.
    """
    if root is not None:
        # PERF-001: pre-filter events.jsonl to only the task_ids that
        # appear in this retrospective set. Bounds memory at O(events-
        # for-retros) instead of O(all-events-ever-emitted-by-project).
        needed_ids: set[str] = {
            r["task_id"]
            for r in retrospectives
            if isinstance(r, dict) and isinstance(r.get("task_id"), str)
        }
        events_by_task: dict[str, list[dict]] | None = _build_events_by_task(
            root, task_ids=needed_ids
        )
    else:
        events_by_task = None
    quads = _extract_quads(retrospectives, events_by_task=events_by_task, root=root)

    # EMA state per quad
    state: dict[QuadKey, dict] = {}
    # Track consecutive quality drops for regression detection
    prev_quality: dict[QuadKey, float] = {}
    drop_streak: dict[QuadKey, int] = {}

    for key, q, c, e in quads:
        if key not in state:
            # Cold-start: alpha = 1.0
            state[key] = {
                "quality_ema": q,
                "cost_ema": c,
                "efficiency_ema": e,
                "sample_count": 1,
            }
            prev_quality[key] = q
            drop_streak[key] = 0
        else:
            s = state[key]
            # Update: alpha = 0.3
            s["quality_ema"] = EMA_ALPHA * q + (1 - EMA_ALPHA) * s["quality_ema"]
            s["cost_ema"] = EMA_ALPHA * c + (1 - EMA_ALPHA) * s["cost_ema"]
            s["efficiency_ema"] = EMA_ALPHA * e + (1 - EMA_ALPHA) * s["efficiency_ema"]
            s["sample_count"] += 1

            # Regression detection: 2+ consecutive quality drops
            if q < prev_quality.get(key, q):
                drop_streak[key] = drop_streak.get(key, 0) + 1
            else:
                drop_streak[key] = 0
            prev_quality[key] = q

            if drop_streak[key] >= 2 and baseline:
                bq, bc, be = baseline.get(key, (s["quality_ema"], s["cost_ema"], s["efficiency_ema"]))
                s["quality_ema"] = EMA_ALPHA * bq + (1 - EMA_ALPHA) * s["quality_ema"]
                s["cost_ema"] = EMA_ALPHA * bc + (1 - EMA_ALPHA) * s["cost_ema"]
                s["efficiency_ema"] = EMA_ALPHA * be + (1 - EMA_ALPHA) * s["efficiency_ema"]

            # Clamp to [0, 1]
            s["quality_ema"] = max(0.0, min(1.0, s["quality_ema"]))
            s["cost_ema"] = max(0.0, min(1.0, s["cost_ema"]))
            s["efficiency_ema"] = max(0.0, min(1.0, s["efficiency_ema"]))

    ts = now_iso()
    rows = []
    for (role, model, task_type, source), s in state.items():
        rows.append({
            "role": role,
            "model": model,
            "task_type": task_type,
            "source": source,
            "quality_ema": round(s["quality_ema"], 4),
            "cost_ema": round(s["cost_ema"], 4),
            "efficiency_ema": round(s["efficiency_ema"], 4),
            "sample_count": s["sample_count"],
            "updated": ts,
        })

    # Row cap: keep most recent 50 by sample_count descending
    rows.sort(key=lambda r: (-r["sample_count"], r["role"], r["model"]))
    return rows[:MAX_EFFECTIVENESS_ROWS]


def derive_model_policy(effectiveness_scores: list[dict]) -> dict[str, dict]:
    """Derive Model Policy from effectiveness scores using composite scoring.

    Returns {role:task_type -> {model, confidence, updated}}.
    """
    # Group by (role, task_type), aggregate across source
    groups: dict[tuple[str, str], dict[str, list[dict]]] = {}
    for row in effectiveness_scores:
        key = (row["role"], row["task_type"])
        groups.setdefault(key, {}).setdefault(row["model"], []).append(row)

    result: dict[str, dict] = {}
    for (role, task_type), model_rows in groups.items():
        candidates = []
        for model, rows in model_rows.items():
            if model not in VALID_MODELS:
                continue
            # Weighted average across sources
            total_samples = sum(r["sample_count"] for r in rows)
            if total_samples == 0:
                continue
            q = sum(r["quality_ema"] * r["sample_count"] for r in rows) / total_samples
            c = sum(r["cost_ema"] * r["sample_count"] for r in rows) / total_samples
            e = sum(r["efficiency_ema"] * r["sample_count"] for r in rows) / total_samples
            wq, wc, we = COMPOSITE_WEIGHTS
            composite = wq * q + wc * c + we * e
            candidates.append({
                "model": model,
                "composite": composite,
                "quality_ema": q,
                "efficiency_ema": e,
                "sample_count": total_samples,
            })

        if not candidates:
            continue

        # Deterministic ranking
        candidates.sort(key=lambda x: (
            -x["composite"],
            -x["quality_ema"],
            -x["efficiency_ema"],
            MODEL_COST_ORDER.get(x["model"], 99),
            x["model"],
        ))

        # Tie-breaking: if top two within threshold, prefer higher quality
        if len(candidates) >= 2:
            if abs(candidates[0]["composite"] - candidates[1]["composite"]) < EMA_TIE_BREAKING_THRESHOLD:
                if candidates[1]["quality_ema"] > candidates[0]["quality_ema"]:
                    candidates[0], candidates[1] = candidates[1], candidates[0]

        winner = candidates[0]
        model = winner["model"]

        # Monotonicity: security-auditor always opus
        if role == "security-auditor" and model != "opus":
            model = "opus"

        # Confidence
        confidence = min(1.0, winner["quality_ema"] * min(1.0, winner["sample_count"] / UCB_COLD_START_MINIMUM))
        confidence = max(0.0, min(1.0, confidence))

        policy_key = f"{role}:{task_type}"
        result[policy_key] = {
            "model": model,
            "confidence": round(confidence, 4),
            "updated": now_iso(),
        }

    return result


def derive_skip_policy(effectiveness_scores: list[dict]) -> dict[str, dict]:
    """Derive Skip Policy from effectiveness scores.

    Returns {auditor -> {threshold, confidence, updated}}.
    """
    # Average quality_ema per auditor across all quads
    auditor_quality: dict[str, list[float]] = {}
    for row in effectiveness_scores:
        role = row["role"]
        if not role.endswith("-auditor"):
            continue
        if role in SKIP_EXEMPT_AUDITORS:
            continue
        auditor_quality.setdefault(role, []).append(row["quality_ema"])

    result: dict[str, dict] = {}
    for role, qualities in auditor_quality.items():
        avg_quality = _mean(qualities)
        threshold = round(SKIP_THRESHOLD_BASE + SKIP_THRESHOLD_SLOPE * (1 - avg_quality))
        threshold = max(SKIP_THRESHOLD_MIN, min(SKIP_THRESHOLD_MAX, threshold))
        result[role] = {
            "threshold": threshold,
            "confidence": round(avg_quality, 4),
            "updated": now_iso(),
        }

    return result


def compute_routing_composite(effectiveness_scores: list[dict]) -> dict[str, float]:
    """Compute routing composite for each (role, task_type, source).

    Uses calibration weights: quality + efficiency + cost.
    Returns {role:task_type:source -> composite_score}.
    """
    result: dict[str, float] = {}
    for row in effectiveness_scores:
        key = f"{row['role']}:{row['task_type']}:{row['source']}"
        composite = ROUTING_WEIGHT_QUALITY * row["quality_ema"] + ROUTING_WEIGHT_EFFICIENCY * row["efficiency_ema"] + ROUTING_WEIGHT_COST * row["cost_ema"]
        result[key] = round(composite, 4)
    return result


def _build_model_policy_data(
    retrospectives: list[dict],
) -> dict[str, dict]:
    """Compute model policy data: {role:task_type -> {model, sample_count, mean_quality}}.

    Only includes entries with >= 2 observations (enough signal to recommend).
    """
    observations: dict[tuple[str, str], list[tuple[str, float]]] = {}
    for retro in retrospectives:
        task_type = retro.get("task_type")
        quality = retro.get("quality_score")
        models = retro.get("model_used_by_agent", {})
        if not isinstance(task_type, str) or not isinstance(models, dict):
            continue
        if not isinstance(quality, (int, float)):
            quality = 0.0
        for role, model in models.items():
            if isinstance(model, str) and model:
                observations.setdefault((role, task_type), []).append((model, float(quality)))
    result: dict[str, dict] = {}
    for (role, task_type), obs_list in observations.items():
        model_scores: dict[str, list[float]] = {}
        for model, quality in obs_list:
            model_scores.setdefault(model, []).append(quality)
        ranked = sorted(model_scores.items(), key=lambda x: -_mean(x[1]))
        if ranked and len(ranked[0][1]) >= 2:
            best_model = ranked[0][0]
            scores = ranked[0][1]
            key = f"{role}:{task_type}"
            result[key] = {
                "model": best_model,
                "sample_count": len(scores),
                "mean_quality": round(_mean(scores), 4),
            }
    return result


def _build_skip_policy_data(
    retrospectives: list[dict],
    auditor_roles: list[str],
) -> dict[str, dict]:
    """Compute skip policy data: {auditor -> {threshold, confidence}}.

    Skip-exempt auditors are excluded.
    """
    streaks: dict[str, list[float]] = {}
    for item in retrospectives:
        for role, value in item.get("auditor_zero_finding_streaks", {}).items():
            if isinstance(role, str) and isinstance(value, (int, float)):
                streaks.setdefault(role, []).append(float(value))
    result: dict[str, dict] = {}
    for role in auditor_roles:
        if role in SKIP_EXEMPT_AUDITORS:
            continue
        avg_streak = _mean(streaks.get(role, []))
        threshold = max(3, min(6, int(round(avg_streak)) or 3))
        confidence = min(CONFIDENCE_MAX_CLAMP, round(avg_streak / UCB_COLD_START_MINIMUM, 2))
        result[role] = {
            "threshold": threshold,
            "confidence": confidence,
        }
    return result


def _build_route_policy_data(
    task_types: list[str],
    executor_roles: list[str],
    auditor_roles: list[str],
    registry: dict,
) -> dict[str, dict]:
    """Compute route policy data: {role:task_type -> {mode, agent_path, agent_name, composite_score}}."""
    result: dict[str, dict] = {}
    for item in registry.get("agents", []):
        role = str(item.get("role", ""))
        task_type = str(item.get("task_type", ""))
        if not role or not task_type:
            continue
        key = f"{role}:{task_type}"
        composite = float(item.get("benchmark_summary", {}).get("mean_composite", 0.0) or 0.0)
        result[key] = {
            "mode": str(item.get("mode", "shadow")),
            "agent_path": str(item.get("path", "")),
            "agent_name": str(item.get("agent_name", "")),
            "composite_score": round(composite, 4),
        }
    for role in sorted(set(executor_roles + auditor_roles)):
        for task_type in task_types:
            key = f"{role}:{task_type}"
            if key not in result:
                result[key] = {
                    "mode": "generic",
                    "agent_path": None,
                    "agent_name": None,
                    "composite_score": 0.0,
                }
    return result


def _migrate_model_overrides(root: Path, model_policy_data: dict[str, dict]) -> dict[str, dict]:
    """Migrate model_overrides from policy.json into model-policy.json data.

    Entries from policy.json are seeded with source "explicit_policy".
    Existing explicit_policy entries in model-policy.json are preserved.
    After successful migration, model_overrides is removed from policy.json.
    Returns the merged model policy data.
    """
    persistent = _persistent_project_dir(root)
    policy_path = persistent / "policy.json"
    mp_path = persistent / "model-policy.json"

    # Load existing model-policy.json to preserve explicit_policy entries
    existing_mp: dict[str, dict] = {}
    if mp_path.exists():
        try:
            existing_mp = load_json(mp_path)
        except (json.JSONDecodeError, OSError):
            existing_mp = {}

    # Load policy.json for migration
    policy: dict = {}
    if policy_path.exists():
        try:
            policy = load_json(policy_path)
        except (json.JSONDecodeError, OSError):
            policy = {}

    overrides = policy.get("model_overrides", {})

    # Start with computed data
    merged = dict(model_policy_data)

    # Layer on migrated overrides with source "explicit_policy",
    # but do not overwrite existing explicit_policy entries
    for key, model in overrides.items():
        if isinstance(model, str) and key not in existing_mp:
            merged[key] = {
                "model": model,
                "source": "explicit_policy",
                "sample_count": 0,
                "mean_quality": 0.0,
            }

    # Preserve existing explicit_policy entries from model-policy.json
    for key, entry in existing_mp.items():
        if isinstance(entry, dict) and entry.get("source") == "explicit_policy":
            merged[key] = entry

    # Remove model_overrides from policy.json after migration
    if "model_overrides" in policy:
        del policy["model_overrides"]
        write_json(policy_path, policy)

    return merged


def _write_policy_json_files(
    root: Path,
    model_policy_data: dict[str, dict],
    skip_policy_data: dict[str, dict],
    route_policy_data: dict[str, dict],
) -> None:
    """Write all three JSON policy files atomically."""
    persistent = _persistent_project_dir(root)
    write_json(persistent / "model-policy.json", model_policy_data)
    write_json(persistent / "skip-policy.json", skip_policy_data)
    write_json(persistent / "route-policy.json", route_policy_data)


def build_patterns_markdown(
    root: Path,
    *,
    model_policy_data: dict[str, dict] | None = None,
    skip_policy_data: dict[str, dict] | None = None,
    route_policy_data: dict[str, dict] | None = None,
) -> str:
    """Build the project_rules.md content.

    When policy data dicts are provided, uses them directly (data-first path).
    When called without them, computes the data inline (backward compat).
    """
    retrospectives = collect_retrospectives(root)
    registry = ensure_learned_registry(root)
    task_types = _observed_task_types(retrospectives, registry)
    executor_roles = _observed_executor_roles(retrospectives, registry)
    auditor_roles = _observed_auditor_roles(retrospectives, registry)

    if model_policy_data is None:
        model_policy_data = _build_model_policy_data(retrospectives)
    if skip_policy_data is None:
        skip_policy_data = _build_skip_policy_data(retrospectives, auditor_roles)
    if route_policy_data is None:
        route_policy_data = _build_route_policy_data(
            task_types, executor_roles, auditor_roles, registry
        )

    active_routes = sum(
        1 for item in registry.get("agents", []) if item.get("route_allowed")
    )
    lines = [
        "# Project Rules",
        "",
        f"Generated at: {now_iso()}",
        f"Source task count: {len(retrospectives)}",
        f"Learned component count: {len(registry.get('agents', []))}",
        f"Active learned routes: {active_routes}",
        "",
        "## Prevention Rules",
        "",
        "| Executor | Rule | Source |",
        "|----------|------|--------|",
        "| ui-executor | Validate visual intent against existing dashboard structure before broad styling changes. | default |",
        "| backend-executor | Prefer narrower edits with explicit acceptance coverage and test verification. | default |",
        "| testing-executor | Capture failing expectations before broad test rewrites. | default |",
    ]
    # Append learned prevention rules from postmortem_improve
    learned_rules = _load_prevention_rules(root)
    for rule in learned_rules:
        cat = rule.get("category", "unknown")
        text = rule.get("rule", "")
        lines.append(f"| all | {text} | learned:{cat} |")
    lines.extend([
        "",
        "## Gold Standard Instances",
        "",
        "| Task ID | Type | Why It Matters |",
        "|---------|------|----------------|",
    ])
    scored_tasks = [
        item
        for item in retrospectives
        if isinstance(item.get("task_id"), str)
        and isinstance(item.get("task_type"), str)
        and isinstance(item.get("quality_score"), (int, float))
    ]
    top_tasks = sorted(
        scored_tasks,
        key=lambda item: (
            -float(item.get("quality_score", 0.0)),
            -float(item.get("efficiency_score", 0.0) or 0.0),
            str(item.get("task_id")),
        ),
    )[:5]
    if top_tasks:
        for item in top_tasks:
            task_id = str(item.get("task_id"))
            task_type = str(item.get("task_type"))
            score = float(item.get("quality_score", 0.0))
            lines.append(
                f"| {task_id} | {task_type} | High-quality completed task (quality {score:.2f}). |"
            )
    else:
        lines.append("| none | n/a | No completed retrospectives available yet. |")
    # Data tables (effectiveness scores, model policy, skip policy, agent routing)
    # are NOT included in the markdown. They are written as JSON files
    # (model-policy.json, skip-policy.json, route-policy.json) which the
    # router reads directly. Keeping them out of the markdown avoids
    # injecting ~60 lines of numeric tables into the LLM context window
    # where they serve no purpose (the LLM can't act on EMA scores).

    return "\n".join(lines) + "\n"


def write_patterns(root: Path) -> dict:
    """Generate policy data, write JSON files, then render and write markdown."""
    steps_completed: list[str] = []

    # Step 1: Collect source data
    retrospectives = collect_retrospectives(root)
    registry = ensure_learned_registry(root)
    task_types = _observed_task_types(retrospectives, registry)
    executor_roles = _observed_executor_roles(retrospectives, registry)
    auditor_roles = _observed_auditor_roles(retrospectives, registry)

    if not retrospectives:
        log_event(root, "learn_step_failed", step="collect", reason="no retrospectives found")
        return {"written_at": now_iso(), "written_paths": [], "failed_paths": [], "error": "no retrospectives"}

    steps_completed.append(f"collect:{len(retrospectives)} retros")
    log_event(root, "learn_step", step="collect", retrospective_count=len(retrospectives),
              task_types=list(task_types), executor_roles=list(executor_roles), auditor_roles=list(auditor_roles))

    # Step 2: Compute effectiveness scores (EMA) and derive policies.
    # Pass `root` so the EMA ingest cross-checks retrospective
    # `learned:X` agent_source claims against .dynos/events.jsonl.
    effectiveness_scores = compute_effectiveness_scores(retrospectives, root=root)
    ema_model_policy = derive_model_policy(effectiveness_scores)
    ema_skip_policy = derive_skip_policy(effectiveness_scores)

    steps_completed.append(f"ema:{len(effectiveness_scores)} scores, {len(ema_model_policy)} model, {len(ema_skip_policy)} skip")
    log_event(root, "learn_step", step="ema_compute", effectiveness_count=len(effectiveness_scores),
              model_policy_count=len(ema_model_policy), skip_policy_count=len(ema_skip_policy))

    # Step 2b: Legacy policy computation (backward compat, feeds markdown tables)
    model_policy_data = _build_model_policy_data(retrospectives)
    skip_policy_data = _build_skip_policy_data(retrospectives, auditor_roles)

    # Merge EMA-derived policies into JSON files (EMA takes precedence)
    for key, entry in ema_model_policy.items():
        if key not in model_policy_data:
            model_policy_data[key] = {"model": entry["model"], "sample_count": 0, "mean_quality": 0.0}
        model_policy_data[key]["model"] = entry["model"]
    for key, entry in ema_skip_policy.items():
        skip_policy_data[key] = entry
    route_policy_data = _build_route_policy_data(
        task_types, executor_roles, auditor_roles, registry
    )

    # Step 3: Migrate model_overrides from policy.json (merges with computed data)
    model_policy_data = _migrate_model_overrides(root, model_policy_data)

    steps_completed.append(f"route:{len(route_policy_data)} routes")

    # Step 4: Write JSON policy files + effectiveness scores
    _write_policy_json_files(root, model_policy_data, skip_policy_data, route_policy_data)
    persistent = _persistent_project_dir(root)
    write_json(persistent / "effectiveness-scores.json", effectiveness_scores)
    steps_completed.append("json_written")
    log_event(root, "learn_step", step="write_json_policies",
              model_count=len(model_policy_data), skip_count=len(skip_policy_data), route_count=len(route_policy_data))

    # Step 5: Render markdown from the same data
    content = build_patterns_markdown(
        root,
        model_policy_data=model_policy_data,
        skip_policy_data=skip_policy_data,
        route_policy_data=route_policy_data,
    )

    # Step 6: Write markdown files
    written: list[str] = []
    failed: list[dict] = []
    for path in pattern_paths(root):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            written.append(str(path))
        except OSError as exc:
            failed.append({"path": str(path), "error": str(exc)})
    steps_completed.append(f"markdown:{len(written)} written, {len(failed)} failed")
    log_event(root, "patterns_written", written_count=len(written), failed_count=len(failed),
              retrospective_count=len(retrospectives), local_path=str(local_patterns_path(root)),
              steps_completed=steps_completed)
    return {
        "written_at": now_iso(),
        "written_paths": written,
        "failed_paths": failed,
        "local_path": str(local_patterns_path(root)),
    }


def cmd_write_patterns(args: argparse.Namespace) -> int:
    result = write_patterns(Path(args.root).resolve())
    print(json.dumps(result, indent=2))
    return 0


def cmd_effectiveness(args: argparse.Namespace) -> int:
    """Compute and print effectiveness scores from retrospectives."""
    root = Path(args.root).resolve()
    retrospectives = collect_retrospectives(root)
    scores = compute_effectiveness_scores(retrospectives, root=root)
    output = {
        "effectiveness_scores": scores,
        "model_policy": derive_model_policy(scores),
        "skip_policy": derive_skip_policy(scores),
        "routing_composites": compute_routing_composite(scores),
    }
    print(json.dumps(output, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")

    # Default (no subcommand): write patterns
    parser.add_argument("--root", default=".")
    parser.set_defaults(func=cmd_write_patterns)

    eff = sub.add_parser("effectiveness", help="Compute effectiveness scores from retrospectives")
    eff.add_argument("--root", default=".")
    eff.set_defaults(func=cmd_effectiveness)

    return parser


if __name__ == "__main__":
    from cli_base import cli_main
    raise SystemExit(cli_main(build_parser))
