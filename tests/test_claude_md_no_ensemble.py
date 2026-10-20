"""TDD-first tests for claude-md-auditor ensemble=false (task-20260430-008).

The haiku→sonnet→opus ensemble cascade exists for auditors whose
verdicts vary across model tiers. claude-md-auditor's verdict comes from
``hooks/lib_claude_md.py`` (deterministic AST/glob analysis surfaced as
structured JSON); running three sequential model tiers against the same
deterministic input is pure waste. With high risk_level the cascade
otherwise fires by default (router.py:992-994).

This test suite drives the contract: claude-md-auditor must always
emit ``ensemble: false`` in the audit plan, regardless of:

  - risk_level (no risk level forces ensemble for non-default auditors)
  - sampling roll (every risk level otherwise CRC32-samples)
  - fast_track flag (already false-correct)
  - any future ensemble_auditors override that includes claude-md

Other deterministic-first auditors that may exist (none today, but
the design must support them) join via the
``_DETERMINISTIC_FIRST_AUDITORS`` set.

Other auditors are NOT affected. security-auditor and db-schema-auditor
remain in _DEFAULT_ENSEMBLE_AUDITORS and continue to run the full
cascade by default.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))


def _build_plan(*, risk_level: str, fast_track: bool = False) -> dict:
    from router import build_audit_plan
    return build_audit_plan(
        Path.cwd(),
        task_type="backend",
        domains=[],
        fast_track=fast_track,
        risk_level=risk_level,
        task_id="test-task",
        diff_files=["CLAUDE.md"],  # ensure claude-md-auditor isn't risk-gate-skipped
    )


def _entry(plan: dict, name: str) -> dict | None:
    for e in plan.get("auditors", []):
        if e.get("name") == name:
            return e
    return None


def test_claude_md_ensemble_false_at_high_risk():
    plan = _build_plan(risk_level="high")
    e = _entry(plan, "claude-md-auditor")
    assert e is not None
    assert e.get("action") == "spawn"
    assert e.get("ensemble") is False, (
        f"claude-md-auditor must not run ensemble cascade — it is "
        f"deterministic-first and the haiku→sonnet→opus chain is wasted "
        f"work. Got: {e}"
    )


def test_claude_md_ensemble_false_at_critical_risk():
    plan = _build_plan(risk_level="critical")
    e = _entry(plan, "claude-md-auditor")
    assert e is not None
    assert e.get("ensemble") is False


def test_claude_md_ensemble_false_at_medium_risk():
    plan = _build_plan(risk_level="medium")
    e = _entry(plan, "claude-md-auditor")
    assert e is not None
    assert e.get("ensemble") is False


def test_claude_md_ensemble_false_at_low_risk():
    plan = _build_plan(risk_level="low")
    e = _entry(plan, "claude-md-auditor")
    assert e is not None
    assert e.get("ensemble") is False


def test_security_auditor_ensemble_unchanged_at_high_risk():
    """security-auditor remains in _DEFAULT_ENSEMBLE_AUDITORS and continues
    to run the full cascade — only claude-md is exempted by this change."""
    plan = _build_plan(risk_level="high")
    e = _entry(plan, "security-auditor")
    assert e is not None
    assert e.get("ensemble") is True, (
        f"security-auditor must continue to run ensemble cascade; got: {e}"
    )


def test_claude_md_no_ensemble_voting_models_when_excluded():
    """When ensemble=false the entry must NOT carry ensemble_voting_models
    or ensemble_escalation_model — those keys are consumed by the audit
    skill to drive the cascade and including them on a false entry would
    invite an orchestrator to run the cascade anyway."""
    plan = _build_plan(risk_level="high")
    e = _entry(plan, "claude-md-auditor")
    assert e is not None
    assert "ensemble_voting_models" not in e
    assert "ensemble_escalation_model" not in e


def _entries(plan: dict) -> dict:
    return {e["name"]: e for e in plan.get("auditors", []) if e.get("action") == "spawn"}


def test_high_risk_non_security_auditor_follows_sampling():
    """At high/critical risk, non-default auditors use the CRC32 sample, never 'high_risk'."""
    import zlib
    from router import ENSEMBLE_SAMPLE_RATE, _DEFAULT_ENSEMBLE_AUDITORS, _DETERMINISTIC_FIRST_AUDITORS
    for risk in ("high", "critical"):
        plan = _build_plan(risk_level=risk)
        checked = 0
        for name, e in _entries(plan).items():
            if name in _DEFAULT_ENSEMBLE_AUDITORS or name in _DETERMINISTIC_FIRST_AUDITORS:
                continue
            sampled = zlib.crc32(f"test-task|{name}".encode()) % 10000 < int(ENSEMBLE_SAMPLE_RATE * 10000)
            assert e["ensemble"] is sampled, (risk, name, e)
            checked += 1
        assert checked, f"no sampled auditors in {risk} plan"
        assert "high_risk" not in str(plan)


def test_ensemble_auditor_set_ensemble_at_every_risk():
    from router import _DEFAULT_ENSEMBLE_AUDITORS
    for risk in ("low", "medium", "high", "critical"):
        entries = _entries(_build_plan(risk_level=risk))
        present = [n for n in _DEFAULT_ENSEMBLE_AUDITORS if n in entries]
        for name in present:
            assert entries[name]["ensemble"] is True, (risk, name)
