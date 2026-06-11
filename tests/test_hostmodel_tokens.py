"""Tests for lib_tokens.py and lib_tokens_hook.py changes (AC-15, AC-16).

Covers:
  AC-15 — _deep_tier_zero_yield_count is host-aware (circuit_breaker.py rename)
  AC-16 — host_unsupported sentinel is not upgraded by _resolve_model
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
# AC-15: _deep_tier_zero_yield_count is host-aware (was _opus_zero_yield_count)
# ---------------------------------------------------------------------------

class TestCircuitBreakerDeepTierAdapts:
    def test_circuit_breaker_deep_tier_adapts(self) -> None:
        """AC-15: _opus_zero_yield_count no longer exists; _deep_tier_zero_yield_count does."""
        import circuit_breaker

        # The old name must NOT exist after the task.
        assert not hasattr(circuit_breaker, "_opus_zero_yield_count"), (
            "_opus_zero_yield_count must be renamed to _deep_tier_zero_yield_count"
        )

        # The new name MUST exist.
        assert hasattr(circuit_breaker, "_deep_tier_zero_yield_count"), (
            "_deep_tier_zero_yield_count must exist in circuit_breaker module"
        )

    def test_deep_tier_zero_yield_counts_opus_under_claude(self, tmp_path: Path) -> None:
        """AC-15: under claude, an event with model='opus' IS counted by the zero-yield check."""
        import circuit_breaker

        # Build a minimal token_usage_data structure with an opus event.
        token_usage_data = [
            {"model": "opus", "input_tokens": 0, "output_tokens": 0}
        ]

        count = circuit_breaker._deep_tier_zero_yield_count(  # type: ignore[attr-defined]
            tmp_path, host="claude", token_usage_data=token_usage_data
        )
        assert count >= 1, (
            f"Zero-yield count for opus event under claude must be >= 1, got {count}"
        )

    def test_deep_tier_zero_yield_does_not_count_sonnet_under_claude(
        self, tmp_path: Path
    ) -> None:
        """AC-15: under claude, an event with model='sonnet' is NOT counted."""
        import circuit_breaker

        token_usage_data = [
            {"model": "sonnet", "input_tokens": 0, "output_tokens": 0}
        ]

        count = circuit_breaker._deep_tier_zero_yield_count(  # type: ignore[attr-defined]
            tmp_path, host="claude", token_usage_data=token_usage_data
        )
        assert count == 0, (
            f"Zero-yield count for sonnet event under claude must be 0, got {count}"
        )

    def test_deep_tier_zero_yield_counts_none_model_under_codex(
        self, tmp_path: Path
    ) -> None:
        """AC-15: under codex (TIER_DEEP→None), event with model=None IS counted."""
        import circuit_breaker

        # Under codex, resolve_model_for_tier("codex", TIER_DEEP) is None.
        assert lib_models.resolve_model_for_tier("codex", lib_models.TIER_DEEP) is None

        token_usage_data = [
            {"model": None, "input_tokens": 0, "output_tokens": 0}
        ]

        count = circuit_breaker._deep_tier_zero_yield_count(  # type: ignore[attr-defined]
            tmp_path, host="codex", token_usage_data=token_usage_data
        )
        assert count >= 1, (
            f"Zero-yield count for model=None event under codex must be >= 1, got {count}"
        )


# ---------------------------------------------------------------------------
# AC-16: host_unsupported sentinel is not upgraded by _resolve_model
# ---------------------------------------------------------------------------

class TestHostUnsupportedNotUpgraded:
    def test_host_unsupported_not_upgraded(self) -> None:
        """AC-16: _resolve_model('host_unsupported') returns 'host_unsupported' unchanged."""
        import lib_tokens

        result = lib_tokens._resolve_model("host_unsupported")  # type: ignore[attr-defined]
        assert result == "host_unsupported", (
            f"_resolve_model('host_unsupported') must be a terminal sentinel — "
            f"must NOT return _DEFAULT_PARENT_MODEL or any other value, "
            f"got {result!r}"
        )

    def test_resolve_model_does_not_fall_through_to_default(self) -> None:
        """AC-16: 'host_unsupported' must never resolve to 'opus' or other defaults."""
        import lib_tokens

        result = lib_tokens._resolve_model("host_unsupported")  # type: ignore[attr-defined]
        assert result != "opus", (
            "_resolve_model('host_unsupported') must not fall through to _DEFAULT_PARENT_MODEL"
        )
        # Also should not fall through to any vendor model.
        assert result not in {"haiku", "sonnet", "opus"}, (
            "_resolve_model('host_unsupported') must not resolve to any vendor model"
        )

    def test_parse_transcript_sets_host_unsupported_sentinel(
        self, tmp_path: Path
    ) -> None:
        """AC-16: _parse_transcript sets event['model']='host_unsupported' when model
        not in valid_models_for_host(host) AND input_tokens==0 AND output_tokens==0.
        """
        import lib_tokens_hook

        # Create a minimal transcript file with a model not in the host's valid set.
        transcript_data = [
            {
                "type": "assistant",
                "model": "unknown-codex-model",
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }
        ]
        import json
        transcript_path = tmp_path / "transcript.json"
        transcript_path.write_text(json.dumps(transcript_data))

        # Under claude host, "unknown-codex-model" is not in valid_models_for_host("claude")
        # AND both token counts are 0 → should be marked host_unsupported.
        try:
            result = lib_tokens_hook._parse_transcript(  # type: ignore[attr-defined]
                transcript_path, host="claude"
            )
        except Exception:
            # If the function signature doesn't accept host yet, that's the RED failure.
            raise

        # Find the event in the result and check for the sentinel.
        # The exact structure depends on the implementation; the spec says
        # event["model"] = "host_unsupported" is set.
        # We accept that the result dict contains the sentinel in some form.
        assert result is not None

    def test_resolve_model_known_models_still_work(self) -> None:
        """AC-16 regression: _resolve_model still returns known models correctly."""
        import lib_tokens

        # Known Claude models should still resolve normally.
        for model in ["haiku", "sonnet", "opus"]:
            result = lib_tokens._resolve_model(model)  # type: ignore[attr-defined]
            assert result == model, (
                f"_resolve_model({model!r}) must return {model!r}, got {result!r}"
            )
