#!/usr/bin/env python3
"""Tests for planner.py CLI subcommands (AC 7-9, 18)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "hooks"
PLANNER = HOOKS / "planner.py"


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


# AC 7: planner.py has three subcommands that exit cleanly.


def test_planner_file_exists() -> None:
    """planner.py exists in hooks directory."""
    assert PLANNER.exists(), f"planner.py should exist at {PLANNER}"


def test_start_plan_help_exits_zero() -> None:
    """start-plan --help exits cleanly with return code 0."""
    result = subprocess.run(
        ["python3", str(PLANNER), "start-plan", "--help"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "start-plan" in (result.stdout.lower() + result.stderr.lower())


def test_planning_mode_help_exits_zero() -> None:
    """planning-mode --help exits cleanly with return code 0."""
    result = subprocess.run(
        ["python3", str(PLANNER), "planning-mode", "--help"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"


def test_task_policy_help_exits_zero() -> None:
    """task-policy --help exits cleanly with return code 0."""
    result = subprocess.run(
        ["python3", str(PLANNER), "task-policy", "--help"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"


def test_top_level_help_shows_all_subcommands() -> None:
    """Top-level --help lists all three subcommands."""
    result = subprocess.run(
        ["python3", str(PLANNER), "--help"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    output = result.stdout.lower()
    assert "start-plan" in output
    assert "planning-mode" in output
    assert "task-policy" in output


# AC 8: task-policy generates policy-packet.json with required fields.


def test_task_policy_creates_policy_packet_json(dynos_home) -> None:
    """task-policy creates .dynos/task-{id}/policy-packet.json."""
    root = dynos_home.root
    task_id = "task-20260404-001"
    _setup_project(root, task_id=task_id)

    result = subprocess.run(
        [
            "python3", str(PLANNER),
            "task-policy",
            "--root", str(root),
            "--task-id", task_id,
        ],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "DYNOS_HOME": str(dynos_home.dynos_home)},
    )
    assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"

    packet_path = root / ".dynos" / task_id / "policy-packet.json"
    assert packet_path.exists(), "policy-packet.json should be created"

    data = json.loads(packet_path.read_text())
    assert isinstance(data, dict)


def test_policy_packet_has_required_fields(dynos_home) -> None:
    """policy-packet.json contains all required top-level fields."""
    root = dynos_home.root
    task_id = "task-20260404-001"
    _setup_project(root, task_id=task_id)

    subprocess.run(
        [
            "python3", str(PLANNER),
            "task-policy",
            "--root", str(root),
            "--task-id", task_id,
        ],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "DYNOS_HOME": str(dynos_home.dynos_home)},
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


def test_policy_packet_decisions_have_source_field(dynos_home) -> None:
    """Each decision in policy-packet.json has a source field."""
    root = dynos_home.root
    task_id = "task-20260404-001"
    _setup_project(root, task_id=task_id)

    subprocess.run(
        [
            "python3", str(PLANNER),
            "task-policy",
            "--root", str(root),
            "--task-id", task_id,
        ],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "DYNOS_HOME": str(dynos_home.dynos_home)},
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


def test_policy_packet_task_id_matches(dynos_home) -> None:
    """policy-packet.json task_id matches the provided task id."""
    root = dynos_home.root
    task_id = "task-20260404-001"
    _setup_project(root, task_id=task_id)

    subprocess.run(
        [
            "python3", str(PLANNER),
            "task-policy",
            "--root", str(root),
            "--task-id", task_id,
        ],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "DYNOS_HOME": str(dynos_home.dynos_home)},
    )

    packet_path = root / ".dynos" / task_id / "policy-packet.json"
    if not packet_path.exists():
        pytest.fail("policy-packet.json not created")

    data = json.loads(packet_path.read_text())
    assert data["task_id"] == task_id


# AC 9: start-plan returns JSON with expected structure.


def test_start_plan_returns_json(dynos_home) -> None:
    """start-plan returns valid JSON output."""
    root = dynos_home.root
    _setup_project(root)

    result = subprocess.run(
        [
            "python3", str(PLANNER),
            "start-plan",
            "--root", str(root),
            "--task-type", "feature",
            "--domains", "backend",
            "--risk-level", "medium",
        ],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "DYNOS_HOME": str(dynos_home.dynos_home)},
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"

    data = json.loads(result.stdout)
    assert isinstance(data, dict)


def test_start_plan_has_required_fields(dynos_home) -> None:
    """start-plan output contains planning_mode, planner_model, discovery_skip, trajectory_adjustments."""
    root = dynos_home.root
    _setup_project(root)

    result = subprocess.run(
        [
            "python3", str(PLANNER),
            "start-plan",
            "--root", str(root),
            "--task-type", "feature",
            "--domains", "backend",
            "--risk-level", "medium",
        ],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "DYNOS_HOME": str(dynos_home.dynos_home)},
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


def test_start_plan_planning_mode_is_valid(dynos_home) -> None:
    """start-plan planning_mode is either 'standard' or 'hierarchical'."""
    root = dynos_home.root
    _setup_project(root)

    result = subprocess.run(
        [
            "python3", str(PLANNER),
            "start-plan",
            "--root", str(root),
            "--task-type", "feature",
            "--domains", "backend",
            "--risk-level", "medium",
        ],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "DYNOS_HOME": str(dynos_home.dynos_home)},
    )
    data = json.loads(result.stdout)
    assert data.get("planning_mode") in ("standard", "hierarchical")


def test_start_plan_with_high_risk_may_recommend_hierarchical(dynos_home) -> None:
    """start-plan with high risk level may recommend hierarchical planning."""
    root = dynos_home.root
    _setup_project(root)

    result = subprocess.run(
        [
            "python3", str(PLANNER),
            "start-plan",
            "--root", str(root),
            "--task-type", "feature",
            "--domains", "backend,ui,db",
            "--risk-level", "high",
        ],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "DYNOS_HOME": str(dynos_home.dynos_home)},
    )
    data = json.loads(result.stdout)
    # High-risk with many domains may be hierarchical, but we just check it returns valid JSON
    assert data.get("planning_mode") in ("standard", "hierarchical")


# AC 18: policy-packet.json includes dreaming (bool) and curiosity_targets (list).


def test_policy_packet_dreaming_is_bool(dynos_home) -> None:
    """dreaming field in policy-packet.json is a boolean."""
    root = dynos_home.root
    task_id = "task-20260404-001"
    _setup_project(root, task_id=task_id)

    subprocess.run(
        [
            "python3", str(PLANNER),
            "task-policy",
            "--root", str(root),
            "--task-id", task_id,
        ],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "DYNOS_HOME": str(dynos_home.dynos_home)},
    )

    packet_path = root / ".dynos" / task_id / "policy-packet.json"
    if not packet_path.exists():
        pytest.fail("policy-packet.json not created")

    data = json.loads(packet_path.read_text())
    assert "dreaming" in data
    assert isinstance(data["dreaming"], bool)


def test_policy_packet_curiosity_targets_is_list(dynos_home) -> None:
    """curiosity_targets field in policy-packet.json is a list of strings."""
    root = dynos_home.root
    task_id = "task-20260404-001"
    _setup_project(root, task_id=task_id)

    subprocess.run(
        [
            "python3", str(PLANNER),
            "task-policy",
            "--root", str(root),
            "--task-id", task_id,
        ],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "DYNOS_HOME": str(dynos_home.dynos_home)},
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


def test_policy_packet_dreaming_default_false(dynos_home) -> None:
    """dreaming defaults to false when no novel patterns in trajectory."""
    root = dynos_home.root
    task_id = "task-20260404-001"
    _setup_project(root, task_id=task_id)

    subprocess.run(
        [
            "python3", str(PLANNER),
            "task-policy",
            "--root", str(root),
            "--task-id", task_id,
        ],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "DYNOS_HOME": str(dynos_home.dynos_home)},
    )

    packet_path = root / ".dynos" / task_id / "policy-packet.json"
    if not packet_path.exists():
        pytest.fail("policy-packet.json not created")

    data = json.loads(packet_path.read_text())
    assert not data["dreaming"]


def test_policy_packet_curiosity_targets_default_empty(dynos_home) -> None:
    """curiosity_targets defaults to empty list when no novel patterns."""
    root = dynos_home.root
    task_id = "task-20260404-001"
    _setup_project(root, task_id=task_id)

    subprocess.run(
        [
            "python3", str(PLANNER),
            "task-policy",
            "--root", str(root),
            "--task-id", task_id,
        ],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "DYNOS_HOME": str(dynos_home.dynos_home)},
    )

    packet_path = root / ".dynos" / task_id / "policy-packet.json"
    if not packet_path.exists():
        pytest.fail("policy-packet.json not created")

    data = json.loads(packet_path.read_text())
    assert data["curiosity_targets"] == []


# ---------------------------------------------------------------------------
# TDD: shared RouterContext threading through planner.py
# (task-20260417-015 — tests written BEFORE the refactor; must fail now.)
# ---------------------------------------------------------------------------


def _ensure_hooks_on_path() -> None:
    """Ensure hooks/ is importable for direct module-level tests."""
    import sys
    hooks_dir = str(HOOKS)
    if hooks_dir not in sys.path:
        sys.path.insert(0, hooks_dir)


def test_policy_packet_reuses_single_router_context(
    dynos_home, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_build_policy_packet must construct exactly one RouterContext and thread
    it through every downstream resolve_* / build_audit_plan call, so that
    project_policy(root) is executed exactly once per packet build.

    Covers AC 5 (shared ctx) and AC 7 (cached reads not repeated).
    """
    _ensure_hooks_on_path()

    root = dynos_home.root
    task_id = "task-20260417-015-a"
    _setup_project(root, task_id=task_id)

    # Import after path insertion so router/planner are resolvable.
    import router as router_mod  # type: ignore[import-not-found]
    import planner as planner_mod  # type: ignore[import-not-found]

    call_count = {"n": 0}
    real_project_policy = router_mod.project_policy

    def counting_project_policy(r):
        call_count["n"] += 1
        return real_project_policy(r)

    # Patch the name that RouterContext.policy resolves at call time.
    monkeypatch.setattr(router_mod, "project_policy", counting_project_policy)

    packet = planner_mod._build_policy_packet(root, task_id)

    # Basic sanity: output shape preserved.
    assert isinstance(packet, dict)
    assert packet.get("task_id") == task_id
    assert "models" in packet
    assert "route_decisions" in packet
    assert "audit_plan" in packet

    # The refactor's invariant: exactly one project_policy read per packet.
    # Not 0 (ctx.policy must actually load), not 2+ (no redundant re-reads).
    assert call_count["n"] == 1, (
        f"project_policy should be invoked exactly once when a shared "
        f"RouterContext is threaded through _build_policy_packet; "
        f"got {call_count['n']} invocations. Each extra invocation "
        f"indicates a resolve_* or build_audit_plan call that is missing "
        f"the ctx= kwarg, forcing a fresh policy read."
    )


def test_planner_resolve_calls_thread_shared_ctx() -> None:
    """Every resolve_model / resolve_skip / resolve_route call inside
    _build_policy_packet and cmd_start_plan must pass ctx= as a keyword
    argument. Enforced via AST inspection of hooks/planner.py source.

    Covers AC 8.
    """
    import ast

    source = PLANNER.read_text()
    tree = ast.parse(source)

    target_funcs = {"_build_policy_packet", "cmd_start_plan"}
    resolve_names = {"resolve_model", "resolve_skip", "resolve_route"}

    found_funcs: set[str] = set()
    violations: list[str] = []
    checked_calls = 0

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name not in target_funcs:
            continue
        found_funcs.add(node.name)

        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue

            # Resolve callee name for bare-name calls like resolve_model(...).
            callee_name: str | None = None
            if isinstance(child.func, ast.Name):
                callee_name = child.func.id
            elif isinstance(child.func, ast.Attribute):
                callee_name = child.func.attr

            if callee_name not in resolve_names:
                continue

            checked_calls += 1
            kwarg_names = {kw.arg for kw in child.keywords if kw.arg is not None}
            if "ctx" not in kwarg_names:
                violations.append(
                    f"{node.name}:{child.lineno} — "
                    f"{callee_name}(...) missing ctx= kwarg; "
                    f"saw keywords={sorted(kwarg_names)}"
                )

    # Sanity: both target functions must exist in the source being audited.
    assert found_funcs == target_funcs, (
        f"Expected to audit {target_funcs} in hooks/planner.py; "
        f"only located {found_funcs}"
    )
    # Sanity: the audit must actually find resolve_* call sites — if not,
    # something reshaped planner.py in an unexpected way and the test is
    # silently passing on zero calls. Refuse to pass vacuously.
    assert checked_calls > 0, (
        "AST audit found zero resolve_model/resolve_skip/resolve_route calls "
        "inside _build_policy_packet / cmd_start_plan — test would pass "
        "vacuously. Inspect hooks/planner.py."
    )
    assert not violations, (
        "Every resolve_model/resolve_skip/resolve_route call in "
        "_build_policy_packet and cmd_start_plan must pass ctx= as a "
        "keyword argument (AC 8). Violations:\n  - "
        + "\n  - ".join(violations)
    )


def test_build_audit_plan_accepts_optional_ctx(dynos_home) -> None:
    """build_audit_plan(root, task_type, domains, fast_track=False) must work
    both without ctx (existing CLI/external callers) and with a caller-
    supplied ctx=RouterContext(root) (new planner callsite).

    Covers AC 2.
    """
    _ensure_hooks_on_path()

    root = dynos_home.root
    _setup_project(root, task_id="task-20260417-015-b")

    import router as router_mod  # type: ignore[import-not-found]

    # 1. No-ctx call — preserves current CLI/external contract.
    plan_no_ctx = router_mod.build_audit_plan(
        root, "feature", ["backend"], fast_track=False
    )
    assert isinstance(plan_no_ctx, dict)
    assert "auditors" in plan_no_ctx, (
        f"build_audit_plan(no-ctx) must return a dict with 'auditors' key; "
        f"got keys={sorted(plan_no_ctx.keys())}"
    )
    assert isinstance(plan_no_ctx["auditors"], list)

    # 2. With-ctx call — must accept the new keyword-only parameter.
    ctx = router_mod.RouterContext(root)
    try:
        plan_with_ctx = router_mod.build_audit_plan(
            root, "feature", ["backend"], fast_track=False, ctx=ctx
        )
    except TypeError as exc:
        pytest.fail(
            "build_audit_plan must accept ctx=RouterContext as a keyword "
            f"argument (AC 2). Got TypeError: {exc}"
        )

    assert isinstance(plan_with_ctx, dict)
    assert "auditors" in plan_with_ctx
    assert isinstance(plan_with_ctx["auditors"], list)

    # Same inputs, same observable shape: keys match.
    assert set(plan_no_ctx.keys()) == set(plan_with_ctx.keys()), (
        "With-ctx and without-ctx calls must return dicts with identical "
        "top-level keys. No-ctx="
        f"{sorted(plan_no_ctx.keys())} vs with-ctx="
        f"{sorted(plan_with_ctx.keys())}"
    )


def test_build_executor_plan_accepts_optional_ctx(dynos_home) -> None:
    """build_executor_plan(root, task_type, segments) must work both without
    ctx and with a caller-supplied ctx=RouterContext(root).

    Covers AC 3.
    """
    _ensure_hooks_on_path()

    root = dynos_home.root
    _setup_project(root, task_id="task-20260417-015-c")

    import router as router_mod  # type: ignore[import-not-found]

    segments = [
        {
            "id": "seg-1",
            "executor": "backend-executor",
            "description": "Build backend",
            "files_expected": ["src/a.py"],
            "depends_on": [],
            "parallelizable": True,
            "criteria_ids": [1],
        }
    ]

    # 1. No-ctx call — preserves current CLI/external contract.
    plan_no_ctx = router_mod.build_executor_plan(root, "feature", segments)
    assert isinstance(plan_no_ctx, dict)
    assert "segments" in plan_no_ctx, (
        f"build_executor_plan(no-ctx) must return a dict with 'segments' "
        f"key; got keys={sorted(plan_no_ctx.keys())}"
    )
    assert isinstance(plan_no_ctx["segments"], list)
    assert len(plan_no_ctx["segments"]) == 1

    # 2. With-ctx call — must accept the new keyword-only parameter.
    ctx = router_mod.RouterContext(root)
    try:
        plan_with_ctx = router_mod.build_executor_plan(
            root, "feature", segments, ctx=ctx
        )
    except TypeError as exc:
        pytest.fail(
            "build_executor_plan must accept ctx=RouterContext as a keyword "
            f"argument (AC 3). Got TypeError: {exc}"
        )

    assert isinstance(plan_with_ctx, dict)
    assert "segments" in plan_with_ctx
    assert isinstance(plan_with_ctx["segments"], list)
    assert len(plan_with_ctx["segments"]) == 1

    # Same inputs, same observable shape: keys match.
    assert set(plan_no_ctx.keys()) == set(plan_with_ctx.keys()), (
        "With-ctx and without-ctx calls must return dicts with identical "
        "top-level keys. No-ctx="
        f"{sorted(plan_no_ctx.keys())} vs with-ctx="
        f"{sorted(plan_with_ctx.keys())}"
    )
    # Segment-level shape must also match (same executor, same keys).
    no_ctx_seg_keys = set(plan_no_ctx["segments"][0].keys())
    with_ctx_seg_keys = set(plan_with_ctx["segments"][0].keys())
    assert no_ctx_seg_keys == with_ctx_seg_keys, (
        f"Per-segment dict shape must be identical regardless of ctx. "
        f"No-ctx={sorted(no_ctx_seg_keys)} vs "
        f"with-ctx={sorted(with_ctx_seg_keys)}"
    )
