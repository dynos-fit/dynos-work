#!/usr/bin/env python3
"""Tabular Q-learning for the dynos-work repair loop.

Two Q-tables:
  - executor assignment: which executor handles which finding type
  - model selection: which model to use for each executor/finding combo

Toggle via policy.json: "repair_qlearning": true/false
Depends only on lib_core.
"""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent)); _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "hooks"))

import json
import random
from pathlib import Path

from lib_core import _persistent_project_dir, load_json, now_iso, project_policy, write_json
from lib_usage_telemetry import record_usage as _record_usage
_record_usage("lib_qlearn")
from lib_defaults import (
    EFFICIENCY_CLAMP,
    EFFICIENCY_WEIGHT,
    ESCALATION_RETRY_THRESHOLD,
    QLEARN_ALPHA,
    QLEARN_EPSILON,
    QLEARN_GAMMA,
    QVALUE_PRECISION,
    REWARD_FAILURE,
    REWARD_MAX,
    REWARD_MIN,
    REWARD_PARTIAL,
    REWARD_PENALTY_PER_NEW_FINDING,
    REWARD_SUCCESS,
    TOKEN_BUDGETS,
)

# Lazy import to avoid circular dependency — router imports lib_core too
_router = None

def _get_router():
    global _router
    if _router is None:
        import router as _r
        _router = _r
    return _router

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_ALPHA = QLEARN_ALPHA      # learning rate
DEFAULT_GAMMA = QLEARN_GAMMA      # discount factor
DEFAULT_EPSILON = QLEARN_EPSILON  # exploration rate

ALL_EXECUTORS = [
    "backend-executor",
    "db-executor",
    "docs-executor",
    "integration-executor",
    "ml-executor",
    "refactor-executor",
    "testing-executor",
    "ui-executor",
]

# Per-category executor action spaces — restrict to executors that
# can actually fix this type of finding. Eliminates impossible
# assignments and makes Q-tables converge faster.
EXECUTOR_ACTION_SPACE: dict[str, list[str]] = {
    "sec":  ["backend-executor", "integration-executor"],
    "comp": ["integration-executor", "backend-executor"],
    "cq":   ["backend-executor", "refactor-executor", "testing-executor"],
    "dc":   ["refactor-executor"],
    "db":   ["db-executor"],
    "ui":   ["ui-executor"],
    "perf": ["backend-executor", "db-executor"],
    "sc":   ["backend-executor", "ui-executor", "db-executor", "integration-executor"],
}

VALID_ROUTE_MODES = ["generic", "learned"]
VALID_MODELS = ["haiku", "sonnet", "opus"]

# Backwards compat
VALID_EXECUTORS = ALL_EXECUTORS

# TOKEN_BUDGETS imported from lib_defaults


# ---------------------------------------------------------------------------
# Q-table storage
# ---------------------------------------------------------------------------

def _q_table_path(root: Path, table_name: str) -> Path:
    return _persistent_project_dir(root) / f"q-repair-{table_name}.json"


def load_q_table(root: Path, table_name: str) -> dict:
    """Load a Q-table from persistent storage. Returns empty table if not found."""
    path = _q_table_path(root, table_name)
    if not path.exists():
        return {"version": 1, "updated_at": now_iso(), "entries": {}}
    try:
        data = load_json(path)
        if not isinstance(data, dict) or "entries" not in data:
            return {"version": 1, "updated_at": now_iso(), "entries": {}}
        return data
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "updated_at": now_iso(), "entries": {}}


def save_q_table(root: Path, table_name: str, table: dict) -> None:
    """Save a Q-table to persistent storage."""
    table["updated_at"] = now_iso()
    write_json(_q_table_path(root, table_name), table)



# ---------------------------------------------------------------------------
# State encoding
# ---------------------------------------------------------------------------


def encode_repair_state(
    finding_category: str,
    severity: str,
    task_type: str,
    retry_count: int,
) -> str:
    """Encode repair state as a compact string key.

    Example: "security:critical:feature:0"
    """
    return f"{finding_category}:{severity}:{task_type}:{retry_count}"


def _finding_category(finding_id: str) -> str:
    """Extract category from finding ID. 'sec-001' → 'sec'."""
    return finding_id.split("-")[0] if "-" in finding_id else finding_id


# ---------------------------------------------------------------------------
# Action selection (epsilon-greedy)
# ---------------------------------------------------------------------------

def select_action(
    q_table: dict,
    state: str,
    valid_actions: list[str],
    epsilon: float,
) -> tuple[str, str]:
    """Select an action using epsilon-greedy policy.

    Returns (action, source) where source is "q-learning" or "q-explore".
    """
    if not valid_actions:
        raise ValueError("No valid actions")

    entries = q_table.get("entries", {})
    state_q = entries.get(state, {})

    # Epsilon-greedy: explore with probability epsilon
    if random.random() < epsilon:
        action = random.choice(valid_actions)
        return action, "q-explore"

    # Exploit: pick action with highest Q-value
    best_action = None
    best_value = float("-inf")
    for action in valid_actions:
        value = float(state_q.get(action, 0.0))
        if value > best_value:
            best_value = value
            best_action = action

    # If all Q-values are 0 (unseen state), pick randomly
    if best_value == 0.0 and all(float(state_q.get(a, 0.0)) == 0.0 for a in valid_actions):
        return random.choice(valid_actions), "q-explore"

    return best_action, "q-learning"


def get_q_value(q_table: dict, state: str, action: str) -> float:
    """Get the Q-value for a state-action pair."""
    return float(q_table.get("entries", {}).get(state, {}).get(action, 0.0))


# ---------------------------------------------------------------------------
# Q-value update
# ---------------------------------------------------------------------------

def update_q_value(
    q_table: dict,
    state: str,
    action: str,
    reward: float,
    next_state: str | None,
    alpha: float = DEFAULT_ALPHA,
    gamma: float = DEFAULT_GAMMA,
) -> float:
    """Update Q-value using the standard Q-learning rule.

    Q(s,a) = Q(s,a) + alpha * (reward + gamma * max_a'(Q(s',a')) - Q(s,a))

    If next_state is None (terminal), the future value term is 0.
    Returns the new Q-value.
    """
    entries = q_table.setdefault("entries", {})
    state_q = entries.setdefault(state, {})
    old_value = float(state_q.get(action, 0.0))

    # Future value: max Q(s', a') over all actions in next state
    future_value = 0.0
    if next_state is not None:
        next_q = entries.get(next_state, {})
        if next_q:
            future_value = max(float(v) for v in next_q.values())

    # Q-learning update
    new_value = old_value + alpha * (reward + gamma * future_value - old_value)
    state_q[action] = round(new_value, QVALUE_PRECISION)

    return new_value


# ---------------------------------------------------------------------------
# Reward computation
# ---------------------------------------------------------------------------

def compute_repair_reward(
    finding_resolved: bool,
    new_findings_introduced: int,
    tokens_used: int,
    token_budget: int,
) -> float:
    """Compute reward for a single repair action.

    Returns a float in [-1.0, +1.0].
    """
    # Base reward
    if finding_resolved and new_findings_introduced == 0:
        reward = REWARD_SUCCESS
    elif finding_resolved:
        reward = max(0.0, REWARD_PARTIAL - REWARD_PENALTY_PER_NEW_FINDING * new_findings_introduced)
    else:
        reward = REWARD_FAILURE

    # Token efficiency bonus/penalty
    if token_budget > 0 and tokens_used > 0:
        efficiency = EFFICIENCY_WEIGHT * (1.0 - tokens_used / token_budget)
        efficiency = max(-EFFICIENCY_CLAMP, min(EFFICIENCY_CLAMP, efficiency))
        reward += efficiency

    return max(REWARD_MIN, min(REWARD_MAX, reward))


# ---------------------------------------------------------------------------
# Repair plan builder
# ---------------------------------------------------------------------------

def _executors_for_category(category: str) -> list[str]:
    """Return the valid executor action space for a finding category."""
    return EXECUTOR_ACTION_SPACE.get(category, ALL_EXECUTORS)


def _has_learned_agent(root: Path, executor: str, task_type: str) -> tuple[bool, str | None, str | None]:
    """Check if a learned agent exists for this executor + task_type.

    Returns (exists, agent_path, agent_name).
    """
    try:
        router = _get_router()
        route = router.resolve_route(root, executor, task_type)
        if route.get("mode") in ("replace", "alongside") and route.get("agent_path"):
            return True, route.get("agent_path"), route.get("agent_name")
    except Exception:
        pass
    return False, None, None


def build_repair_plan(
    root: Path,
    findings: list[dict],
    task_type: str,
) -> dict:
    """Build executor, route-mode, and model assignments using hierarchical Q-learning.

    Three sequential decisions per finding, each conditioned on the prior:
      1. Executor (restricted action space per finding category)
      2. Route mode: generic vs learned (only if learned agent exists)
      3. Model: haiku/sonnet/opus (hard constraints override)

    If repair_qlearning is disabled in policy.json, returns default assignments.
    """
    policy = project_policy(root)
    enabled = bool(policy.get("repair_qlearning", True))
    epsilon = float(policy.get("repair_epsilon", DEFAULT_EPSILON))

    executor_q = load_q_table(root, "executor") if enabled else {"entries": {}}
    route_q = load_q_table(root, "route") if enabled else {"entries": {}}
    model_q = load_q_table(root, "model") if enabled else {"entries": {}}

    assignments = []
    for finding in findings:
        finding_id = finding.get("id", finding.get("finding_id", "unknown"))
        severity = finding.get("severity", "medium")
        auditor = finding.get("auditor", finding.get("auditor_name", ""))
        retry_count = int(finding.get("retry_count", 0))
        category = _finding_category(finding_id)

        # Base state: category:severity:task_type:retry
        base_state = encode_repair_state(category, severity, task_type, retry_count)

        # --- Step 1: Executor (restricted by category) ---
        valid_executors = _executors_for_category(category)
        if enabled:
            executor, executor_source = select_action(
                executor_q, base_state, valid_executors, epsilon,
            )
            executor_q_val = get_q_value(executor_q, base_state, executor)
        else:
            executor = None
            executor_source = "default"
            executor_q_val = None

        # --- Step 2: Route mode (conditioned on executor) ---
        # Only offer "learned" if a learned agent actually exists
        has_learned, agent_path, agent_name = (
            _has_learned_agent(root, executor, task_type)
            if executor and enabled else (False, None, None)
        )

        if enabled and executor and has_learned:
            route_state = f"{base_state}:{executor}"
            route_mode, route_source = select_action(
                route_q, route_state, VALID_ROUTE_MODES, epsilon,
            )
            route_q_val = get_q_value(route_q, route_state, route_mode)
        elif has_learned:
            route_mode = "generic"
            route_source = "default"
            route_q_val = None
        else:
            route_mode = "generic"
            route_source = "no_learned_agent"
            route_q_val = None
            agent_path = None
            agent_name = None

        # --- Step 3: Model (conditioned on executor + route mode) ---
        # Hard constraints first
        if retry_count >= ESCALATION_RETRY_THRESHOLD:
            model = "opus"
            model_source = "escalation"
            model_q_val = None
        elif auditor.startswith("security"):
            model = "opus"
            model_source = "security_floor"
            model_q_val = None
        elif enabled and executor:
            model_state = f"{base_state}:{executor}:{route_mode}"
            model, model_source = select_action(
                model_q, model_state, VALID_MODELS, epsilon,
            )
            model_q_val = get_q_value(model_q, model_state, model)
        else:
            model = None
            model_source = "default"
            model_q_val = None

        assignments.append({
            "finding_id": finding_id,
            "state": base_state,
            "assigned_executor": executor,
            "executor_source": executor_source,
            "executor_q_value": executor_q_val,
            "route_mode": route_mode,
            "route_source": route_source,
            "route_q_value": route_q_val if enabled else None,
            "agent_path": agent_path if route_mode == "learned" else None,
            "agent_name": agent_name if route_mode == "learned" else None,
            "model_override": model,
            "model_source": model_source,
            "model_q_value": model_q_val,
        })

    return {
        "generated_at": now_iso(),
        "source": "q-learning" if enabled else "default",
        "enabled": enabled,
        "epsilon": epsilon if enabled else None,
        "assignments": assignments,
    }


# ---------------------------------------------------------------------------
# Repair outcome update
# ---------------------------------------------------------------------------

def update_from_outcomes(root: Path, outcomes: list[dict], task_type: str) -> dict:
    """Update all three Q-tables from repair outcomes.

    Each outcome has: finding_id, state, executor, route_mode, model,
    resolved, new_findings, tokens_used, next_state.

    The same reward feeds all three tables — the executor, route-mode,
    and model all contributed to the outcome jointly.

    Returns summary of updates applied.
    """
    policy = project_policy(root)
    enabled = bool(policy.get("repair_qlearning", True))
    if not enabled:
        return {"updated": False, "reason": "repair_qlearning disabled"}

    alpha = float(policy.get("repair_alpha", DEFAULT_ALPHA))
    gamma = float(policy.get("repair_gamma", DEFAULT_GAMMA))

    executor_q = load_q_table(root, "executor")
    route_q = load_q_table(root, "route")
    model_q = load_q_table(root, "model")

    updates = []
    for outcome in outcomes:
        finding_id = outcome.get("finding_id", "unknown")
        state = outcome.get("state", "")
        executor = outcome.get("executor", "")
        route_mode = outcome.get("route_mode", "generic")
        model = outcome.get("model")
        resolved = bool(outcome.get("resolved", False))
        new_findings = int(outcome.get("new_findings", 0))
        tokens_used = int(outcome.get("tokens_used", 0))
        next_state = outcome.get("next_state")

        severity = state.split(":")[1] if ":" in state else "medium"
        token_budget = TOKEN_BUDGETS.get(severity, 12000)

        reward = compute_repair_reward(resolved, new_findings, tokens_used, token_budget)

        # Update executor Q-table: state → executor
        new_eq = None
        if executor and state:
            new_eq = update_q_value(executor_q, state, executor, reward, next_state, alpha, gamma)

        # Update route Q-table: state:executor → route_mode
        new_rq = None
        if executor and state and route_mode:
            route_state = f"{state}:{executor}"
            new_rq = update_q_value(route_q, route_state, route_mode, reward, next_state, alpha, gamma)

        # Update model Q-table: state:executor:route_mode → model
        new_mq = None
        if model and executor:
            model_state = f"{state}:{executor}:{route_mode}"
            new_mq = update_q_value(model_q, model_state, model, reward, next_state, alpha, gamma)

        updates.append({
            "finding_id": finding_id,
            "reward": round(reward, 4),
            "executor_q_new": round(new_eq, 4) if new_eq is not None else None,
            "route_q_new": round(new_rq, 4) if new_rq is not None else None,
            "model_q_new": round(new_mq, 4) if new_mq is not None else None,
        })

    save_q_table(root, "executor", executor_q)
    save_q_table(root, "route", route_q)
    save_q_table(root, "model", model_q)

    return {
        "updated": True,
        "alpha": alpha,
        "gamma": gamma,
        "updates": updates,
    }
