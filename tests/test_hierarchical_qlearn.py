"""Tests for hierarchical Q-learning repair planning.

Covers:
  - Per-category executor action spaces
  - Three-table hierarchical selection (executor → route → model)
  - Hard constraints (security floor, escalation) override Q-table
  - Route mode only offers learned when agent exists
  - Update feeds all three tables
  - Disabled Q-learning returns defaults
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestExecutorActionSpace:
    def test_dead_code_restricted_to_refactor(self):
        from memory.lib_qlearn import _executors_for_category
        executors = _executors_for_category("dc")
        assert executors == ["refactor-executor"]

    def test_security_restricted(self):
        from memory.lib_qlearn import _executors_for_category
        executors = _executors_for_category("sec")
        assert "ui-executor" not in executors
        assert "ml-executor" not in executors
        assert "backend-executor" in executors

    def test_db_restricted(self):
        from memory.lib_qlearn import _executors_for_category
        assert _executors_for_category("db") == ["db-executor"]

    def test_unknown_category_gets_all(self):
        from memory.lib_qlearn import _executors_for_category, ALL_EXECUTORS
        assert _executors_for_category("unknown_xyz") == ALL_EXECUTORS

    def test_performance_restricted(self):
        from memory.lib_qlearn import _executors_for_category
        executors = _executors_for_category("perf")
        assert "backend-executor" in executors
        assert "db-executor" in executors
        assert "ui-executor" not in executors


class TestBuildRepairPlanHierarchical:
    def _make_finding(self, finding_id="sec-001", severity="critical",
                      auditor="security-auditor", retry_count=0):
        return {
            "id": finding_id,
            "severity": severity,
            "auditor": auditor,
            "retry_count": retry_count,
        }

    @mock.patch("memory.lib_qlearn.project_policy", return_value={"repair_qlearning": True, "repair_epsilon": 0.0})
    @mock.patch("memory.lib_qlearn._has_learned_agent", return_value=(False, None, None))
    def test_executor_restricted_for_dead_code(self, mock_learned, mock_policy, tmp_path):
        from memory.lib_qlearn import build_repair_plan
        with mock.patch.dict(os.environ, {"DYNOS_HOME": str(tmp_path)}):
            result = build_repair_plan(tmp_path, [self._make_finding("dc-001", "major", "dead-code-auditor")], "feature")
        assignment = result["assignments"][0]
        assert assignment["assigned_executor"] == "refactor-executor"

    @mock.patch("memory.lib_qlearn.project_policy", return_value={"repair_qlearning": True, "repair_epsilon": 0.0})
    @mock.patch("memory.lib_qlearn._has_learned_agent", return_value=(False, None, None))
    def test_security_floor_overrides_model(self, mock_learned, mock_policy, tmp_path):
        from memory.lib_qlearn import build_repair_plan
        with mock.patch.dict(os.environ, {"DYNOS_HOME": str(tmp_path)}):
            result = build_repair_plan(tmp_path, [self._make_finding("sec-001", "critical", "security-auditor")], "feature")
        assignment = result["assignments"][0]
        assert assignment["model_override"] == "opus"
        assert assignment["model_source"] == "security_floor"

    @mock.patch("memory.lib_qlearn.project_policy", return_value={"repair_qlearning": True, "repair_epsilon": 0.0})
    @mock.patch("memory.lib_qlearn._has_learned_agent", return_value=(False, None, None))
    def test_escalation_overrides_model(self, mock_learned, mock_policy, tmp_path):
        from memory.lib_qlearn import build_repair_plan
        with mock.patch.dict(os.environ, {"DYNOS_HOME": str(tmp_path)}):
            result = build_repair_plan(tmp_path, [self._make_finding("cq-001", "major", "code-quality-auditor", retry_count=3)], "feature")
        assignment = result["assignments"][0]
        assert assignment["model_override"] == "opus"
        assert assignment["model_source"] == "escalation"

    @mock.patch("memory.lib_qlearn.project_policy", return_value={"repair_qlearning": True, "repair_epsilon": 0.0})
    @mock.patch("memory.lib_qlearn._has_learned_agent", return_value=(True, "/path/to/agent.md", "learned-backend-v1"))
    def test_route_mode_offered_when_learned_exists(self, mock_learned, mock_policy, tmp_path):
        from memory.lib_qlearn import build_repair_plan
        with mock.patch.dict(os.environ, {"DYNOS_HOME": str(tmp_path)}):
            result = build_repair_plan(tmp_path, [self._make_finding("cq-001", "major", "code-quality-auditor")], "feature")
        assignment = result["assignments"][0]
        # Route mode should be either generic or learned (Q-table decides)
        assert assignment["route_mode"] in ("generic", "learned")
        assert assignment["route_source"] != "no_learned_agent"

    @mock.patch("memory.lib_qlearn.project_policy", return_value={"repair_qlearning": True, "repair_epsilon": 0.0})
    @mock.patch("memory.lib_qlearn._has_learned_agent", return_value=(False, None, None))
    def test_route_mode_generic_when_no_learned(self, mock_learned, mock_policy, tmp_path):
        from memory.lib_qlearn import build_repair_plan
        with mock.patch.dict(os.environ, {"DYNOS_HOME": str(tmp_path)}):
            result = build_repair_plan(tmp_path, [self._make_finding("cq-001", "major", "code-quality-auditor")], "feature")
        assignment = result["assignments"][0]
        assert assignment["route_mode"] == "generic"
        assert assignment["route_source"] == "no_learned_agent"
        assert assignment["agent_path"] is None

    @mock.patch("memory.lib_qlearn.project_policy", return_value={"repair_qlearning": False})
    def test_disabled_returns_defaults(self, mock_policy, tmp_path):
        from memory.lib_qlearn import build_repair_plan
        with mock.patch.dict(os.environ, {"DYNOS_HOME": str(tmp_path)}):
            result = build_repair_plan(tmp_path, [self._make_finding()], "feature")
        assert result["source"] == "default"
        assignment = result["assignments"][0]
        assert assignment["assigned_executor"] is None
        assert assignment["model_override"] == "opus"  # security floor still applies

    @mock.patch("memory.lib_qlearn.project_policy", return_value={"repair_qlearning": True, "repair_epsilon": 0.0})
    @mock.patch("memory.lib_qlearn._has_learned_agent", return_value=(False, None, None))
    def test_output_has_all_fields(self, mock_learned, mock_policy, tmp_path):
        from memory.lib_qlearn import build_repair_plan
        with mock.patch.dict(os.environ, {"DYNOS_HOME": str(tmp_path)}):
            result = build_repair_plan(tmp_path, [self._make_finding("cq-001", "minor", "code-quality-auditor")], "feature")
        a = result["assignments"][0]
        assert "assigned_executor" in a
        assert "executor_source" in a
        assert "route_mode" in a
        assert "route_source" in a
        assert "model_override" in a
        assert "model_source" in a


class TestUpdateFromOutcomes:
    @mock.patch("memory.lib_qlearn.project_policy", return_value={"repair_qlearning": True})
    def test_updates_all_three_tables(self, mock_policy, tmp_path):
        from memory.lib_qlearn import update_from_outcomes, load_q_table
        with mock.patch.dict(os.environ, {"DYNOS_HOME": str(tmp_path)}):
            outcome = {
                "finding_id": "sec-001",
                "state": "sec:critical:feature:0",
                "executor": "backend-executor",
                "route_mode": "generic",
                "model": "opus",
                "resolved": True,
                "new_findings": 0,
                "tokens_used": 5000,
                "next_state": None,
            }
            result = update_from_outcomes(tmp_path, [outcome], "feature")

        assert result["updated"] is True
        update = result["updates"][0]
        assert update["executor_q_new"] is not None
        assert update["route_q_new"] is not None
        assert update["model_q_new"] is not None
        assert update["reward"] > 0

    @mock.patch("memory.lib_qlearn.project_policy", return_value={"repair_qlearning": False})
    def test_disabled_skips_update(self, mock_policy, tmp_path):
        from memory.lib_qlearn import update_from_outcomes
        result = update_from_outcomes(tmp_path, [{"finding_id": "x"}], "feature")
        assert result["updated"] is False

    @mock.patch("memory.lib_qlearn.project_policy", return_value={"repair_qlearning": True})
    def test_failed_repair_negative_reward(self, mock_policy, tmp_path):
        from memory.lib_qlearn import update_from_outcomes
        with mock.patch.dict(os.environ, {"DYNOS_HOME": str(tmp_path)}):
            outcome = {
                "finding_id": "cq-001",
                "state": "cq:major:feature:0",
                "executor": "refactor-executor",
                "route_mode": "generic",
                "model": "sonnet",
                "resolved": False,
                "new_findings": 2,
                "tokens_used": 15000,
                "next_state": "cq:major:feature:1",
            }
            result = update_from_outcomes(tmp_path, [outcome], "feature")

        assert result["updates"][0]["reward"] < 0


class TestHierarchicalStateEncoding:
    def test_route_state_includes_executor(self):
        """Route Q-table state should include the chosen executor."""
        from memory.lib_qlearn import encode_repair_state
        base = encode_repair_state("sec", "critical", "feature", 0)
        route_state = f"{base}:backend-executor"
        assert "sec:critical:feature:0:backend-executor" == route_state

    def test_model_state_includes_executor_and_route(self):
        """Model Q-table state should include executor and route mode."""
        from memory.lib_qlearn import encode_repair_state
        base = encode_repair_state("cq", "major", "bugfix", 1)
        model_state = f"{base}:refactor-executor:learned"
        assert "cq:major:bugfix:1:refactor-executor:learned" == model_state
