"""Tests for file-lock protection of read-modify-write JSON mutators.

Once worktrees share main's persistent dir (the slug-normalization fix),
concurrent writes from multiple checkouts become possible. The existing
atomic write_json protects against torn reads, but not against lost
updates in the RMW pattern:
    A: read → merge(new_A) → write    (doesn't see B's changes)
    B: read → merge(new_B) → write    (overwrites A's result)
Result: only B's new rule survives. A's update is lost.

The fix wraps RMW sequences in fcntl.LOCK_EX. These tests use real
multiprocessing forks to prove concurrent writers don't lose rules.
"""
from __future__ import annotations

import json
import multiprocessing
import os
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "memory"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sandbox" / "calibration"))


def _worker_apply_analysis(task_dir_str: str, rule_text: str, delay: float = 0.0):
    """Child process entry point for concurrent apply_analysis."""
    import sys as _s
    _s.path.insert(0, str(Path(task_dir_str).parent.parent.parent / "hooks"))
    _s.path.insert(0, str(Path(task_dir_str).parent.parent.parent / "memory"))
    from postmortem_analysis import apply_analysis
    if delay:
        time.sleep(delay)
    apply_analysis(Path(task_dir_str), {
        "summary": f"worker applying {rule_text}",
        "prevention_rules": [{
            "executor": "all",
            "category": "sec",
            "rule": rule_text,
            "source_finding": f"finding-for-{rule_text}",
            "rationale": "test",
            "enforcement": "prompt-constraint",
        }],
    })


def _make_task_dir(tmp_path: Path, task_id: str) -> Path:
    task_dir = tmp_path / ".dynos" / task_id
    task_dir.mkdir(parents=True)
    (task_dir / "task-retrospective.json").write_text(json.dumps({
        "task_id": task_id, "task_outcome": "DONE",
        "findings_by_auditor": {}, "repair_cycle_count": 0,
    }))
    return task_dir


class TestPreventionRulesLock:
    def test_sequential_apply_accumulates(self, tmp_path: Path, monkeypatch):
        """Sanity baseline: sequential calls obviously accumulate."""
        monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "home"))
        task_dir = _make_task_dir(tmp_path, "task-A")
        from postmortem_analysis import apply_analysis
        apply_analysis(task_dir, {"summary": "t", "prevention_rules": [
            {"executor": "all", "category": "sec", "rule": "rule-A",
             "source_finding": "f-A", "rationale": "r", "enforcement": "prompt-constraint"}
        ]})
        apply_analysis(task_dir, {"summary": "t", "prevention_rules": [
            {"executor": "all", "category": "sec", "rule": "rule-B",
             "source_finding": "f-B", "rationale": "r", "enforcement": "prompt-constraint"}
        ]})
        # Find the rules file
        from lib_core import _persistent_project_dir
        rules_path = _persistent_project_dir(tmp_path) / "prevention-rules.json"
        data = json.loads(rules_path.read_text())
        rule_texts = [r["rule"] for r in data["rules"]]
        assert "rule-A" in rule_texts
        assert "rule-B" in rule_texts

    def test_concurrent_apply_does_not_lose_updates(self, tmp_path: Path, monkeypatch):
        """The load-bearing test: two concurrent apply_analysis calls with
        disjoint rule sets. Without the file lock, one rule is lost. With
        the lock, both survive."""
        monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "home"))
        task_dir = _make_task_dir(tmp_path, "task-concurrent")
        # Make the child processes inherit DYNOS_HOME
        os.environ["DYNOS_HOME"] = str(tmp_path / "home")

        ctx = multiprocessing.get_context("spawn")
        procs = [
            ctx.Process(target=_worker_apply_analysis, args=(str(task_dir), f"concurrent-rule-{i}", 0.01 * i))
            for i in range(8)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=30)
            assert not p.is_alive(), "worker hung"
            assert p.exitcode == 0, f"worker exited {p.exitcode}"

        from lib_core import _persistent_project_dir
        rules_path = _persistent_project_dir(tmp_path) / "prevention-rules.json"
        data = json.loads(rules_path.read_text())
        rule_texts = {r["rule"] for r in data["rules"]}
        expected = {f"concurrent-rule-{i}" for i in range(8)}
        assert expected.issubset(rule_texts), (
            f"lost updates: expected {expected}, got {rule_texts}. "
            f"Missing: {expected - rule_texts}"
        )


def _worker_register_agent(root_str: str, agent_name: str, delay: float = 0.0):
    """Child process entry point for concurrent register_learned_agent."""
    import sys as _s
    _s.path.insert(0, str(Path(root_str) / "hooks"))
    _s.path.insert(0, str(Path(root_str) / "sandbox" / "calibration"))
    from lib_registry import register_learned_agent
    if delay:
        time.sleep(delay)
    register_learned_agent(
        Path(root_str),
        agent_name=agent_name,
        role="backend-executor",
        task_type="refactor",
        path=f"/tmp/{agent_name}.md",
        generated_from=f"task-{agent_name}",
    )


class TestLearnedAgentRegistryLock:
    def test_concurrent_register_does_not_lose_agents(self, tmp_path: Path, monkeypatch):
        """Concurrent register_learned_agent calls with different agent
        names must produce a registry containing ALL agents. Without the
        lock, the slower writer's entry overwrites the faster's."""
        monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "home"))
        os.environ["DYNOS_HOME"] = str(tmp_path / "home")
        # Give the root a real filesystem existence
        root = tmp_path / "fake-repo"
        root.mkdir()

        ctx = multiprocessing.get_context("spawn")
        N = 6
        procs = [
            ctx.Process(target=_worker_register_agent, args=(str(root), f"agent-{i}", 0.01 * i))
            for i in range(N)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=30)
            assert not p.is_alive()
            assert p.exitcode == 0

        from lib_core import _persistent_project_dir
        reg_path = _persistent_project_dir(root) / "learned-agents" / "registry.json"
        data = json.loads(reg_path.read_text())
        names = {a["agent_name"] for a in data.get("agents", [])}
        expected = {f"agent-{i}" for i in range(N)}
        assert expected == names, (
            f"lost agents: expected {expected}, got {names}. Missing: {expected - names}"
        )
