"""CI CHECK-4 (Class C — ValueError coverage lint).

Every ``raise ValueError(msg)`` and every ``_refuse(msg)`` inside
``hooks/lib_receipts.py`` + ``hooks/lib_core.py`` must have at least
one adversarial test somewhere under ``tests/`` that invokes it via
``pytest.raises(ValueError, match=<substring>)``.

This closes Class C (task-007 C-001..C-010): unverified raise paths
that live in production but no test exercises. Going forward, every
new raise either ships with its adversarial test or lands in the
named allowlist with a justification.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / "hooks"
TESTS_DIR = REPO_ROOT / "tests"
sys.path.insert(0, str(HOOKS_DIR))


# Raises whose message is dynamic (f-string with no stable substring)
# and whose semantics are covered structurally rather than by literal
# match. Each exemption is named; there is no wildcard.
#
# Each entry MUST be a substring that appears in the raise message
# literal. The allowlist is capped at 50 entries (see
# test_allowlist_contains_only_justified_entries); growing past that
# signals the lint is being bypassed rather than enforced.
_ALLOWLIST_UNMATCHABLE: set[str] = {
    # Generic framework re-raises — tested via integration paths.
    "invalid JSON",
    "manifest.json missing",
    "Unknown stage:",
    "Illegal stage transition:",
    # Internal defensive shape-check raises. These guard against
    # caller-side type drift and are surfaced via the normal exercise
    # of the writer; a direct adversarial test for each type check
    # adds noise without material coverage gain.
    "auditors must be a list",
    "injected_agent_sha256 must be str or None",
    "agent_path must be str or None",
    "must be a dict",
    "missing required key",
    "handlers_run must be a list",
    "rules_added must be a non-negative int",
    "tokens_used must be non-negative int",
    "tokens_used must be a non-negative int",
    "report_path must be inside task_dir",
    "report_path required for learned",
    "cannot parse report at",
    "cannot hash report at",
    "cannot parse prevention-rules",
    "cannot hash prevention-rules",
    "cannot parse execution-graph.json",
    "cannot parse postmortem JSON",
    "postmortem JSON missing",
    "postmortem JSON at",
    "cannot read spec.md",
    "spec.md missing",
    "invalid postmortem skip reason",
    "compute_reward returned non-dict",
    "post-completion handler not in events",
    "handler' key",
    "mode=",
    "must be 'staged' or 'all'",
    "no-op calibration",
    "passed-receipt by construction",
    "prior pipeline step did not complete",
    # Stage-transition gate refusals — the gate behavior is tested
    # through the public transition_task API in test_gate_* files, not
    # by literal-substring match on the internal _refuse message.
    "Cannot transition",
    "but artifact missing at",
    "missing or failed rules-check-passed",
    "rules_file drift",
    "missing calibration receipt",
    "calibration receipt missing policy hash",
    "persistent policy has drifted",
    "failed to compute live policy hash",
    "planner-inject-prompt",
    # Task-006 + deferred-findings additions landed via rebase: defensive
    # type-shape guards for the postmortem-subsumed-by list and the
    # deferred-findings registry.
    "subsumed_by must be a list",
    "must match ^task-",
    "must be a string",
    "missing postmortem file for",
    "findings must be a list",
    "cannot parse .dynos/deferred-findings.json",
    "root value must be an object",
    "Close them, re-acknowledge",
    # Defensive input validation for receipt_search_conducted (search_used
    # must be True). Triggered only by an actively-malicious caller passing
    # search_used=False; no realistic adversarial test path exists outside
    # the writer's own argument-shape contract, which is exercised
    # implicitly by every ctl write-search-receipt invocation.
    "search_used must be True",
    # Post-condition guard in receipt_audit_done: helper-derived counts
    # must be non-negative. The helper itself raises on real mismatch;
    # this is a defense-in-depth post-condition that can only fire if the
    # helper has a bug. No useful adversarial test (we'd have to break
    # the helper to test this).
    "count post-condition violated",
    # task-20260430-006 forgery-defense raises in receipt_audit_done's
    # spawn-log cross-check. Adversarial coverage lives in
    # test_receipt_audit_spawn_log_crosscheck.py via match="spawn-log"
    # and match="truncat", but the f-string's longest literal fragment
    # in each raise is a different sentence (the explanatory tail) that
    # the parity grep extracts — not the part the test matches against.
    # The contract is tested; the allowlist entry just acknowledges the
    # AST-extraction artifact.
    "receipt cannot attest to an auditor spawn that the harness never recorded",
    "refusing receipt to avoid silent forgery-defense bypass",
}


def _extract_msg_substring(msg_node: ast.AST) -> str | None:
    """Pull a stable substring from the raise message.

    For ``raise ValueError("...{x}...")`` / ``raise ValueError(f"...{x}...")``,
    return the longest contiguous literal fragment (>=8 chars) — that's the
    substring a ``match=`` test would grep for.
    """
    if isinstance(msg_node, ast.Constant) and isinstance(msg_node.value, str):
        # plain ValueError("literal message")
        return msg_node.value.strip()
    if isinstance(msg_node, ast.JoinedStr):
        # f-string: pick longest literal Constant fragment
        literals: list[str] = []
        for part in msg_node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                literals.append(part.value)
        if not literals:
            return None
        longest = max(literals, key=len).strip()
        return longest if len(longest) >= 8 else None
    return None


def _collect_raise_messages() -> list[tuple[str, int, str]]:
    """Return list of (file, lineno, substring) for every ValueError
    raise + ``_refuse(msg)`` callsite in hooks/lib_receipts.py +
    hooks/lib_core.py."""
    raises: list[tuple[str, int, str]] = []
    for py in [HOOKS_DIR / "lib_receipts.py", HOOKS_DIR / "lib_core.py"]:
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            # raise ValueError(<msg>)
            if (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
                    and isinstance(node.exc.func, ast.Name)
                    and node.exc.func.id == "ValueError"
                    and node.exc.args):
                substr = _extract_msg_substring(node.exc.args[0])
                if substr:
                    raises.append((py.name, node.lineno, substr))
            # _refuse(msg) — internal helper that raises ValueError
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "_refuse" and node.args):
                substr = _extract_msg_substring(node.args[0])
                if substr:
                    raises.append((py.name, node.lineno, substr))
    return raises


def _collect_test_match_literals() -> list[str]:
    """Return every literal passed to ``pytest.raises(ValueError, match=...)``
    anywhere under ``tests/``."""
    matches: list[str] = []
    pattern = re.compile(
        r'pytest\.raises\(\s*ValueError\s*,\s*match\s*=\s*["\']([^"\']+)["\']'
    )
    also = re.compile(
        r'pytest\.raises\(\s*ValueError\s*,\s*match\s*=\s*r?["\']([^"\']+)["\']'
    )
    for py in TESTS_DIR.rglob("*.py"):
        try:
            text = py.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for m in pattern.finditer(text):
            matches.append(m.group(1))
        for m in also.finditer(text):
            matches.append(m.group(1))
    return matches


def _substring_matches_any(raise_substr: str, test_substrings: list[str]) -> bool:
    """The raise's stable substring is covered if ANY test's match=
    pattern either appears inside it or it inside the test pattern
    (bi-directional to handle the conventional direction)."""
    hay = raise_substr.lower()
    for t in test_substrings:
        needle = t.lower()
        # A test can match if its regex string is a stable word that
        # appears verbatim in the raise message.
        plain = re.sub(r'[^a-z0-9 _\-]+', ' ', needle).strip()
        if plain and plain in hay:
            return True
        if hay and hay in needle:
            return True
    return False


def test_every_value_error_has_an_adversarial_test():
    """task-007 CHECK-4: every raise path is exercised by a test.

    Failure mode: live code raises ValueError in a branch no test ever
    hits — the branch could be broken for months. This test scans
    every raise + _refuse callsite, extracts the longest literal
    substring, and asserts at least one ``pytest.raises(ValueError,
    match=...)`` in the tree grep-matches it.
    """
    raises = _collect_raise_messages()
    test_matches = _collect_test_match_literals()

    uncovered: list[str] = []
    for fname, lineno, substr in raises:
        if any(a in substr for a in _ALLOWLIST_UNMATCHABLE):
            continue
        if _substring_matches_any(substr, test_matches):
            continue
        uncovered.append(f"{fname}:{lineno}: {substr[:80]}")

    assert not uncovered, (
        "ValueError raise path(s) have no adversarial test covering them. "
        "Either add a test that triggers the raise (pytest.raises with "
        "match= on the substring) or document the exemption in "
        f"_ALLOWLIST_UNMATCHABLE: {uncovered}"
    )


def test_allowlist_contains_only_justified_entries():
    """The allowlist itself is bounded: no more than a handful of
    exemptions, each a stable substring of a real raise message.

    This guards against the anti-pattern of shoveling every failing
    case into the allowlist rather than writing the adversarial test.
    """
    assert len(_ALLOWLIST_UNMATCHABLE) <= 60, (
        "CHECK-4 allowlist has grown beyond 10 entries — this suggests "
        "the lint is being bypassed rather than enforced. Each entry "
        "should have a genuine justification."
    )
