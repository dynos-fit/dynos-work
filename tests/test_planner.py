#!/usr/bin/env python3
"""Tests for planner.py CLI subcommands (AC 7-9, 18)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "hooks"
DYNOPLANNER = HOOKS / "planner.py"


def _make_retrospective(
    task_id: str,
    task_type: str = "feature",
    quality_score: float = 0.8,
    risk_level: str = "medium",
    domains: str = "backend",
) -> dict:
    """Helper to build a minimal retrospective."""
    return {
        "task_id": task_id,
        "task_type": task_type,
        "quality_score": quality_score,
        "cost_score": 0.5,
        "efficiency_score": 0.6,
        "task_risk_level": risk_level,
        "task_domains": domains,
        "model_used_by_agent": {},
        "auditor_zero_finding_streaks": {},
        "executor_repair_frequency": {"backend-executor": 1},
        "findings_by_category": {},
        "findings_by_auditor": {},
        "repair_cycle_count": 0,
        "spec_review_iterations": 1,
        "subagent_spawn_count": 3,
        "wasted_spawns": 0,
        "total_token_usage": 10000,
        "agent_source": {},
        "task_outcome": "DONE",
    }


def _setup_project(
    root: Path,
    task_id: str = "task-20260404-001",
    retrospectives: list[dict] | None = None,
) -> Path:
    """Create minimal project structure for CLI testing."""
    dynos = root / ".dynos"
    dynos.mkdir(parents=True, exist_ok=True)

    # Task dir with manifest
    task_dir = dynos / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "task_id": task_id,
        "created_at": "2026-04-04T00:00:00Z",
        "title": "Test task",
        "raw_input": "Build a thing",
        "stage": "EXECUTING",
        "classification": {
            "type": "feature",
            "domains": ["backend"],
            "risk_level": "medium",
            "notes": "test",
        },
        "retry_counts": {},
        "blocked_reason": None,
        "completion_at": None,
    }
    (task_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    # Execution graph
    graph = {
        "task_id": task_id,
        "segments": [
            {
                "id": "seg-1",
                "executor": "backend-executor",
                "description": "Build backend",
                "files_expected": ["src/a.py"],
                "depends_on": [],
                "parallelizable": True,
                "criteria_ids": [1],
            }
        ],
    }
    (task_dir / "execution-graph.json").write_text(json.dumps(graph, indent=2))

    # Retrospectives
    for retro in (retrospectives or []):
        tid = retro["task_id"]
        rdir = dynos / tid
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / "task-retrospective.json").write_text(json.dumps(retro, indent=2))

    # Learned agents registry
    reg_dir = dynos / "learned-agents"
    reg_dir.mkdir(parents=True, exist_ok=True)
    (reg_dir / "registry.json").write_text(json.dumps({"version": 1, "agents": []}, indent=2))

    return task_dir


class TestDynoPlannerCLISubcommands:
    """AC 7: planner.py has three subcommands that exit cleanly."""

    def test_dynoplanner_file_exists(self) -> None:
        """planner.py exists in hooks directory."""
        assert DYNOPLANNER.exists(), f"planner.py should exist at {DYNOPLANNER}"

    def test_start_plan_help_exits_zero(self) -> None:
        """start-plan --help exits cleanly with return code 0."""
        result = subprocess.run(
            ["python3", str(DYNOPLANNER), "start-plan", "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "start-plan" in (result.stdout.lower() + result.stderr.lower())

    def test_planning_mode_help_exits_zero(self) -> None:
        """planning-mode --help exits cleanly with return code 0."""
        result = subprocess.run(
            ["python3", str(DYNOPLANNER), "planning-mode", "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_task_policy_help_exits_zero(self) -> None:
        """task-policy --help exits cleanly with return code 0."""
        result = subprocess.run(
            ["python3", str(DYNOPLANNER), "task-policy", "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_top_level_help_shows_all_subcommands(self) -> None:
        """Top-level --help lists all three subcommands."""
        result = subprocess.run(
            ["python3", str(DYNOPLANNER), "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        output = result.stdout.lower()
        assert "start-plan" in output
        assert "planning-mode" in output
        assert "task-policy" in output


class TestTaskPolicySubcommand:
    """AC 8: task-policy generates policy-packet.json with required fields."""

    def test_task_policy_creates_policy_packet_json(self, tmp_path: Path, monkeypatch) -> None:
        """task-policy creates .dynos/task-{id}/policy-packet.json."""
        root = tmp_path
        monkeypatch.setenv("DYNOS_HOME", str(root / ".dynos-home"))
        task_id = "task-20260404-001"
        _setup_project(root, task_id=task_id)

        result = subprocess.run(
            [
                "python3", str(DYNOPLANNER),
                "task-policy",
                "--root", str(root),
                "--task-id", task_id,
            ],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "DYNOS_HOME": str(root / ".dynos-home")},
        )
        assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"

        packet_path = root / ".dynos" / task_id / "policy-packet.json"
        assert packet_path.exists(), "policy-packet.json should be created"

        data = json.loads(packet_path.read_text())
        assert isinstance(data, dict)

    def test_policy_packet_has_required_fields(self, tmp_path: Path, monkeypatch) -> None:
        """policy-packet.json contains all required top-level fields."""
        root = tmp_path
        monkeypatch.setenv("DYNOS_HOME", str(root / ".dynos-home"))
        task_id = "task-20260404-001"
        _setup_project(root, task_id=task_id)

        subprocess.run(
            [
                "python3", str(DYNOPLANNER),
                "task-policy",
                "--root", str(root),
                "--task-id", task_id,
            ],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "DYNOS_HOME": str(root / ".dynos-home")},
        )

        packet_path = root / ".dynos" / task_id / "policy-packet.json"
        if not packet_path.exists():
            pytest.fail("policy-packet.json not created")

        data = json.loads(packet_path.read_text())

        required_fields = [
            "task_id",
            "models",
            "skip_decisions",
            "route_decisions",
            "prevention_rules",
            "fast_track",
            "planning_mode",
            "dreaming",
            "curiosity_targets",
        ]
        for field in required_fields:
            assert field in data, f"policy-packet.json missing required field: {field}"

    def test_policy_packet_decisions_have_source_field(self, tmp_path: Path, monkeypatch) -> None:
        """Each decision in policy-packet.json has a source field."""
        root = tmp_path
        monkeypatch.setenv("DYNOS_HOME", str(root / ".dynos-home"))
        task_id = "task-20260404-001"
        _setup_project(root, task_id=task_id)

        subprocess.run(
            [
                "python3", str(DYNOPLANNER),
                "task-policy",
                "--root", str(root),
                "--task-id", task_id,
            ],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "DYNOS_HOME": str(root / ".dynos-home")},
        )

        packet_path = root / ".dynos" / task_id / "policy-packet.json"
        if not packet_path.exists():
            pytest.fail("policy-packet.json not created")

        data = json.loads(packet_path.read_text())

        # Models should have source
        for key, value in data.get("models", {}).items():
            assert "source" in value, f"Model decision for {key} missing source field"

        # Skip decisions should have source
        for key, value in data.get("skip_decisions", {}).items():
            assert "source" in value, f"Skip decision for {key} missing source field"

        # Route decisions should have source
        for key, value in data.get("route_decisions", {}).items():
            assert "source" in value, f"Route decision for {key} missing source field"

    def test_policy_packet_task_id_matches(self, tmp_path: Path, monkeypatch) -> None:
        """policy-packet.json task_id matches the provided task id."""
        root = tmp_path
        monkeypatch.setenv("DYNOS_HOME", str(root / ".dynos-home"))
        task_id = "task-20260404-001"
        _setup_project(root, task_id=task_id)

        subprocess.run(
            [
                "python3", str(DYNOPLANNER),
                "task-policy",
                "--root", str(root),
                "--task-id", task_id,
            ],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "DYNOS_HOME": str(root / ".dynos-home")},
        )

        packet_path = root / ".dynos" / task_id / "policy-packet.json"
        if not packet_path.exists():
            pytest.fail("policy-packet.json not created")

        data = json.loads(packet_path.read_text())
        assert data["task_id"] == task_id


class TestStartPlanSubcommand:
    """AC 9: start-plan returns JSON with expected structure."""

    def test_start_plan_returns_json(self, tmp_path: Path, monkeypatch) -> None:
        """start-plan returns valid JSON output."""
        root = tmp_path
        monkeypatch.setenv("DYNOS_HOME", str(root / ".dynos-home"))
        _setup_project(root)

        result = subprocess.run(
            [
                "python3", str(DYNOPLANNER),
                "start-plan",
                "--root", str(root),
                "--task-type", "feature",
                "--domains", "backend",
                "--risk-level", "medium",
            ],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "DYNOS_HOME": str(root / ".dynos-home")},
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"

        data = json.loads(result.stdout)
        assert isinstance(data, dict)

    def test_start_plan_has_required_fields(self, tmp_path: Path, monkeypatch) -> None:
        """start-plan output contains planning_mode, planner_model, discovery_skip, trajectory_adjustments."""
        root = tmp_path
        monkeypatch.setenv("DYNOS_HOME", str(root / ".dynos-home"))
        _setup_project(root)

        result = subprocess.run(
            [
                "python3", str(DYNOPLANNER),
                "start-plan",
                "--root", str(root),
                "--task-type", "feature",
                "--domains", "backend",
                "--risk-level", "medium",
            ],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "DYNOS_HOME": str(root / ".dynos-home")},
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"

        data = json.loads(result.stdout)

        required_fields = [
            "planning_mode",
            "planner_model",
            "discovery_skip",
            "trajectory_adjustments",
        ]
        for field in required_fields:
            assert field in data, f"start-plan output missing required field: {field}"

    def test_start_plan_planning_mode_is_valid(self, tmp_path: Path, monkeypatch) -> None:
        """start-plan planning_mode is either 'standard' or 'hierarchical'."""
        root = tmp_path
        monkeypatch.setenv("DYNOS_HOME", str(root / ".dynos-home"))
        _setup_project(root)

        result = subprocess.run(
            [
                "python3", str(DYNOPLANNER),
                "start-plan",
                "--root", str(root),
                "--task-type", "feature",
                "--domains", "backend",
                "--risk-level", "medium",
            ],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "DYNOS_HOME": str(root / ".dynos-home")},
        )
        data = json.loads(result.stdout)
        assert data.get("planning_mode") in ("standard", "hierarchical")

    def test_start_plan_with_high_risk_may_recommend_hierarchical(self, tmp_path: Path, monkeypatch) -> None:
        """start-plan with high risk level may recommend hierarchical planning."""
        root = tmp_path
        monkeypatch.setenv("DYNOS_HOME", str(root / ".dynos-home"))
        _setup_project(root)

        result = subprocess.run(
            [
                "python3", str(DYNOPLANNER),
                "start-plan",
                "--root", str(root),
                "--task-type", "feature",
                "--domains", "backend,ui,db",
                "--risk-level", "high",
            ],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "DYNOS_HOME": str(root / ".dynos-home")},
        )
        data = json.loads(result.stdout)
        # High-risk with many domains may be hierarchical, but we just check it returns valid JSON
        assert data.get("planning_mode") in ("standard", "hierarchical")


class TestDreamingAndCuriosity:
    """AC 18: policy-packet.json includes dreaming (bool) and curiosity_targets (list)."""

    def test_policy_packet_dreaming_is_bool(self, tmp_path: Path, monkeypatch) -> None:
        """dreaming field in policy-packet.json is a boolean."""
        root = tmp_path
        monkeypatch.setenv("DYNOS_HOME", str(root / ".dynos-home"))
        task_id = "task-20260404-001"
        _setup_project(root, task_id=task_id)

        subprocess.run(
            [
                "python3", str(DYNOPLANNER),
                "task-policy",
                "--root", str(root),
                "--task-id", task_id,
            ],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "DYNOS_HOME": str(root / ".dynos-home")},
        )

        packet_path = root / ".dynos" / task_id / "policy-packet.json"
        if not packet_path.exists():
            pytest.fail("policy-packet.json not created")

        data = json.loads(packet_path.read_text())
        assert "dreaming" in data
        assert isinstance(data["dreaming"], bool)

    def test_policy_packet_curiosity_targets_is_list(self, tmp_path: Path, monkeypatch) -> None:
        """curiosity_targets field in policy-packet.json is a list of strings."""
        root = tmp_path
        monkeypatch.setenv("DYNOS_HOME", str(root / ".dynos-home"))
        task_id = "task-20260404-001"
        _setup_project(root, task_id=task_id)

        subprocess.run(
            [
                "python3", str(DYNOPLANNER),
                "task-policy",
                "--root", str(root),
                "--task-id", task_id,
            ],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "DYNOS_HOME": str(root / ".dynos-home")},
        )

        packet_path = root / ".dynos" / task_id / "policy-packet.json"
        if not packet_path.exists():
            pytest.fail("policy-packet.json not created")

        data = json.loads(packet_path.read_text())
        assert "curiosity_targets" in data
        assert isinstance(data["curiosity_targets"], list)
        # Each item should be a string
        for item in data["curiosity_targets"]:
            assert isinstance(item, str)

    def test_policy_packet_dreaming_default_false(self, tmp_path: Path, monkeypatch) -> None:
        """dreaming defaults to false when no novel patterns in trajectory."""
        root = tmp_path
        monkeypatch.setenv("DYNOS_HOME", str(root / ".dynos-home"))
        task_id = "task-20260404-001"
        _setup_project(root, task_id=task_id)

        subprocess.run(
            [
                "python3", str(DYNOPLANNER),
                "task-policy",
                "--root", str(root),
                "--task-id", task_id,
            ],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "DYNOS_HOME": str(root / ".dynos-home")},
        )

        packet_path = root / ".dynos" / task_id / "policy-packet.json"
        if not packet_path.exists():
            pytest.fail("policy-packet.json not created")

        data = json.loads(packet_path.read_text())
        assert data["dreaming"] is False

    def test_policy_packet_curiosity_targets_default_empty(self, tmp_path: Path, monkeypatch) -> None:
        """curiosity_targets defaults to empty list when no novel patterns."""
        root = tmp_path
        monkeypatch.setenv("DYNOS_HOME", str(root / ".dynos-home"))
        task_id = "task-20260404-001"
        _setup_project(root, task_id=task_id)

        subprocess.run(
            [
                "python3", str(DYNOPLANNER),
                "task-policy",
                "--root", str(root),
                "--task-id", task_id,
            ],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "DYNOS_HOME": str(root / ".dynos-home")},
        )

        packet_path = root / ".dynos" / task_id / "policy-packet.json"
        if not packet_path.exists():
            pytest.fail("policy-packet.json not created")

        data = json.loads(packet_path.read_text())
        assert data["curiosity_targets"] == []
