#!/usr/bin/env python3
"""Tabular Q-learning for the dynos-work repair loop.

Two Q-tables:
  - executor assignment: which executor handles which finding type
  - model selection: which model to use for each executor/finding combo

Toggle via policy.json: "repair_qlearning": true/false
Depends only on lib_core.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from lib_core import _persistent_project_dir, load_json, now_iso, project_policy, write_json
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

VALID_EXECUTORS = [
    "backend-executor",
    "db-executor",
    "integration-executor",
    "ml-executor",
    "refactor-executor",
    "testing-executor",
    "ui-executor",
]

VALID_MODELS = [None, "haiku", "sonnet", "opus"]

# TOKEN_BUDGETS imported from lib_defaults


# ---------------------------------------------------------------------------
# Q-table storage
# ---------------------------------------------------------------------------

def _q_table_path(root: Path, table_name: str) -> Path:
    return _persistent_project_dir(root) / f"q-repair-{table_name}.json"


def _q_autofix_table_path(root: Path) -> Path:
    """Return path to the autofix Q-table (q-autofix.json)."""
    return _persistent_project_dir(root) / "q-autofix.json"


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


def load_autofix_q_table(root: Path) -> dict:
    """Load the autofix Q-table from persistent storage. Returns empty table if not found."""
    path = _q_autofix_table_path(root)
    if not path.exists():
        return {"version": 1, "updated_at": now_iso(), "entries": {}}
    try:
        data = load_json(path)
        if not isinstance(data, dict) or "entries" not in data:
            return {"version": 1, "updated_at": now_iso(), "entries": {}}
        return data
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "updated_at": now_iso(), "entries": {}}


def save_autofix_q_table(root: Path, table: dict) -> None:
    """Save the autofix Q-table to persistent storage."""
    table["updated_at"] = now_iso()
    write_json(_q_autofix_table_path(root), table)


# ---------------------------------------------------------------------------
# State encoding
# ---------------------------------------------------------------------------

def encode_autofix_state(
    finding_category: str,
    file_extension: str,
    centrality_tier: str,
    severity: str,
) -> str:
    """Encode autofix state as a compact colon-separated string key.

    Centrality tier is one of "high", "medium", "low" (derived from PageRank quartiles).
    Example: "llm-review:.dart:high:medium"
    """
    return f"{finding_category}:{file_extension}:{centrality_tier}:{severity}"


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

def build_repair_plan(
    root: Path,
    findings: list[dict],
    task_type: str,
) -> dict:
    """Build executor and model assignments for findings using Q-learning.

    If repair_qlearning is disabled in policy.json, returns default assignments.
    Hard constraints (escalation, security floor) are always enforced.
    """
    policy = project_policy(root)
    enabled = bool(policy.get("repair_qlearning", True))
    epsilon = float(policy.get("repair_epsilon", DEFAULT_EPSILON))

    executor_q = load_q_table(root, "executor") if enabled else {"entries": {}}
    model_q = load_q_table(root, "model") if enabled else {"entries": {}}

    assignments = []
    for finding in findings:
        finding_id = finding.get("id", finding.get("finding_id", "unknown"))
        severity = finding.get("severity", "medium")
        auditor = finding.get("auditor", finding.get("auditor_name", ""))
        retry_count = int(finding.get("retry_count", 0))
        category = _finding_category(finding_id)

        # Encode state
        state = encode_repair_state(category, severity, task_type, retry_count)

        # --- Decision 1: Executor assignment ---
        if enabled:
            executor, executor_source = select_action(
                executor_q, state, VALID_EXECUTORS, epsilon,
            )
            executor_q_val = get_q_value(executor_q, state, executor)
        else:
            executor = None  # let repair coordinator decide
            executor_source = "default"
            executor_q_val = None

        # --- Decision 2: Model selection ---
        # Hard constraints first
        if retry_count >= ESCALATION_RETRY_THRESHOLD:
            model = "opus"
            model_source = "escalation"
            model_q_val = None
        elif auditor.startswith("security"):
            model = "opus"
            model_source = "security_floor"
            model_q_val = None
        elif enabled:
            model_state = f"{executor or 'unknown'}:{task_type}:{severity}:{retry_count}"
            # Valid models for Q-selection (exclude None for clarity in Q-table)
            model_actions = ["haiku", "sonnet", "opus"]
            model, model_source = select_action(model_q, model_state, model_actions, epsilon)
            model_q_val = get_q_value(model_q, model_state, model)
        else:
            model = None
            model_source = "default"
            model_q_val = None

        # --- Decision 3: Route resolution (learned agent check) ---
        # Q-table picks the role. Router decides if a learned agent replaces the generic.
        route_mode = "generic"
        agent_path = None
        agent_name = None
        if executor and enabled:
            try:
                router = _get_router()
                route = router.resolve_route(root, executor, task_type)
                route_mode = route.get("mode", "generic")
                agent_path = route.get("agent_path")
                agent_name = route.get("agent_name")
            except Exception:
                pass  # router unavailable, use generic

        assignments.append({
            "finding_id": finding_id,
            "state": state,
            "assigned_executor": executor,
            "executor_source": executor_source,
            "executor_q_value": executor_q_val,
            "model_override": model,
            "model_source": model_source,
            "model_q_value": model_q_val,
            "route_mode": route_mode,
            "agent_path": agent_path,
            "agent_name": agent_name,
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
    """Update Q-tables from repair outcomes.

    Each outcome has: finding_id, state, executor, model, resolved,
    new_findings, tokens_used, next_state.

    Returns summary of updates applied.
    """
    policy = project_policy(root)
    enabled = bool(policy.get("repair_qlearning", True))
    if not enabled:
        return {"updated": False, "reason": "repair_qlearning disabled"}

    alpha = float(policy.get("repair_alpha", DEFAULT_ALPHA))
    gamma = float(policy.get("repair_gamma", DEFAULT_GAMMA))

    executor_q = load_q_table(root, "executor")
    model_q = load_q_table(root, "model")

    updates = []
    for outcome in outcomes:
        finding_id = outcome.get("finding_id", "unknown")
        state = outcome.get("state", "")
        executor = outcome.get("executor", "")
        model = outcome.get("model")
        resolved = bool(outcome.get("resolved", False))
        new_findings = int(outcome.get("new_findings", 0))
        tokens_used = int(outcome.get("tokens_used", 0))
        next_state = outcome.get("next_state")

        severity = state.split(":")[1] if ":" in state else "medium"
        token_budget = TOKEN_BUDGETS.get(severity, 12000)

        reward = compute_repair_reward(resolved, new_findings, tokens_used, token_budget)

        # Update executor Q-table
        if executor and state:
            new_eq = update_q_value(executor_q, state, executor, reward, next_state, alpha, gamma)
        else:
            new_eq = None

        # Update model Q-table
        if model and executor:
            model_state = f"{executor}:{task_type}:{severity}:{state.split(':')[-1]}"
            new_mq = update_q_value(model_q, model_state, model, reward, next_state, alpha, gamma)
        else:
            new_mq = None

        updates.append({
            "finding_id": finding_id,
            "reward": round(reward, 4),
            "executor_q_new": round(new_eq, 4) if new_eq is not None else None,
            "model_q_new": round(new_mq, 4) if new_mq is not None else None,
        })

    save_q_table(root, "executor", executor_q)
    save_q_table(root, "model", model_q)

    return {
        "updated": True,
        "alpha": alpha,
        "gamma": gamma,
        "updates": updates,
    }
