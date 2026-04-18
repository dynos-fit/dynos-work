#!/usr/bin/env python3
"""Tests for JSON policy file generation and consumption (AC 1-6, 12, 15, 16)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Import hooks modules
sys.path.insert(0, str(Path(__file__).parent.parent / "hooks"))


def _make_retrospective(
    task_id: str,
    task_type: str = "feature",
    quality_score: float = 0.8,
    models: dict | None = None,
    streaks: dict | None = None,
    findings_by_auditor: dict | None = None,
    risk_level: str = "medium",
) -> dict:
    """Helper to build a minimal retrospective for testing."""
    return {
        "task_id": task_id,
        "task_type": task_type,
        "quality_score": quality_score,
        "cost_score": 0.5,
        "efficiency_score": 0.6,
        "task_risk_level": risk_level,
        "task_domains": "backend",
        "model_used_by_agent": models or {},
        "auditor_zero_finding_streaks": streaks or {},
        "executor_repair_frequency": {},
        "findings_by_category": {},
        "findings_by_auditor": findings_by_auditor or {},
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
    retrospectives: list[dict] | None = None,
    registry: dict | None = None,
    policy: dict | None = None,
) -> None:
    """Create minimal project structure for testing."""
    dynos = root / ".dynos"
    dynos.mkdir(parents=True, exist_ok=True)

    # Write retrospectives
    for retro in (retrospectives or []):
        tid = retro["task_id"]
        task_dir = dynos / tid
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "task-retrospective.json").write_text(json.dumps(retro, indent=2))

    # Write registry
    reg = registry or {"version": 1, "agents": []}
    reg_dir = dynos / "learned-agents"
    reg_dir.mkdir(parents=True, exist_ok=True)
    (reg_dir / "registry.json").write_text(json.dumps(reg, indent=2))


# AC 1: write_patterns() creates model-policy.json with correct schema.


def test_model_policy_json_created_on_write_patterns(dynos_home) -> None:
    """model-policy.json is written to persistent dir by write_patterns()."""
    from patterns import write_patterns
    from lib import _persistent_project_dir

    root = dynos_home.root
    retros = [
        _make_retrospective(
            "task-001", "feature",
            quality_score=0.9,
            models={"backend-executor": "opus", "testing-executor": "sonnet"},
        ),
        _make_retrospective(
            "task-002", "feature",
            quality_score=0.85,
            models={"backend-executor": "opus", "testing-executor": "sonnet"},
        ),
    ]
    _setup_project(root, retrospectives=retros)

    write_patterns(root)

    model_policy_path = _persistent_project_dir(root) / "model-policy.json"
    assert model_policy_path.exists(), "model-policy.json should be created"

    data = json.loads(model_policy_path.read_text())
    assert isinstance(data, dict)

    # Each value should have model, sample_count, mean_quality
    for key, value in data.items():
        assert ":" in key, f"Key should be 'role:task_type' format, got {key}"
        assert "model" in value
        assert "sample_count" in value
        assert "mean_quality" in value
        assert isinstance(value["model"], str)
        assert isinstance(value["sample_count"], int)
        assert isinstance(value["mean_quality"], (int, float))


def test_model_policy_json_has_entry_for_observed_role_task_type(dynos_home) -> None:
    """Every (role, task_type) pair from retrospectives appears in model-policy.json."""
    from patterns import write_patterns
    from lib import _persistent_project_dir

    root = dynos_home.root
    retros = [
        _make_retrospective(
            "task-001", "feature",
            quality_score=0.9,
            models={"backend-executor": "opus"},
        ),
        _make_retrospective(
            "task-002", "feature",
            quality_score=0.85,
            models={"backend-executor": "opus"},
        ),
    ]
    _setup_project(root, retrospectives=retros)

    write_patterns(root)

    model_policy_path = _persistent_project_dir(root) / "model-policy.json"
    data = json.loads(model_policy_path.read_text())

    # backend-executor:feature should exist because we have 2 observations
    assert "backend-executor:feature" in data
    assert data["backend-executor:feature"]["model"] == "opus"


# AC 2: write_patterns() creates skip-policy.json with correct schema.


def test_skip_policy_json_created_on_write_patterns(dynos_home) -> None:
    """skip-policy.json is written to persistent dir by write_patterns()."""
    from patterns import write_patterns
    from lib import _persistent_project_dir

    root = dynos_home.root
    retros = [
        _make_retrospective(
            "task-001", "feature",
            streaks={"ui-auditor": 4, "dead-code-auditor": 2},
        ),
    ]
    _setup_project(root, retrospectives=retros)

    write_patterns(root)

    skip_policy_path = _persistent_project_dir(root) / "skip-policy.json"
    assert skip_policy_path.exists(), "skip-policy.json should be created"

    data = json.loads(skip_policy_path.read_text())
    assert isinstance(data, dict)

    # Each value should have threshold and confidence
    for auditor, value in data.items():
        assert "threshold" in value
        assert "confidence" in value
        assert isinstance(value["threshold"], int)
        assert isinstance(value["confidence"], float)


def test_skip_policy_excludes_exempt_auditors(dynos_home) -> None:
    """Skip-exempt auditors (security, spec-completion, code-quality) are not in skip-policy.json."""
    from patterns import write_patterns, SKIP_EXEMPT_AUDITORS
    from lib import _persistent_project_dir

    root = dynos_home.root
    retros = [
        _make_retrospective(
            "task-001", "feature",
            streaks={"security-auditor": 5, "ui-auditor": 3},
        ),
    ]
    _setup_project(root, retrospectives=retros)

    write_patterns(root)

    skip_policy_path = _persistent_project_dir(root) / "skip-policy.json"
    data = json.loads(skip_policy_path.read_text())

    for exempt in SKIP_EXEMPT_AUDITORS:
        assert exempt not in data, f"{exempt} is skip-exempt and should not be in skip-policy.json"


# AC 3: write_patterns() creates route-policy.json with correct schema.


def test_route_policy_json_created_on_write_patterns(dynos_home) -> None:
    """route-policy.json is written to persistent dir by write_patterns()."""
    from patterns import write_patterns
    from lib import _persistent_project_dir

    root = dynos_home.root
    registry = {
        "version": 1,
        "agents": [
            {
                "agent_name": "auto-backend-feature",
                "role": "backend-executor",
                "task_type": "feature",
                "path": ".dynos/learned-agents/executors/auto-backend-feature.md",
                "mode": "alongside",
                "status": "active",
                "benchmark_summary": {"mean_composite": 0.75},
                "generated_from": "task-001",
            }
        ],
    }
    retros = [_make_retrospective("task-001", "feature")]
    _setup_project(root, retrospectives=retros, registry=registry)

    write_patterns(root)

    route_policy_path = _persistent_project_dir(root) / "route-policy.json"
    assert route_policy_path.exists(), "route-policy.json should be created"

    data = json.loads(route_policy_path.read_text())
    assert isinstance(data, dict)

    # Each value should have mode, agent_path, agent_name, composite_score
    for key, value in data.items():
        assert ":" in key
        assert "mode" in value
        assert "composite_score" in value
        assert isinstance(value["composite_score"], (int, float))


# AC 5: resolve_model() reads model-policy.json first, falls back to markdown.


def test_resolve_model_prefers_json_over_markdown(dynos_home) -> None:
    """When model-policy.json exists with a matching key, it is used over markdown."""
    from lib import _persistent_project_dir, write_json
    from router import resolve_model

    root = dynos_home.root
    persistent = _persistent_project_dir(root)

    # Write model-policy.json with a specific model
    write_json(persistent / "model-policy.json", {
        "backend-executor:feature": {
            "model": "haiku",
            "sample_count": 5,
            "mean_quality": 0.85,
        }
    })

    # Write markdown with a different model
    (persistent / "project_rules.md").write_text(
        "## Model Policy\n\n"
        "| Role | Task Type | Recommended Model |\n"
        "|------|-----------|-------------------|\n"
        "| backend-executor | feature | opus |\n"
    )

    # Write minimal policy.json (no overrides)
    write_json(persistent / "policy.json", {"exploration_epsilon": 0})

    result = resolve_model(root, "backend-executor", "feature")
    assert result["model"] == "haiku"
    assert result["source"] == "learned_history"


def test_resolve_model_json_source_is_learned_history(dynos_home) -> None:
    """When model comes from JSON, source field is 'learned_history'."""
    from lib import _persistent_project_dir, write_json
    from router import resolve_model

    root = dynos_home.root
    persistent = _persistent_project_dir(root)
    write_json(persistent / "model-policy.json", {
        "testing-executor:bugfix": {
            "model": "sonnet",
            "sample_count": 3,
            "mean_quality": 0.7,
        }
    })
    write_json(persistent / "policy.json", {"exploration_epsilon": 0})

    result = resolve_model(root, "testing-executor", "bugfix")
    assert result["model"] == "sonnet"
    assert result["source"] == "learned_history"


def test_resolve_model_explicit_policy_overrides_json(dynos_home) -> None:
    """policy.json explicit overrides take priority over model-policy.json."""
    from lib import _persistent_project_dir, write_json
    from router import resolve_model

    root = dynos_home.root
    persistent = _persistent_project_dir(root)
    write_json(persistent / "model-policy.json", {
        "backend-executor:feature": {
            "model": "sonnet",
            "sample_count": 5,
            "mean_quality": 0.85,
        }
    })
    write_json(persistent / "policy.json", {
        "model_overrides": {"backend-executor:feature": "opus"}
    })

    result = resolve_model(root, "backend-executor", "feature")
    # Explicit policy should win
    assert result["model"] == "opus"
    assert result["source"] in ("policy", "explicit_policy")


def test_security_floor_overrides_json(dynos_home) -> None:
    """Security floor for security-auditor overrides model-policy.json haiku selection."""
    from lib import _persistent_project_dir, write_json
    from router import resolve_model

    root = dynos_home.root
    persistent = _persistent_project_dir(root)
    write_json(persistent / "model-policy.json", {
        "security-auditor:feature": {
            "model": "haiku",
            "sample_count": 5,
            "mean_quality": 0.85,
        }
    })
    write_json(persistent / "policy.json", {"exploration_epsilon": 0})

    result = resolve_model(root, "security-auditor", "feature")
    assert result["model"] == "opus"
    assert result["source"] == "security_floor"


# AC 6: resolve_skip() / _get_skip_threshold() reads skip-policy.json first.


def test_skip_threshold_from_json_preferred_over_markdown(dynos_home) -> None:
    """When skip-policy.json has threshold for an auditor, use it over markdown."""
    from lib import _persistent_project_dir, write_json
    from router import _get_skip_threshold

    root = dynos_home.root
    persistent = _persistent_project_dir(root)
    write_json(persistent / "skip-policy.json", {
        "ui-auditor": {"threshold": 5, "confidence": 0.80}
    })
    # Write markdown with different threshold
    (persistent / "project_rules.md").write_text(
        "## Skip Policy\n\n"
        "| Auditor | Skip Threshold | Confidence |\n"
        "|---------|----------------|------------|\n"
        "| ui-auditor | 3 | 0.60 |\n"
    )

    threshold = _get_skip_threshold(root, "ui-auditor")
    assert threshold == 5


def test_skip_threshold_falls_back_to_markdown_when_no_json_key(dynos_home) -> None:
    """When skip-policy.json exists but has no matching key, fall back to markdown."""
    from lib import _persistent_project_dir, write_json
    from router import _get_skip_threshold

    root = dynos_home.root
    persistent = _persistent_project_dir(root)
    write_json(persistent / "skip-policy.json", {
        "dead-code-auditor": {"threshold": 4, "confidence": 0.70}
    })
    # Write markdown with ui-auditor threshold
    (persistent / "project_rules.md").write_text(
        "## Skip Policy\n\n"
        "| Auditor | Skip Threshold | Confidence |\n"
        "|---------|----------------|------------|\n"
        "| ui-auditor | 3 | 0.60 |\n"
    )

    threshold = _get_skip_threshold(root, "ui-auditor")
    assert threshold == 3


# AC 12: apply_improvement() with adjust_model_policy writes to model-policy.json.


def test_adjust_model_policy_writes_model_policy_json(dynos_home) -> None:
    """adjust_model_policy action writes entries to model-policy.json with source postmortem_recommendation."""
    from lib import _persistent_project_dir, write_json
    from postmortem import apply_improvement

    root = dynos_home.root
    persistent = _persistent_project_dir(root)
    # Initialize empty policy.json
    write_json(persistent / "policy.json", {"exploration_epsilon": 0})

    proposal = {
        "id": "imp-model-haiku",
        "type": "model_recommendation",
        "action": "adjust_model_policy",
        "suggested_value": "haiku for low-risk auditors",
    }

    result = apply_improvement(root, proposal)
    assert result["applied"]

    # Check model-policy.json was written
    mp_path = persistent / "model-policy.json"
    assert mp_path.exists(), "model-policy.json should be created by apply_improvement"

    data = json.loads(mp_path.read_text())
    # Should have entries for non-security auditors
    for role in ("spec-completion-auditor", "code-quality-auditor", "dead-code-auditor"):
        for tt in ("feature", "bugfix", "refactor"):
            key = f"{role}:{tt}"
            assert key in data, f"Expected {key} in model-policy.json"
            assert data[key].get("source") == "postmortem_recommendation"


def test_adjust_model_policy_preserves_explicit_policy_entries(dynos_home) -> None:
    """Postmortem recommendations do not overwrite existing explicit_policy entries."""
    from lib import _persistent_project_dir, write_json
    from postmortem import apply_improvement

    root = dynos_home.root
    persistent = _persistent_project_dir(root)
    write_json(persistent / "policy.json", {"exploration_epsilon": 0})

    # Pre-seed model-policy.json with an explicit_policy entry
    write_json(persistent / "model-policy.json", {
        "spec-completion-auditor:feature": {
            "model": "opus",
            "source": "explicit_policy",
            "sample_count": 0,
            "mean_quality": 0.0,
        }
    })

    proposal = {
        "id": "imp-model-haiku",
        "type": "model_recommendation",
        "action": "adjust_model_policy",
        "suggested_value": "haiku for low-risk auditors",
    }

    apply_improvement(root, proposal)

    data = json.loads((persistent / "model-policy.json").read_text())
    # The explicit_policy entry should NOT be overwritten
    entry = data.get("spec-completion-auditor:feature", {})
    assert entry.get("source") == "explicit_policy"
    assert entry.get("model") == "opus"


# AC 15: When JSON files are missing, consumers fall back to markdown parsing.


def test_resolve_model_returns_default_when_no_json_no_markdown(dynos_home) -> None:
    """Without both JSON and markdown, resolve_model() returns default."""
    from lib import _persistent_project_dir, write_json
    from router import resolve_model

    root = dynos_home.root
    persistent = _persistent_project_dir(root)
    persistent.mkdir(parents=True, exist_ok=True)
    write_json(persistent / "policy.json", {"exploration_epsilon": 0})
    # No model-policy.json, no project_rules.md

    result = resolve_model(root, "backend-executor", "feature")
    assert result["source"] == "default"


def test_skip_threshold_returns_default_when_no_json_no_markdown(dynos_home) -> None:
    """Without both JSON and markdown, _get_skip_threshold() returns DEFAULT_SKIP_THRESHOLD."""
    from lib import _persistent_project_dir
    from router import _get_skip_threshold, DEFAULT_SKIP_THRESHOLD

    root = dynos_home.root
    persistent = _persistent_project_dir(root)
    persistent.mkdir(parents=True, exist_ok=True)
    # No skip-policy.json, no project_rules.md

    threshold = _get_skip_threshold(root, "ui-auditor")
    assert threshold == DEFAULT_SKIP_THRESHOLD


def test_corrupt_json_falls_back_gracefully(dynos_home) -> None:
    """Corrupt JSON file does not crash; falls back to markdown or default."""
    from lib import _persistent_project_dir, write_json
    from router import resolve_model

    root = dynos_home.root
    persistent = _persistent_project_dir(root)
    persistent.mkdir(parents=True, exist_ok=True)
    # Write corrupt JSON
    (persistent / "model-policy.json").write_text("{not valid json!!!")
    (persistent / "project_rules.md").write_text(
        "## Model Policy\n\n"
        "| Role | Task Type | Recommended Model |\n"
        "|------|-----------|-------------------|\n"
        "| backend-executor | feature | haiku |\n"
    )
    write_json(persistent / "policy.json", {"exploration_epsilon": 0})

    # Should not crash; should fall back
    result = resolve_model(root, "backend-executor", "feature")
    assert result["source"] in ("policy", "learned_history", "default")


# AC 16: model_overrides migration from policy.json to model-policy.json.


def test_model_overrides_migrated_to_model_policy_json(dynos_home) -> None:
    """write_patterns() migrates model_overrides from policy.json to model-policy.json."""
    from patterns import write_patterns
    from lib import _persistent_project_dir, write_json

    root = dynos_home.root
    persistent = _persistent_project_dir(root)
    write_json(persistent / "policy.json", {
        "model_overrides": {
            "backend-executor:feature": "opus",
            "testing-executor:bugfix": "haiku",
        }
    })
    retros = [
        _make_retrospective("task-001", "feature"),
        _make_retrospective("task-002", "bugfix"),
    ]
    _setup_project(root, retrospectives=retros)

    write_patterns(root)

    # model-policy.json should have the migrated entries with source "explicit_policy"
    mp_path = persistent / "model-policy.json"
    assert mp_path.exists()
    data = json.loads(mp_path.read_text())

    assert "backend-executor:feature" in data
    assert data["backend-executor:feature"]["model"] == "opus"
    assert data["backend-executor:feature"].get("source") == "explicit_policy"

    assert "testing-executor:bugfix" in data
    assert data["testing-executor:bugfix"]["model"] == "haiku"
    assert data["testing-executor:bugfix"].get("source") == "explicit_policy"


def test_model_overrides_removed_from_policy_json_after_migration(dynos_home) -> None:
    """After migration, model_overrides key is removed from policy.json."""
    from patterns import write_patterns
    from lib import _persistent_project_dir, write_json

    root = dynos_home.root
    persistent = _persistent_project_dir(root)
    write_json(persistent / "policy.json", {
        "model_overrides": {"backend-executor:feature": "opus"}
    })
    retros = [_make_retrospective("task-001", "feature")]
    _setup_project(root, retrospectives=retros)

    write_patterns(root)

    policy = json.loads((persistent / "policy.json").read_text())
    assert "model_overrides" not in policy, "model_overrides should be removed from policy.json"


def test_migration_preserves_existing_explicit_policy(dynos_home) -> None:
    """Migration does not overwrite existing explicit_policy entries in model-policy.json."""
    from patterns import write_patterns
    from lib import _persistent_project_dir, write_json

    root = dynos_home.root
    persistent = _persistent_project_dir(root)

    # Pre-existing model-policy.json with an explicit_policy entry
    write_json(persistent / "model-policy.json", {
        "backend-executor:feature": {
            "model": "sonnet",
            "source": "explicit_policy",
            "sample_count": 0,
            "mean_quality": 0.0,
        }
    })

    # policy.json with a conflicting override for the same key
    write_json(persistent / "policy.json", {
        "model_overrides": {"backend-executor:feature": "opus"}
    })

    retros = [_make_retrospective("task-001", "feature")]
    _setup_project(root, retrospectives=retros)

    write_patterns(root)

    data = json.loads((persistent / "model-policy.json").read_text())
    # The existing explicit_policy entry should be preserved (not overwritten)
    entry = data.get("backend-executor:feature", {})
    assert entry.get("source") == "explicit_policy"


def test_migration_is_idempotent(dynos_home) -> None:
    """Running write_patterns() twice produces the same model-policy.json."""
    from patterns import write_patterns
    from lib import _persistent_project_dir, write_json

    root = dynos_home.root
    persistent = _persistent_project_dir(root)
    write_json(persistent / "policy.json", {
        "model_overrides": {"backend-executor:feature": "opus"}
    })
    retros = [
        _make_retrospective("task-001", "feature",
                            models={"backend-executor": "sonnet"}),
        _make_retrospective("task-002", "feature",
                            models={"backend-executor": "sonnet"}),
    ]
    _setup_project(root, retrospectives=retros)

    write_patterns(root)
    first_data = json.loads((persistent / "model-policy.json").read_text())

    # Run again (model_overrides already removed)
    write_patterns(root)
    second_data = json.loads((persistent / "model-policy.json").read_text())

    # Should be the same
    assert first_data == second_data
