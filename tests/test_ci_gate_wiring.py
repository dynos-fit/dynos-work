"""TDD-first tests for task-20260420-001 D5 — ci-gate rule wiring.

Covers acceptance criteria 23, 24, 25:

    AC23 Five rules from task-009 postmortem promoted to enforcement: ci-gate
         in .dynos/persistent/prevention-rules.json.
    AC24 Two new rules added by this task, both enforcement: ci-gate:
         - task-20260420-001-caller-supplied-fields-in-trust-registry
         - task-20260420-001-task-attributed-log-events-per-task-only
    AC25 rules_engine wiring: each ci-gate rule's template must be one of
         the enforced-six set. Per plan audit revision #4 (split):
           - STRICT: the 2 NEW rules must have enforced template.
           - ALLOWLIST: 5 promoted rules may be declared-not-enforced
                        if listed in _KNOWN_DECLARED_NOT_ENFORCED_ALLOWLIST.
           - no rule outside that allowlist may declare ci-gate with
             advisory template AND source_task in the two tasks.

TODAY these tests FAIL because prevention-rules.json only has 1 rule.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
RULES_PATH = REPO_ROOT / ".dynos" / "persistent" / "prevention-rules.json"


ENFORCED_SIX = frozenset(
    {
        "pattern_must_not_appear",
        "co_modification_required",
        "signature_lock",
        "caller_count_required",
        "import_constant_only",
        "every_name_in_X_satisfies_Y",
    }
)


NEW_TASK_RULE_IDS = frozenset(
    {
        "task-20260420-001-caller-supplied-fields-in-trust-registry",
        "task-20260420-001-task-attributed-log-events-per-task-only",
    }
)


# The 5 promoted task-009 rules may sit at declared-not-enforced per ADR-9.
# This allowlist is test-visible so the postmortem can enumerate them.
# Exact rule_ids are not spec-pinned beyond the descriptive names in AC23;
# we accept any 5 task-20260419-009 rules with ci-gate enforcement so long
# as they either (a) have an enforced template or (b) appear in this
# allowlist. The allowlist is written by hand at D5 land time.
_KNOWN_DECLARED_NOT_ENFORCED_ALLOWLIST: frozenset[str] = frozenset(
    {
        # Populated in tests/test_ci_gate_wiring.py at D5 land; until then
        # the 5 task-009 rules will fail the wiring check — that is the
        # TDD-first red state, NOT permanent acceptance.
    }
)


def _load_rules() -> list[dict]:
    data = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    rules = data.get("rules", [])
    assert isinstance(rules, list)
    return rules


def _this_task_rules() -> list[dict]:
    return [
        r
        for r in _load_rules()
        if r.get("source_task") in {"task-20260419-009", "task-20260420-001"}
        and r.get("enforcement") == "ci-gate"
    ]


# ---------------------------------------------------------------------------
# AC24 STRICT: the 2 NEW rules must exist, be ci-gate, AND have enforced template
# ---------------------------------------------------------------------------


def test_two_new_rules_exist_with_ci_gate_enforcement():
    rules = _load_rules()
    by_id = {r.get("rule_id"): r for r in rules if r.get("rule_id")}
    missing = NEW_TASK_RULE_IDS - set(by_id)
    assert not missing, f"New task rules missing from catalog: {missing}"
    for rid in NEW_TASK_RULE_IDS:
        r = by_id[rid]
        assert r.get("enforcement") == "ci-gate", (
            f"{rid} must have enforcement=='ci-gate' (got {r.get('enforcement')!r})"
        )
        assert r.get("source_task") == "task-20260420-001", (
            f"{rid} must be tagged source_task=task-20260420-001"
        )
        rationale = r.get("rationale")
        assert isinstance(rationale, str) and rationale.strip(), (
            f"{rid} must have non-empty rationale"
        )


def test_task_001_new_rules_have_enforced_template():
    """AC25 STRICT — the 2 NEW rules MUST have one of the enforced-six
    templates. Declared-not-enforced is NOT acceptable for the new rules."""
    rules = _load_rules()
    by_id = {r.get("rule_id"): r for r in rules if r.get("rule_id")}
    for rid in NEW_TASK_RULE_IDS:
        assert rid in by_id, f"new rule {rid} missing"
        tmpl = by_id[rid].get("template")
        assert tmpl in ENFORCED_SIX, (
            f"{rid}: template {tmpl!r} is not in the enforced-six set "
            f"({sorted(ENFORCED_SIX)}) — declared-not-enforced is NOT "
            f"acceptable for the 2 new task-20260420-001 rules"
        )


# ---------------------------------------------------------------------------
# AC23: exactly 5 promoted + 2 new = 7 rules at ci-gate across both tasks
# ---------------------------------------------------------------------------


def test_promotion_count_is_exactly_seven():
    """jq equivalent: count rules with source_task in the two and
    enforcement == 'ci-gate' — expect exactly 7 (5 promoted + 2 new)."""
    matching = _this_task_rules()
    assert len(matching) == 7, (
        f"Expected exactly 7 rules at enforcement=='ci-gate' across "
        f"task-20260419-009 + task-20260420-001 (5 promoted + 2 new). "
        f"Got {len(matching)}: "
        f"{[r.get('rule_id') for r in matching]}"
    )


def test_five_task_009_rules_are_promoted_to_ci_gate():
    """AC23: 5 rules with source_task='task-20260419-009' at ci-gate."""
    rules = _load_rules()
    promoted = [
        r
        for r in rules
        if r.get("source_task") == "task-20260419-009"
        and r.get("enforcement") == "ci-gate"
    ]
    assert len(promoted) == 5, (
        f"Expected exactly 5 promoted task-009 rules at ci-gate, got "
        f"{len(promoted)}: {[r.get('rule_id') for r in promoted]}"
    )


# ---------------------------------------------------------------------------
# AC25 PROMOTED-RULE ALLOWLIST: named-exception for declared-not-enforced
# ---------------------------------------------------------------------------


def test_task_009_promoted_rules_declared_not_enforced_allowlist():
    """Per plan-audit revision #4: promoted rules whose template is
    'advisory' MUST be in _KNOWN_DECLARED_NOT_ENFORCED_ALLOWLIST. We log
    WARN for each — this test records the known-gap for postmortem but
    does not hard-fail on the allowlisted entries."""
    rules = _load_rules()
    promoted = [
        r
        for r in rules
        if r.get("source_task") == "task-20260419-009"
        and r.get("enforcement") == "ci-gate"
    ]
    diagnostics: list[str] = []
    leaks: list[str] = []
    for r in promoted:
        rid = r.get("rule_id")
        tmpl = r.get("template")
        if tmpl in ENFORCED_SIX:
            continue
        # Declared-not-enforced: must be in the allowlist.
        if rid in _KNOWN_DECLARED_NOT_ENFORCED_ALLOWLIST:
            diagnostics.append(f"{rid}: template={tmpl} (allowlisted)")
        else:
            leaks.append(f"{rid}: template={tmpl} (NOT allowlisted)")
    if diagnostics:
        print("\n".join(["[WARN] declared-not-enforced:"] + diagnostics))
    assert not leaks, (
        "Promoted task-009 rules with advisory template MUST be in "
        "_KNOWN_DECLARED_NOT_ENFORCED_ALLOWLIST or have an enforced template. "
        f"Leaks:\n  " + "\n  ".join(leaks)
    )


def test_no_new_declared_not_enforced_outside_allowlist():
    """AC25 STRICT GATE — any rule in either task with enforcement=ci-gate
    and template=advisory must be either (a) an allowlisted task-009 rule
    OR (b) one of the 2 new rules that DO have enforced template per
    test_task_001_new_rules_have_enforced_template.

    Any other row is unauthorized declared-not-enforced drift."""
    unauthorized: list[str] = []
    for r in _this_task_rules():
        tmpl = r.get("template")
        rid = r.get("rule_id")
        if tmpl in ENFORCED_SIX:
            continue
        if rid in _KNOWN_DECLARED_NOT_ENFORCED_ALLOWLIST:
            continue
        if rid in NEW_TASK_RULE_IDS:
            # Caught by the strict test above — don't double-report.
            continue
        unauthorized.append(f"{rid}: template={tmpl}")
    assert not unauthorized, (
        "Unauthorized declared-not-enforced rule(s): "
        + ", ".join(unauthorized)
    )


# ---------------------------------------------------------------------------
# Rules_engine catalog sanity: no broken `template` values
# ---------------------------------------------------------------------------


def test_every_this_task_rule_has_known_template():
    """The rules_engine dispatches on template. A typo in template means
    the rule is silently skipped — another form of declared-not-enforced.
    The template MUST be either one of the enforced-six OR 'advisory'."""
    all_known = ENFORCED_SIX | {"advisory"}
    for r in _this_task_rules():
        tmpl = r.get("template")
        assert tmpl in all_known, (
            f"rule {r.get('rule_id')!r}: unknown template {tmpl!r}. "
            f"rules_engine will silently skip this rule (declared-not-enforced)."
        )
