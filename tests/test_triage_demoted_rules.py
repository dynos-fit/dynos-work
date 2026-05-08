"""Tests for scripts/triage_demoted_rules.py — task-20260508-007 seg-1.

These tests are written TDD-first. The implementation does NOT yet exist;
all tests are RED until seg-2 delivers scripts/triage_demoted_rules.py.

Coverage: ACs 1-22 (where unit-testable). ACs 18 and 19 require live file I/O
and are covered by seg-2's live execution. ACs 16 and 22 are integration tests
marked @pytest.mark.integration and skipped in normal CI runs.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Mark registration — required by addopts = --strict-markers in pytest.ini
# ---------------------------------------------------------------------------
# (integration marker is already declared in pytest.ini; no re-declaration needed)

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(ROOT / "scripts"))

from scripts.triage_demoted_rules import apply_mutations, compute_triage_doc, REGISTRY_PATH  # noqa: E402

# ---------------------------------------------------------------------------
# FIXTURE_RULES
#
# 15 demoted rules (rule_ids verbatim from spec ACs 1-15) + 3 clean rules.
# Field values for the 15 demoted rules use the pre-mutation state:
#   template="advisory", params={}, _demote_reason=<placeholder or verbatim>.
# For KEEP-PENDING rules (ACs 14, 15) the _demote_reason is a pre-state
# placeholder — the tests only assert POST-state matches the spec recipes.
# ---------------------------------------------------------------------------

DELETE_IDS = {
    "sec-e91392f0686e",
    "test-aa258399ad2e",
    "test-fc380dbf5cf3",
    "test-d0c6fe4832c2",
    "sec-92f009e1851f",
    "sec-2bf3c6cd183c",
    "test-3d8b06cdfc58",
    "sec-dbbfd6d46b79",
}

RE_PROMOTE_IDS = {
    "proc-1b48ed8447a4",
    "sec-caeca6dc4d41",
    "proc-e2deebce9e55",
    "proc-1b8219ff2da0",
    "test-15f7da751ace",
}

KEEP_PENDING_IDS = {
    "sec-e019ea060c46",
    "sec-344b09c453df",
}

ALL_DEMOTED_IDS = DELETE_IDS | RE_PROMOTE_IDS | KEEP_PENDING_IDS

FIXTURE_RULES: list[dict] = [
    # --- DELETE bucket (8 rules) ---
    {
        "rule_id": "sec-e91392f0686e",
        "rule": "Past-AC sentinel for task-20260423-001 AC13.",
        "category": "security",
        "executor": "all",
        "source_task": "task-20260423-001",
        "source_finding": "sf-sec-e91392f0686e",
        "rationale": "Past-AC sentinel; delete.",
        "enforcement": "prompt-constraint",
        "added_at": "2026-04-23T00:00:00Z",
        "template": "advisory",
        "params": {},
        "_demote_reason": "placeholder pre-state demote reason",
    },
    {
        "rule_id": "test-aa258399ad2e",
        "rule": "Past-task sentinel; no symbol to anchor.",
        "category": "test",
        "executor": "all",
        "source_task": "task-20260424-001",
        "source_finding": "sf-test-aa258399ad2e",
        "rationale": "Past-task sentinel; delete by default per discovery Q5.",
        "enforcement": "prompt-constraint",
        "added_at": "2026-04-24T00:00:00Z",
        "template": "advisory",
        "params": {},
        "_demote_reason": "placeholder pre-state demote reason",
    },
    {
        "rule_id": "test-fc380dbf5cf3",
        "rule": "Past-AC sentinel for task-20260506-002.",
        "category": "test",
        "executor": "all",
        "source_task": "task-20260506-002",
        "source_finding": "sf-test-fc380dbf5cf3",
        "rationale": "Past-AC sentinel; delete.",
        "enforcement": "prompt-constraint",
        "added_at": "2026-05-06T00:00:00Z",
        "template": "advisory",
        "params": {},
        "_demote_reason": "placeholder pre-state demote reason",
    },
    {
        "rule_id": "test-d0c6fe4832c2",
        "rule": "Past-AC sentinel for task-20260506-005.",
        "category": "test",
        "executor": "all",
        "source_task": "task-20260506-005",
        "source_finding": "sf-test-d0c6fe4832c2",
        "rationale": "Past-AC sentinel; delete.",
        "enforcement": "prompt-constraint",
        "added_at": "2026-05-06T00:00:00Z",
        "template": "advisory",
        "params": {},
        "_demote_reason": "placeholder pre-state demote reason",
    },
    {
        "rule_id": "sec-92f009e1851f",
        "rule": "Regex fundamentally too broad; no clean re-template path.",
        "category": "security",
        "executor": "all",
        "source_task": "task-20260424-002",
        "source_finding": "sf-sec-92f009e1851f",
        "rationale": "Bucket B — broad regex; delete.",
        "enforcement": "prompt-constraint",
        "added_at": "2026-04-24T00:00:00Z",
        "template": "advisory",
        "params": {},
        "_demote_reason": "placeholder pre-state demote reason",
    },
    {
        "rule_id": "sec-2bf3c6cd183c",
        "rule": "co_modification_required cannot express function-level scope.",
        "category": "security",
        "executor": "all",
        "source_task": "task-20260425-001",
        "source_finding": "sf-sec-2bf3c6cd183c",
        "rationale": "Bucket D — no substrate; delete.",
        "enforcement": "prompt-constraint",
        "added_at": "2026-04-25T00:00:00Z",
        "template": "advisory",
        "params": {},
        "_demote_reason": "placeholder pre-state demote reason",
    },
    {
        "rule_id": "test-3d8b06cdfc58",
        "rule": "Past-AC sentinel for task-20260507-004 specific run_checks tests.",
        "category": "test",
        "executor": "all",
        "source_task": "task-20260507-004",
        "source_finding": "sf-test-3d8b06cdfc58",
        "rationale": "Bucket E — past-AC sentinel; delete.",
        "enforcement": "prompt-constraint",
        "added_at": "2026-05-07T00:00:00Z",
        "template": "advisory",
        "params": {},
        "_demote_reason": "placeholder pre-state demote reason",
    },
    {
        "rule_id": "sec-dbbfd6d46b79",
        "rule": "Operator-confirmed DELETE after empirical falsification of Option Z.",
        "category": "security",
        "executor": "all",
        "source_task": "task-20260507-005",
        "source_finding": "sf-sec-dbbfd6d46b79",
        "rationale": "Bucket C — false-positive risk confirmed; production fix shipped; delete.",
        "enforcement": "prompt-constraint",
        "added_at": "2026-05-07T00:00:00Z",
        "template": "advisory",
        "params": {},
        "_demote_reason": "placeholder pre-state demote reason",
    },
    # --- RE-PROMOTE bucket (5 rules) ---
    {
        "rule_id": "proc-1b48ed8447a4",
        "rule": "normalize auditor_key via single helper; ban inline subagent_type string compares.",
        "category": "process",
        "executor": "all",
        "source_task": "task-20260430-001",
        "source_finding": "sf-proc-1b48ed8447a4",
        "rationale": "_normalize_auditor_key is the sole normalization helper.",
        "enforcement": "prompt-constraint",
        "added_at": "2026-04-30T00:00:00Z",
        "template": "advisory",
        "params": {},
        "_demote_reason": "placeholder pre-state demote reason",
    },
    {
        "rule_id": "sec-caeca6dc4d41",
        "rule": "new bounds-check sites must call _persistent_project_dir() when artifact persists outside task_dir.",
        "category": "security",
        "executor": "all",
        "source_task": "task-20260501-001",
        "source_finding": "sf-sec-caeca6dc4d41",
        "rationale": "_persistent_project_dir is the bounds-check helper.",
        "enforcement": "prompt-constraint",
        "added_at": "2026-05-01T00:00:00Z",
        "template": "advisory",
        "params": {},
        "_demote_reason": "placeholder pre-state demote reason",
    },
    {
        "rule_id": "proc-e2deebce9e55",
        "rule": "Every promoted rule must round-trip through engine eval before persistence.",
        "category": "process",
        "executor": "all",
        "source_task": "task-20260502-001",
        "source_finding": "sf-proc-e2deebce9e55",
        "rationale": "run_checks is the engine evaluation entry point.",
        "enforcement": "prompt-constraint",
        "added_at": "2026-05-02T00:00:00Z",
        "template": "advisory",
        "params": {},
        "_demote_reason": "placeholder pre-state demote reason",
    },
    {
        "rule_id": "proc-1b8219ff2da0",
        "rule": "_demote_reason entries must reference an open engine bug ticket, not stand alone.",
        "category": "process",
        "executor": "all",
        "source_task": "task-20260503-001",
        "source_finding": "sf-proc-1b8219ff2da0",
        "rationale": "Advisory stays advisory; remove _demote_reason only.",
        "enforcement": "prompt-constraint",
        "added_at": "2026-05-03T00:00:00Z",
        "template": "advisory",
        "params": {},
        "_demote_reason": "placeholder pre-state demote reason",
    },
    {
        "rule_id": "test-15f7da751ace",
        "rule": "Test fixtures declaring function signatures must match production signatures exactly.",
        "category": "test",
        "executor": "all",
        "source_task": "task-20260504-001",
        "source_finding": "sf-test-15f7da751ace",
        "rationale": "signature_lock for run_checks.",
        "enforcement": "prompt-constraint",
        "added_at": "2026-05-04T00:00:00Z",
        "template": "advisory",
        "params": {},
        "_demote_reason": "placeholder pre-state demote reason",
    },
    # --- KEEP-PENDING bucket (2 rules) ---
    {
        "rule_id": "sec-e019ea060c46",
        "rule": "Verify signed events before processing in circuit_breaker.",
        "category": "security",
        "executor": "all",
        "source_task": "task-20260505-001",
        "source_finding": "sf-sec-e019ea060c46",
        "rationale": "Blocked on residual 9afc5266 (SEC-CB-001).",
        "enforcement": "prompt-constraint",
        "added_at": "2026-05-05T00:00:00Z",
        "template": "advisory",
        "params": {},
        "_demote_reason": "placeholder pre-state demote reason",
    },
    {
        "rule_id": "sec-344b09c453df",
        "rule": "Co-hardening of circuit_breaker signed-event reads with policy_engine reads.",
        "category": "security",
        "executor": "all",
        "source_task": "task-20260505-002",
        "source_finding": "sf-sec-344b09c453df",
        "rationale": "Blocked on residual 9afc5266 (SEC-CB-001) and co-modification substrate.",
        "enforcement": "prompt-constraint",
        "added_at": "2026-05-05T00:00:00Z",
        "template": "advisory",
        "params": {},
        "_demote_reason": "placeholder pre-state demote reason",
    },
    # --- Clean rules (3 non-demoted rules, no _demote_reason) ---
    {
        "rule_id": "test-clean-001",
        "rule": "Non-demoted test rule A — must not be touched.",
        "category": "test",
        "executor": "all",
        "source_task": "task-20260101-001",
        "source_finding": "sf-clean-001",
        "rationale": "Fixture clean rule for AC 17 verification.",
        "enforcement": "prompt-constraint",
        "added_at": "2026-01-01T00:00:00Z",
        "template": "advisory",
        "params": {},
    },
    {
        "rule_id": "sec-clean-002",
        "rule": "Non-demoted security rule B — must not be touched.",
        "category": "security",
        "executor": "all",
        "source_task": "task-20260101-002",
        "source_finding": "sf-clean-002",
        "rationale": "Fixture clean rule for AC 17 verification.",
        "enforcement": "prompt-constraint",
        "added_at": "2026-01-01T00:00:00Z",
        "template": "caller_count_required",
        "params": {"symbol": "some_symbol", "scope": "hooks/**/*.py", "min_count": 1},
    },
    {
        "rule_id": "proc-clean-003",
        "rule": "Non-demoted process rule C — must not be touched.",
        "category": "process",
        "executor": "all",
        "source_task": "task-20260101-003",
        "source_finding": "sf-clean-003",
        "rationale": "Fixture clean rule for AC 17 verification.",
        "enforcement": "prompt-constraint",
        "added_at": "2026-01-01T00:00:00Z",
        "template": "advisory",
        "params": {},
    },
]

CLEAN_IDS = {"test-clean-001", "sec-clean-002", "proc-clean-003"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_mutated_rule(mutated_rules: list[dict], rule_id: str) -> dict:
    """Return the first rule matching rule_id, or raise KeyError."""
    for r in mutated_rules:
        if r.get("rule_id") == rule_id:
            return r
    raise KeyError(f"rule_id {rule_id!r} not found in mutated_rules")


def _get_audit_row(audit_rows: list[dict], rule_id: str) -> dict:
    """Return the audit row for rule_id, or raise KeyError."""
    for row in audit_rows:
        if row.get("rule_id") == rule_id:
            return row
    raise KeyError(f"rule_id {rule_id!r} not found in audit_rows")


def _is_hex64(s: object) -> bool:
    if not isinstance(s, str) or len(s) != 64:
        return False
    try:
        int(s, 16)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# AC 1-8: DELETE correctness
# ---------------------------------------------------------------------------

def test_delete_removes_8_rule_ids():
    """ACs 1-8: apply_mutations removes all 8 DELETE rule_ids from the output list.

    No rule whose rule_id is in the DELETE set should survive in mutated_rules.
    """
    mutated_rules, audit_rows = apply_mutations(FIXTURE_RULES)

    surviving_ids = {r["rule_id"] for r in mutated_rules}
    still_present = DELETE_IDS & surviving_ids

    assert still_present == set(), (
        f"The following DELETE rule_ids were NOT removed from mutated_rules: {still_present}"
    )

    # Also verify audit_rows records exactly 8 DELETE decisions
    delete_rows = [row for row in audit_rows if row.get("decision") == "DELETE"]
    assert len(delete_rows) == 8, (
        f"Expected 8 audit rows with decision=DELETE, got {len(delete_rows)}: "
        f"{[r['rule_id'] for r in delete_rows]}"
    )


# ---------------------------------------------------------------------------
# AC 9: RE-PROMOTE proc-1b48ed8447a4
# ---------------------------------------------------------------------------

def test_re_promote_proc_1b48ed8447a4():
    """AC 9: proc-1b48ed8447a4 is re-promoted to caller_count_required with correct params."""
    mutated_rules, _ = apply_mutations(FIXTURE_RULES)
    rule = _get_mutated_rule(mutated_rules, "proc-1b48ed8447a4")

    assert rule["template"] == "caller_count_required", (
        f"Expected template='caller_count_required', got {rule['template']!r}"
    )
    assert rule["params"] == {
        "symbol": "_normalize_auditor_key",
        "scope": "hooks/**/*.py",
        "min_count": 1,
    }, f"params mismatch: {rule['params']}"
    assert "_demote_reason" not in rule, (
        f"_demote_reason must be absent after RE-PROMOTE, but got: {rule.get('_demote_reason')!r}"
    )
    # enforcement must be unchanged
    assert rule["enforcement"] == "prompt-constraint", (
        f"enforcement was modified: {rule['enforcement']!r}"
    )


def test_re_promote_proc_1b48ed8447a4_other_fields_unchanged():
    """AC 9: fields other than template/params/_demote_reason are not modified for proc-1b48ed8447a4."""
    mutated_rules, _ = apply_mutations(FIXTURE_RULES)
    rule = _get_mutated_rule(mutated_rules, "proc-1b48ed8447a4")
    original = _get_fixture_rule("proc-1b48ed8447a4")

    for field in ("rule", "rule_id", "category", "source_task", "source_finding",
                  "rationale", "added_at"):
        assert rule[field] == original[field], (
            f"Field {field!r} was modified: {rule[field]!r} != {original[field]!r}"
        )


# ---------------------------------------------------------------------------
# AC 10: RE-PROMOTE sec-caeca6dc4d41
# ---------------------------------------------------------------------------

def test_re_promote_sec_caeca6dc4d41():
    """AC 10: sec-caeca6dc4d41 is re-promoted to caller_count_required with correct params."""
    mutated_rules, _ = apply_mutations(FIXTURE_RULES)
    rule = _get_mutated_rule(mutated_rules, "sec-caeca6dc4d41")

    assert rule["template"] == "caller_count_required", (
        f"Expected template='caller_count_required', got {rule['template']!r}"
    )
    assert rule["params"] == {
        "symbol": "_persistent_project_dir",
        "scope": "hooks/receipts/*.py",
        "min_count": 1,
    }, f"params mismatch: {rule['params']}"
    assert "_demote_reason" not in rule, (
        f"_demote_reason must be absent after RE-PROMOTE"
    )
    assert rule["enforcement"] == "prompt-constraint", (
        f"enforcement was modified: {rule['enforcement']!r}"
    )


def test_re_promote_sec_caeca6dc4d41_other_fields_unchanged():
    """AC 10: immutable fields are not modified for sec-caeca6dc4d41."""
    mutated_rules, _ = apply_mutations(FIXTURE_RULES)
    rule = _get_mutated_rule(mutated_rules, "sec-caeca6dc4d41")
    original = _get_fixture_rule("sec-caeca6dc4d41")

    for field in ("rule", "rule_id", "category", "source_task", "source_finding",
                  "rationale", "added_at"):
        assert rule[field] == original[field], (
            f"Field {field!r} was modified for sec-caeca6dc4d41"
        )


# ---------------------------------------------------------------------------
# AC 11: RE-PROMOTE proc-e2deebce9e55
# ---------------------------------------------------------------------------

def test_re_promote_proc_e2deebce9e55():
    """AC 11: proc-e2deebce9e55 is re-promoted to caller_count_required with min_count=2."""
    mutated_rules, _ = apply_mutations(FIXTURE_RULES)
    rule = _get_mutated_rule(mutated_rules, "proc-e2deebce9e55")

    assert rule["template"] == "caller_count_required", (
        f"Expected template='caller_count_required', got {rule['template']!r}"
    )
    assert rule["params"] == {
        "symbol": "run_checks",
        "scope": "hooks/**/*.py",
        "min_count": 2,
    }, f"params mismatch: {rule['params']}"
    assert "_demote_reason" not in rule, (
        "_demote_reason must be absent after RE-PROMOTE"
    )
    assert rule["enforcement"] == "prompt-constraint", (
        f"enforcement was modified: {rule['enforcement']!r}"
    )


def test_re_promote_proc_e2deebce9e55_other_fields_unchanged():
    """AC 11: immutable fields are not modified for proc-e2deebce9e55."""
    mutated_rules, _ = apply_mutations(FIXTURE_RULES)
    rule = _get_mutated_rule(mutated_rules, "proc-e2deebce9e55")
    original = _get_fixture_rule("proc-e2deebce9e55")

    for field in ("rule", "rule_id", "category", "source_task", "source_finding",
                  "rationale", "added_at"):
        assert rule[field] == original[field], (
            f"Field {field!r} was modified for proc-e2deebce9e55"
        )


# ---------------------------------------------------------------------------
# AC 12: RE-PROMOTE proc-1b8219ff2da0 (stays advisory, removes _demote_reason)
# ---------------------------------------------------------------------------

def test_re_promote_proc_1b8219ff2da0():
    """AC 12: proc-1b8219ff2da0 stays advisory/prompt-constraint; only _demote_reason removed."""
    mutated_rules, _ = apply_mutations(FIXTURE_RULES)
    rule = _get_mutated_rule(mutated_rules, "proc-1b8219ff2da0")

    assert rule["template"] == "advisory", (
        f"Expected template='advisory' (unchanged), got {rule['template']!r}"
    )
    assert rule["enforcement"] == "prompt-constraint", (
        f"enforcement must remain 'prompt-constraint', got {rule['enforcement']!r}"
    )
    assert "_demote_reason" not in rule, (
        "_demote_reason must be absent after RE-PROMOTE"
    )


def test_re_promote_proc_1b8219ff2da0_other_fields_unchanged():
    """AC 12: all fields other than _demote_reason are unchanged for proc-1b8219ff2da0."""
    mutated_rules, _ = apply_mutations(FIXTURE_RULES)
    rule = _get_mutated_rule(mutated_rules, "proc-1b8219ff2da0")
    original = _get_fixture_rule("proc-1b8219ff2da0")

    for field in ("rule", "rule_id", "category", "source_task", "rationale",
                  "added_at", "params"):
        assert rule[field] == original[field], (
            f"Field {field!r} was modified for proc-1b8219ff2da0"
        )


# ---------------------------------------------------------------------------
# AC 13: RE-PROMOTE test-15f7da751ace (signature_lock)
# ---------------------------------------------------------------------------

def test_re_promote_test_15f7da751ace():
    """AC 13: test-15f7da751ace is re-promoted to signature_lock with exact expected_params."""
    mutated_rules, _ = apply_mutations(FIXTURE_RULES)
    rule = _get_mutated_rule(mutated_rules, "test-15f7da751ace")

    assert rule["template"] == "signature_lock", (
        f"Expected template='signature_lock', got {rule['template']!r}"
    )
    assert rule["params"] == {
        "module": "hooks.rules_engine",
        "function": "run_checks",
        "expected_params": ["root", "mode", "rule_filter", "raw_rules"],
    }, f"params mismatch: {rule['params']}"
    assert "_demote_reason" not in rule, (
        "_demote_reason must be absent after RE-PROMOTE"
    )
    assert rule["enforcement"] == "prompt-constraint", (
        f"enforcement was modified: {rule['enforcement']!r}"
    )


def test_re_promote_test_15f7da751ace_other_fields_unchanged():
    """AC 13: immutable fields are not modified for test-15f7da751ace."""
    mutated_rules, _ = apply_mutations(FIXTURE_RULES)
    rule = _get_mutated_rule(mutated_rules, "test-15f7da751ace")
    original = _get_fixture_rule("test-15f7da751ace")

    for field in ("rule", "rule_id", "category", "source_task", "source_finding",
                  "rationale", "added_at"):
        assert rule[field] == original[field], (
            f"Field {field!r} was modified for test-15f7da751ace"
        )


# ---------------------------------------------------------------------------
# AC 14: KEEP-PENDING sec-e019ea060c46 — exact _demote_reason text
# ---------------------------------------------------------------------------

_KEEP_PENDING_SEC_E019_DEMOTE_REASON = (
    "Re-promote as caller_count_required scope=hooks/circuit_breaker.py "
    "symbol=verify_signed_events min_count=1 once residual "
    "9afc5266-8949-4b9a-9ce7-3e141857b2e1 (SEC-CB-001) ships. "
    "Do not re-promote until that residual is closed."
)


def test_keep_pending_sec_e019ea060c46_demote_reason_text():
    """AC 14: sec-e019ea060c46 _demote_reason is rewritten to the exact verbatim spec recipe."""
    mutated_rules, _ = apply_mutations(FIXTURE_RULES)
    rule = _get_mutated_rule(mutated_rules, "sec-e019ea060c46")

    assert "_demote_reason" in rule, (
        "sec-e019ea060c46 must retain _demote_reason (KEEP-PENDING)"
    )
    assert rule["_demote_reason"] == _KEEP_PENDING_SEC_E019_DEMOTE_REASON, (
        f"_demote_reason text mismatch for sec-e019ea060c46.\n"
        f"Expected: {_KEEP_PENDING_SEC_E019_DEMOTE_REASON!r}\n"
        f"Got:      {rule['_demote_reason']!r}"
    )


def test_keep_pending_sec_e019ea060c46_other_fields_unchanged():
    """AC 14: template, params, enforcement, and immutable fields are unchanged for sec-e019ea060c46."""
    mutated_rules, _ = apply_mutations(FIXTURE_RULES)
    rule = _get_mutated_rule(mutated_rules, "sec-e019ea060c46")
    original = _get_fixture_rule("sec-e019ea060c46")

    assert rule["template"] == "advisory", (
        f"template must remain 'advisory', got {rule['template']!r}"
    )
    assert rule["params"] == {}, (
        f"params must remain {{}}, got {rule['params']!r}"
    )
    assert rule["enforcement"] == "prompt-constraint", (
        f"enforcement must remain 'prompt-constraint', got {rule['enforcement']!r}"
    )
    for field in ("rule", "rule_id", "category", "source_task", "source_finding",
                  "rationale", "added_at"):
        assert rule[field] == original[field], (
            f"Field {field!r} was modified for sec-e019ea060c46"
        )


# ---------------------------------------------------------------------------
# AC 15: KEEP-PENDING sec-344b09c453df — exact _demote_reason text
# ---------------------------------------------------------------------------

_KEEP_PENDING_SEC_344_DEMOTE_REASON = (
    "Re-promote as pattern_must_not_appear or caller_count_required targeting "
    "co-hardening of circuit_breaker signed-event reads with policy_engine reads, "
    "once residual 9afc5266-8949-4b9a-9ce7-3e141857b2e1 (SEC-CB-001) ships and "
    "the function-level co-modification substrate exists. "
    "Do not re-promote until that residual is closed."
)


def test_keep_pending_sec_344b09c453df_demote_reason_text():
    """AC 15: sec-344b09c453df _demote_reason is rewritten to the exact verbatim spec recipe."""
    mutated_rules, _ = apply_mutations(FIXTURE_RULES)
    rule = _get_mutated_rule(mutated_rules, "sec-344b09c453df")

    assert "_demote_reason" in rule, (
        "sec-344b09c453df must retain _demote_reason (KEEP-PENDING)"
    )
    assert rule["_demote_reason"] == _KEEP_PENDING_SEC_344_DEMOTE_REASON, (
        f"_demote_reason text mismatch for sec-344b09c453df.\n"
        f"Expected: {_KEEP_PENDING_SEC_344_DEMOTE_REASON!r}\n"
        f"Got:      {rule['_demote_reason']!r}"
    )


def test_keep_pending_sec_344b09c453df_other_fields_unchanged():
    """AC 15: template, params, enforcement, and immutable fields are unchanged for sec-344b09c453df."""
    mutated_rules, _ = apply_mutations(FIXTURE_RULES)
    rule = _get_mutated_rule(mutated_rules, "sec-344b09c453df")
    original = _get_fixture_rule("sec-344b09c453df")

    assert rule["template"] == "advisory", (
        f"template must remain 'advisory', got {rule['template']!r}"
    )
    assert rule["params"] == {}, (
        f"params must remain {{}}, got {rule['params']!r}"
    )
    assert rule["enforcement"] == "prompt-constraint", (
        f"enforcement must remain 'prompt-constraint', got {rule['enforcement']!r}"
    )
    for field in ("rule", "rule_id", "category", "source_task", "source_finding",
                  "rationale", "added_at"):
        assert rule[field] == original[field], (
            f"Field {field!r} was modified for sec-344b09c453df"
        )


# ---------------------------------------------------------------------------
# AC 17: Non-tampering — clean rules are byte-identical in output
# ---------------------------------------------------------------------------

def test_non_demoted_rules_untouched():
    """AC 17: the 3 non-demoted fixture rules survive in mutated_rules unchanged (deep-equal)."""
    mutated_rules, _ = apply_mutations(FIXTURE_RULES)

    for clean_id in CLEAN_IDS:
        original = _get_fixture_rule(clean_id)
        mutated = _get_mutated_rule(mutated_rules, clean_id)
        assert mutated == original, (
            f"Clean rule {clean_id!r} was modified.\n"
            f"Original: {original}\n"
            f"Mutated:  {mutated}"
        )


def test_no_extra_rules_mutated():
    """AC 17: the only rules that differ between input and output are the 15 demoted rule_ids.

    Any rule_id NOT in the 15 demoted set must be byte-identical in the output.
    """
    mutated_rules, _ = apply_mutations(FIXTURE_RULES)

    mutated_by_id = {r["rule_id"]: r for r in mutated_rules}
    original_by_id = {r["rule_id"]: r for r in FIXTURE_RULES}

    for rule_id, original in original_by_id.items():
        if rule_id in ALL_DEMOTED_IDS:
            continue  # Expected to be mutated or deleted
        assert rule_id in mutated_by_id, (
            f"Non-demoted rule {rule_id!r} was unexpectedly removed"
        )
        assert mutated_by_id[rule_id] == original, (
            f"Non-demoted rule {rule_id!r} was modified"
        )


# ---------------------------------------------------------------------------
# AC 20: Post-state count
# ---------------------------------------------------------------------------

def test_post_state_count():
    """AC 20: len(mutated_rules) == len(FIXTURE_RULES) - 8 (exactly 8 rules deleted)."""
    mutated_rules, _ = apply_mutations(FIXTURE_RULES)

    expected_count = len(FIXTURE_RULES) - 8
    assert len(mutated_rules) == expected_count, (
        f"Expected {expected_count} rules after mutation, got {len(mutated_rules)}"
    )


def test_post_state_count_preserves_non_demoted():
    """AC 20 / AC 17: non-demoted rules all survive — none are accidentally dropped."""
    mutated_rules, _ = apply_mutations(FIXTURE_RULES)

    surviving_ids = {r["rule_id"] for r in mutated_rules}
    for clean_id in CLEAN_IDS:
        assert clean_id in surviving_ids, (
            f"Non-demoted rule {clean_id!r} was incorrectly dropped"
        )


# ---------------------------------------------------------------------------
# AC 21: audit_rows structure and hash correctness
# ---------------------------------------------------------------------------

def test_audit_rows_count():
    """AC 21: exactly 15 audit rows are returned (one per demoted rule)."""
    _, audit_rows = apply_mutations(FIXTURE_RULES)

    assert len(audit_rows) == 15, (
        f"Expected 15 audit rows, got {len(audit_rows)}: "
        f"{[r['rule_id'] for r in audit_rows]}"
    )


def test_audit_rows_structure():
    """AC 21: each audit row contains the required keys with correct types."""
    _, audit_rows = apply_mutations(FIXTURE_RULES)

    required_keys = {
        "rule_id",
        "decision",
        "pre_template",
        "pre_params",
        "pre_demote_reason",
        "pre_hash",
        "post_template",
        "post_params",
        "post_demote_reason",
        "post_demote_reason_present",
        "post_hash",
    }

    for row in audit_rows:
        missing = required_keys - set(row.keys())
        assert not missing, (
            f"Audit row for {row.get('rule_id')!r} is missing keys: {missing}"
        )

        assert isinstance(row["post_demote_reason_present"], bool), (
            f"post_demote_reason_present must be bool for {row['rule_id']!r}"
        )

        assert row["decision"] in ("DELETE", "RE-PROMOTE", "KEEP-PENDING"), (
            f"Invalid decision {row['decision']!r} for {row['rule_id']!r}"
        )


def test_audit_rows_delete_post_hash_is_deleted_string():
    """AC 21: DELETE rows have post_hash == 'DELETED'."""
    _, audit_rows = apply_mutations(FIXTURE_RULES)

    for row in audit_rows:
        if row["decision"] == "DELETE":
            assert row["post_hash"] == "DELETED", (
                f"DELETE row for {row['rule_id']!r} has post_hash={row['post_hash']!r}, expected 'DELETED'"
            )


def test_audit_rows_surviving_rules_post_hash_is_hex64():
    """AC 21: RE-PROMOTE and KEEP-PENDING rows have a 64-char hex SHA-256 as post_hash."""
    _, audit_rows = apply_mutations(FIXTURE_RULES)

    for row in audit_rows:
        if row["decision"] in ("RE-PROMOTE", "KEEP-PENDING"):
            assert _is_hex64(row["post_hash"]), (
                f"Row {row['rule_id']!r} decision={row['decision']!r} has "
                f"invalid post_hash: {row['post_hash']!r} (expected 64-char hex)"
            )


def test_audit_rows_pre_hash_is_hex64():
    """AC 21: all audit rows have a valid 64-char hex SHA-256 as pre_hash."""
    _, audit_rows = apply_mutations(FIXTURE_RULES)

    for row in audit_rows:
        assert _is_hex64(row["pre_hash"]), (
            f"Row {row['rule_id']!r} has invalid pre_hash: {row['pre_hash']!r}"
        )


def test_audit_rows_pre_hash_matches_input_rule():
    """AC 21: pre_hash matches hashlib.sha256(json.dumps(rule, sort_keys=True)).hexdigest() for each demoted rule."""
    _, audit_rows = apply_mutations(FIXTURE_RULES)

    fixture_by_id = {r["rule_id"]: r for r in FIXTURE_RULES}

    for row in audit_rows:
        rule_id = row["rule_id"]
        original = fixture_by_id[rule_id]
        expected_hash = hashlib.sha256(
            json.dumps(original, sort_keys=True).encode()
        ).hexdigest()
        assert row["pre_hash"] == expected_hash, (
            f"pre_hash mismatch for {rule_id!r}.\n"
            f"Expected: {expected_hash}\n"
            f"Got:      {row['pre_hash']}"
        )


def test_audit_rows_decisions_cover_all_demoted_ids():
    """AC 21: audit_rows account for all 15 demoted rule_ids — one row per rule_id."""
    _, audit_rows = apply_mutations(FIXTURE_RULES)

    row_ids = {row["rule_id"] for row in audit_rows}
    assert row_ids == ALL_DEMOTED_IDS, (
        f"audit_rows rule_id set mismatch.\n"
        f"Expected: {sorted(ALL_DEMOTED_IDS)}\n"
        f"Got:      {sorted(row_ids)}"
    )


def test_audit_rows_keep_pending_post_demote_reason_present_true():
    """AC 21: KEEP-PENDING rows have post_demote_reason_present == True."""
    _, audit_rows = apply_mutations(FIXTURE_RULES)

    for row in audit_rows:
        if row["decision"] == "KEEP-PENDING":
            assert row["post_demote_reason_present"] is True, (
                f"KEEP-PENDING row {row['rule_id']!r} has "
                f"post_demote_reason_present={row['post_demote_reason_present']!r}, expected True"
            )


def test_audit_rows_re_promote_post_demote_reason_present_false():
    """AC 21: RE-PROMOTE rows have post_demote_reason_present == False."""
    _, audit_rows = apply_mutations(FIXTURE_RULES)

    for row in audit_rows:
        if row["decision"] == "RE-PROMOTE":
            assert row["post_demote_reason_present"] is False, (
                f"RE-PROMOTE row {row['rule_id']!r} has "
                f"post_demote_reason_present={row['post_demote_reason_present']!r}, expected False"
            )


# ---------------------------------------------------------------------------
# AC 21: compute_triage_doc renders Markdown with required content
# ---------------------------------------------------------------------------

def test_compute_triage_doc_renders_markdown():
    """AC 21: compute_triage_doc returns a string containing all required content."""
    _, audit_rows = apply_mutations(FIXTURE_RULES)
    doc = compute_triage_doc(
        audit_rows,
        pre_count=18,
        post_count=10,
        check_output="(none)",
    )

    assert isinstance(doc, str), "compute_triage_doc must return a str"

    # All 15 rule_ids must appear
    for rule_id in ALL_DEMOTED_IDS:
        assert rule_id in doc, (
            f"rule_id {rule_id!r} is missing from the triage document"
        )

    # Decision labels must appear
    for decision in ("DELETE", "RE-PROMOTE", "KEEP-PENDING"):
        assert decision in doc, (
            f"Decision label {decision!r} is missing from the triage document"
        )

    # Pre/post counts must appear
    assert "18" in doc, "pre_count=18 must appear in the triage document"
    assert "10" in doc, "post_count=10 must appear in the triage document"

    # check_output must appear
    assert "(none)" in doc, "check_output '(none)' must appear in the triage document"


def test_compute_triage_doc_contains_pre_state_fields():
    """AC 21: triage doc includes pre_template and pre_demote_reason for each rule."""
    _, audit_rows = apply_mutations(FIXTURE_RULES)
    doc = compute_triage_doc(
        audit_rows,
        pre_count=18,
        post_count=10,
        check_output="no violations",
    )

    # Each row must have its pre_template surfaced
    for row in audit_rows:
        assert row["pre_template"] in doc, (
            f"pre_template {row['pre_template']!r} for {row['rule_id']!r} not found in doc"
        )


def test_compute_triage_doc_contains_hashes():
    """AC 21: triage doc contains SHA-256 hashes for at least the non-deleted rules."""
    _, audit_rows = apply_mutations(FIXTURE_RULES)
    doc = compute_triage_doc(
        audit_rows,
        pre_count=18,
        post_count=10,
        check_output="",
    )

    for row in audit_rows:
        assert row["pre_hash"] in doc, (
            f"pre_hash for {row['rule_id']!r} not found in triage document"
        )
        if row["post_hash"] != "DELETED":
            assert row["post_hash"] in doc, (
                f"post_hash for {row['rule_id']!r} not found in triage document"
            )


def test_compute_triage_doc_includes_deleted_label():
    """AC 21: triage doc explicitly marks deleted rules with 'DELETED' for their post_hash."""
    _, audit_rows = apply_mutations(FIXTURE_RULES)
    doc = compute_triage_doc(
        audit_rows,
        pre_count=18,
        post_count=10,
        check_output="",
    )

    # 'DELETED' must appear for the 8 deleted rules
    assert doc.count("DELETED") >= 8, (
        "Expected at least 8 occurrences of 'DELETED' in the triage document "
        f"(one per deleted rule), got {doc.count('DELETED')}"
    )


# ---------------------------------------------------------------------------
# AC 20 regression: apply_mutations is pure — calling twice gives same result
# ---------------------------------------------------------------------------

def test_apply_mutations_is_pure_idempotent_on_re_call():
    """apply_mutations must not mutate FIXTURE_RULES in place; calling twice gives same results."""
    import copy
    fixture_copy = copy.deepcopy(FIXTURE_RULES)

    mutated1, rows1 = apply_mutations(FIXTURE_RULES)
    mutated2, rows2 = apply_mutations(FIXTURE_RULES)

    # Input must be unmodified
    assert FIXTURE_RULES == fixture_copy, (
        "apply_mutations modified the input list in place"
    )

    # Both calls must produce identical results
    assert mutated1 == mutated2, "apply_mutations is not deterministic"
    assert rows1 == rows2, "apply_mutations audit_rows are not deterministic"


# ---------------------------------------------------------------------------
# Regression: DELETE rules produce no surviving rule in output (AC 1-8 per-id)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rule_id", sorted(DELETE_IDS))
def test_each_delete_id_not_in_output(rule_id: str):
    """ACs 1-8 (parametrized): each DELETE rule_id is absent from mutated_rules."""
    mutated_rules, _ = apply_mutations(FIXTURE_RULES)
    surviving_ids = {r["rule_id"] for r in mutated_rules}
    assert rule_id not in surviving_ids, (
        f"DELETE rule_id {rule_id!r} survived in mutated_rules"
    )


# ---------------------------------------------------------------------------
# Integration tests — skipped unless -m integration is passed
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_validate_rules_passes_against_actual_post_state():
    """AC 16: after mutations, validate-rules exits 0 against the actual registry.

    Skipped if REGISTRY_PATH does not exist (i.e., pre-seg-2 or different machine).
    """
    if not Path(REGISTRY_PATH).exists():
        pytest.skip(f"Registry not found at {REGISTRY_PATH}; skipping integration test")

    result = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "rules_engine.py"), "validate-rules"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, (
        f"validate-rules exited {result.returncode} after mutations.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    # Must not print error lines
    error_lines = [
        line for line in result.stdout.splitlines()
        if "error" in line.lower() or "invalid" in line.lower()
    ]
    assert not error_lines, (
        f"validate-rules printed error lines: {error_lines}"
    )


@pytest.mark.integration
def test_check_all_no_violations_from_re_promoted():
    """AC 22: check --mode all produces zero violations for the 5 re-promoted rule_ids.

    Skipped if REGISTRY_PATH does not exist.
    """
    if not Path(REGISTRY_PATH).exists():
        pytest.skip(f"Registry not found at {REGISTRY_PATH}; skipping integration test")

    result = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "rules_engine.py"), "check", "--mode", "all"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )

    # Parse violations — look for re-promoted rule_ids in the output
    re_promoted_violations = [
        line for line in result.stdout.splitlines()
        if any(rid in line for rid in RE_PROMOTE_IDS)
    ]

    assert not re_promoted_violations, (
        f"check --mode all produced violations from re-promoted rules:\n"
        + "\n".join(re_promoted_violations)
    )


# ---------------------------------------------------------------------------
# Internal helper — not a test
# ---------------------------------------------------------------------------

def _get_fixture_rule(rule_id: str) -> dict:
    """Return the original fixture rule for rule_id."""
    for r in FIXTURE_RULES:
        if r["rule_id"] == rule_id:
            return r
    raise KeyError(f"rule_id {rule_id!r} not in FIXTURE_RULES")
