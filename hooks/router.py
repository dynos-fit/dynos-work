#!/usr/bin/env python3
"""Deterministic routing decisions for dynos-work.

Reads project-local policy, patterns, and learned-agent registry.
Returns structured spawn decisions. No prompt interpretation needed.
"""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import argparse
import json
import math
from pathlib import Path

from lib_core import (
    _persistent_project_dir,
    _safe_float,
    benchmark_history_path,
    collect_retrospectives,
    is_learning_enabled,
    load_json,
    now_iso,
    project_policy,
)
from lib_defaults import (
    DEFAULT_MODEL as _DEFAULT_MODEL_CONST,
    DEFAULT_SKIP_THRESHOLD as _DEFAULT_SKIP_THRESHOLD,
    ROUTER_WEIGHT_COST,
    ROUTER_WEIGHT_EFFICIENCY,
    ROUTER_WEIGHT_QUALITY,
    UCB_COLD_START_MINIMUM,
    UCB_EXPLORATION_CONSTANT,
)
from lib_log import log_event
from lib_registry import ensure_learned_registry


# ---------------------------------------------------------------------------
# Router context — cached data for a single plan build
# ---------------------------------------------------------------------------

class RouterContext:
    """Pre-loads all data needed for routing decisions once per plan build.

    Eliminates redundant file reads: policy, patterns, retrospectives,
    and registry are each read exactly once and shared across all
    resolve_model / resolve_route / resolve_skip calls.
    """

    def __init__(self, root: Path):
        self.root = root
        self._policy: dict | None = None
        self._patterns_text: str | None = None
        self._retrospectives: list[dict] | None = None
        self._registry: dict | None = None
        self._learning: bool | None = None

    @property
    def policy(self) -> dict:
        if self._policy is None:
            self._policy = project_policy(self.root)
        return self._policy

    @property
    def patterns_text(self) -> str | None:
        """Deprecated — data tables removed from markdown. Use effectiveness_scores instead."""
        if self._patterns_text is None:
            path = _persistent_project_dir(self.root) / "project_rules.md"
            try:
                self._patterns_text = path.read_text()
            except (FileNotFoundError, OSError):
                self._patterns_text = ""
        return self._patterns_text or None

    @property
    def effectiveness_scores(self) -> list[dict]:
        """Read effectiveness scores from JSON (no longer parsed from markdown)."""
        if not hasattr(self, "_effectiveness"):
            path = _persistent_project_dir(self.root) / "effectiveness-scores.json"
            try:
                data = load_json(path)
                self._effectiveness = data if isinstance(data, list) else []
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                self._effectiveness = []
        return self._effectiveness

    @property
    def retrospectives(self) -> list[dict]:
        if self._retrospectives is None:
            self._retrospectives = collect_retrospectives(self.root)
        return self._retrospectives

    @property
    def registry(self) -> dict:
        if self._registry is None:
            self._registry = _read_learned_registry(self.root)
        return self._registry

    @property
    def learning_enabled(self) -> bool:
        if self._learning is None:
            self._learning = is_learning_enabled(self.root)
        return self._learning


def _read_learned_registry(root: Path) -> dict:
    """Read the learned agent registry without creating files (pure read)."""
    from lib_registry import learned_registry_path
    path = learned_registry_path(root)
    if not path.exists():
        return {"agents": [], "benchmarks": []}
    try:
        data = load_json(path)
        if isinstance(data, dict) and "agents" in data:
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {"agents": [], "benchmarks": []}


# ---------------------------------------------------------------------------
# Exploration
# ---------------------------------------------------------------------------

import random

VALID_MODELS = ["haiku", "sonnet", "opus"]
DEFAULT_EPSILON = 0.1  # 10% exploration rate


# ---------------------------------------------------------------------------
# Model selection
# ---------------------------------------------------------------------------

SECURITY_FLOOR_MODEL = "opus"
DEFAULT_MODEL = _DEFAULT_MODEL_CONST  # "opus" — from lib_defaults.py


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


COMPOSITE_WEIGHTS = (ROUTER_WEIGHT_QUALITY, ROUTER_WEIGHT_COST, ROUTER_WEIGHT_EFFICIENCY)
DEFAULT_UCB_C = UCB_EXPLORATION_CONSTANT
COLD_START_MINIMUM = UCB_COLD_START_MINIMUM


def _parse_effectiveness_scores(
    path: Path, role: str, task_type: str,
) -> list[dict]:
    """Parse the Effectiveness Scores table from a file path."""
    try:
        text = path.read_text()
    except OSError:
        return []
    return _parse_effectiveness_scores_from_text(text, role, task_type)


def _parse_effectiveness_scores_from_text(
    text: str, role: str, task_type: str,
) -> list[dict]:
    """Parse the Effectiveness Scores table for a given role and task_type.

    Returns a list of dicts with keys: model, quality, cost, efficiency, samples.
    Aggregates across source values (generic + learned) per model.
    """
    rows: dict[str, dict] = {}  # keyed by model

    in_table = False
    for line in text.splitlines():
        if "## Effectiveness Scores" in line:
            in_table = True
            continue
        if in_table and line.startswith("## "):
            break
        if not in_table or not line.startswith("|") or "---" in line or "Role" in line:
            continue
        parts = [p.strip() for p in line.split("|") if p.strip()]
        # Columns: Role, Model, Task Type, Source, Quality EMA, Cost EMA, Efficiency EMA, Sample Count, Updated
        if len(parts) < 8:
            continue
        if parts[0] != role or parts[2] != task_type:
            continue

        m = parts[1]
        try:
            quality = float(parts[4])
            cost = float(parts[5])
            efficiency = float(parts[6])
            samples = int(float(parts[7]))
        except (ValueError, IndexError):
            continue

        if m in rows:
            # Aggregate: weighted average by sample count
            existing = rows[m]
            total = existing["samples"] + samples
            if total > 0:
                w_old = existing["samples"] / total
                w_new = samples / total
                existing["quality"] = existing["quality"] * w_old + quality * w_new
                existing["cost"] = existing["cost"] * w_old + cost * w_new
                existing["efficiency"] = existing["efficiency"] * w_old + efficiency * w_new
                existing["samples"] = total
        else:
            rows[m] = {
                "model": m,
                "quality": quality,
                "cost": cost,
                "efficiency": efficiency,
                "samples": max(samples, 1),
            }

    return list(rows.values())


def _filter_effectiveness_scores(
    scores: list[dict], role: str, task_type: str,
) -> list[dict]:
    """Filter and aggregate effectiveness scores for a given role and task_type.

    Reads from the JSON effectiveness-scores.json (no markdown parsing).
    Aggregates across source values (generic + learned) per model.
    """
    rows: dict[str, dict] = {}
    for entry in scores:
        if entry.get("role") != role or entry.get("task_type") != task_type:
            continue
        m = entry.get("model", "")
        if not m or m not in ("haiku", "sonnet", "opus"):
            continue
        try:
            quality = float(entry.get("quality_ema", 0))
            cost = float(entry.get("cost_ema", 0))
            efficiency = float(entry.get("efficiency_ema", 0))
            samples = int(entry.get("sample_count", 1))
        except (ValueError, TypeError):
            continue

        if m in rows:
            existing = rows[m]
            total = existing["samples"] + samples
            if total > 0:
                w_old = existing["samples"] / total
                w_new = samples / total
                existing["quality"] = existing["quality"] * w_old + quality * w_new
                existing["cost"] = existing["cost"] * w_old + cost * w_new
                existing["efficiency"] = existing["efficiency"] * w_old + efficiency * w_new
                existing["samples"] = total
        else:
            rows[m] = {
                "model": m,
                "quality": quality,
                "cost": cost,
                "efficiency": efficiency,
                "samples": max(samples, 1),
            }
    return list(rows.values())


def _ucb_select_model(
    candidates: list[dict], exploration_c: float,
) -> dict | None:
    """Select the best model using UCB1.

    Each candidate has: model, quality, cost, efficiency, samples.
    Returns the winning candidate dict with added ucb_score and exploration_bonus,
    or None if no candidates.
    """
    if not candidates:
        return None

    total_pulls = sum(c["samples"] for c in candidates)
    if total_pulls < COLD_START_MINIMUM:
        return None

    best = None
    best_ucb = -1.0

    for c in candidates:
        wq, wc, we = COMPOSITE_WEIGHTS
        composite = wq * c["quality"] + wc * c["cost"] + we * c["efficiency"]

        if c["samples"] > 0 and total_pulls > 0:
            exploration = exploration_c * math.sqrt(math.log(total_pulls) / c["samples"])
        else:
            exploration = float("inf")  # untried arm gets maximum exploration

        ucb_score = composite + exploration

        if ucb_score > best_ucb:
            best_ucb = ucb_score
            best = {**c, "ucb_score": round(ucb_score, 4), "exploration_bonus": round(exploration, 4)}

    return best


BENCHMARK_MODEL_MIN_SAMPLES = 2


def _benchmark_model_for_agent(root: Path, role: str, task_type: str) -> dict | None:
    """Find the best model for a role based on learned agent benchmark runs.

    Looks up the active learned agent for (role, task_type), then filters
    benchmark history runs for that agent. Groups by model, computes mean
    composite score, and returns the best model with >= BENCHMARK_MODEL_MIN_SAMPLES.

    Returns {"model": str, "mean_composite": float, "sample_count": int} or None.
    """
    registry = _read_learned_registry(root)
    # Find matching active learned agent
    agent_name = None
    for agent in registry.get("agents", []):
        if (
            agent.get("role") == role
            and agent.get("task_type") == task_type
            and agent.get("status") not in ("archived", "demoted_on_regression")
            and agent.get("mode") in ("replace", "alongside")
        ):
            agent_name = agent.get("agent_name")
            break
    if not agent_name:
        return None

    # Load benchmark history and filter for this agent
    try:
        history = load_json(benchmark_history_path(root))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    runs = history.get("runs", [])

    # Group composite scores by model for matching runs
    model_scores: dict[str, list[float]] = {}
    wq, wc, we = COMPOSITE_WEIGHTS
    for run in runs:
        if run.get("target_name") != agent_name:
            continue
        if run.get("role") != role or run.get("task_type") != task_type:
            continue
        model = run.get("model")
        if not model:
            continue
        # Compute composite from run-level scores or evaluation candidate
        q = _safe_float(run.get("quality_score"))
        c = _safe_float(run.get("cost_score"))
        e = _safe_float(run.get("efficiency_score"))
        if q or c or e:
            composite = wq * q + wc * c + we * e
        else:
            evaluation = run.get("evaluation", {})
            candidate = evaluation.get("candidate", {})
            composite = candidate.get("mean_composite")
        if composite is None:
            continue
        model_scores.setdefault(model, []).append(float(composite))

    # Pick the model with highest mean composite (min samples required)
    best_model = None
    best_composite = -1.0
    best_count = 0
    for model, scores in model_scores.items():
        if len(scores) < BENCHMARK_MODEL_MIN_SAMPLES:
            continue
        mean = sum(scores) / len(scores)
        if mean > best_composite:
            best_composite = mean
            best_model = model
            best_count = len(scores)

    if best_model is None:
        return None
    return {
        "model": best_model,
        "source": "benchmark_model",
        "mean_composite": round(best_composite, 4),
        "sample_count": best_count,
    }


def resolve_model(root: Path, role: str, task_type: str, ctx: RouterContext | None = None) -> dict:
    """Determine which model an agent should use.

    Priority order:
      0.  Epsilon-greedy exploration       -> source: "exploration"
      1.  policy.json model_overrides     -> source: "explicit_policy"
      2.  UCB1 over effectiveness scores  -> source: "ucb"
      2b. Benchmark model performance     -> source: "benchmark_model"
      3.  model-policy.json fallback      -> source: "learned_history"
      4.  Patterns markdown table         -> source: "learned_history"
      5.  No match                        -> source: "default"
      *   Security floor enforcement      -> source: "security_floor"

    Returns {"model": str|None, "source": str, ...}.
    """
    policy = ctx.policy if ctx else project_policy(root)
    key = f"{role}:{task_type}"

    # 1. Explicit policy.json overrides (highest priority — never overridden)
    model_overrides = policy.get("model_overrides", {})
    model = model_overrides.get(key) or model_overrides.get(role)
    if model:
        result = {"model": model, "source": "explicit_policy"}
        log_event(root, "router_model_decision", role=role, task_type=task_type, model=result["model"], source=result["source"])
        return result

    # 1b. Epsilon-greedy exploration — randomly try a different model
    # to feed the UCB1 bandit with multi-arm data. Fires AFTER explicit
    # policy (user config always wins) but BEFORE learned data tiers.
    epsilon = float(policy.get("exploration_epsilon", DEFAULT_EPSILON))
    if (
        epsilon > 0
        and role != "security-auditor"
        and (ctx.learning_enabled if ctx else is_learning_enabled(root))
        and random.random() < epsilon
    ):
        model = random.choice([m for m in VALID_MODELS if m != SECURITY_FLOOR_MODEL])
        result = {"model": model, "source": "exploration", "epsilon": epsilon}
        log_event(root, "router_model_decision", role=role, task_type=task_type, model=model, source="exploration")
        return result

    # Steps 2-4 use learned data — skip when learning is disabled.
    if not (ctx.learning_enabled if ctx else is_learning_enabled(root)):
        result = {"model": DEFAULT_MODEL, "source": "default"}
        log_event(root, "router_model_decision", role=role, task_type=task_type, model=result["model"], source="default (learning_enabled=false)")
        return result

    # 2. UCB1 over effectiveness scores (read from JSON, not markdown)
    if ctx:
        all_scores = ctx.effectiveness_scores
    else:
        try:
            all_scores = load_json(_persistent_project_dir(root) / "effectiveness-scores.json")
            if not isinstance(all_scores, list):
                all_scores = []
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            all_scores = []
    if all_scores:
        candidates = _filter_effectiveness_scores(all_scores, role, task_type)
        if candidates:
            exploration_c = float(policy.get("ucb_exploration_constant", DEFAULT_UCB_C))
            winner = _ucb_select_model(candidates, exploration_c)
            if winner:
                model = winner["model"]
                source = "ucb"
                result = {"model": model, "source": source,
                          "ucb_score": winner["ucb_score"],
                          "exploration_bonus": winner["exploration_bonus"]}
                # Security floor: security-auditor never below opus
                if role == "security-auditor" and model in ("haiku", "sonnet"):
                    result["model"] = SECURITY_FLOOR_MODEL
                    result["source"] = "security_floor"
                log_event(root, "router_model_decision", role=role, task_type=task_type, model=result["model"], source=result["source"])
                return result

    # 2b. Benchmark model performance — learned agent benchmark runs grouped by model
    benchmark_result = _benchmark_model_for_agent(root, role, task_type)
    if benchmark_result:
        model = benchmark_result["model"]
        result = {"model": model, "source": "benchmark_model",
                  "mean_composite": benchmark_result["mean_composite"],
                  "sample_count": benchmark_result["sample_count"]}
        # Security floor: security-auditor never below opus
        if role == "security-auditor" and model in ("haiku", "sonnet"):
            result["model"] = SECURITY_FLOOR_MODEL
            result["source"] = "security_floor"
        log_event(root, "router_model_decision", role=role, task_type=task_type, model=result["model"], source=result["source"])
        return result

    # 3. model-policy.json fallback (backward compat for pre-UCB projects)
    entry = _read_policy_json(root, "model-policy.json", key)
    if entry and isinstance(entry, dict) and entry.get("model"):
        model = entry["model"]
        # Security floor
        if role == "security-auditor" and model in ("haiku", "sonnet"):
            result = {"model": SECURITY_FLOOR_MODEL, "source": "security_floor"}
            log_event(root, "router_model_decision", role=role, task_type=task_type, model=result["model"], source=result["source"])
            return result
        result = {"model": model, "source": "learned_history"}
        log_event(root, "router_model_decision", role=role, task_type=task_type, model=result["model"], source=result["source"])
        return result

    # 4. No data — default
    if role == "security-auditor":
        result = {"model": SECURITY_FLOOR_MODEL, "source": "security_floor"}
        log_event(root, "router_model_decision", role=role, task_type=task_type, model=result["model"], source=result["source"])
        return result

    result = {"model": DEFAULT_MODEL, "source": "default"}
    log_event(root, "router_model_decision", role=role, task_type=task_type, model=result["model"], source=result["source"])
    return result


def _parse_model_from_patterns(path: Path, role: str, task_type: str) -> str | None:
    """Parse Model Policy table from project_rules.md."""
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
DEFAULT_SKIP_THRESHOLD = _DEFAULT_SKIP_THRESHOLD


def resolve_skip(root: Path, auditor: str, task_type: str, ctx: RouterContext | None = None) -> dict:
    """Determine whether an auditor should be skipped.

    Returns {"skip": bool, "reason": str, "streak": int, "threshold": int}.
    """
    if auditor in SKIP_EXEMPT:
        return {"skip": False, "reason": "skip-exempt", "streak": 0, "threshold": 0}

    if not (ctx.learning_enabled if ctx else is_learning_enabled(root)):
        return {"skip": False, "reason": "learning_enabled=false (no skip)", "streak": 0, "threshold": 0}

    # Get streak from most recent prior task (cached via ctx)
    retros = ctx.retrospectives if ctx else collect_retrospectives(root)
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

    Priority: skip-policy.json -> DEFAULT_SKIP_THRESHOLD.
    """
    entry = _read_policy_json(root, "skip-policy.json", auditor)
    if entry and isinstance(entry, dict) and "threshold" in entry:
        return int(entry["threshold"])

    return DEFAULT_SKIP_THRESHOLD


# ---------------------------------------------------------------------------
# Agent routing
# ---------------------------------------------------------------------------


def resolve_route(root: Path, role: str, task_type: str, ctx: RouterContext | None = None) -> dict:
    """Determine whether to use generic, learned, or alongside agent.

    Returns {
        "mode": "generic"|"learned"|"alongside",
        "agent_path": str|None,
        "agent_name": str|None,
        "composite_score": float,
        "source": str
    }.
    """
    if not (ctx.learning_enabled if ctx else is_learning_enabled(root)):
        result = {
            "mode": "generic",
            "agent_path": None,
            "agent_name": None,
            "composite_score": 0.0,
            "source": "learning_enabled=false",
        }
        log_event(root, "router_route_decision", role=role, task_type=task_type, mode="generic", agent_name=None, composite_score=0.0, source="learning_enabled=false")
        return result

    registry = ctx.registry if ctx else _read_learned_registry(root)
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
        result = {
            "mode": "generic",
            "agent_path": None,
            "agent_name": None,
            "composite_score": 0.0,
            "source": "no learned agent",
        }
        log_event(root, "router_route_decision", role=role, task_type=task_type, mode=result["mode"], agent_name=result["agent_name"], composite_score=result["composite_score"], source=result["source"])
        return result

    mode = learned.get("mode", "shadow")
    composite = float(learned.get("benchmark_summary", {}).get("mean_composite", 0.0) or 0.0)
    agent_path = learned.get("path", "")
    agent_name = learned.get("agent_name", "")

    # Security-auditor can never be replaced
    if role == "security-auditor" and mode == "replace":
        mode = "alongside"

    # Shadow mode means it's not yet proven — use generic
    if mode == "shadow":
        result = {
            "mode": "generic",
            "agent_path": agent_path,
            "agent_name": agent_name,
            "composite_score": composite,
            "source": f"shadow (not yet promoted): {agent_name}",
        }
        log_event(root, "router_route_decision", role=role, task_type=task_type, mode=result["mode"], agent_name=result["agent_name"], composite_score=result["composite_score"], source=result["source"])
        return result

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
            result = {
                "mode": "generic",
                "agent_path": None,
                "agent_name": agent_name,
                "composite_score": composite,
                "source": f"learned agent file not found: {agent_path}",
            }
            log_event(root, "router_route_decision", role=role, task_type=task_type, mode=result["mode"], agent_name=result["agent_name"], composite_score=result["composite_score"], source=result["source"])
            return result
        agent_path = str(full_path)

    result = {
        "mode": mode,
        "agent_path": agent_path,
        "agent_name": agent_name,
        "composite_score": composite,
        "source": f"learned:{agent_name}",
    }
    log_event(root, "router_route_decision", role=role, task_type=task_type, mode=result["mode"], agent_name=result["agent_name"], composite_score=result["composite_score"], source=result["source"])
    return result


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


# Ensemble voting defaults — overridable via .dynos/config/policy.json
_DEFAULT_ENSEMBLE_AUDITORS = {"security-auditor", "db-schema-auditor"}
_DEFAULT_ENSEMBLE_VOTING_MODELS = ["haiku", "sonnet"]
_DEFAULT_ENSEMBLE_ESCALATION_MODEL = "opus"

# Default auditor registry — overridable via .dynos/config/auditors.json
_DEFAULT_AUDITOR_REGISTRY = {
    "always": ["spec-completion-auditor", "security-auditor", "code-quality-auditor", "dead-code-auditor"],
    "fast_track": ["spec-completion-auditor", "security-auditor"],
    "domain_conditional": {
        "ui": ["ui-auditor"],
        "db": ["db-schema-auditor", "performance-auditor"],
        "backend": ["performance-auditor"],
    },
}


def _load_auditor_registry(root: Path) -> dict:
    """Load auditor registry from .dynos/config/auditors.json with fallback to defaults."""
    config_path = root / ".dynos" / "config" / "auditors.json"
    try:
        data = load_json(config_path)
        if isinstance(data, dict) and "always" in data:
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return _DEFAULT_AUDITOR_REGISTRY


def _load_ensemble_config(config: dict) -> tuple[set[str], list[str], str]:
    """Load ensemble voting config from .dynos/config/policy.json with fallback to defaults."""
    auditors = set(config.get("ensemble_auditors", _DEFAULT_ENSEMBLE_AUDITORS))
    models = config.get("ensemble_voting_models", _DEFAULT_ENSEMBLE_VOTING_MODELS)
    escalation = config.get("ensemble_escalation_model", _DEFAULT_ENSEMBLE_ESCALATION_MODEL)
    return auditors, models, escalation


def build_audit_plan(root: Path, task_type: str, domains: list[str], fast_track: bool = False) -> dict:
    """Build a complete, deterministic audit spawn plan.

    Reads .dynos/config/auditors.json for the auditor registry and
    .dynos/config/policy.json for ensemble voting config. Falls back
    to hardcoded defaults when config files are missing.
    """
    ctx = RouterContext(root)
    registry = _load_auditor_registry(root)

    # Load user config from .dynos/config/policy.json
    config_policy_path = root / ".dynos" / "config" / "policy.json"
    try:
        user_config = load_json(config_policy_path)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        user_config = {}

    ensemble_auditors, ensemble_models, ensemble_escalation = _load_ensemble_config(user_config)

    plan = {
        "generated_at": now_iso(),
        "task_type": task_type,
        "domains": domains,
        "fast_track": fast_track,
        "auditors": [],
    }

    # Determine which auditors are eligible from the registry
    if fast_track:
        eligible = list(registry.get("fast_track", _DEFAULT_AUDITOR_REGISTRY["fast_track"]))
    else:
        eligible = list(registry.get("always", _DEFAULT_AUDITOR_REGISTRY["always"]))
        domain_map = registry.get("domain_conditional", _DEFAULT_AUDITOR_REGISTRY["domain_conditional"])
        for domain in domains:
            for auditor in domain_map.get(domain, []):
                if auditor not in eligible:
                    eligible.append(auditor)

    for auditor in eligible:
        # Skip check (uses cached retrospectives via ctx)
        skip_decision = resolve_skip(root, auditor, task_type, ctx=ctx)
        if skip_decision["skip"]:
            plan["auditors"].append({
                "name": auditor,
                "action": "skip",
                "reason": skip_decision["reason"],
                "streak": skip_decision["streak"],
                "threshold": skip_decision["threshold"],
            })
            continue

        # Model selection (uses cached policy + patterns via ctx)
        model_decision = resolve_model(root, auditor, task_type, ctx=ctx)

        # Fast-track model override: haiku for spec-completion
        if fast_track and auditor == "spec-completion-auditor" and model_decision["source"] == "default":
            model_decision = {"model": "haiku", "source": "fast_track_override"}

        # Route selection (uses cached registry via ctx)
        route_decision = resolve_route(root, auditor, task_type, ctx=ctx)

        entry = {
            "name": auditor,
            "action": "spawn",
            "model": model_decision["model"],
            "model_source": model_decision["source"],
            "route_mode": route_decision["mode"],
            "route_source": route_decision["source"],
            "agent_path": route_decision["agent_path"],
            "agent_name": route_decision["agent_name"],
            "composite_score": route_decision["composite_score"],
        }

        # Ensemble voting for high-risk auditors
        if auditor in ensemble_auditors and not fast_track:
            entry["ensemble"] = True
            entry["ensemble_voting_models"] = list(ensemble_models)
            entry["ensemble_escalation_model"] = ensemble_escalation
        else:
            entry["ensemble"] = False

        plan["auditors"].append(entry)

    log_event(root, "router_audit_plan", task_type=task_type, domains=domains, fast_track=fast_track, auditor_count=len(plan["auditors"]), auditors=[{"name": a["name"], "action": a["action"], "model": a.get("model")} for a in plan["auditors"]])
    return plan


def build_executor_plan(root: Path, task_type: str, segments: list[dict]) -> dict:
    """Build a complete, deterministic execution spawn plan.

    Returns structured decisions for each segment's executor.
    """
    ctx = RouterContext(root)
    all_rules = load_prevention_rules(root)
    plan = {
        "generated_at": now_iso(),
        "task_type": task_type,
        "segments": [],
    }

    for seg in segments:
        executor = seg.get("executor", "")
        seg_id = seg.get("id", "")

        model_decision = resolve_model(root, executor, task_type, ctx=ctx)
        route_decision = resolve_route(root, executor, task_type, ctx=ctx)

        # Filter prevention rules relevant to this executor
        executor_rules = [
            r["rule"] for r in all_rules
            if isinstance(r, dict) and r.get("rule")
            and (not r.get("executor") or r.get("executor") == executor)
        ]

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

    log_event(root, "router_executor_plan", task_type=task_type, segment_count=len(plan["segments"]), segments=[{"segment_id": s["segment_id"], "executor": s["executor"], "model": s.get("model"), "model_source": s.get("model_source")} for s in plan["segments"]])
    return plan


# ---------------------------------------------------------------------------
# Learned agent prompt injection
# ---------------------------------------------------------------------------


def build_executor_prompt(
    root: Path,
    segment: dict,
    plan_entry: dict,
    base_prompt: str,
) -> str:
    """Build the complete executor prompt with learned agent rules injected.

    This is the ONLY function that should be used to construct executor prompts.
    It deterministically injects learned agent instructions when the router
    assigns mode=replace or mode=alongside.

    Args:
        root: Project root.
        segment: The segment dict from execution-graph.json.
        plan_entry: The segment entry from build_executor_plan() output.
        base_prompt: The base instruction prompt for the executor.

    Returns:
        The complete prompt string with learned rules appended.
    """
    agent_path = plan_entry.get("agent_path")
    route_mode = plan_entry.get("route_mode", "generic")
    agent_name = plan_entry.get("agent_name")
    prevention_rules = plan_entry.get("prevention_rules", [])

    parts = [base_prompt]

    # Inject learned agent instructions
    if route_mode in ("replace", "alongside") and agent_path:
        try:
            p = Path(agent_path)
            if not p.is_absolute():
                p = root / p
            if p.exists():
                agent_content = p.read_text().strip()
                # Strip frontmatter if present
                if agent_content.startswith("---"):
                    end = agent_content.find("---", 3)
                    if end != -1:
                        agent_content = agent_content[end + 3:].strip()
                parts.append(
                    f"\n\n## Learned Agent Instructions ({agent_name})\n"
                    f"You are running as the **{agent_name}** learned agent (mode={route_mode}). "
                    f"These rules were learned from past failures and regressions. Treat them as hard constraints unless the current spec explicitly overrides them. "
                    f"If one of these rules is violated without justification, assume you are recreating a known bug.\n\n"
                    f"{agent_content}"
                )
                log_event(
                    root,
                    "learned_agent_injected",
                    agent_name=agent_name,
                    agent_path=str(agent_path),
                    route_mode=route_mode,
                    segment_id=plan_entry.get("segment_id", ""),
                )
            else:
                log_event(
                    root,
                    "learned_agent_missing",
                    agent_name=agent_name,
                    agent_path=str(agent_path),
                    segment_id=plan_entry.get("segment_id", ""),
                )
        except Exception as exc:
            log_event(
                root,
                "learned_agent_error",
                agent_name=agent_name,
                error=str(exc),
            )

    # Inject prevention rules
    if prevention_rules:
        rules_text = "\n".join(f"- {r}" for r in prevention_rules)
        parts.append(
            f"\n\n## Prevention Rules\n"
            f"These patterns have already caused audit findings in real tasks. Do not repeat them. "
            f"Treat each rule below as a known failure mode that must be actively prevented, not passively remembered. "
            f"If you violate one without an explicit, spec-backed reason, assume you are shipping a regression:\n{rules_text}"
        )

    return "\n".join(parts)



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


def cmd_inject_prompt(args: argparse.Namespace) -> int:
    """Read base prompt from stdin, inject learned agent rules, print result."""
    root = Path(args.root).resolve()
    graph_path = Path(args.graph)
    if not graph_path.exists():
        print(json.dumps({"error": f"graph not found: {graph_path}"}))
        return 1
    graph = load_json(graph_path)
    segments = graph.get("segments", [])

    # Find the target segment
    target_seg = None
    for seg in segments:
        if seg.get("id") == args.segment_id:
            target_seg = seg
            break
    if not target_seg:
        print(json.dumps({"error": f"segment not found: {args.segment_id}"}))
        return 1

    # Build executor plan for this segment
    plan = build_executor_plan(root, args.task_type, [target_seg])
    if not plan["segments"]:
        print(json.dumps({"error": "no plan entry for segment"}))
        return 1
    plan_entry = plan["segments"][0]

    # Read base prompt from stdin
    import sys as _sys
    base_prompt = _sys.stdin.read()

    # Build complete prompt
    result = build_executor_prompt(root, target_seg, plan_entry, base_prompt)
    print(result)
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

    ip = subparsers.add_parser("inject-prompt", help="Inject learned agent rules into executor prompt")
    ip.add_argument("--root", default=".")
    ip.add_argument("--task-type", required=True)
    ip.add_argument("--graph", required=True)
    ip.add_argument("--segment-id", required=True)
    ip.set_defaults(func=cmd_inject_prompt)

    res = subparsers.add_parser("resolve", help="Resolve model/skip/route for one role")
    res.add_argument("role")
    res.add_argument("task_type")
    res.add_argument("--root", default=".")
    res.set_defaults(func=cmd_resolve)

    return parser


if __name__ == "__main__":
    from cli_base import cli_main
    raise SystemExit(cli_main(build_parser))
