#!/usr/bin/env python3
"""Deterministic routing decisions for dynos-work.

Reads project-local policy, patterns, and learned-agent registry.
Returns structured spawn decisions. No prompt interpretation needed.
"""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import argparse
import json
from pathlib import Path

from dynoslib import (
    _persistent_project_dir,
    _safe_float,
    collect_retrospectives,
    ensure_learned_registry,
    load_json,
    now_iso,
    project_policy,
)


# ---------------------------------------------------------------------------
# Model selection
# ---------------------------------------------------------------------------

SECURITY_FLOOR_MODEL = "opus"
DEFAULT_MODEL = None  # None means "use whatever the caller's default is"


def _read_policy_json(root: Path, filename: str, key: str) -> dict | None:
    """Read a value from a JSON policy file in the persistent project dir.

    Returns the value dict for *key* if found, None otherwise.
    Gracefully handles missing files and corrupt JSON.
    """
    try:
        policy_path = _persistent_project_dir(root) / filename
        data = json.loads(policy_path.read_text())
        if isinstance(data, dict) and key in data:
            return data[key]
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return None


def resolve_model(root: Path, role: str, task_type: str) -> dict:
    """Determine which model an agent should use.

    Priority order:
      1. policy.json model_overrides  -> source: "explicit_policy"
      2. model-policy.json            -> source: "learned_history"
      3. dynos_patterns.md markdown   -> source: "learned_history"
      4. no match                     -> source: "default"
      5. security floor enforcement   -> source: "security_floor"

    Returns {"model": str|None, "source": str}.
    """
    policy = project_policy(root)
    key = f"{role}:{task_type}"

    # 1. Explicit policy.json overrides (highest priority)
    model_overrides = policy.get("model_overrides", {})
    model = model_overrides.get(key) or model_overrides.get(role)
    if model:
        source = "explicit_policy"
    else:
        source = "default"

        # 2. JSON policy file (learned history)
        entry = _read_policy_json(root, "model-policy.json", key)
        if entry and isinstance(entry, dict) and entry.get("model"):
            model = entry["model"]
            source = "learned_history"

        # 3. Markdown fallback
        if not model:
            patterns_path = _persistent_project_dir(root) / "dynos_patterns.md"
            if patterns_path.exists():
                try:
                    model = _parse_model_from_patterns(patterns_path, role, task_type)
                    if model and model != "default":
                        source = "learned_history"
                    else:
                        model = None
                except (OSError, ValueError):
                    pass

    # Security floor: security-auditor never below opus
    if role == "security-auditor":
        if not model or model in ("haiku", "sonnet"):
            model = SECURITY_FLOOR_MODEL
            source = "security_floor"

    return {"model": model, "source": source}


def _parse_model_from_patterns(path: Path, role: str, task_type: str) -> str | None:
    """Parse Model Policy table from dynos_patterns.md."""
    text = path.read_text()
    in_table = False
    for line in text.splitlines():
        if "## Model Policy" in line:
            in_table = True
            continue
        if in_table and line.startswith("## "):
            break
        if not in_table or not line.startswith("|") or "---" in line or "Role" in line:
            continue
        parts = [p.strip() for p in line.split("|") if p.strip()]
        if len(parts) >= 3 and parts[0] == role and parts[1] == task_type:
            model = parts[2]
            return model if model != "default" else None
    return None


# ---------------------------------------------------------------------------
# Skip decisions
# ---------------------------------------------------------------------------

SKIP_EXEMPT = {"security-auditor", "spec-completion-auditor", "code-quality-auditor"}
DEFAULT_SKIP_THRESHOLD = 3


def resolve_skip(root: Path, auditor: str, task_type: str) -> dict:
    """Determine whether an auditor should be skipped.

    Returns {"skip": bool, "reason": str, "streak": int, "threshold": int}.
    """
    if auditor in SKIP_EXEMPT:
        return {"skip": False, "reason": "skip-exempt", "streak": 0, "threshold": 0}

    # Get streak from most recent prior task
    retros = collect_retrospectives(root)
    streak = 0
    if retros:
        latest = retros[-1]
        streaks = latest.get("auditor_zero_finding_streaks", {})
        if isinstance(streaks, dict):
            streak = int(streaks.get(auditor, 0) or 0)

    # Get threshold from patterns or policy
    threshold = _get_skip_threshold(root, auditor)

    skip = streak >= threshold
    reason = f"streak {streak} >= threshold {threshold}" if skip else f"streak {streak} < threshold {threshold}"
    return {"skip": skip, "reason": reason, "streak": streak, "threshold": threshold}


def _get_skip_threshold(root: Path, auditor: str) -> int:
    """Read skip threshold for *auditor*.

    Priority: skip-policy.json -> dynos_patterns.md markdown -> DEFAULT_SKIP_THRESHOLD.
    """
    # 1. JSON policy file
    entry = _read_policy_json(root, "skip-policy.json", auditor)
    if entry and isinstance(entry, dict) and "threshold" in entry:
        return int(entry["threshold"])

    # 2. Markdown fallback
    patterns_path = _persistent_project_dir(root) / "dynos_patterns.md"
    if patterns_path.exists():
        try:
            text = patterns_path.read_text()
            in_table = False
            for line in text.splitlines():
                if "## Skip Policy" in line:
                    in_table = True
                    continue
                if in_table and line.startswith("## "):
                    break
                if not in_table or not line.startswith("|") or "---" in line or "Auditor" in line:
                    continue
                parts = [p.strip() for p in line.split("|") if p.strip()]
                if len(parts) >= 2 and parts[0] == auditor:
                    return int(parts[1])
        except (OSError, ValueError):
            pass

    return DEFAULT_SKIP_THRESHOLD


# ---------------------------------------------------------------------------
# Agent routing
# ---------------------------------------------------------------------------


def resolve_route(root: Path, role: str, task_type: str) -> dict:
    """Determine whether to use generic, learned, or alongside agent.

    Returns {
        "mode": "generic"|"learned"|"alongside",
        "agent_path": str|None,
        "agent_name": str|None,
        "composite_score": float,
        "source": str
    }.
    """
    registry = ensure_learned_registry(root)
    agents = registry.get("agents", [])

    # Find matching learned agent
    learned = None
    for agent in agents:
        if (
            agent.get("role") == role
            and agent.get("task_type") == task_type
            and agent.get("status") not in ("archived", "demoted_on_regression")
        ):
            learned = agent
            break

    if not learned:
        return {
            "mode": "generic",
            "agent_path": None,
            "agent_name": None,
            "composite_score": 0.0,
            "source": "no learned agent",
        }

    mode = learned.get("mode", "shadow")
    composite = float(learned.get("benchmark_summary", {}).get("mean_composite", 0.0) or 0.0)
    agent_path = learned.get("path", "")
    agent_name = learned.get("agent_name", "")

    # Security-auditor can never be replaced
    if role == "security-auditor" and mode == "replace":
        mode = "alongside"

    # Shadow mode means it's not yet proven — use generic
    if mode == "shadow":
        return {
            "mode": "generic",
            "agent_path": agent_path,
            "agent_name": agent_name,
            "composite_score": composite,
            "source": f"shadow (not yet promoted): {agent_name}",
        }

    # Path validation — resolve against persistent dir
    if agent_path:
        p = Path(agent_path)
        # Try as absolute first, then persistent dir, then repo-relative
        if p.is_absolute():
            full_path = p
        else:
            persistent_root = _persistent_project_dir(root) / "learned-agents"
            full_path = persistent_root / p.name
            if not full_path.exists():
                # Try relative to persistent learned-agents
                full_path = persistent_root / p
            if not full_path.exists():
                # Last resort: repo-relative
                full_path = root / p
        if not full_path.exists():
            return {
                "mode": "generic",
                "agent_path": None,
                "agent_name": agent_name,
                "composite_score": composite,
                "source": f"learned agent file not found: {agent_path}",
            }
            agent_path = str(full_path)

    return {
        "mode": mode,
        "agent_path": agent_path,
        "agent_name": agent_name,
        "composite_score": composite,
        "source": f"learned:{agent_name}",
    }


# ---------------------------------------------------------------------------
# Full spawn plan
# ---------------------------------------------------------------------------

def load_prevention_rules(root: Path) -> list[dict]:
    """Load project-local prevention rules from persistent storage."""
    rules_path = _persistent_project_dir(root) / "prevention-rules.json"
    if not rules_path.exists():
        return []
    try:
        data = load_json(rules_path)
        return data.get("rules", [])
    except (json.JSONDecodeError, FileNotFoundError, OSError):
        return []


AUDITOR_ROLES = [
    "spec-completion-auditor",
    "security-auditor",
    "code-quality-auditor",
    "dead-code-auditor",
    "ui-auditor",
    "db-schema-auditor",
]


def build_audit_plan(root: Path, task_type: str, domains: list[str], fast_track: bool = False) -> dict:
    """Build a complete, deterministic audit spawn plan.

    Returns a structured dict that tells the caller exactly:
    - which auditors to spawn
    - which to skip
    - what model each should use
    - whether to use generic or learned prompt

    No prompt interpretation needed. The caller just follows the plan.
    """
    plan = {
        "generated_at": now_iso(),
        "task_type": task_type,
        "domains": domains,
        "fast_track": fast_track,
        "auditors": [],
    }

    # Determine which auditors are eligible
    if fast_track:
        eligible = ["spec-completion-auditor", "security-auditor"]
    else:
        eligible = ["spec-completion-auditor", "security-auditor", "code-quality-auditor", "dead-code-auditor"]
        if "ui" in domains:
            eligible.append("ui-auditor")
        if "db" in domains:
            eligible.append("db-schema-auditor")

    for auditor in eligible:
        # Skip check
        skip_decision = resolve_skip(root, auditor, task_type)
        if skip_decision["skip"]:
            plan["auditors"].append({
                "name": auditor,
                "action": "skip",
                "reason": skip_decision["reason"],
                "streak": skip_decision["streak"],
                "threshold": skip_decision["threshold"],
            })
            continue

        # Model selection
        model_decision = resolve_model(root, auditor, task_type)

        # Fast-track model override: haiku for spec-completion
        if fast_track and auditor == "spec-completion-auditor" and model_decision["source"] == "default":
            model_decision = {"model": "haiku", "source": "fast_track_override"}

        # Route selection
        route_decision = resolve_route(root, auditor, task_type)

        plan["auditors"].append({
            "name": auditor,
            "action": "spawn",
            "model": model_decision["model"],
            "model_source": model_decision["source"],
            "route_mode": route_decision["mode"],
            "route_source": route_decision["source"],
            "agent_path": route_decision["agent_path"],
            "agent_name": route_decision["agent_name"],
            "composite_score": route_decision["composite_score"],
        })

    return plan


def build_executor_plan(root: Path, task_type: str, segments: list[dict]) -> dict:
    """Build a complete, deterministic execution spawn plan.

    Returns structured decisions for each segment's executor.
    """
    all_rules = load_prevention_rules(root)
    plan = {
        "generated_at": now_iso(),
        "task_type": task_type,
        "segments": [],
    }

    for seg in segments:
        executor = seg.get("executor", "")
        seg_id = seg.get("id", "")

        model_decision = resolve_model(root, executor, task_type)
        route_decision = resolve_route(root, executor, task_type)

        # Filter prevention rules relevant to this executor
        executor_rules = [r["rule"] for r in all_rules if r.get("rule")]

        plan["segments"].append({
            "segment_id": seg_id,
            "executor": executor,
            "model": model_decision["model"],
            "model_source": model_decision["source"],
            "route_mode": route_decision["mode"],
            "route_source": route_decision["source"],
            "agent_path": route_decision["agent_path"],
            "agent_name": route_decision["agent_name"],
            "composite_score": route_decision["composite_score"],
            "prevention_rules": executor_rules,
        })

    return plan


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def cmd_audit_plan(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    domains = [d.strip() for d in args.domains.split(",") if d.strip()] if args.domains else []
    plan = build_audit_plan(root, args.task_type, domains, fast_track=args.fast_track)
    print(json.dumps(plan, indent=2))
    return 0


def cmd_executor_plan(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    graph_path = Path(args.graph)
    if not graph_path.exists():
        print(json.dumps({"error": f"graph not found: {graph_path}"}))
        return 1
    graph = load_json(graph_path)
    plan = build_executor_plan(root, args.task_type, graph.get("segments", []))
    print(json.dumps(plan, indent=2))
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    result = {
        "model": resolve_model(root, args.role, args.task_type),
        "skip": resolve_skip(root, args.role, args.task_type),
        "route": resolve_route(root, args.role, args.task_type),
    }
    print(json.dumps(result, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    ap = subparsers.add_parser("audit-plan", help="Build deterministic audit spawn plan")
    ap.add_argument("--root", default=".")
    ap.add_argument("--task-type", required=True)
    ap.add_argument("--domains", default="")
    ap.add_argument("--fast-track", action="store_true")
    ap.set_defaults(func=cmd_audit_plan)

    ep = subparsers.add_parser("executor-plan", help="Build deterministic executor spawn plan")
    ep.add_argument("--root", default=".")
    ep.add_argument("--task-type", required=True)
    ep.add_argument("--graph", required=True)
    ep.set_defaults(func=cmd_executor_plan)

    res = subparsers.add_parser("resolve", help="Resolve model/skip/route for one role")
    res.add_argument("role")
    res.add_argument("task_type")
    res.add_argument("--root", default=".")
    res.set_defaults(func=cmd_resolve)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
