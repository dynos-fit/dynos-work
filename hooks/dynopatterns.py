#!/usr/bin/env python3
"""Generate dynos_patterns.md from live runtime data."""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import argparse
import json
from pathlib import Path

from dynoslib import collect_retrospectives, ensure_learned_registry, now_iso, _persistent_project_dir, load_json, write_json

DEFAULT_TASK_TYPES = ["feature", "bugfix", "refactor", "migration", "ml", "full-stack"]
DEFAULT_EXECUTOR_ROLES = [
    "ui-executor",
    "backend-executor",
    "ml-executor",
    "db-executor",
    "refactor-executor",
    "testing-executor",
    "integration-executor",
]
DEFAULT_AUDITOR_ROLES = [
    "ui-auditor",
    "db-schema-auditor",
    "dead-code-auditor",
    "security-auditor",
    "spec-completion-auditor",
    "code-quality-auditor",
]
SKIP_EXEMPT_AUDITORS = {"security-auditor", "spec-completion-auditor", "code-quality-auditor"}


def project_slug(root: Path) -> str:
    return str(root.resolve()).replace("/", "-")


def local_patterns_path(root: Path) -> Path:
    return _persistent_project_dir(root) / "dynos_patterns.md"


def claude_patterns_path(root: Path) -> Path:
    return Path.home() / ".claude" / "projects" / project_slug(root) / "memory" / "dynos_patterns.md"


def pattern_paths(root: Path) -> list[Path]:
    paths = [local_patterns_path(root), claude_patterns_path(root)]
    unique: list[Path] = []
    for path in paths:
        if path not in unique:
            unique.append(path)
    return unique


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


def _build_model_policy(
    task_types: list[str],
    executor_roles: list[str],
    auditor_roles: list[str],
    model_policy_data: dict[str, dict],
) -> list[str]:
    """Render model policy markdown from pre-computed data."""
    lines = [
        "## Model Policy",
        "",
        "| Role | Task Type | Recommended Model |",
        "|------|-----------|-------------------|",
    ]
    for role in sorted(set(executor_roles + auditor_roles)):
        for task_type in task_types:
            key = f"{role}:{task_type}"
            entry = model_policy_data.get(key)
            model = entry["model"] if entry else "default"
            lines.append(f"| {role} | {task_type} | {model} |")
    return lines


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
        confidence = min(0.99, round(avg_streak / 5, 2))
        result[role] = {
            "threshold": threshold,
            "confidence": confidence,
        }
    return result


def _build_skip_policy(
    auditor_roles: list[str],
    skip_policy_data: dict[str, dict],
) -> list[str]:
    """Render skip policy markdown from pre-computed data."""
    lines = [
        "## Skip Policy",
        "",
        "| Auditor | Skip Threshold | Confidence |",
        "|---------|----------------|------------|",
    ]
    for role in auditor_roles:
        if role in SKIP_EXEMPT_AUDITORS:
            continue
        entry = skip_policy_data.get(role, {"threshold": 3, "confidence": 0.0})
        lines.append(f"| {role} | {entry['threshold']} | {entry['confidence']:.2f} |")
    return lines


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


def _build_agent_routing(
    task_types: list[str],
    executor_roles: list[str],
    auditor_roles: list[str],
    registry: dict,
    route_policy_data: dict[str, dict],
) -> list[str]:
    """Render agent routing markdown from pre-computed data."""
    lines = [
        "## Agent Routing",
        "",
    ]
    generated_from = [
        str(item.get("generated_from"))
        for item in registry.get("agents", [])
        if isinstance(item.get("generated_from"), str) and item.get("generated_from")
    ]
    last_generation = max(generated_from) if generated_from else "none"
    lines.extend(
        [
            f"Last generation: {last_generation}",
            "",
            "| Role | Task Type | Agent Source | Agent Path | Composite Score | Mode |",
            "|------|-----------|-------------|------------|-----------------|------|",
        ]
    )
    seen: set[tuple[str, str]] = set()
    for item in registry.get("agents", []):
        role = str(item.get("role", ""))
        task_type = str(item.get("task_type", ""))
        if not role or not task_type:
            continue
        seen.add((role, task_type))
        source = f"learned:{item.get('agent_name')}"
        path = str(item.get("path", ""))
        composite = float(item.get("benchmark_summary", {}).get("mean_composite", 0.0) or 0.0)
        mode = str(item.get("mode", "shadow"))
        lines.append(f"| {role} | {task_type} | {source} | {path} | {composite:.3f} | {mode} |")
    for role in sorted(set(executor_roles + auditor_roles)):
        for task_type in task_types:
            if (role, task_type) in seen:
                continue
            lines.append(f"| {role} | {task_type} | built-in | built-in | 0.000 | generic |")
    return lines


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
    """Build the dynos_patterns.md content.

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
        "# dynos-work Patterns",
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
        "",
        "## Gold Standard Instances",
        "",
        "| Task ID | Type | Why It Matters |",
        "|---------|------|----------------|",
    ]
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
    lines.extend(
        [""]
        + _build_model_policy(task_types, executor_roles, auditor_roles, model_policy_data)
    )
    lines.extend([""] + _build_skip_policy(auditor_roles, skip_policy_data))
    lines.extend(
        [""]
        + _build_agent_routing(
            task_types, executor_roles, auditor_roles, registry, route_policy_data
        )
    )
    return "\n".join(lines) + "\n"


def write_patterns(root: Path) -> dict:
    """Generate policy data, write JSON files, then render and write markdown."""
    # Step 1: Collect source data
    retrospectives = collect_retrospectives(root)
    registry = ensure_learned_registry(root)
    task_types = _observed_task_types(retrospectives, registry)
    executor_roles = _observed_executor_roles(retrospectives, registry)
    auditor_roles = _observed_auditor_roles(retrospectives, registry)

    # Step 2: Compute policy data structures
    model_policy_data = _build_model_policy_data(retrospectives)
    skip_policy_data = _build_skip_policy_data(retrospectives, auditor_roles)
    route_policy_data = _build_route_policy_data(
        task_types, executor_roles, auditor_roles, registry
    )

    # Step 3: Migrate model_overrides from policy.json (merges with computed data)
    model_policy_data = _migrate_model_overrides(root, model_policy_data)

    # Step 4: Write JSON policy files
    _write_policy_json_files(root, model_policy_data, skip_policy_data, route_policy_data)

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
    return {
        "written_at": now_iso(),
        "written_paths": written,
        "failed_paths": failed,
        "local_path": str(local_patterns_path(root)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    result = write_patterns(Path(args.root).resolve())
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
