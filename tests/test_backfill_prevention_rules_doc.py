"""
Documentation regression test for scripts/backfill_prevention_rules.py — AC 15
(task-20260616-002, finding #70).

The module docstring (line 13) and the inline phase-2 comment (lines 167-168)
claim that a "non-conformant rule_id" triggers regeneration. The behavioural
fix (a format-regex in hooks/rules_engine.py) is explicitly out of scope, so
the only correct action is to remove the over-claiming wording. This test
encodes the FIXED behaviour: the substring "non-conformant" must not appear
anywhere in the file.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKFILL_SCRIPT = ROOT / "scripts" / "backfill_prevention_rules.py"


def test_backfill_docstring_no_nonconformant_claim():
    """Neither 'non-conformant' nor 'non-conformant rule_id' may appear in the
    backfill script source. FAILS while the docstring and inline comment still
    claim non-conformant rule_ids are regenerated."""
    src = BACKFILL_SCRIPT.read_text(encoding="utf-8")
    assert "non-conformant rule_id" not in src, (
        "stale 'non-conformant rule_id' claim still present in "
        "backfill_prevention_rules.py"
    )
    assert "non-conformant" not in src, (
        "stale 'non-conformant' wording still present in "
        "backfill_prevention_rules.py"
    )


def test_backfill_rules_engine_untouched_marker():
    """Guard the out-of-scope boundary: the backfill script must not be edited
    to import a new format-regex validator from rules_engine; the doc fix is
    source-text only. This asserts the existing _validate_rule_entry usage is
    still the decision seam (no new behavioural wiring was introduced)."""
    src = BACKFILL_SCRIPT.read_text(encoding="utf-8")
    assert "_validate_rule_entry" in src, (
        "backfill script no longer references _validate_rule_entry — the fix "
        "must be doc-only and must not change the decision logic"
    )
