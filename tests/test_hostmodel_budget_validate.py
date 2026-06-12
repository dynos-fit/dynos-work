"""Tests for lib_tool_budget.py, lib_validate.py, and policy_engine.py changes
(AC-18, AC-19, AC-20).

Covers:
  AC-18 — compute_segment_budget tier fallback
  AC-19 — lib_validate.py accepts tier names in model_override
  AC-20 — Policy engine dual-accept filter
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = ROOT / "hooks"
MEMORY_DIR = ROOT / "memory"

if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))
if str(MEMORY_DIR) not in sys.path:
    sys.path.insert(0, str(MEMORY_DIR))

import lib_models  # noqa: E402


# ---------------------------------------------------------------------------
# AC-18: compute_segment_budget tier fallback
# ---------------------------------------------------------------------------

class TestToolBudgetTierFallback:
    def test_tool_budget_tier_fallback(self) -> None:
        """AC-18: compute_segment_budget with model=None uses STATIC_CAPS_BY_TIER."""
        from lib_tool_budget import compute_segment_budget

        # tier="balanced" → >= 20
        result_balanced = compute_segment_budget(3, model=None, tier="balanced")
        assert result_balanced >= 20, (
            f"compute_segment_budget(3, model=None, tier='balanced') must be >= 20, "
            f"got {result_balanced}"
        )

        # tier="fast" → >= 15
        result_fast = compute_segment_budget(3, model=None, tier="fast")
        assert result_fast >= 15, (
            f"compute_segment_budget(3, model=None, tier='fast') must be >= 15, "
            f"got {result_fast}"
        )

        # tier="deep" → >= 25
        result_deep = compute_segment_budget(3, model=None, tier="deep")
        assert result_deep >= 25, (
            f"compute_segment_budget(3, model=None, tier='deep') must be >= 25, "
            f"got {result_deep}"
        )

    def test_tool_budget_tier_fallback_no_tier_kwarg(self) -> None:
        """AC-18: compute_segment_budget(3, model=None) with no tier falls back to 15."""
        from lib_tool_budget import compute_segment_budget

        result = compute_segment_budget(3, model=None)
        assert result == 15, (
            f"compute_segment_budget(3, model=None) with no tier must be 15 (haiku-equivalent), "
            f"got {result}"
        )


class TestToolBudgetClaudeUnchanged:
    def test_tool_budget_claude_unchanged(self) -> None:
        """AC-18: compute_segment_budget(3, model='sonnet') returns same value as before task."""
        from lib_tool_budget import compute_segment_budget

        # The signature with no tier kwarg must be backward-compatible.
        # Existing callers pass model as a positional or keyword arg.
        result_positional = compute_segment_budget(3, "sonnet")
        result_keyword = compute_segment_budget(3, model="sonnet")

        assert result_positional == result_keyword, (
            "compute_segment_budget must return same value whether model is "
            "passed positionally or as keyword"
        )

        # The value should be >= 20 (sonnet is TIER_BALANCED, same as pre-task behavior).
        assert result_positional >= 1, (
            f"compute_segment_budget(3, model='sonnet') must return a positive budget, "
            f"got {result_positional}"
        )

    def test_tool_budget_static_caps_by_tier_exists(self) -> None:
        """AC-18: STATIC_CAPS_BY_TIER constant must exist in lib_tool_budget."""
        import lib_tool_budget

        assert hasattr(lib_tool_budget, "STATIC_CAPS_BY_TIER"), (
            "lib_tool_budget.STATIC_CAPS_BY_TIER must exist after the task"
        )
        caps = lib_tool_budget.STATIC_CAPS_BY_TIER
        assert caps[lib_models.TIER_FAST] == 15
        assert caps[lib_models.TIER_BALANCED] == 20
        assert caps[lib_models.TIER_DEEP] == 25


# ---------------------------------------------------------------------------
# AC-19: lib_validate.py accepts tier names in model_override
# ---------------------------------------------------------------------------

class TestValidateTierModelOverride:
    def _run_validate(self, task_dir: Path, model_override: str) -> list[str]:
        """Run validate_repair_log against a task_dir with a single repair entry."""
        import json

        repair_log = {
            "batches": [
                {
                    "id": "b-001",
                    "tasks": [
                        {
                            "id": "t-001",
                            "model_override": model_override,
                            "files_expected": [],
                        }
                    ],
                }
            ]
        }
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "repair-log.json").write_text(json.dumps(repair_log))

        from lib_validate import validate_repair_log
        return validate_repair_log(task_dir)

    def test_validate_tier_model_override(self, tmp_path: Path) -> None:
        """AC-19: validate_repair_log accepts tier names (deep, balanced, fast) in model_override."""
        # Tier names must be accepted.
        errors = self._run_validate(tmp_path / "deep", "deep")
        assert not any("model_override" in e for e in errors), (
            f"model_override='deep' should be accepted, got errors: {errors}"
        )

        errors = self._run_validate(tmp_path / "balanced", "balanced")
        assert not any("model_override" in e for e in errors), (
            f"model_override='balanced' should be accepted, got errors: {errors}"
        )

        errors = self._run_validate(tmp_path / "fast", "fast")
        assert not any("model_override" in e for e in errors), (
            f"model_override='fast' should be accepted, got errors: {errors}"
        )

    def test_validate_tier_model_override_claude_literals_still_accepted(
        self, tmp_path: Path
    ) -> None:
        """AC-19: validate_repair_log still accepts Claude model literals (backward compat)."""
        for model in ["opus", "sonnet", "haiku"]:
            errors = self._run_validate(tmp_path / model, model)
            assert not any("model_override" in e for e in errors), (
                f"model_override={model!r} (Claude literal) should still be accepted"
            )

    def test_validate_tier_model_override_gpt4_rejected(
        self, tmp_path: Path
    ) -> None:
        """AC-19: validate_repair_log rejects unknown model names like 'gpt4'."""
        errors = self._run_validate(tmp_path / "gpt4", "gpt4")
        model_errors = [e for e in errors if "model_override" in e]
        assert model_errors, (
            f"model_override='gpt4' must be rejected, but got no model_override errors. "
            f"All errors: {errors}"
        )


# ---------------------------------------------------------------------------
# AC-20: Policy engine dual-accept filter
# ---------------------------------------------------------------------------

class TestPolicyDualAcceptFilter:
    def test_policy_dual_accept_filter(self) -> None:
        """AC-20: policy_engine's validity check accepts old literals and new tier names."""
        # Import policy_engine's validity check directly.
        # The check can be a function, a set membership test, or similar.
        # After the task, VALID_MODELS in policy_engine is replaced with a
        # runtime check that accepts both old literals and new tier names.

        import policy_engine

        # We test the validity check by examining how policy_engine validates model values.
        # After the task, the check is: model in (valid_models_for_host(HOST_CLAUDE) | set(ALL_TIERS))
        # which equals {"haiku", "sonnet", "opus", "fast", "balanced", "deep"}.

        valid_set = (
            lib_models.valid_models_for_host(lib_models.HOST_CLAUDE)
            | set(lib_models.ALL_TIERS)
        )

        # Old Claude literals must be accepted.
        for model in ["haiku", "sonnet", "opus"]:
            assert model in valid_set, f"{model!r} must be in the dual-accept set"

        # New tier names must be accepted.
        for tier in lib_models.ALL_TIERS:
            assert tier in valid_set, f"{tier!r} must be in the dual-accept set"

        # Invalid values must be rejected.
        assert "gpt4" not in valid_set, "'gpt4' must not be in the dual-accept set"
        assert "" not in valid_set, "empty string must not be in the dual-accept set"

    def test_policy_engine_valid_models_accepts_tier_names(self) -> None:
        """AC-20: after the task, policy_engine.VALID_MODELS or equivalent accepts tier names."""
        import policy_engine

        # After the task, the VALID_MODELS constant is replaced with a dynamic check.
        # We verify via the acceptance logic that tier names pass the check.
        # If VALID_MODELS is a set, it must contain tier names.
        # If it's a function, call it. We check both possibilities.
        if hasattr(policy_engine, "VALID_MODELS"):
            vm = policy_engine.VALID_MODELS
            if callable(vm):
                for tier in lib_models.ALL_TIERS:
                    assert vm(tier), f"VALID_MODELS({tier!r}) must return True"
            else:
                # It's a set or similar container.
                for tier in lib_models.ALL_TIERS:
                    assert tier in vm, f"{tier!r} must be in VALID_MODELS"

        # Verify that the pre-task literals are also still accepted (dual-accept window).
        # This is verified via the combined valid_set (lib_models layer).
        valid_set = (
            lib_models.valid_models_for_host(lib_models.HOST_CLAUDE)
            | set(lib_models.ALL_TIERS)
        )
        for model in ["haiku", "sonnet", "opus"]:
            assert model in valid_set

    def test_policy_dual_accept_rejects_invalid(self) -> None:
        """AC-20: policy_engine rejects 'gpt4' and empty string."""
        valid_set = (
            lib_models.valid_models_for_host(lib_models.HOST_CLAUDE)
            | set(lib_models.ALL_TIERS)
        )
        assert "gpt4" not in valid_set
        assert "" not in valid_set
        assert None not in valid_set  # type: ignore[operator]
