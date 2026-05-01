"""TDD-first tests for hooks/plan_signature_check.py (Segment B).

ACs covered:
  AC 40 — file exists with 8 named test functions covering extract_signature_claims,
           extract_plan_signatures, and compare_signatures.

All tests in this file are RED at first commit because hooks/plan_signature_check.py
does not exist yet — the top-level import will raise ModuleNotFoundError at collection.
Tests turn GREEN when Segment B lands.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Add hooks/ to sys.path so the import resolves once Segment B creates the module.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from plan_signature_check import (  # noqa: E402  # RED: ModuleNotFoundError until Segment B
    compare_signatures,
    extract_plan_signatures,
    extract_signature_claims,
)


# ---------------------------------------------------------------------------
# extract_signature_claims
# ---------------------------------------------------------------------------


def test_extract_signature_claims_typed_arg():
    """extract_signature_claims finds a claim when an AC bullet contains a typed argument
    pattern like `fn_name(arg: Type) -> ReturnType`."""
    spec_text = (
        "**AC-1:** The function `process_event(event: dict) -> None` must handle all events.\n"
    )
    claims = extract_signature_claims(spec_text)

    assert len(claims) >= 1, f"Expected at least one claim; got {claims!r}"
    names = [c["fn_name"] for c in claims]
    assert "process_event" in names, (
        f"Expected 'process_event' in extracted claims; got {names!r}"
    )
    claim = next(c for c in claims if c["fn_name"] == "process_event")
    assert claim["ac_index"] == 1, (
        f"Expected ac_index=1 (1-based); got {claim['ac_index']!r}"
    )
    assert "raw_text" in claim and claim["raw_text"], (
        "Expected non-empty raw_text on claim"
    )


def test_extract_signature_claims_fenced_block():
    """extract_signature_claims finds a claim from a `def fn(...)` line inside a
    fenced Python code block within an AC."""
    spec_text = (
        "**AC-2:** The implementation must expose:\n"
        "\n"
        "```python\n"
        "def parse_result(raw: str) -> list[str]:\n"
        "    ...\n"
        "```\n"
    )
    claims = extract_signature_claims(spec_text)

    assert len(claims) >= 1, f"Expected at least one claim from fenced block; got {claims!r}"
    names = [c["fn_name"] for c in claims]
    assert "parse_result" in names, (
        f"Expected 'parse_result' in extracted claims; got {names!r}"
    )
    claim = next(c for c in claims if c["fn_name"] == "parse_result")
    assert claim["ac_index"] == 2, (
        f"Expected ac_index=2 for AC-2; got {claim['ac_index']!r}"
    )


def test_extract_signature_claims_no_match():
    """extract_signature_claims returns an empty list when the spec text contains
    no typed-argument or fenced-def patterns."""
    spec_text = (
        "**AC-1:** The system must log all events.\n"
        "**AC-2:** The database must be idempotent.\n"
    )
    claims = extract_signature_claims(spec_text)

    assert isinstance(claims, list), (
        f"Expected a list; got {type(claims)!r}"
    )
    assert claims == [], (
        f"Expected empty list for spec with no typed-arg or def patterns; got {claims!r}"
    )


# ---------------------------------------------------------------------------
# extract_plan_signatures
# ---------------------------------------------------------------------------


def test_extract_plan_signatures_finds_component():
    """extract_plan_signatures extracts a function signature from a ### Component: subsection."""
    plan_text = (
        "## Overview\n"
        "Some text.\n"
        "\n"
        "### Component: hooks/my_module.py\n"
        "\n"
        "- `build_index(paths: list[str]) -> dict` — builds the index.\n"
    )
    sigs = extract_plan_signatures(plan_text)

    assert isinstance(sigs, dict), f"Expected dict; got {type(sigs)!r}"
    assert "build_index" in sigs, (
        f"Expected 'build_index' in extracted plan signatures; got {list(sigs.keys())!r}"
    )
    entry = sigs["build_index"]
    assert "args" in entry, "Expected 'args' key in signature entry"
    assert "source_section" in entry, "Expected 'source_section' key in signature entry"
    assert "hooks/my_module.py" in entry["source_section"], (
        f"Expected source_section to reference the component; got {entry['source_section']!r}"
    )


def test_extract_plan_signatures_empty_plan():
    """extract_plan_signatures returns an empty dict when no ### Component: sections exist."""
    plan_text = (
        "## Overview\n"
        "This plan has no component subsections.\n"
        "\n"
        "### Summary\n"
        "Nothing to see here.\n"
    )
    sigs = extract_plan_signatures(plan_text)

    assert isinstance(sigs, dict), f"Expected dict; got {type(sigs)!r}"
    assert sigs == {}, (
        f"Expected empty dict for plan with no ### Component: sections; got {sigs!r}"
    )


# ---------------------------------------------------------------------------
# compare_signatures
# ---------------------------------------------------------------------------


def test_compare_signatures_mismatch_finding():
    """compare_signatures emits a finding with severity='mismatch' when the spec claims
    a function signature that demonstrably differs from what the plan specifies."""
    claims = [
        {
            "ac_index": 3,
            "fn_name": "do_thing",
            "raw_text": "do_thing(x: int, y: str) -> bool",
        }
    ]
    plan_sigs = {
        "do_thing": {
            "args": ["x: int"],          # spec says 2 args, plan says 1 — clear mismatch
            "returns": "bool",
            "source_section": "### Component: hooks/thing.py",
        }
    }
    findings, warnings = compare_signatures(claims, plan_sigs)

    assert isinstance(findings, list), f"Expected list for findings; got {type(findings)!r}"
    assert len(findings) >= 1, (
        f"Expected at least one finding for a demonstrable arg-count mismatch; "
        f"got findings={findings!r}"
    )
    finding = findings[0]
    assert finding["severity"] == "mismatch", (
        f"Expected severity='mismatch'; got {finding['severity']!r}"
    )
    assert finding["fn_name"] == "do_thing", (
        f"Expected fn_name='do_thing'; got {finding['fn_name']!r}"
    )
    assert finding["ac_index"] == 3, (
        f"Expected ac_index=3; got {finding['ac_index']!r}"
    )


def test_compare_signatures_missing_plan_section_is_warning():
    """compare_signatures emits a warning (not a finding) when the spec claims a function
    that has no corresponding entry in the plan signatures dict."""
    claims = [
        {
            "ac_index": 5,
            "fn_name": "orphan_fn",
            "raw_text": "orphan_fn(data: bytes) -> str",
        }
    ]
    plan_sigs = {}  # plan has no entry for orphan_fn

    findings, warnings = compare_signatures(claims, plan_sigs)

    assert isinstance(warnings, list), f"Expected list for warnings; got {type(warnings)!r}"
    assert len(warnings) >= 1, (
        f"Expected at least one warning for a missing plan section; got {warnings!r}"
    )
    warning = warnings[0]
    assert warning["fn_name"] == "orphan_fn", (
        f"Expected fn_name='orphan_fn' in warning; got {warning['fn_name']!r}"
    )
    assert warning["ac_index"] == 5, (
        f"Expected ac_index=5; got {warning['ac_index']!r}"
    )
    # Must not produce a finding for a missing plan section.
    assert findings == [], (
        f"Expected no findings for a missing plan section; got {findings!r}"
    )


def test_compare_signatures_no_findings_on_ambiguous():
    """compare_signatures produces a warning (not a finding) when the plan signature
    uses *args or the parse is incomplete, making definitive comparison impossible."""
    claims = [
        {
            "ac_index": 7,
            "fn_name": "flexible_fn",
            "raw_text": "flexible_fn(a: int, b: str) -> None",
        }
    ]
    plan_sigs = {
        "flexible_fn": {
            "args": ["*args", "**kwargs"],  # ambiguous — no definitive mismatch possible
            "returns": None,
            "source_section": "### Component: hooks/flexible.py",
        }
    }
    findings, warnings = compare_signatures(claims, plan_sigs)

    assert isinstance(findings, list), f"Expected list for findings; got {type(findings)!r}"
    assert findings == [], (
        f"Expected no findings for ambiguous (*args/**kwargs) plan signature; "
        f"got {findings!r}"
    )
    # Should emit a warning instead of a finding.
    assert isinstance(warnings, list), f"Expected list for warnings; got {type(warnings)!r}"
    assert len(warnings) >= 1, (
        f"Expected at least one warning for ambiguous plan signature; got {warnings!r}"
    )
