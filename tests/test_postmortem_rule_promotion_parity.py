"""Tests for postmortem rule-promotion parity (derived-vs-added accounting).

PR-130 self-proof bug: task-20260419-002's LLM postmortem-analyzer emitted
8 prevention rules under a `derived_rules` top-level key. The ingester
in `_normalize_analysis` read `prevention_rules` only. Zero rules were
promoted. `apply_analysis` returned `rules_added: 0` without any
corresponding event. The signal vanished silently.

This test suite pins three invariants that prevent silent drops:

1. `_normalize_analysis` accepts BOTH `prevention_rules` AND
   `derived_rules` top-level keys, merged and dedup'd by rule text.
2. Every dropped rule emits a `postmortem_rule_dropped` event with a
   concrete reason (normalize_returned_none, duplicate_text_within_analysis,
   or schema failure).
3. When `_input_rule_count > 0` AND `rules_added == 0`, `apply_analysis`
   emits a `postmortem_rule_promotion_dropped` event so a reviewer can
   grep for the specific class of drift. The receipt payload carries
   `input_rule_count` + `normalization_drops` + `rejected_count` +
   `rules_added` — a full chain of custody.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "memory"))

from postmortem_analysis import _normalize_analysis, apply_analysis


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / ".dynos").mkdir(parents=True)
    return root


def _task_dir(root: Path, task_id: str = "task-20260419-TP") -> Path:
    td = root / ".dynos" / task_id
    td.mkdir(parents=True, exist_ok=True)
    (td / "task-retrospective.json").write_text(json.dumps({
        "task_id": task_id,
        "task_type": "bugfix",
        "quality_score": 0.9,
    }))
    return td


def _read_events(root: Path, task_id: str | None = None) -> list[dict]:
    """Read events from both the global `.dynos/events.jsonl` and any
    per-task log. `log_event` writes to the task-scoped log when a
    `task=` kwarg is present, so tests must check the task-scoped file
    first (and the global file as a fallback for events without a task
    tag)."""
    out: list[dict] = []
    candidates = [root / ".dynos" / "events.jsonl"]
    if task_id is not None:
        candidates.append(root / ".dynos" / task_id / "events.jsonl")
    # Also cover any per-task dir (test fixtures sometimes vary the id).
    for task_dir in (root / ".dynos").glob("task-*"):
        candidates.append(task_dir / "events.jsonl")
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen or not path.exists():
            continue
        seen.add(key)
        for line in path.read_text().splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def _mk_rule(rule_text: str, category: str = "cq") -> dict:
    """Build a rule shape that passes `_validate_rule_schema`'s advisory
    template. The advisory template is the most permissive so this
    fixture exercises the happy path reliably."""
    return {
        "rule": rule_text,
        "category": category,
        "executor": "all",
        "enforcement": "advisory",
        "rationale": "test-fixture rule",
        "source_finding": "TEST-001",
        "template": "advisory",
        "params": {},
    }


# ---------------------------------------------------------------------------
# Invariant 1 — dual-key acceptance (the PR-130 bug)
# ---------------------------------------------------------------------------


def test_normalize_accepts_derived_rules_top_level_key():
    """LLM output using `derived_rules` (PR-130 prompt's key) must now
    be accepted. Previously the ingester read only `prevention_rules`
    and silently dropped everything under `derived_rules`."""
    analysis = {
        "summary": "test",
        "derived_rules": [
            _mk_rule("rule one"),
            _mk_rule("rule two"),
        ],
    }
    sanitized = _normalize_analysis(analysis)
    assert len(sanitized["prevention_rules"]) == 2
    assert sanitized["_input_rule_count"] == 2
    assert sanitized["_normalization_drops"] == []


def test_normalize_merges_both_top_level_keys():
    """When an LLM emits both keys (unusual but possible), both sets merge
    and duplicate rule text dedupes. Input count still equals the raw
    pre-dedup count so parity metadata reflects the true input."""
    analysis = {
        "prevention_rules": [_mk_rule("shared rule")],
        "derived_rules": [
            _mk_rule("shared rule"),
            _mk_rule("unique rule"),
        ],
    }
    sanitized = _normalize_analysis(analysis)
    assert len(sanitized["prevention_rules"]) == 2
    assert sanitized["_input_rule_count"] == 3
    assert len(sanitized["_normalization_drops"]) == 1
    assert sanitized["_normalization_drops"][0]["reason"] == "duplicate_text_within_analysis"


def test_normalize_drops_non_dict_entries_with_reason():
    """Junk entries that _normalize_rule returns None for each produce
    a drop record explaining why."""
    analysis = {
        "prevention_rules": [
            _mk_rule("valid"),
            "not-a-dict",
            {},
            _mk_rule("also valid"),
        ],
    }
    sanitized = _normalize_analysis(analysis)
    assert len(sanitized["prevention_rules"]) == 2
    assert sanitized["_input_rule_count"] == 4
    drop_reasons = [d["reason"] for d in sanitized["_normalization_drops"]]
    assert drop_reasons == [
        "normalize_returned_none",
        "normalize_returned_none",
    ]


# ---------------------------------------------------------------------------
# Invariant 2 — every drop emits an event
# ---------------------------------------------------------------------------


def test_normalization_drops_emit_events(tmp_path: Path):
    """apply_analysis emits one postmortem_rule_dropped event per drop
    (stage=normalize). Silent drop of the PR-130 kind cannot happen."""
    root = _project(tmp_path)
    td = _task_dir(root)
    analysis = {
        "summary": "test",
        "prevention_rules": [
            _mk_rule("valid rule one"),
            "garbage",
            _mk_rule("valid rule one"),
        ],
    }
    apply_analysis(td, analysis)
    events = _read_events(root)
    dropped = [
        e for e in events
        if e.get("event") == "postmortem_rule_dropped" and e.get("stage") == "normalize"
    ]
    reasons = sorted(e.get("reason", "") for e in dropped)
    assert reasons == ["duplicate_text_within_analysis", "normalize_returned_none"]


# ---------------------------------------------------------------------------
# Invariant 3 — input>0 AND added==0 emits a loud event
# ---------------------------------------------------------------------------


def test_silent_drop_produces_promotion_dropped_event(tmp_path: Path):
    """The PR-130 self-proof bug: LLM emits rules that all fail
    normalization. Result: rules_added=0. apply_analysis MUST emit a
    `postmortem_rule_promotion_dropped` event naming the input count."""
    root = _project(tmp_path)
    td = _task_dir(root)
    analysis = {
        "summary": "test",
        "derived_rules": [
            "junk-a",
            "junk-b",
            {},
            {"rule": ""},
        ],
    }
    result = apply_analysis(td, analysis)
    assert result["rules_added"] == 0
    assert result["input_rule_count"] == 4
    assert result["normalization_drops"] == 4

    events = _read_events(root)
    promo_dropped = [
        e for e in events if e.get("event") == "postmortem_rule_promotion_dropped"
    ]
    assert len(promo_dropped) == 1, (
        f"expected exactly one promotion-dropped event; got {promo_dropped}"
    )
    entry = promo_dropped[0]
    assert entry.get("input_rule_count") == 4
    assert entry.get("rules_added") == 0
    assert "zero were promoted" in entry.get("reason", "")


def test_no_spurious_promotion_dropped_when_clean_empty(tmp_path: Path):
    """When the LLM emits zero rule items, no drift event fires —
    legitimate no-rules case is different from the PR-130 silent drop."""
    root = _project(tmp_path)
    td = _task_dir(root)
    analysis = {"summary": "test", "prevention_rules": []}
    result = apply_analysis(td, analysis)
    assert result["rules_added"] == 0
    assert result["input_rule_count"] == 0

    events = _read_events(root)
    assert not any(
        e.get("event") == "postmortem_rule_promotion_dropped" for e in events
    ), "no drift event should fire when the LLM emitted zero rules"


def test_successful_promotion_reports_parity_counts(tmp_path: Path):
    """Happy path: one valid rule + one garbage item. Return value
    reflects the full chain of custody (1 added, 1 normalization drop,
    0 schema rejected, 2 input count). No drift event fires."""
    root = _project(tmp_path)
    td = _task_dir(root)
    analysis = {
        "summary": "test",
        "prevention_rules": [_mk_rule("one clean rule")],
        "derived_rules": ["garbage-string"],
    }
    result = apply_analysis(td, analysis)
    assert result["rules_added"] == 1
    assert result["input_rule_count"] == 2
    assert result["normalization_drops"] == 1
    assert result["rejected_count"] == 0

    events = _read_events(root)
    assert not any(
        e.get("event") == "postmortem_rule_promotion_dropped" for e in events
    )


# ---------------------------------------------------------------------------
# MA-007 — successful promotions emit events, one per rule
# ---------------------------------------------------------------------------


def test_successful_promotion_emits_event_per_rule(tmp_path: Path):
    """MA-007 regression: `apply_analysis` was silent on successful
    promotions. PR-131 added events for drops; parity demands the add
    side be equally visible. One `postmortem_rule_promoted` event per
    promoted rule, carrying category + executor + template +
    enforcement + source_finding so a reviewer can trace each rule's
    individual provenance without parsing a compound event."""
    root = _project(tmp_path)
    td = _task_dir(root)
    analysis = {
        "summary": "test",
        "prevention_rules": [
            _mk_rule("rule alpha", category="sec"),
            _mk_rule("rule bravo", category="cq"),
            _mk_rule("rule charlie", category="perf"),
        ],
    }
    result = apply_analysis(td, analysis)
    assert result["rules_added"] == 3

    events = _read_events(root)
    promoted = [e for e in events if e.get("event") == "postmortem_rule_promoted"]
    assert len(promoted) == 3, f"expected 3 promotion events, got {promoted}"
    categories = sorted(e.get("category") for e in promoted)
    assert categories == ["cq", "perf", "sec"]
    # Every event carries the full provenance schema.
    for ev in promoted:
        assert ev.get("executor") == "all"
        assert ev.get("template") == "advisory"
        # enforcement "advisory" is not in VALID_ENFORCEMENT on main;
        # the normalizer rewrites to "prompt-constraint" as the
        # fallback. The event carries the post-normalization value.
        assert ev.get("enforcement") == "prompt-constraint"
        assert ev.get("source_finding") == "TEST-001"
        assert ev.get("task_id") == "task-20260419-TP"


def test_no_promotion_event_when_rule_already_exists(tmp_path: Path):
    """Dedup-by-existing-text path: a rule whose text already appears in
    prevention-rules.json is NOT appended and therefore must NOT emit a
    postmortem_rule_promoted event (the provenance was recorded on the
    original add, not this one). Verifies `added` does not double-count
    and parity events don't inflate on re-runs."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
    from lib_core import _persistent_project_dir  # noqa: E402

    root = _project(tmp_path)
    td = _task_dir(root)
    # Pre-seed the rules file with an existing entry matching our rule text.
    persistent = _persistent_project_dir(root)
    persistent.mkdir(parents=True, exist_ok=True)
    existing_rules_path = persistent / "prevention-rules.json"
    existing_rules_path.write_text(json.dumps({
        "rules": [{
            "rule": "rule alpha",
            "category": "sec",
            "executor": "all",
            "enforcement": "advisory",
            "source_task": "task-prior",
            "source_finding": "PRIOR-001",
            "added_at": "2026-04-01T00:00:00Z",
            "template": "advisory",
            "params": {},
        }],
    }))

    analysis = {
        "summary": "test",
        "prevention_rules": [
            _mk_rule("rule alpha", category="sec"),  # dup → skip
            _mk_rule("rule bravo", category="cq"),   # new → promote
        ],
    }
    result = apply_analysis(td, analysis)
    assert result["rules_added"] == 1

    events = _read_events(root)
    promoted = [e for e in events if e.get("event") == "postmortem_rule_promoted"]
    assert len(promoted) == 1, (
        f"expected 1 promotion event (only the new rule); got {promoted}"
    )
    assert promoted[0].get("category") == "cq"
