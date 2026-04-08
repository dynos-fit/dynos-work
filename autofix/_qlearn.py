#!/usr/bin/env python3
"""Q-learning for the autofix scanner.

Autofix-specific subset of dynoslib_qlearn — no dynorouter dependency.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from autofix._core import _persistent_project_dir, load_json, now_iso, write_json
from autofix._defaults import (
    QLEARN_ALPHA,
    QLEARN_EPSILON,
    QLEARN_GAMMA,
    QVALUE_PRECISION,
)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_ALPHA = QLEARN_ALPHA      # learning rate
DEFAULT_GAMMA = QLEARN_GAMMA      # discount factor
DEFAULT_EPSILON = QLEARN_EPSILON  # exploration rate


# ---------------------------------------------------------------------------
# Q-table storage
# ---------------------------------------------------------------------------

def _q_autofix_table_path(root: Path) -> Path:
    """Return path to the autofix Q-table (q-autofix.json)."""
    return _persistent_project_dir(root) / "q-autofix.json"


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
