"""TDD-first tests for hooks/spec_lint.py (Segment E).

ACs covered:
  AC 41 — file exists with 8 named test functions covering extract_entities_per_ac,
           classify_ac_intent, and detect_anti_patterns.

All tests in this file are RED at first commit because hooks/spec_lint.py does not
exist yet — the top-level import will raise ModuleNotFoundError at collection.
Tests turn GREEN when Segment E lands.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Add hooks/ to sys.path so the import resolves once Segment E creates the module.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from spec_lint import (  # noqa: E402  # RED: ModuleNotFoundError until Segment E
    classify_ac_intent,
    detect_anti_patterns,
    extract_entities_per_ac,
)


# ---------------------------------------------------------------------------
# extract_entities_per_ac
# ---------------------------------------------------------------------------


def test_extract_entities_word_boundary():
    """extract_entities_per_ac uses word-boundary matching so that entity `log` does NOT
    match inside the longer identifier `log_event`."""
    spec_text = (
        "**AC-1:** The `log_event` function must emit structured output.\n"
        "**AC-2:** Call `log` before each transition.\n"
    )
    entities = extract_entities_per_ac(spec_text)

    assert isinstance(entities, dict), f"Expected dict; got {type(entities)!r}"
    # AC-1 has log_event — `log` must NOT appear as a separate entity for AC-1
    ac1_entities = entities.get(1, set())
    assert "log" not in ac1_entities, (
        f"'log' must not match inside 'log_event' due to word-boundary rule; "
        f"AC-1 entities={ac1_entities!r}"
    )
    assert "log_event" in ac1_entities, (
        f"'log_event' must appear in AC-1 entities; got {ac1_entities!r}"
    )
    # AC-2 has bare `log` — it MUST appear
    ac2_entities = entities.get(2, set())
    assert "log" in ac2_entities, (
        f"'log' must appear in AC-2 entities; got {ac2_entities!r}"
    )


def test_extract_entities_empty_spec():
    """extract_entities_per_ac returns an empty dict (or dict with empty sets) when the
    spec contains no backtick-quoted identifiers."""
    spec_text = (
        "**AC-1:** The system must be reliable.\n"
        "**AC-2:** All outputs must be correct.\n"
    )
    entities = extract_entities_per_ac(spec_text)

    assert isinstance(entities, dict), f"Expected dict; got {type(entities)!r}"
    # Either empty dict or every value is an empty set — no identifiers should appear.
    all_entities = set().union(*entities.values()) if entities else set()
    assert all_entities == set(), (
        f"Expected no entities when no backtick identifiers in spec; got {all_entities!r}"
    )


# ---------------------------------------------------------------------------
# classify_ac_intent
# ---------------------------------------------------------------------------


def test_classify_ac_intent_measurement():
    """classify_ac_intent returns 'measurement' for AC text containing measurement keywords."""
    ac_text = "The output must be at most 4096 bytes and sha-256 verified."
    intent = classify_ac_intent(ac_text)

    assert isinstance(intent, set), f"Expected set; got {type(intent)!r}"
    assert "measurement" in intent, (
        f"Expected 'measurement' for AC with 'at most' and 'sha-256'; got {intent!r}"
    )


def test_classify_ac_intent_structural():
    """classify_ac_intent returns 'structural' for AC text containing structural keywords."""
    ac_text = "Decompose the monolithic handler into three separate modules."
    intent = classify_ac_intent(ac_text)

    assert isinstance(intent, set), f"Expected set; got {type(intent)!r}"
    assert "structural" in intent, (
        f"Expected 'structural' for AC with 'decompose'; got {intent!r}"
    )
    assert "measurement" not in intent, (
        f"Expected no 'measurement' for structural-only AC; got {intent!r}"
    )


def test_classify_ac_intent_both():
    """classify_ac_intent returns both 'measurement' and 'structural' when the AC text
    contains keywords from both categories."""
    ac_text = (
        "Refactor the pipeline so that the result is byte-identical to the original output."
    )
    intent = classify_ac_intent(ac_text)

    assert isinstance(intent, set), f"Expected set; got {type(intent)!r}"
    assert "measurement" in intent, (
        f"Expected 'measurement' (byte-identical) in intent; got {intent!r}"
    )
    assert "structural" in intent, (
        f"Expected 'structural' (refactor) in intent; got {intent!r}"
    )


# ---------------------------------------------------------------------------
# detect_anti_patterns
# ---------------------------------------------------------------------------


def test_detect_anti_patterns_same_entity_triggers_finding():
    """detect_anti_patterns emits a finding when a backtick-quoted entity appears in
    at least one measurement AC AND at least one structural AC."""
    spec_text = (
        "**AC-1:** The `cache` must be byte-identical after each flush.\n"
        "**AC-2:** Decompose `cache` into a separate module.\n"
    )
    findings, acked = detect_anti_patterns(spec_text)

    assert isinstance(findings, list), f"Expected list for findings; got {type(findings)!r}"
    assert isinstance(acked, list), f"Expected list for acked; got {type(acked)!r}"
    finding_entities = [f.get("entity") for f in findings]
    assert "cache" in finding_entities, (
        f"Expected a finding for entity 'cache' present in both measurement and structural ACs; "
        f"got {findings!r}"
    )


def test_detect_anti_patterns_different_entities_no_finding():
    """detect_anti_patterns does NOT emit a finding when measurement and structural ACs
    reference different entities (no overlap)."""
    spec_text = (
        "**AC-1:** The `buffer` must be at most 1024 bytes.\n"
        "**AC-2:** Refactor `pipeline` into sub-stages.\n"
    )
    findings, acked = detect_anti_patterns(spec_text)

    assert isinstance(findings, list), f"Expected list for findings; got {type(findings)!r}"
    finding_entities = [f.get("entity") for f in findings]
    assert "buffer" not in finding_entities, (
        f"'buffer' appears only in a measurement AC; must not trigger finding; "
        f"got {findings!r}"
    )
    assert "pipeline" not in finding_entities, (
        f"'pipeline' appears only in a structural AC; must not trigger finding; "
        f"got {findings!r}"
    )


def test_detect_anti_patterns_ack_suppresses():
    """detect_anti_patterns does NOT emit a finding (moves it to acked) when the AC line
    has a <!-- spec-lint: ack --> comment on the AC line, the immediately preceding line,
    or the immediately following line."""
    spec_text = (
        "<!-- spec-lint: ack -->\n"
        "**AC-1:** The `index` must be byte-identical after each rebuild.\n"
        "**AC-2:** Decompose `index` into a read and a write module.\n"
    )
    findings, acked = detect_anti_patterns(spec_text)

    assert isinstance(acked, list), f"Expected list for acked; got {type(acked)!r}"
    assert isinstance(findings, list), f"Expected list for findings; got {type(findings)!r}"

    # The finding for `index` must be suppressed (moved to acked) because of the ack comment.
    finding_entities = [f.get("entity") for f in findings]
    assert "index" not in finding_entities, (
        f"'index' finding must be suppressed by spec-lint: ack; "
        f"got findings={findings!r}, acked={acked!r}"
    )
    # It must appear in acked instead.
    acked_entities = [a.get("entity") for a in acked]
    assert "index" in acked_entities, (
        f"'index' must appear in acked list after suppression; got {acked!r}"
    )
