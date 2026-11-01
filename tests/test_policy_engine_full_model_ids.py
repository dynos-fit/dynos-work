"""Full Claude model IDs in retrospectives must reach the policy engine.

On Claude Code the token hook records each agent's model straight from the
subagent transcript ("claude-sonnet-5-5"), without normalizing it. The policy
engine compared those values literally against aliases and tier names, so every
row was dropped and ``patterns.py effectiveness`` returned empty scores and
policies. Readers now map a full ID to its alias with
``lib_models.normalize_model`` before the check.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "hooks", ROOT / "memory"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import lib_models  # noqa: E402
import policy_engine  # noqa: E402


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("claude-sonnet-5-5", "sonnet"),
        ("claude-opus-5-5", "opus"),
        ("claude-haiku-5-5", "haiku"),
        ("claude-fable-5-1", "fable"),
        ("claude-3-5-sonnet-20241022", "sonnet"),
        ("us.anthropic.claude-opus-5-5[1m]", "opus"),
        ("Claude-Sonnet-5-5", "sonnet"),
        ("sonnet", "sonnet"),
        ("OPUS", "opus"),
        ("deep", "deep"),
        ("frontier", "frontier"),
    ],
)
def test_normalize_model_maps_ids_to_aliases(raw: str, expected: str) -> None:
    assert lib_models.normalize_model(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["gpt-5", "unknown", "", "claude", "claude-sonnet-opus-mix", "A"],
)
def test_normalize_model_leaves_unrecognized_values_unchanged(raw: str) -> None:
    assert lib_models.normalize_model(raw) == raw


def test_normalize_model_passes_none_through() -> None:
    assert lib_models.normalize_model(None) is None


def test_model_to_tier_and_rank_accept_full_ids() -> None:
    assert lib_models.model_to_tier("claude-sonnet-5-5") == lib_models.TIER_BALANCED
    assert lib_models.model_to_tier("claude-fable-5-1") == lib_models.TIER_FRONTIER
    assert lib_models.model_rank("claude-haiku-5-5") == lib_models.TIER_RANK["fast"]
    assert lib_models.model_rank("claude-opus-5-5") == lib_models.TIER_RANK["deep"]
    assert lib_models.model_rank("gpt-5") == -1


def _retro(task_id: str, models: dict[str, str], quality: float = 0.9) -> dict:
    return {
        "task_id": task_id,
        "task_type": "feature",
        "quality_score": quality,
        "cost_score": 0.7,
        "efficiency_score": 0.8,
        "model_used_by_agent": models,
        "agent_source": {role: "generic" for role in models},
    }


def test_effectiveness_scores_nonempty_for_full_model_ids() -> None:
    retros = [
        _retro(
            f"task-2026110{i}-001",
            {
                "backend-executor": "claude-sonnet-5-5",
                "security-auditor": "claude-opus-5-5",
                "dead-code-auditor": "claude-haiku-5-5",
                "planning": "claude-fable-5-1",
            },
        )
        for i in range(1, 6)
    ]

    scores = policy_engine.compute_effectiveness_scores(retros)

    assert scores, "full model IDs must not be filtered out"
    by_role = {row["role"]: row for row in scores}
    assert by_role["backend-executor"]["model"] == "sonnet"
    assert by_role["security-auditor"]["model"] == "opus"
    assert by_role["dead-code-auditor"]["model"] == "haiku"
    assert by_role["planning"]["model"] == "fable"
    assert by_role["backend-executor"]["sample_count"] == 5

    policy = policy_engine.derive_model_policy(scores)
    assert policy["backend-executor:feature"]["model"] == "sonnet"
    assert policy["security-auditor:feature"]["model"] == "opus"


def test_full_ids_and_aliases_pool_into_one_row() -> None:
    retros = [
        _retro("task-20261101-001", {"backend-executor": "claude-sonnet-5-5"}),
        _retro("task-20261101-002", {"backend-executor": "sonnet"}),
        _retro("task-20261101-003", {"backend-executor": "claude-sonnet-4-5"}),
    ]

    scores = policy_engine.compute_effectiveness_scores(retros)

    assert [(r["role"], r["model"], r["sample_count"]) for r in scores] == [
        ("backend-executor", "sonnet", 3)
    ]


def test_derive_model_policy_accepts_stored_rows_with_full_ids() -> None:
    row = {
        "role": "backend-executor",
        "model": "claude-sonnet-5-5",
        "task_type": "feature",
        "source": "generic",
        "quality_ema": 0.9,
        "cost_ema": 0.7,
        "efficiency_ema": 0.8,
        "sample_count": 4,
    }

    policy = policy_engine.derive_model_policy([row])

    assert policy["backend-executor:feature"]["model"] == "sonnet"


def test_model_policy_data_pools_full_ids() -> None:
    retros = [
        _retro("task-20261101-001", {"backend-executor": "claude-sonnet-5-5"}),
        _retro("task-20261101-002", {"backend-executor": "sonnet"}),
    ]

    data = policy_engine._build_model_policy_data(retros)

    assert data["backend-executor:feature"]["model"] == "sonnet"
    assert data["backend-executor:feature"]["sample_count"] == 2
