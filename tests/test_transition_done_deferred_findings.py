"""Tests for task-20260419-002 G4.c: ``transition_task`` DONE gate
integration with ``check_deferred_findings``.

Covers acceptance criterion 12 from the task spec:

  - When a deferred finding is TTL-expired AND its ``files``
    intersects this task's changed files, the DONE transition
    refuses with a ValueError whose message contains the literal
    ``deferred findings expired`` AND the expired-finding id(s).
  - When the registry has entries but none are expired + intersecting,
    the DONE transition proceeds.
  - When the registry is missing (cold start), the DONE transition
    proceeds.
  - When entries exist but do not intersect the changed files, the
    DONE transition proceeds regardless of TTL.

Each test builds a full-DONE-ready task directory with every gate
precondition satisfied (audit-routing, retrospective receipt,
rules-check-passed receipt, postmortem-skipped receipt, task
retrospective JSON, audit-reports/*.json). The test's variable is
which deferred-findings registry we write + where ``execution-graph
.json`` points its ``files_expected``.

Changed-files discovery path (under test): the DONE gate walks
``executor-routing`` → ``executor-{seg}`` receipts' ``files_expected``
fields (empty in these fixtures since ``receipt_executor_done``
doesn't store that field) AND falls back to
``execution-graph.json::segments[].files_expected``. We rely on the
execution-graph.json fallback to supply changed_files deterministically.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "hooks"))

from lib_core import transition_task  # noqa: E402
from lib_receipts import (  # noqa: E402
    receipt_audit_routing,
    receipt_postmortem_skipped,
    receipt_retrospective,
    receipt_rules_check_passed,
)


@pytest.fixture(autouse=True)
def _empty_auditor_registry(monkeypatch):
    """PR #127 (task-006) AC 6 added a registry-eligible auditor cross-check.
    These deferred-finding gate tests pre-date the registry check and focus
    on the deferred-finding logic — mock the registry empty to keep the
    cross-check vacuous."""
    import router
    monkeypatch.setattr(router, "_load_auditor_registry", lambda root: {
        "always": [], "fast_track": [], "domain_conditional": {},
    })


def _setup_done_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a task dir whose manifest sits at CHECKPOINT_AUDIT and
    has EVERY receipt + artifact the DONE gate demands. Returns the
    task dir path. After this setup, ``transition_task(td, "DONE")``
    would succeed if the deferred-findings gate is clean.

    Pins DYNOS_HOME so any persistent-dir side effects land in the
    test sandbox.
    """
    monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "dynos-home"))
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260419-DF"
    td.mkdir(parents=True)
    # Manifest at CHECKPOINT_AUDIT — the source stage for -> DONE.
    (td / "manifest.json").write_text(json.dumps({
        "task_id": "task-20260419-DF",
        "stage": "CHECKPOINT_AUDIT",
        "classification": {"risk_level": "medium"},
    }))
    # Retrospective JSON + per-receipt record.
    (td / "task-retrospective.json").write_text(
        json.dumps({"quality_score": 0.95})
    )
    # At least one audit-report file.
    audit_dir = td / "audit-reports"
    audit_dir.mkdir()
    (audit_dir / "report.json").write_text(json.dumps({"findings": []}))
    # receipt_retrospective (proves reward was computed).
    receipt_retrospective(td, 0.95, 0.9, 0.9, 1000)
    # PR #127 (task-006) AC 1: receipt_rules_check_passed self-computes;
    # callers pass only (task_dir, mode). Stub run_checks to a clean pass.
    import rules_engine
    _orig_run_checks = rules_engine.run_checks
    rules_engine.run_checks = lambda root, mode: []
    try:
        receipt_rules_check_passed(td, "all")
    finally:
        rules_engine.run_checks = _orig_run_checks
    # audit-routing with empty auditors → no per-auditor receipts required.
    receipt_audit_routing(td, [])
    # postmortem-skipped (cheap path — reason=no-findings so subsumed_by
    # may be empty).
    receipt_postmortem_skipped(
        td, "no-findings", "d" * 64, subsumed_by=[]
    )
    return td


def _write_execution_graph(td: Path, files_by_segment: dict[str, list[str]]) -> None:
    """Write a minimal execution-graph.json with the given files_expected
    per segment. The transition_task DONE gate uses this as a fallback
    source for the changed-files list."""
    segments = [
        {"id": seg_id, "files_expected": files}
        for seg_id, files in files_by_segment.items()
    ]
    (td / "execution-graph.json").write_text(
        json.dumps({"segments": segments})
    )


def _write_registry(root: Path, entries: list[dict]) -> Path:
    """Write ``.dynos/deferred-findings.json`` verbatim."""
    reg = root / ".dynos" / "deferred-findings.json"
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(json.dumps({"findings": entries}))
    return reg


def _deferred_entry(
    *,
    id: str,
    files: list[str],
    first_seen: int = 0,
    ttl: int = 3,
    task_id: str = "task-20260319-001",
    category: str = "security",
) -> dict:
    return {
        "id": id,
        "category": category,
        "task_id": task_id,
        "files": files,
        "first_seen_at": "2026-03-19T00:00:00Z",
        "first_seen_at_task_count": first_seen,
        "acknowledged_until_task_count": ttl,
    }


# ---------------------------------------------------------------------------
# (a) expired-and-intersecting → refuse
# ---------------------------------------------------------------------------


def test_done_transition_refuses_when_expired_finding_intersects_changed_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Full-DONE-ready task + deferred-findings registry with ONE
    expired-and-intersecting entry. The transition must refuse with
    a ValueError whose message contains the literal ``deferred
    findings expired`` and the id of the offending entry."""
    td = _setup_done_ready(tmp_path, monkeypatch)
    root = td.parent.parent

    # execution-graph.json names hooks/lib_core.py as a changed file
    # via its single segment's files_expected. (This is the fallback
    # path the DONE gate uses since executor-done receipts don't
    # store files_expected directly in this fixture.)
    _write_execution_graph(td, {"seg-1": ["hooks/lib_core.py"]})

    # Deferred finding: TTL=0 from task_count=0 → elapsed >= ttl
    # immediately. `files` intersects the execution-graph entry.
    _write_registry(root, [
        _deferred_entry(
            id="SEC-003",
            files=["hooks/lib_core.py"],
            first_seen=0,
            ttl=0,
        ),
    ])

    with pytest.raises(ValueError) as excinfo:
        transition_task(td, "DONE")
    msg = str(excinfo.value)
    assert "deferred findings expired" in msg, (
        f"expected 'deferred findings expired' in refusal message; "
        f"got: {msg}"
    )
    assert "SEC-003" in msg, (
        f"expected offending finding id 'SEC-003' in refusal message; "
        f"got: {msg}"
    )
    # Stage did NOT advance.
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == "CHECKPOINT_AUDIT"


def test_done_transition_reports_all_expired_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Multiple expired-and-intersecting entries → refusal message
    enumerates ALL of them. A single fail-first implementation would
    miss later entries and hide deferred work."""
    td = _setup_done_ready(tmp_path, monkeypatch)
    root = td.parent.parent
    _write_execution_graph(td, {
        "seg-1": ["hooks/lib_core.py"],
        "seg-2": ["hooks/rules_engine.py"],
    })
    _write_registry(root, [
        _deferred_entry(id="SEC-003", files=["hooks/lib_core.py"],
                        first_seen=0, ttl=0),
        _deferred_entry(id="PERF-002", files=["hooks/rules_engine.py"],
                        first_seen=0, ttl=0),
    ])
    with pytest.raises(ValueError) as excinfo:
        transition_task(td, "DONE")
    msg = str(excinfo.value)
    assert "SEC-003" in msg
    assert "PERF-002" in msg
    assert "deferred findings expired" in msg


# ---------------------------------------------------------------------------
# (b) finding exists but TTL not yet exceeded → proceed
# ---------------------------------------------------------------------------


def test_done_transition_proceeds_when_no_expired_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Registry has an intersecting finding but its TTL is set high
    enough that elapsed < ttl. DONE transition succeeds."""
    td = _setup_done_ready(tmp_path, monkeypatch)
    root = td.parent.parent
    _write_execution_graph(td, {"seg-1": ["hooks/lib_core.py"]})
    _write_registry(root, [
        _deferred_entry(
            id="SEC-003",
            files=["hooks/lib_core.py"],
            first_seen=0,
            ttl=999,  # elapsed=0 << 999 → not expired
        ),
    ])
    transition_task(td, "DONE")
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == "DONE"


# ---------------------------------------------------------------------------
# (c) registry missing → proceed (cold start)
# ---------------------------------------------------------------------------


def test_done_transition_proceeds_when_registry_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Cold start: no ``.dynos/deferred-findings.json`` file. The
    DONE gate must treat this as "no signal" and proceed. Any
    regression that raised on missing file would wedge every
    fresh-project DONE transition."""
    td = _setup_done_ready(tmp_path, monkeypatch)
    # Write an execution-graph to confirm the gate runs its
    # changed-files path without crashing when the registry is absent.
    _write_execution_graph(td, {"seg-1": ["hooks/lib_core.py"]})
    # Deliberately do NOT write .dynos/deferred-findings.json.
    assert not (td.parent.parent / ".dynos" / "deferred-findings.json").exists()

    transition_task(td, "DONE")
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == "DONE"


# ---------------------------------------------------------------------------
# (d) registry has entries but they do not intersect → proceed
# ---------------------------------------------------------------------------


def test_done_transition_proceeds_when_intersection_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Registry has an entry that is past TTL, but the entry's
    ``files`` do NOT overlap this task's changed files. No
    intersection = nothing to report = transition proceeds."""
    td = _setup_done_ready(tmp_path, monkeypatch)
    root = td.parent.parent
    _write_execution_graph(td, {"seg-1": ["hooks/lib_core.py"]})
    _write_registry(root, [
        _deferred_entry(
            id="SEC-003",
            files=["unrelated/path/nowhere.py"],
            first_seen=0,
            ttl=0,  # TTL-expired but no file overlap
        ),
    ])
    transition_task(td, "DONE")
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == "DONE"


# ---------------------------------------------------------------------------
# Force bypass — deferred gate MUST be bypassable via --force
# ---------------------------------------------------------------------------


def test_done_transition_force_bypasses_deferred_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """``force=True`` bypasses every gate including the new deferred-
    findings one. An expired+intersecting entry is present, and the
    transition still succeeds because force is the break-glass door.
    Without this contract, an operator couldn't escape a wedged DONE
    gate caused by a corrupt registry or stuck category."""
    td = _setup_done_ready(tmp_path, monkeypatch)
    root = td.parent.parent
    _write_execution_graph(td, {"seg-1": ["hooks/lib_core.py"]})
    _write_registry(root, [
        _deferred_entry(id="SEC-003", files=["hooks/lib_core.py"],
                        first_seen=0, ttl=0),
    ])
    transition_task(td, "DONE", force=True)
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == "DONE"
