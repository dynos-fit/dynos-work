"""Tests for `_extract_quads` cross-check against events.jsonl
(CRITERIA 3, 4, Fix E).

Covers:
  - learned:X claim preserved when a matching `learned_agent_applied`
    event exists for the retrospective's task_id.
  - learned:X claim rewritten to "generic" when no matching event
    exists, and an `agent_source_reclassified` event is emitted.
  - Auditor-role prefix stripping (`audit-security` matches segment
    id `security`).
  - events_by_task=None triggers the legacy no-cross-check path.
  - source="generic" is a no-op (no cross-check).
  - compute_effectiveness_scores wires events_by_task through when
    given a root path.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "memory"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from policy_engine import (  # noqa: E402
    _extract_quads,
    compute_effectiveness_scores,
)


def _retro(
    task_id: str,
    *,
    role: str = "backend-executor",
    model: str = "haiku",
    source: str = "learned:foo",
    task_type: str = "feature",
    quality: float = 0.8,
    cost: float = 0.7,
    efficiency: float = 0.9,
) -> dict:
    """Build a minimal retrospective that `_extract_quads` will accept."""
    return {
        "task_id": task_id,
        "task_type": task_type,
        "quality_score": quality,
        "cost_score": cost,
        "efficiency_score": efficiency,
        "model_used_by_agent": {role: model},
        "agent_source": {role: source},
    }


def _events_path(root: Path) -> Path:
    return root / ".dynos" / "events.jsonl"


def _read_events(root: Path) -> list[dict]:
    path = _events_path(root)
    if not path.exists():
        return []
    lines = path.read_text().splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _project_root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / ".dynos").mkdir(parents=True)
    return root


def test_preserves_learned_when_event_matches(tmp_path: Path):
    """Matching event for (task_id, agent_name, segment_id) → source kept."""
    retro = _retro("task-001", role="backend-executor", source="learned:foo")
    events_by_task = {
        "task-001": [
            {
                "event": "learned_agent_applied",
                "agent_name": "foo",
                "segment_id": "backend-executor",
            }
        ]
    }
    root = _project_root(tmp_path)

    quads = _extract_quads([retro], events_by_task=events_by_task, root=root)
    assert len(quads) == 1
    key, q, c, e = quads[0]
    role, model, task_type, source = key
    assert role == "backend-executor"
    assert source == "learned:foo"
    # No reclassification event should have been emitted.
    events = _read_events(root)
    assert not any(ev.get("event") == "agent_source_reclassified" for ev in events)


def test_downgrades_when_event_missing(tmp_path: Path):
    """No matching event → source rewritten to 'generic' and exactly one
    agent_source_reclassified event emitted with the original source."""
    retro = _retro("task-002", role="backend-executor", source="learned:foo")
    events_by_task: dict[str, list[dict]] = {"task-002": []}
    root = _project_root(tmp_path)

    quads = _extract_quads([retro], events_by_task=events_by_task, root=root)
    assert len(quads) == 1
    (role, model, task_type, source), *_ = quads[0:1]
    # The returned quad's source must be "generic".
    key = quads[0][0]
    assert key[3] == "generic"

    events = _read_events(root)
    reclass = [ev for ev in events if ev.get("event") == "agent_source_reclassified"]
    assert len(reclass) == 1
    entry = reclass[0]
    assert entry["reclassified_to"] == "generic"
    assert entry["original_source"] == "learned:foo"
    assert entry.get("task_id") == "task-002"
    assert entry.get("role") == "backend-executor"


def test_auditor_role_prefix_match(tmp_path: Path):
    """Role 'audit-security' with event segment_id='security' must match
    via the audit-prefix strip and preserve learned:X."""
    retro = _retro(
        "task-003",
        role="audit-security",
        source="learned:sec-hardener",
    )
    events_by_task = {
        "task-003": [
            {
                "event": "learned_agent_applied",
                "agent_name": "sec-hardener",
                "segment_id": "security",
            }
        ]
    }
    root = _project_root(tmp_path)

    quads = _extract_quads([retro], events_by_task=events_by_task, root=root)
    assert len(quads) == 1
    assert quads[0][0][3] == "learned:sec-hardener"
    # No reclassification.
    assert not any(
        ev.get("event") == "agent_source_reclassified" for ev in _read_events(root)
    )


def test_events_by_task_none_skips_check(tmp_path: Path):
    """events_by_task=None → cross-check disabled, learned:foo kept, no events."""
    retro = _retro("task-004", role="backend-executor", source="learned:foo")
    root = _project_root(tmp_path)

    quads = _extract_quads([retro], events_by_task=None, root=root)
    assert len(quads) == 1
    assert quads[0][0][3] == "learned:foo"
    # No events file write should have occurred.
    assert not _events_path(root).exists() or not any(
        ev.get("event") == "agent_source_reclassified" for ev in _read_events(root)
    )


def test_source_generic_no_cross_check(tmp_path: Path):
    """source='generic' bypasses cross-check — even with empty events."""
    retro = _retro("task-005", role="backend-executor", source="generic")
    events_by_task: dict[str, list[dict]] = {"task-005": []}
    root = _project_root(tmp_path)

    quads = _extract_quads([retro], events_by_task=events_by_task, root=root)
    assert len(quads) == 1
    assert quads[0][0][3] == "generic"
    # No reclassification event — generic sources are never reclassified.
    assert not any(
        ev.get("event") == "agent_source_reclassified" for ev in _read_events(root)
    )


def test_mismatched_agent_name_triggers_downgrade(tmp_path: Path):
    """Event exists but agent_name differs → downgrade fires."""
    retro = _retro("task-006", role="backend-executor", source="learned:foo")
    events_by_task = {
        "task-006": [
            {
                "event": "learned_agent_applied",
                "agent_name": "bar",  # wrong
                "segment_id": "backend-executor",
            }
        ]
    }
    root = _project_root(tmp_path)

    quads = _extract_quads([retro], events_by_task=events_by_task, root=root)
    assert quads[0][0][3] == "generic"
    reclass = [
        ev for ev in _read_events(root) if ev.get("event") == "agent_source_reclassified"
    ]
    assert len(reclass) == 1


def test_multiple_retros_handled_independently(tmp_path: Path):
    """Two retrospectives in a single _extract_quads call: the one whose
    event matches keeps its learned source; the one without a match gets
    reclassified to 'generic' and exactly one reclassification event fires.

    Covers spec criterion 3 sub-case (f) — cross-check state does not bleed
    between retros.
    """
    retro_matched = _retro(
        "task-m01",
        role="backend-executor",
        model="sonnet",
        source="learned:foo",
    )
    retro_unmatched = _retro(
        "task-u02",
        role="backend-executor",
        model="sonnet",
        source="learned:bar",
    )
    events_by_task = {
        "task-m01": [
            {
                "event": "learned_agent_applied",
                "agent_name": "foo",
                "segment_id": "backend-executor",
            }
        ],
        "task-u02": [],
    }
    root = _project_root(tmp_path)

    quads = _extract_quads(
        [retro_matched, retro_unmatched],
        events_by_task=events_by_task,
        root=root,
    )
    assert len(quads) == 2
    by_task = {r.get("task_id"): r for r in [retro_matched, retro_unmatched]}
    # Quads are sorted by task_id ascending — but we key by source to be safe.
    sources = sorted(q[0][3] for q in quads)
    assert sources == ["generic", "learned:foo"], (
        f"expected one preserved + one reclassified; got {sources}"
    )
    # Exactly one reclassification event — NOT two.
    reclass = [
        ev
        for ev in _read_events(root)
        if ev.get("event") == "agent_source_reclassified"
    ]
    assert len(reclass) == 1, f"expected 1 reclass event; got {reclass}"
    assert reclass[0].get("task_id") == "task-u02"
    assert reclass[0].get("original_source") == "learned:bar"


def test_compute_scores_wires_events_through(tmp_path: Path):
    """compute_effectiveness_scores with a root that has no matching
    learned_agent_applied events must produce a `generic` quad (no
    `learned:foo` row) for the claimed (role, model, task_type)."""
    root = _project_root(tmp_path)
    # Write events.jsonl with ONLY unrelated events — no match for our claim.
    events_path = _events_path(root)
    events_path.write_text(json.dumps({
        "task_id": "task-007",
        "event": "router_model_decision",
        "role": "backend-executor",
        "model": "haiku",
    }) + "\n")

    retro = _retro(
        "task-007",
        role="backend-executor",
        model="haiku",
        source="learned:foo",
        task_type="feature",
    )
    scores = compute_effectiveness_scores([retro], root=root)
    # No score row should report source=="learned:foo" for this quad key.
    matching_learned = [
        r
        for r in scores
        if r["role"] == "backend-executor"
        and r["model"] == "haiku"
        and r["task_type"] == "feature"
        and r["source"] == "learned:foo"
    ]
    assert matching_learned == [], (
        f"expected no learned:foo row; got {matching_learned}"
    )
    matching_generic = [
        r
        for r in scores
        if r["role"] == "backend-executor"
        and r["model"] == "haiku"
        and r["task_type"] == "feature"
        and r["source"] == "generic"
    ]
    assert matching_generic, (
        f"expected a generic row for the reclassified quad; got scores={scores}"
    )
