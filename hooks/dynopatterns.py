#!/usr/bin/env python3
"""Generate dynos_patterns.md from live runtime data."""

from __future__ import annotations

import argparse
from pathlib import Path

from dynoslib import collect_retrospectives, ensure_learned_registry, now_iso

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
    return root / ".dynos" / "dynos_patterns.md"


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


def _build_model_policy(task_types: list[str], executor_roles: list[str], auditor_roles: list[str]) -> list[str]:
    lines = [
        "## Model Policy",
        "",
        "| Role | Task Type | Recommended Model |",
        "|------|-----------|-------------------|",
    ]
    for role in sorted(set(executor_roles + auditor_roles)):
        for task_type in task_types:
            lines.append(f"| {role} | {task_type} | default |")
    return lines


def _build_skip_policy(retrospectives: list[dict], auditor_roles: list[str]) -> list[str]:
    streaks: dict[str, list[float]] = {}
    for item in retrospectives:
        for role, value in item.get("auditor_zero_finding_streaks", {}).items():
            if isinstance(role, str) and isinstance(value, (int, float)):
                streaks.setdefault(role, []).append(float(value))
    lines = [
        "## Skip Policy",
        "",
        "| Auditor | Skip Threshold | Confidence |",
        "|---------|----------------|------------|",
    ]
    for role in auditor_roles:
        if role in SKIP_EXEMPT_AUDITORS:
            continue
        avg_streak = _mean(streaks.get(role, []))
        threshold = max(3, min(6, int(round(avg_streak)) or 3))
        confidence = min(0.99, round(avg_streak / 5, 2))
        lines.append(f"| {role} | {threshold} | {confidence:.2f} |")
    return lines


def _build_agent_routing(task_types: list[str], executor_roles: list[str], auditor_roles: list[str], registry: dict) -> list[str]:
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


def build_patterns_markdown(root: Path) -> str:
    retrospectives = collect_retrospectives(root)
    registry = ensure_learned_registry(root)
    task_types = _observed_task_types(retrospectives, registry)
    executor_roles = _observed_executor_roles(retrospectives, registry)
    auditor_roles = _observed_auditor_roles(retrospectives, registry)
    active_routes = sum(1 for item in registry.get("agents", []) if item.get("route_allowed"))
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
    top_tasks = sorted(
        (
            item
            for item in retrospectives
            if isinstance(item.get("task_id"), str) and isinstance(item.get("task_type"), str)
        ),
        key=lambda item: (
            -float(item.get("quality_score", 0.0) or 0.0),
            -float(item.get("efficiency_score", 0.0) or 0.0),
            str(item.get("task_id")),
        ),
    )[:5]
    if top_tasks:
        for item in top_tasks:
            task_id = str(item.get("task_id"))
            task_type = str(item.get("task_type"))
            score = float(item.get("quality_score", 0.0) or 0.0)
            lines.append(f"| {task_id} | {task_type} | High-quality completed task (quality {score:.2f}). |")
    else:
        lines.append("| none | n/a | No completed retrospectives available yet. |")
    lines.extend([""] + _build_model_policy(task_types, executor_roles, auditor_roles))
    lines.extend([""] + _build_skip_policy(retrospectives, auditor_roles))
    lines.extend([""] + _build_agent_routing(task_types, executor_roles, auditor_roles, registry))
    return "\n".join(lines) + "\n"


def write_patterns(root: Path) -> dict:
    content = build_patterns_markdown(root)
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
    print(__import__("json").dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
