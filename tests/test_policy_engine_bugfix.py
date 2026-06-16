"""
Regression tests for AC4 (#30): memory/policy_engine.py filter-then-sort fix.

Bug: _build_model_policy_data at line 571-572 sorts all models by mean descending,
then gates the recommendation on the TOP-MEAN model having >=2 observations.
If the top-mean model has only 1 observation, the entire group is skipped —
even if a lower-ranked model has >=2 observations and a strong mean.

Fix: filter-then-sort — build eligible = [(m, s) for m, s in model_scores.items()
if len(s) >= 2], skip if eligible is empty, then sort eligible and take ranked[0].

These tests encode the FIXED behavior and will FAIL on current (unfixed) code.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "memory") not in sys.path:
    sys.path.insert(0, str(ROOT / "memory"))
if str(ROOT / "hooks") not in sys.path:
    sys.path.insert(0, str(ROOT / "hooks"))


def _get_build_model_policy_data():
    """Import the function under test. Uses memory module path."""
    import policy_engine
    return policy_engine._build_model_policy_data


class TestPolicyRankingFilterThenSort:
    """AC4 (#30): filter-then-sort must be applied before ranking models."""

    def test_policy_ranking_multi_obs_wins(self):
        """
        Scenario: model A has 1 observation (quality 1.0), model B has 2 observations
        (quality 0.9 each). The group is (role='executor', task_type='feature').

        BEFORE fix: sorted([A=1.0mean, B=0.9mean]) → A is ranked[0].
                    len(A's scores) == 1, which fails the >= 2 gate → no recommendation emitted.

        AFTER fix: filter first → eligible = [B] (A excluded because len==1).
                   Sort eligible by mean → [B]. Take ranked[0] = B.
                   Recommendation: model B.

        This test FAILS today because the current code emits nothing for this group.
        """
        fn = _get_build_model_policy_data()

        # Build retrospectives: A has 1 obs with quality 1.0, B has 2 obs with quality 0.9
        retrospectives = [
            # Model A: 1 observation, high quality
            {
                "task_type": "feature",
                "quality_score": 1.0,
                "agent_roles": {"executor": "model-A"},
            },
            # Model B: 2 observations, slightly lower quality
            {
                "task_type": "feature",
                "quality_score": 0.9,
                "agent_roles": {"executor": "model-B"},
            },
            {
                "task_type": "feature",
                "quality_score": 0.9,
                "agent_roles": {"executor": "model-B"},
            },
        ]

        result = fn(retrospectives)

        key = "executor:feature"
        assert key in result, (
            f"Expected recommendation for 'executor:feature' group but got nothing. "
            f"Result: {result}. "
            "AC4 (#30): filter-then-sort should emit model-B (2 obs) not suppress the group."
        )
        assert result[key]["model"] == "model-B", (
            f"Expected model-B (2 observations, eligible) to be recommended. "
            f"Got: {result[key]['model']}. "
            "model-A has only 1 observation and should be excluded by the >= 2 filter."
        )
        assert result[key]["sample_count"] == 2, (
            f"Expected sample_count == 2 for model-B. Got: {result[key]['sample_count']}"
        )

    def test_policy_ranking_no_eligible_emits_nothing(self):
        """
        Scenario: ALL models in a group have only 1 observation.
        After fix: eligible list is empty → continue → no recommendation for this group.

        Before fix: ranked[0] has 1 obs → gate fails → also no recommendation.
        This test is consistent both before and after the fix (same behavior expected),
        but is included as a guard to ensure the filter-then-sort doesn't accidentally
        emit recommendations for 1-observation models.
        """
        fn = _get_build_model_policy_data()

        retrospectives = [
            {
                "task_type": "bugfix",
                "quality_score": 0.95,
                "agent_roles": {"executor": "model-X"},
            },
            {
                "task_type": "bugfix",
                "quality_score": 0.85,
                "agent_roles": {"executor": "model-Y"},
            },
        ]

        result = fn(retrospectives)

        key = "executor:bugfix"
        assert key not in result, (
            f"Expected no recommendation for 'executor:bugfix' (all models have 1 obs each). "
            f"Got: {result.get(key)}. "
            "No model should be recommended when none have >= 2 observations."
        )

    def test_policy_ranking_eligible_model_selected_despite_lower_mean(self):
        """
        Edge case: model C has 3 observations (mean 0.75) and model D has 1 observation
        (mean 0.99). After filter, only C is eligible. C must be recommended, not suppressed.
        """
        fn = _get_build_model_policy_data()

        retrospectives = [
            # D: 1 observation, very high quality
            {"task_type": "refactor", "quality_score": 0.99, "agent_roles": {"planner": "model-D"}},
            # C: 3 observations, decent quality
            {"task_type": "refactor", "quality_score": 0.75, "agent_roles": {"planner": "model-C"}},
            {"task_type": "refactor", "quality_score": 0.80, "agent_roles": {"planner": "model-C"}},
            {"task_type": "refactor", "quality_score": 0.70, "agent_roles": {"planner": "model-C"}},
        ]

        result = fn(retrospectives)

        key = "planner:refactor"
        assert key in result, (
            f"Expected recommendation for 'planner:refactor'. Result: {result}"
        )
        assert result[key]["model"] == "model-C", (
            f"model-C (3 obs) must win over model-D (1 obs, higher mean). "
            f"Got: {result[key]['model']}."
        )

    def test_policy_ranking_multiple_eligible_selects_highest_mean(self):
        """
        When multiple models are eligible (>= 2 obs each), the one with the highest
        mean quality must be selected.
        """
        fn = _get_build_model_policy_data()

        retrospectives = [
            # E: 2 obs, mean 0.60
            {"task_type": "feature", "quality_score": 0.60, "agent_roles": {"auditor": "model-E"}},
            {"task_type": "feature", "quality_score": 0.60, "agent_roles": {"auditor": "model-E"}},
            # F: 2 obs, mean 0.90
            {"task_type": "feature", "quality_score": 0.90, "agent_roles": {"auditor": "model-F"}},
            {"task_type": "feature", "quality_score": 0.90, "agent_roles": {"auditor": "model-F"}},
        ]

        result = fn(retrospectives)

        key = "auditor:feature"
        assert key in result, f"Expected recommendation for 'auditor:feature'. Result: {result}"
        assert result[key]["model"] == "model-F", (
            f"model-F (mean 0.90) must be selected over model-E (mean 0.60). "
            f"Got: {result[key]['model']}."
        )
