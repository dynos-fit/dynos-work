#!/usr/bin/env python3
"""Tests for JSON policy file generation and consumption (AC 1-6, 12, 15, 16)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
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


class TestModelPolicyJsonGeneration(unittest.TestCase):
    """AC 1: write_patterns() creates model-policy.json with correct schema."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        # Override DYNOS_HOME so persistent dir is inside temp
        self.orig_dynos_home = os.environ.get("DYNOS_HOME")
        os.environ["DYNOS_HOME"] = str(self.root / ".dynos-home")

    def tearDown(self) -> None:
        if self.orig_dynos_home is None:
            os.environ.pop("DYNOS_HOME", None)
        else:
            os.environ["DYNOS_HOME"] = self.orig_dynos_home
        self.tempdir.cleanup()

    def test_model_policy_json_created_on_write_patterns(self) -> None:
        """model-policy.json is written to persistent dir by write_patterns()."""
        from patterns import write_patterns
        from lib import _persistent_project_dir

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
        _setup_project(self.root, retrospectives=retros)

        write_patterns(self.root)

        model_policy_path = _persistent_project_dir(self.root) / "model-policy.json"
        self.assertTrue(model_policy_path.exists(), "model-policy.json should be created")

        data = json.loads(model_policy_path.read_text())
        self.assertIsInstance(data, dict)

        # Each value should have model, sample_count, mean_quality
        for key, value in data.items():
            self.assertIn(":", key, f"Key should be 'role:task_type' format, got {key}")
            self.assertIn("model", value)
            self.assertIn("sample_count", value)
            self.assertIn("mean_quality", value)
            self.assertIsInstance(value["model"], str)
            self.assertIsInstance(value["sample_count"], int)
            self.assertIsInstance(value["mean_quality"], (int, float))

    def test_model_policy_json_has_entry_for_observed_role_task_type(self) -> None:
        """Every (role, task_type) pair from retrospectives appears in model-policy.json."""
        from patterns import write_patterns
        from lib import _persistent_project_dir

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
        _setup_project(self.root, retrospectives=retros)

        write_patterns(self.root)

        model_policy_path = _persistent_project_dir(self.root) / "model-policy.json"
        data = json.loads(model_policy_path.read_text())

        # backend-executor:feature should exist because we have 2 observations
        self.assertIn("backend-executor:feature", data)
        self.assertEqual(data["backend-executor:feature"]["model"], "opus")


class TestSkipPolicyJsonGeneration(unittest.TestCase):
    """AC 2: write_patterns() creates skip-policy.json with correct schema."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.orig_dynos_home = os.environ.get("DYNOS_HOME")
        os.environ["DYNOS_HOME"] = str(self.root / ".dynos-home")

    def tearDown(self) -> None:
        if self.orig_dynos_home is None:
            os.environ.pop("DYNOS_HOME", None)
        else:
            os.environ["DYNOS_HOME"] = self.orig_dynos_home
        self.tempdir.cleanup()

    def test_skip_policy_json_created_on_write_patterns(self) -> None:
        """skip-policy.json is written to persistent dir by write_patterns()."""
        from patterns import write_patterns
        from lib import _persistent_project_dir

        retros = [
            _make_retrospective(
                "task-001", "feature",
                streaks={"ui-auditor": 4, "dead-code-auditor": 2},
            ),
        ]
        _setup_project(self.root, retrospectives=retros)

        write_patterns(self.root)

        skip_policy_path = _persistent_project_dir(self.root) / "skip-policy.json"
        self.assertTrue(skip_policy_path.exists(), "skip-policy.json should be created")

        data = json.loads(skip_policy_path.read_text())
        self.assertIsInstance(data, dict)

        # Each value should have threshold and confidence
        for auditor, value in data.items():
            self.assertIn("threshold", value)
            self.assertIn("confidence", value)
            self.assertIsInstance(value["threshold"], int)
            self.assertIsInstance(value["confidence"], float)

    def test_skip_policy_excludes_exempt_auditors(self) -> None:
        """Skip-exempt auditors (security, spec-completion, code-quality) are not in skip-policy.json."""
        from patterns import write_patterns, SKIP_EXEMPT_AUDITORS
        from lib import _persistent_project_dir

        retros = [
            _make_retrospective(
                "task-001", "feature",
                streaks={"security-auditor": 5, "ui-auditor": 3},
            ),
        ]
        _setup_project(self.root, retrospectives=retros)

        write_patterns(self.root)

        skip_policy_path = _persistent_project_dir(self.root) / "skip-policy.json"
        data = json.loads(skip_policy_path.read_text())

        for exempt in SKIP_EXEMPT_AUDITORS:
            self.assertNotIn(exempt, data, f"{exempt} is skip-exempt and should not be in skip-policy.json")


class TestRoutePolicyJsonGeneration(unittest.TestCase):
    """AC 3: write_patterns() creates route-policy.json with correct schema."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.orig_dynos_home = os.environ.get("DYNOS_HOME")
        os.environ["DYNOS_HOME"] = str(self.root / ".dynos-home")

    def tearDown(self) -> None:
        if self.orig_dynos_home is None:
            os.environ.pop("DYNOS_HOME", None)
        else:
            os.environ["DYNOS_HOME"] = self.orig_dynos_home
        self.tempdir.cleanup()

    def test_route_policy_json_created_on_write_patterns(self) -> None:
        """route-policy.json is written to persistent dir by write_patterns()."""
        from patterns import write_patterns
        from lib import _persistent_project_dir

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
        _setup_project(self.root, retrospectives=retros, registry=registry)

        write_patterns(self.root)

        route_policy_path = _persistent_project_dir(self.root) / "route-policy.json"
        self.assertTrue(route_policy_path.exists(), "route-policy.json should be created")

        data = json.loads(route_policy_path.read_text())
        self.assertIsInstance(data, dict)

        # Each value should have mode, agent_path, agent_name, composite_score
        for key, value in data.items():
            self.assertIn(":", key)
            self.assertIn("mode", value)
            self.assertIn("composite_score", value)
            self.assertIsInstance(value["composite_score"], (int, float))


class TestJsonMatchesMarkdown(unittest.TestCase):
    """AC 4: JSON and markdown are generated from the same data."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.orig_dynos_home = os.environ.get("DYNOS_HOME")
        os.environ["DYNOS_HOME"] = str(self.root / ".dynos-home")

    def tearDown(self) -> None:
        if self.orig_dynos_home is None:
            os.environ.pop("DYNOS_HOME", None)
        else:
            os.environ["DYNOS_HOME"] = self.orig_dynos_home
        self.tempdir.cleanup()

    def test_model_policy_json_keys_match_markdown_rows(self) -> None:
        """Every role:task_type in model-policy.json has a matching markdown table row."""
        from patterns import write_patterns, local_patterns_path
        from lib import _persistent_project_dir

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
        _setup_project(self.root, retrospectives=retros)

        write_patterns(self.root)

        model_policy_path = _persistent_project_dir(self.root) / "model-policy.json"
        data = json.loads(model_policy_path.read_text())

        # Read markdown and extract model policy rows
        md_path = local_patterns_path(self.root)
        md_content = md_path.read_text()
        md_rows = set()
        in_model = False
        for line in md_content.splitlines():
            if "## Model Policy" in line:
                in_model = True
                continue
            if in_model and line.startswith("## "):
                break
            if not in_model or not line.startswith("|") or "---" in line or "Role" in line:
                continue
            parts = [p.strip() for p in line.split("|") if p.strip()]
            if len(parts) >= 3:
                md_rows.add(f"{parts[0]}:{parts[1]}")

        # Every key in the JSON with a non-default model should exist in the markdown
        for key in data:
            self.assertIn(key, md_rows, f"JSON key {key} should have a matching markdown row")


class TestResolveModelJsonFirst(unittest.TestCase):
    """AC 5: resolve_model() reads model-policy.json first, falls back to markdown."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.orig_dynos_home = os.environ.get("DYNOS_HOME")
        os.environ["DYNOS_HOME"] = str(self.root / ".dynos-home")

    def tearDown(self) -> None:
        if self.orig_dynos_home is None:
            os.environ.pop("DYNOS_HOME", None)
        else:
            os.environ["DYNOS_HOME"] = self.orig_dynos_home
        self.tempdir.cleanup()

    def test_resolve_model_prefers_json_over_markdown(self) -> None:
        """When model-policy.json exists with a matching key, it is used over markdown."""
        from lib import _persistent_project_dir, write_json
        from router import resolve_model

        persistent = _persistent_project_dir(self.root)

        # Write model-policy.json with a specific model
        write_json(persistent / "model-policy.json", {
            "backend-executor:feature": {
                "model": "haiku",
                "sample_count": 5,
                "mean_quality": 0.85,
            }
        })

        # Write markdown with a different model
        (persistent / "dynos_patterns.md").write_text(
            "## Model Policy\n\n"
            "| Role | Task Type | Recommended Model |\n"
            "|------|-----------|-------------------|\n"
            "| backend-executor | feature | opus |\n"
        )

        # Write minimal policy.json (no overrides)
        write_json(persistent / "policy.json", {})

        result = resolve_model(self.root, "backend-executor", "feature")
        self.assertEqual(result["model"], "haiku")
        self.assertEqual(result["source"], "learned_history")

    def test_resolve_model_json_source_is_learned_history(self) -> None:
        """When model comes from JSON, source field is 'learned_history'."""
        from lib import _persistent_project_dir, write_json
        from router import resolve_model

        persistent = _persistent_project_dir(self.root)
        write_json(persistent / "model-policy.json", {
            "testing-executor:bugfix": {
                "model": "sonnet",
                "sample_count": 3,
                "mean_quality": 0.7,
            }
        })
        write_json(persistent / "policy.json", {})

        result = resolve_model(self.root, "testing-executor", "bugfix")
        self.assertEqual(result["model"], "sonnet")
        self.assertEqual(result["source"], "learned_history")

    def test_resolve_model_explicit_policy_overrides_json(self) -> None:
        """policy.json explicit overrides take priority over model-policy.json."""
        from lib import _persistent_project_dir, write_json
        from router import resolve_model

        persistent = _persistent_project_dir(self.root)
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

        result = resolve_model(self.root, "backend-executor", "feature")
        # Explicit policy should win
        self.assertEqual(result["model"], "opus")
        self.assertIn(result["source"], ("policy", "explicit_policy"))

    def test_security_floor_overrides_json(self) -> None:
        """Security floor for security-auditor overrides model-policy.json haiku selection."""
        from lib import _persistent_project_dir, write_json
        from router import resolve_model

        persistent = _persistent_project_dir(self.root)
        write_json(persistent / "model-policy.json", {
            "security-auditor:feature": {
                "model": "haiku",
                "sample_count": 5,
                "mean_quality": 0.85,
            }
        })
        write_json(persistent / "policy.json", {})

        result = resolve_model(self.root, "security-auditor", "feature")
        self.assertEqual(result["model"], "opus")
        self.assertEqual(result["source"], "security_floor")


class TestResolveSkipJsonFirst(unittest.TestCase):
    """AC 6: resolve_skip() / _get_skip_threshold() reads skip-policy.json first."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.orig_dynos_home = os.environ.get("DYNOS_HOME")
        os.environ["DYNOS_HOME"] = str(self.root / ".dynos-home")

    def tearDown(self) -> None:
        if self.orig_dynos_home is None:
            os.environ.pop("DYNOS_HOME", None)
        else:
            os.environ["DYNOS_HOME"] = self.orig_dynos_home
        self.tempdir.cleanup()

    def test_skip_threshold_from_json_preferred_over_markdown(self) -> None:
        """When skip-policy.json has threshold for an auditor, use it over markdown."""
        from lib import _persistent_project_dir, write_json
        from router import _get_skip_threshold

        persistent = _persistent_project_dir(self.root)
        write_json(persistent / "skip-policy.json", {
            "ui-auditor": {"threshold": 5, "confidence": 0.80}
        })
        # Write markdown with different threshold
        (persistent / "dynos_patterns.md").write_text(
            "## Skip Policy\n\n"
            "| Auditor | Skip Threshold | Confidence |\n"
            "|---------|----------------|------------|\n"
            "| ui-auditor | 3 | 0.60 |\n"
        )

        threshold = _get_skip_threshold(self.root, "ui-auditor")
        self.assertEqual(threshold, 5)

    def test_skip_threshold_falls_back_to_markdown_when_no_json_key(self) -> None:
        """When skip-policy.json exists but has no matching key, fall back to markdown."""
        from lib import _persistent_project_dir, write_json
        from router import _get_skip_threshold

        persistent = _persistent_project_dir(self.root)
        write_json(persistent / "skip-policy.json", {
            "dead-code-auditor": {"threshold": 4, "confidence": 0.70}
        })
        # Write markdown with ui-auditor threshold
        (persistent / "dynos_patterns.md").write_text(
            "## Skip Policy\n\n"
            "| Auditor | Skip Threshold | Confidence |\n"
            "|---------|----------------|------------|\n"
            "| ui-auditor | 3 | 0.60 |\n"
        )

        threshold = _get_skip_threshold(self.root, "ui-auditor")
        self.assertEqual(threshold, 3)


class TestPostmortemWritesToModelPolicyJson(unittest.TestCase):
    """AC 12: apply_improvement() with adjust_model_policy writes to model-policy.json."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.orig_dynos_home = os.environ.get("DYNOS_HOME")
        os.environ["DYNOS_HOME"] = str(self.root / ".dynos-home")

    def tearDown(self) -> None:
        if self.orig_dynos_home is None:
            os.environ.pop("DYNOS_HOME", None)
        else:
            os.environ["DYNOS_HOME"] = self.orig_dynos_home
        self.tempdir.cleanup()

    def test_adjust_model_policy_writes_model_policy_json(self) -> None:
        """adjust_model_policy action writes entries to model-policy.json with source postmortem_recommendation."""
        from lib import _persistent_project_dir, write_json
        from postmortem import apply_improvement

        persistent = _persistent_project_dir(self.root)
        # Initialize empty policy.json
        write_json(persistent / "policy.json", {})

        proposal = {
            "id": "imp-model-haiku",
            "type": "model_recommendation",
            "action": "adjust_model_policy",
            "suggested_value": "haiku for low-risk auditors",
        }

        result = apply_improvement(self.root, proposal)
        self.assertTrue(result["applied"])

        # Check model-policy.json was written
        mp_path = persistent / "model-policy.json"
        self.assertTrue(mp_path.exists(), "model-policy.json should be created by apply_improvement")

        data = json.loads(mp_path.read_text())
        # Should have entries for non-security auditors
        for role in ("spec-completion-auditor", "code-quality-auditor", "dead-code-auditor"):
            for tt in ("feature", "bugfix", "refactor"):
                key = f"{role}:{tt}"
                self.assertIn(key, data, f"Expected {key} in model-policy.json")
                self.assertEqual(data[key].get("source"), "postmortem_recommendation")

    def test_adjust_model_policy_preserves_explicit_policy_entries(self) -> None:
        """Postmortem recommendations do not overwrite existing explicit_policy entries."""
        from lib import _persistent_project_dir, write_json
        from postmortem import apply_improvement

        persistent = _persistent_project_dir(self.root)
        write_json(persistent / "policy.json", {})

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

        apply_improvement(self.root, proposal)

        data = json.loads((persistent / "model-policy.json").read_text())
        # The explicit_policy entry should NOT be overwritten
        entry = data.get("spec-completion-auditor:feature", {})
        self.assertEqual(entry.get("source"), "explicit_policy")
        self.assertEqual(entry.get("model"), "opus")


class TestBackwardCompatFallback(unittest.TestCase):
    """AC 15: When JSON files are missing, consumers fall back to markdown parsing."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.orig_dynos_home = os.environ.get("DYNOS_HOME")
        os.environ["DYNOS_HOME"] = str(self.root / ".dynos-home")

    def tearDown(self) -> None:
        if self.orig_dynos_home is None:
            os.environ.pop("DYNOS_HOME", None)
        else:
            os.environ["DYNOS_HOME"] = self.orig_dynos_home
        self.tempdir.cleanup()

    def test_resolve_model_falls_back_to_markdown_when_no_json(self) -> None:
        """Without model-policy.json, resolve_model() reads from markdown."""
        from lib import _persistent_project_dir, write_json
        from router import resolve_model

        persistent = _persistent_project_dir(self.root)
        # No model-policy.json, only markdown
        (persistent / "dynos_patterns.md").write_text(
            "## Model Policy\n\n"
            "| Role | Task Type | Recommended Model |\n"
            "|------|-----------|-------------------|\n"
            "| backend-executor | feature | sonnet |\n"
        )
        write_json(persistent / "policy.json", {})

        result = resolve_model(self.root, "backend-executor", "feature")
        self.assertEqual(result["model"], "sonnet")
        # Source should reflect fallback
        self.assertIn(result["source"], ("policy", "learned_history", "default"))

    def test_resolve_model_returns_default_when_no_json_no_markdown(self) -> None:
        """Without both JSON and markdown, resolve_model() returns default."""
        from lib import _persistent_project_dir, write_json
        from router import resolve_model

        persistent = _persistent_project_dir(self.root)
        write_json(persistent / "policy.json", {})
        # No model-policy.json, no dynos_patterns.md

        result = resolve_model(self.root, "backend-executor", "feature")
        self.assertEqual(result["source"], "default")

    def test_skip_threshold_falls_back_to_markdown_when_no_json(self) -> None:
        """Without skip-policy.json, _get_skip_threshold() reads from markdown."""
        from lib import _persistent_project_dir
        from router import _get_skip_threshold

        persistent = _persistent_project_dir(self.root)
        (persistent / "dynos_patterns.md").write_text(
            "## Skip Policy\n\n"
            "| Auditor | Skip Threshold | Confidence |\n"
            "|---------|----------------|------------|\n"
            "| ui-auditor | 4 | 0.80 |\n"
        )

        threshold = _get_skip_threshold(self.root, "ui-auditor")
        self.assertEqual(threshold, 4)

    def test_skip_threshold_returns_default_when_no_json_no_markdown(self) -> None:
        """Without both JSON and markdown, _get_skip_threshold() returns DEFAULT_SKIP_THRESHOLD."""
        from lib import _persistent_project_dir
        from router import _get_skip_threshold, DEFAULT_SKIP_THRESHOLD

        _persistent_project_dir(self.root)
        # No skip-policy.json, no dynos_patterns.md

        threshold = _get_skip_threshold(self.root, "ui-auditor")
        self.assertEqual(threshold, DEFAULT_SKIP_THRESHOLD)

    def test_corrupt_json_falls_back_gracefully(self) -> None:
        """Corrupt JSON file does not crash; falls back to markdown or default."""
        from lib import _persistent_project_dir, write_json
        from router import resolve_model

        persistent = _persistent_project_dir(self.root)
        # Write corrupt JSON
        (persistent / "model-policy.json").write_text("{not valid json!!!")
        (persistent / "dynos_patterns.md").write_text(
            "## Model Policy\n\n"
            "| Role | Task Type | Recommended Model |\n"
            "|------|-----------|-------------------|\n"
            "| backend-executor | feature | haiku |\n"
        )
        write_json(persistent / "policy.json", {})

        # Should not crash; should fall back
        result = resolve_model(self.root, "backend-executor", "feature")
        self.assertIn(result["source"], ("policy", "learned_history", "default"))


class TestModelOverridesMigration(unittest.TestCase):
    """AC 16: model_overrides migration from policy.json to model-policy.json."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.orig_dynos_home = os.environ.get("DYNOS_HOME")
        os.environ["DYNOS_HOME"] = str(self.root / ".dynos-home")

    def tearDown(self) -> None:
        if self.orig_dynos_home is None:
            os.environ.pop("DYNOS_HOME", None)
        else:
            os.environ["DYNOS_HOME"] = self.orig_dynos_home
        self.tempdir.cleanup()

    def test_model_overrides_migrated_to_model_policy_json(self) -> None:
        """write_patterns() migrates model_overrides from policy.json to model-policy.json."""
        from patterns import write_patterns
        from lib import _persistent_project_dir, write_json

        persistent = _persistent_project_dir(self.root)
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
        _setup_project(self.root, retrospectives=retros)

        write_patterns(self.root)

        # model-policy.json should have the migrated entries with source "explicit_policy"
        mp_path = persistent / "model-policy.json"
        self.assertTrue(mp_path.exists())
        data = json.loads(mp_path.read_text())

        self.assertIn("backend-executor:feature", data)
        self.assertEqual(data["backend-executor:feature"]["model"], "opus")
        self.assertEqual(data["backend-executor:feature"].get("source"), "explicit_policy")

        self.assertIn("testing-executor:bugfix", data)
        self.assertEqual(data["testing-executor:bugfix"]["model"], "haiku")
        self.assertEqual(data["testing-executor:bugfix"].get("source"), "explicit_policy")

    def test_model_overrides_removed_from_policy_json_after_migration(self) -> None:
        """After migration, model_overrides key is removed from policy.json."""
        from patterns import write_patterns
        from lib import _persistent_project_dir, write_json

        persistent = _persistent_project_dir(self.root)
        write_json(persistent / "policy.json", {
            "model_overrides": {"backend-executor:feature": "opus"}
        })
        retros = [_make_retrospective("task-001", "feature")]
        _setup_project(self.root, retrospectives=retros)

        write_patterns(self.root)

        policy = json.loads((persistent / "policy.json").read_text())
        self.assertNotIn("model_overrides", policy, "model_overrides should be removed from policy.json")

    def test_migration_preserves_existing_explicit_policy(self) -> None:
        """Migration does not overwrite existing explicit_policy entries in model-policy.json."""
        from patterns import write_patterns
        from lib import _persistent_project_dir, write_json

        persistent = _persistent_project_dir(self.root)

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
        _setup_project(self.root, retrospectives=retros)

        write_patterns(self.root)

        data = json.loads((persistent / "model-policy.json").read_text())
        # The existing explicit_policy entry should be preserved (not overwritten)
        entry = data.get("backend-executor:feature", {})
        self.assertEqual(entry.get("source"), "explicit_policy")

    def test_migration_is_idempotent(self) -> None:
        """Running write_patterns() twice produces the same model-policy.json."""
        from patterns import write_patterns
        from lib import _persistent_project_dir, write_json

        persistent = _persistent_project_dir(self.root)
        write_json(persistent / "policy.json", {
            "model_overrides": {"backend-executor:feature": "opus"}
        })
        retros = [
            _make_retrospective("task-001", "feature",
                                models={"backend-executor": "sonnet"}),
            _make_retrospective("task-002", "feature",
                                models={"backend-executor": "sonnet"}),
        ]
        _setup_project(self.root, retrospectives=retros)

        write_patterns(self.root)
        first_data = json.loads((persistent / "model-policy.json").read_text())

        # Run again (model_overrides already removed)
        write_patterns(self.root)
        second_data = json.loads((persistent / "model-policy.json").read_text())

        # Should be the same
        self.assertEqual(first_data, second_data)


if __name__ == "__main__":
    unittest.main()
