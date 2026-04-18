#!/usr/bin/env python3
"""Tests for reward scoring: deterministic quality, token estimation, trajectory guard."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest


# Import hooks modules
sys.path.insert(0, str(Path(__file__).parent.parent / "hooks"))


def _make_retrospective(
    task_id: str = "task-001",
    quality_score: float = 0.0,
    cost_score: float = 0.0,
    efficiency_score: float = 0.0,
    task_type: str = "feature",
    risk_level: str = "medium",
    findings: dict | None = None,
    findings_by_auditor: dict | None = None,
    repair_cycles: int = 0,
    spawns: int = 3,
    tokens: float = 10000,
    model_used: dict | None = None,
) -> dict:
    """Build a minimal retrospective for reward scoring tests."""
    return {
        "task_id": task_id,
        "task_type": task_type,
        "quality_score": quality_score,
        "cost_score": cost_score,
        "efficiency_score": efficiency_score,
        "task_risk_level": risk_level,
        "task_domains": "backend",
        "model_used_by_agent": model_used or {},
        "auditor_zero_finding_streaks": {},
        "executor_repair_frequency": {},
        "findings_by_category": findings or {},
        "findings_by_auditor": findings_by_auditor or {},
        "repair_cycle_count": repair_cycles,
        "spec_review_iterations": 1,
        "subagent_spawn_count": spawns,
        "wasted_spawns": 0,
        "total_token_usage": tokens,
        "agent_source": {},
        "task_outcome": "DONE",
    }


# --- compute_quality_score: deterministic quality function (AC 1) ---

def test_zero_findings_returns_09() -> None:
    from lib import compute_quality_score
    assert compute_quality_score({}, 0) == pytest.approx(0.9)


def test_findings_no_repairs() -> None:
    from lib import compute_quality_score
    # 8 findings, 0 repairs → 1/(1+8) = 0.111...
    score = compute_quality_score({"sec": 6, "cq": 2}, 0)
    assert score == pytest.approx(1 / 9, abs=1e-4)


def test_findings_with_repairs() -> None:
    from lib import compute_quality_score
    # 8 findings, 2 repair cycles → surviving = max(0, 8-4)=4 → 1-(4/8)=0.5
    score = compute_quality_score({"sec": 6, "cq": 2}, 2)
    assert score == pytest.approx(0.5, abs=1e-4)


def test_repairs_exceed_findings() -> None:
    from lib import compute_quality_score
    # 2 findings, 3 repairs → surviving = max(0, 2-6)=0 → 1-(0/2)=1.0
    score = compute_quality_score({"sec": 2}, 3)
    assert score == pytest.approx(1.0, abs=1e-4)


def test_invalid_findings_dict() -> None:
    from lib import compute_quality_score
    assert compute_quality_score("not a dict", 0) == pytest.approx(0.9)
    assert compute_quality_score(None, 0) == pytest.approx(0.9)


def test_single_finding_no_repair() -> None:
    from lib import compute_quality_score
    # 1 finding → 1/(1+1) = 0.5
    score = compute_quality_score({"cq": 1}, 0)
    assert score == pytest.approx(0.5, abs=1e-4)


# --- estimate_token_usage: heuristic function (AC 3) ---

def test_with_model_dict() -> None:
    from lib import estimate_token_usage, TOKEN_ESTIMATES
    models = {"sec": "opus", "cq": "haiku"}
    expected = TOKEN_ESTIMATES["opus"] + TOKEN_ESTIMATES["haiku"]
    assert estimate_token_usage(2, models) == expected


def test_empty_model_dict_falls_back() -> None:
    from lib import estimate_token_usage, TOKEN_ESTIMATES
    result = estimate_token_usage(5, {})
    assert result == 5 * TOKEN_ESTIMATES["default"]


def test_none_model_dict() -> None:
    from lib import estimate_token_usage, TOKEN_ESTIMATES
    result = estimate_token_usage(3, None)
    assert result == 3 * TOKEN_ESTIMATES["default"]


def test_unknown_model_uses_default() -> None:
    from lib import estimate_token_usage, TOKEN_ESTIMATES
    result = estimate_token_usage(1, {"x": "unknown-model"})
    assert result == TOKEN_ESTIMATES["default"]


def test_zero_spawns() -> None:
    from lib import estimate_token_usage, TOKEN_ESTIMATES
    result = estimate_token_usage(0, {})
    assert result == TOKEN_ESTIMATES["default"]  # max(1, 0) * default


# --- validate_retrospective_scores: consistency check (AC 5) ---

def test_overwrites_bad_quality() -> None:
    from lib import validate_retrospective_scores
    retro = _make_retrospective(
        quality_score=0.9,  # wrong: has 8 findings, 0 repairs
        findings_by_auditor={"sec": 6, "cq": 2},
        repair_cycles=0,
    )
    fixed = validate_retrospective_scores(retro)
    # Should be 1/(1+8) ≈ 0.111, not 0.9
    assert fixed["quality_score"] == pytest.approx(1 / 9, abs=1e-4)


def test_does_not_mutate_original() -> None:
    from lib import validate_retrospective_scores
    retro = _make_retrospective(quality_score=0.9, findings_by_auditor={"sec": 2})
    validate_retrospective_scores(retro)
    assert retro["quality_score"] == pytest.approx(0.9)  # original unchanged


def test_estimates_tokens_when_zero() -> None:
    from lib import validate_retrospective_scores
    retro = _make_retrospective(tokens=0, spawns=4, model_used={"a": "opus", "b": "haiku"})
    fixed = validate_retrospective_scores(retro)
    assert fixed.get("token_usage_estimated", False)
    assert fixed["cost_score"] > 0.0


def test_validate_real_tokens_not_estimated() -> None:
    from lib import validate_retrospective_scores
    retro = _make_retrospective(tokens=50000, spawns=4)
    fixed = validate_retrospective_scores(retro)
    assert not fixed.get("token_usage_estimated", False)


# --- load_token_usage: reads token-usage.json from task directory ---

def test_reads_valid_file() -> None:
    from lib import load_token_usage
    with tempfile.TemporaryDirectory() as td:
        task_dir = Path(td)
        (task_dir / "token-usage.json").write_text(
            '{"agents": {"security-auditor": 45000, "cq-auditor": 12000}, "total": 57000}'
        )
        data = load_token_usage(task_dir)
        assert data["total"] == 57000
        assert data["agents"]["security-auditor"] == 45000


def test_missing_file_returns_empty() -> None:
    from lib import load_token_usage
    with tempfile.TemporaryDirectory() as td:
        data = load_token_usage(Path(td))
        assert data["agents"] == {}
        assert data["total"] == 0
        assert data["total_input_tokens"] == 0
        assert data["total_output_tokens"] == 0


def test_validate_uses_token_file() -> None:
    from lib import validate_retrospective_scores
    with tempfile.TemporaryDirectory() as td:
        task_dir = Path(td)
        (task_dir / "token-usage.json").write_text(
            '{"agents": {"sec": 45000}, "total": 45000}'
        )
        retro = _make_retrospective(tokens=0, spawns=1, findings_by_auditor={})
        fixed = validate_retrospective_scores(retro, task_dir=task_dir)
        assert fixed["total_token_usage"] == 45000
        assert not fixed.get("token_usage_estimated", False)


# --- make_trajectory_entry: always recomputes quality and estimates tokens (AC 2, 4) ---

def test_quality_always_recomputed_from_findings() -> None:
    """Quality is computed from findings_by_auditor, not the retrospective's quality_score."""
    from lib import make_trajectory_entry
    retro = _make_retrospective(
        quality_score=0.9,  # LLM wrote wrong value
        findings_by_auditor={"sec": 6, "cq": 2},
        repair_cycles=0,
    )
    entry = make_trajectory_entry(retro)
    # Should be 1/(1+8), not 0.9
    assert entry["reward"]["quality_score"] == pytest.approx(1 / 9, abs=1e-4)


def test_quality_zero_findings_capped_at_09() -> None:
    from lib import make_trajectory_entry
    retro = _make_retrospective(findings_by_auditor={})
    entry = make_trajectory_entry(retro)
    assert entry["reward"]["quality_score"] == pytest.approx(0.9)


def test_token_estimation_when_zero_tokens() -> None:
    """When total_token_usage is 0 and spawns > 0, tokens are estimated."""
    from lib import make_trajectory_entry
    retro = _make_retrospective(
        tokens=0,
        spawns=4,
        model_used={"a": "opus", "b": "haiku"},
        findings_by_auditor={},
    )
    entry = make_trajectory_entry(retro)
    assert entry["reward"]["token_usage_estimated"]
    assert entry["reward"]["cost_score"] > 0.0


def test_trajectory_real_tokens_not_estimated() -> None:
    from lib import make_trajectory_entry
    retro = _make_retrospective(tokens=50000, spawns=4, findings_by_auditor={})
    entry = make_trajectory_entry(retro)
    assert not entry["reward"]["token_usage_estimated"]


def test_composite_uses_standard_weights() -> None:
    from lib import make_trajectory_entry, COMPOSITE_WEIGHTS
    retro = _make_retrospective(
        findings_by_auditor={},  # quality=0.9
        repair_cycles=0,  # efficiency=1.0
        tokens=12000,
        spawns=1,
        risk_level="medium",
    )
    entry = make_trajectory_entry(retro)
    wq, we, wc = COMPOSITE_WEIGHTS
    q = entry["reward"]["quality_score"]
    e = entry["reward"]["efficiency_score"]
    c = entry["reward"]["cost_score"]
    expected = round(wq * q + we * e + wc * c, 6)
    assert entry["reward"]["composite_reward"] == pytest.approx(expected, abs=1e-5)


def test_quality_with_repairs_uses_heuristic() -> None:
    from lib import make_trajectory_entry
    retro = _make_retrospective(
        findings_by_auditor={"sec": 4, "cq": 4},  # 8 total
        repair_cycles=2,  # survives: max(0, 8-4)=4 → quality=1-4/8=0.5
    )
    entry = make_trajectory_entry(retro)
    assert entry["reward"]["quality_score"] == pytest.approx(0.5, abs=1e-4)
