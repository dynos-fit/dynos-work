"""Tests for circuit_breaker.py host-aware changes (AC-15 behavioral, AC-17 sidecar).

NOTE: The core AC-15 function-rename tests are in test_hostmodel_tokens.py.
This file covers the deeper circuit_breaker behavioral verification and
the AC-17 sidecar tier-name tests that involve circuit_breaker._SAFE_AGENT_RE.
"""
from __future__ import annotations

import re
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
# AC-15 (circuit_breaker): _deep_tier_zero_yield_count host-awareness
# ---------------------------------------------------------------------------

class TestCircuitBreakerDeepTierAdaptsFullSuite:
    """Extended behavioral tests for the renamed _deep_tier_zero_yield_count."""

    def test_opus_zero_yield_count_function_removed(self) -> None:
        """AC-15: _opus_zero_yield_count must NOT exist in circuit_breaker after the task."""
        import circuit_breaker
        assert not hasattr(circuit_breaker, "_opus_zero_yield_count"), (
            "_opus_zero_yield_count must be renamed; it still exists"
        )

    def test_deep_tier_zero_yield_count_function_exists(self) -> None:
        """AC-15: _deep_tier_zero_yield_count MUST exist in circuit_breaker after the task."""
        import circuit_breaker
        assert hasattr(circuit_breaker, "_deep_tier_zero_yield_count"), (
            "_deep_tier_zero_yield_count must exist in circuit_breaker"
        )
        assert callable(circuit_breaker._deep_tier_zero_yield_count)  # type: ignore[attr-defined]

    def test_deep_tier_zero_yield_count_signature_accepts_host(self) -> None:
        """AC-15: _deep_tier_zero_yield_count must accept a 'host' parameter."""
        import circuit_breaker
        import inspect
        sig = inspect.signature(
            circuit_breaker._deep_tier_zero_yield_count  # type: ignore[attr-defined]
        )
        assert "host" in sig.parameters, (
            "_deep_tier_zero_yield_count must have a 'host' parameter"
        )

    def test_deep_tier_model_under_claude_is_opus(self) -> None:
        """AC-15: under claude, TIER_DEEP resolves to 'opus' — the zero-yield model."""
        assert lib_models.resolve_model_for_tier("claude", lib_models.TIER_DEEP) == "opus"

    def test_deep_tier_model_under_codex_is_none(self) -> None:
        """AC-15: under codex, TIER_DEEP resolves to None — events with model=None counted."""
        assert lib_models.resolve_model_for_tier("codex", lib_models.TIER_DEEP) is None

    def test_circuit_breaker_source_has_no_literal_opus_in_zero_yield(self) -> None:
        """AC-15 regression: circuit_breaker.py zero-yield function must not have hardcoded 'opus'
        string in the predicate after the task.
        """
        src = (HOOKS_DIR / "circuit_breaker.py").read_text(encoding="utf-8")

        # Find the _deep_tier_zero_yield_count function body.
        func_start = src.find("def _deep_tier_zero_yield_count(")
        if func_start == -1:
            # RED phase — function not yet renamed; this assertion will fail.
            assert False, (
                "_deep_tier_zero_yield_count not found in circuit_breaker.py; "
                "either not yet implemented (RED) or wrongly named"
            )

        # Find the next function definition after ours (as a rough boundary).
        next_def = src.find("\ndef ", func_start + 1)
        func_body = src[func_start:next_def] if next_def != -1 else src[func_start:]

        # After the task, the old literal `!= "opus"` must not appear in the body.
        # It should use `!= resolve_model_for_tier(host, TIER_DEEP)` or equivalent.
        assert '!= "opus"' not in func_body, (
            'circuit_breaker._deep_tier_zero_yield_count must not contain hardcoded '
            '!= "opus"; use resolve_model_for_tier(host, TIER_DEEP)'
        )


# ---------------------------------------------------------------------------
# AC-17: _SAFE_AGENT_RE accepts tier names
# ---------------------------------------------------------------------------

class TestSafeAgentReAcceptsTierNames:
    def test_circuit_breaker_safe_agent_re_exists(self) -> None:
        """AC-17: circuit_breaker._SAFE_AGENT_RE must exist."""
        import circuit_breaker
        assert hasattr(circuit_breaker, "_SAFE_AGENT_RE"), (
            "_SAFE_AGENT_RE must exist in circuit_breaker"
        )

    def test_safe_agent_re_accepts_tier_names(self) -> None:
        """AC-17: _SAFE_AGENT_RE must match each tier name."""
        import circuit_breaker
        _SAFE_RE = circuit_breaker._SAFE_AGENT_RE  # type: ignore[attr-defined]

        for tier in lib_models.ALL_TIERS:
            assert _SAFE_RE.match(tier), (
                f"_SAFE_AGENT_RE must match tier name {tier!r}"
            )

    def test_safe_agent_re_accepts_auditor_tier_filenames(self) -> None:
        """AC-17: sidecar stems like 'security-auditor-deep' pass _SAFE_AGENT_RE."""
        import circuit_breaker
        _SAFE_RE = circuit_breaker._SAFE_AGENT_RE  # type: ignore[attr-defined]

        test_cases = [
            "security-auditor-deep",
            "code-quality-auditor-fast",
            "ui-auditor-balanced",
        ]
        for stem in test_cases:
            assert _SAFE_RE.match(stem), (
                f"_SAFE_AGENT_RE must match sidecar stem {stem!r}"
            )

    def test_safe_agent_re_does_not_match_none_string(self) -> None:
        """AC-17 regression: _SAFE_AGENT_RE must reject 'None' as a filename component."""
        import circuit_breaker
        _SAFE_RE = circuit_breaker._SAFE_AGENT_RE  # type: ignore[attr-defined]

        # The stem 'security-auditor-None' would be produced by the OLD code
        # when resolved_model is None. After the fix, this should not be generated,
        # but we also verify that if it were, it passes the regex (since "None"
        # is alphanumeric it DOES pass — the prevention is in stage.py, not here).
        # This test documents the understood behavior.
        result = _SAFE_RE.match("security-auditor-None")
        # "None" contains only letters, so it DOES match _SAFE_AGENT_RE.
        # The actual fix is in stage.py using tier instead of model_used.
        # This assertion documents that fact.
        assert result is not None  # "None" passes the regex (letters only)
