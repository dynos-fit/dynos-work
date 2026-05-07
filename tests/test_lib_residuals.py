"""Unit tests for hooks/lib_residuals.py — task-20260504-007.

Covers AC-1 (public API contracts), AC-2 (row shape), AC-11 (filter),
AC-12 (dedup), AC-13 (concurrent dedup), AC-14 (round-trip status update),
AC-17 (select_next_pending selection logic).

Uses pytest.importorskip so this file collects cleanly even before
lib_residuals.py exists.  Once the module lands, every test runs.
"""

from __future__ import annotations

import json
import sys
import threading
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

lib_residuals = pytest.importorskip("lib_residuals")

queue_path = lib_residuals.queue_path
load_queue = lib_residuals.load_queue
ingest_findings = lib_residuals.ingest_findings
select_next_pending = lib_residuals.select_next_pending
update_row_status = lib_residuals.update_row_status
extract_residual_id = lib_residuals.extract_residual_id
compute_fingerprint = lib_residuals.compute_fingerprint


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ALLOWED_ROW_KEYS = frozenset({
    "id",
    "kind",
    "fingerprint",
    "created_at",
    "source_task_id",
    "source_auditor",
    "title",
    "description",
    "location",
    "status",
    "attempts",
    "last_attempt_at",
})


def _make_root(tmp_path: Path) -> Path:
    """Create a minimal project root with a .dynos directory.
    Idempotent so a single test can call this multiple times against
    distinct sub-tmp_paths without colliding."""
    root = tmp_path / "project"
    (root / ".dynos").mkdir(parents=True, exist_ok=True)
    return root


def _make_finding(
    *,
    id: str = "F-001",
    description: str = "Some finding description",
    location: str = "hooks/lib_core.py:10",
    severity: str = "warning",
    category: str = "dead-code",
    blocking: bool = False,
) -> dict:
    return {
        "id": id,
        "description": description,
        "location": location,
        "severity": severity,
        "category": category,
        "blocking": blocking,
    }


def _make_summary(task_dir: Path, reports: list[dict], task_id: str = "task-test-001") -> dict:
    """Return a minimal audit-summary dict referencing per-auditor report files."""
    return {
        "task_id": task_id,
        "reports": reports,
    }


def _write_auditor_report(task_dir: Path, auditor_name: str, findings: list[dict]) -> Path:
    """Write a per-auditor report JSON file into task_dir/audit-reports/."""
    report_dir = task_dir / "audit-reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{auditor_name}.json"
    report_path.write_text(json.dumps({"findings": findings}))
    return report_path


def _pre_populate_queue(root: Path, rows: list[dict]) -> None:
    """Write rows directly into the queue file (bypasses locking for test setup)."""
    qp = queue_path(root)
    qp.parent.mkdir(parents=True, exist_ok=True)
    qp.write_text(json.dumps({"findings": rows}))


def _make_row(
    *,
    row_id: str | None = None,
    status: str = "pending",
    attempts: int = 0,
    created_at: str = "2026-05-04T10:00:00Z",
    source_auditor: str = "claude-md-auditor",
    fingerprint: str | None = None,
) -> dict:
    rid = row_id or str(uuid.uuid4())
    fp = fingerprint or compute_fingerprint(source_auditor, rid, "description text")
    return {
        "id": rid,
        "kind": "residual",
        "fingerprint": fp,
        "created_at": created_at,
        "source_task_id": "task-test-001",
        "source_auditor": source_auditor,
        "title": "description text",
        "description": "description text",
        "location": "some/file.py:1",
        "status": status,
        "attempts": attempts,
        "last_attempt_at": None,
    }


# ---------------------------------------------------------------------------
# AC-1: load_queue, extract_residual_id, compute_fingerprint, update_row_status
# ---------------------------------------------------------------------------


def test_load_queue_missing_file(tmp_path: Path):
    """load_queue on an absent path returns {"findings":[]} without creating the file."""
    root = _make_root(tmp_path)
    qp = queue_path(root)
    assert not qp.exists(), "precondition: queue file must not exist"

    result = load_queue(qp)

    assert result == {"findings": []}
    assert not qp.exists(), "load_queue must not create the file when it is absent"


def test_extract_residual_id_found():
    """extract_residual_id parses <!-- residual-id: abc-123 --> → 'abc-123'."""
    text = "<!-- residual-id: abc-123 -->\n\nSome task body."
    result = extract_residual_id(text)
    assert result == "abc-123"


def test_extract_residual_id_absent():
    """extract_residual_id returns None when no comment is present."""
    text = "This text has no residual ID comment."
    result = extract_residual_id(text)
    assert result is None


def test_extract_residual_id_midline_no_match():
    """Bare 'residual-id:' substring without HTML comment delimiters does NOT match.

    Mitigates spec.md Risk Note 4: the regex must require the full
    '<!-- residual-id: ' prefix and ' -->' suffix.
    """
    text = "The residual-id: discussion mentions some residual-id: reference here."
    result = extract_residual_id(text)
    assert result is None, (
        f"expected None for bare residual-id: substring, got {result!r}"
    )


def test_extract_residual_id_midline_comment_like():
    """A partial comment-like string that lacks the closing --> does not match."""
    text = "some text <!-- residual-id: test-id-007 and more stuff without closing"
    result = extract_residual_id(text)
    assert result is None


def test_extract_residual_id_first_occurrence_wins():
    """When multiple residual-id comments are present, the first is returned."""
    text = "<!-- residual-id: first-id -->\n<!-- residual-id: second-id -->"
    result = extract_residual_id(text)
    assert result == "first-id"


def test_compute_fingerprint_deterministic():
    """Same inputs produce the same lowercase hex SHA-256 on both calls."""
    fp1 = compute_fingerprint("claude-md-auditor", "F-001", "Some description")
    fp2 = compute_fingerprint("claude-md-auditor", "F-001", "Some description")
    assert fp1 == fp2
    assert len(fp1) == 64
    assert fp1 == fp1.lower()


def test_compute_fingerprint_different_inputs_differ():
    """Different inputs produce different fingerprints."""
    fp1 = compute_fingerprint("claude-md-auditor", "F-001", "description A")
    fp2 = compute_fingerprint("claude-md-auditor", "F-001", "description B")
    assert fp1 != fp2


def test_update_row_status_invalid_status_raises(tmp_path: Path):
    """update_row_status raises ValueError for a status not in the allowed set."""
    root = _make_root(tmp_path)
    row = _make_row(row_id="row-001", status="in_progress")
    _pre_populate_queue(root, [row])

    with pytest.raises(ValueError):
        update_row_status(root, "row-001", "bogus")


def test_update_row_status_missing_id_raises(tmp_path: Path):
    """update_row_status raises ValueError when row_id is not in the queue."""
    root = _make_root(tmp_path)
    row = _make_row(row_id="row-001", status="in_progress")
    _pre_populate_queue(root, [row])

    with pytest.raises(ValueError):
        update_row_status(root, "nonexistent-id", "done")


# ---------------------------------------------------------------------------
# AC-2: Row shape — exactly 12 fields, no others
# ---------------------------------------------------------------------------


def test_row_shape_exact(tmp_path: Path):
    """Accepted row contains exactly the 12 fields in AC-2 and no others."""
    root = _make_root(tmp_path)
    task_dir = tmp_path / "task-dir"
    task_dir.mkdir()

    finding = _make_finding(
        id="F-001",
        description="A proper warning finding",
        severity="warning",
        category="dead-code",
        blocking=False,
    )
    report_path = _write_auditor_report(task_dir, "claude-md-auditor", [finding])
    summary = _make_summary(task_dir, [
        {"auditor_name": "claude-md-auditor", "report_path": str(report_path)}
    ])

    count = ingest_findings(task_dir, root, summary)
    assert count == 1

    q = load_queue(queue_path(root))
    assert len(q["findings"]) == 1
    row = q["findings"][0]

    extra = set(row.keys()) - ALLOWED_ROW_KEYS
    missing = ALLOWED_ROW_KEYS - set(row.keys())
    assert not extra, f"row has unexpected fields: {extra}"
    assert not missing, f"row is missing required fields: {missing}"

    # Spot-check field values
    assert row["kind"] == "residual"
    assert row["status"] == "pending"
    assert row["attempts"] == 0
    assert row["last_attempt_at"] is None
    assert row["source_auditor"] == "claude-md-auditor"
    assert row["source_task_id"] == "task-test-001"
    assert row["description"] == "A proper warning finding"
    assert row["title"] == "A proper warning finding"[:120]
    assert row["location"] == "hooks/lib_core.py:10"
    assert isinstance(row["id"], str) and len(row["id"]) > 0
    assert isinstance(row["fingerprint"], str) and len(row["fingerprint"]) == 64
    assert row["created_at"].endswith("Z")


def test_row_title_truncated_to_120_chars(tmp_path: Path):
    """Title field is truncated to 120 characters when description is longer."""
    root = _make_root(tmp_path)
    task_dir = tmp_path / "task-dir"
    task_dir.mkdir()

    long_description = "X" * 200
    finding = _make_finding(description=long_description, severity="warning", blocking=False)
    report_path = _write_auditor_report(task_dir, "claude-md-auditor", [finding])
    summary = _make_summary(task_dir, [
        {"auditor_name": "claude-md-auditor", "report_path": str(report_path)}
    ])

    ingest_findings(task_dir, root, summary)
    q = load_queue(queue_path(root))
    row = q["findings"][0]

    assert len(row["title"]) == 120
    assert row["description"] == long_description


# ---------------------------------------------------------------------------
# AC-11: Producer filter — correct findings accepted/rejected
# ---------------------------------------------------------------------------


def test_filter_accepts_correct_findings(tmp_path: Path):
    """ingest_findings with a fixture including all four rejection cases plus
    two accepted findings produces exactly two queue rows (one per allowed auditor).
    """
    root = _make_root(tmp_path)
    task_dir = tmp_path / "task-dir"
    task_dir.mkdir()

    # Findings for claude-md-auditor: one accepted, four rejected
    claude_findings = [
        _make_finding(id="F-accept-1", severity="warning", category="dead-code", blocking=False),
        _make_finding(id="F-blocking", severity="warning", category="dead-code", blocking=True),
        _make_finding(id="F-info", severity="info", category="dead-code", blocking=False),
        _make_finding(id="F-bad-category", severity="warning", category="no-rules-found", blocking=False),
    ]
    # Findings for dead-code-auditor: one accepted
    dead_code_findings = [
        _make_finding(id="F-accept-2", severity="error", category="unused-import", blocking=False),
    ]
    # Findings for disallowed auditor: all rejected. v7.4.0 broadened the
    # allowlist (now includes security/performance/code-quality), so use a
    # name NOT in the v7.4.0 set.
    disallowed_findings = [
        _make_finding(id="F-disallowed", severity="warning", category="xss", blocking=False),
    ]

    claude_path = _write_auditor_report(task_dir, "claude-md-auditor", claude_findings)
    dead_path = _write_auditor_report(task_dir, "dead-code-auditor", dead_code_findings)
    disallowed_path = _write_auditor_report(task_dir, "ml-executor-auditor", disallowed_findings)

    summary = _make_summary(task_dir, [
        {"auditor_name": "claude-md-auditor", "report_path": str(claude_path)},
        {"auditor_name": "dead-code-auditor", "report_path": str(dead_path)},
        {"auditor_name": "ml-executor-auditor", "report_path": str(disallowed_path)},
    ])

    count = ingest_findings(task_dir, root, summary)
    assert count == 2

    q = load_queue(queue_path(root))
    assert len(q["findings"]) == 2
    auditors = {row["source_auditor"] for row in q["findings"]}
    assert auditors == {"claude-md-auditor", "dead-code-auditor"}


def test_filter_rejects_blocking_true(tmp_path: Path):
    """blocking=True finding is not appended."""
    root = _make_root(tmp_path)
    task_dir = tmp_path / "task-dir"
    task_dir.mkdir()

    finding = _make_finding(severity="warning", category="dead-code", blocking=True)
    report_path = _write_auditor_report(task_dir, "claude-md-auditor", [finding])
    summary = _make_summary(task_dir, [
        {"auditor_name": "claude-md-auditor", "report_path": str(report_path)}
    ])

    count = ingest_findings(task_dir, root, summary)
    assert count == 0
    q = load_queue(queue_path(root))
    assert q["findings"] == []


def test_filter_rejects_severity_info(tmp_path: Path):
    """severity='info' finding is not appended."""
    root = _make_root(tmp_path)
    task_dir = tmp_path / "task-dir"
    task_dir.mkdir()

    finding = _make_finding(severity="info", category="dead-code", blocking=False)
    report_path = _write_auditor_report(task_dir, "claude-md-auditor", [finding])
    summary = _make_summary(task_dir, [
        {"auditor_name": "claude-md-auditor", "report_path": str(report_path)}
    ])

    count = ingest_findings(task_dir, root, summary)
    assert count == 0
    q = load_queue(queue_path(root))
    assert q["findings"] == []


def test_filter_rejects_bad_category(tmp_path: Path):
    """category='no-rules-found' finding is not appended."""
    root = _make_root(tmp_path)
    task_dir = tmp_path / "task-dir"
    task_dir.mkdir()

    for bad_cat in ("no-rules-found", "tool-error", "no-files-changed"):
        task_dir2 = tmp_path / f"task-dir-{bad_cat}"
        task_dir2.mkdir()
        root2 = _make_root(tmp_path / f"root-{bad_cat}")

        finding = _make_finding(severity="warning", category=bad_cat, blocking=False)
        report_path = _write_auditor_report(task_dir2, "claude-md-auditor", [finding])
        summary = _make_summary(task_dir2, [
            {"auditor_name": "claude-md-auditor", "report_path": str(report_path)}
        ])

        count = ingest_findings(task_dir2, root2, summary)
        assert count == 0, f"expected category={bad_cat!r} to be rejected"


def test_filter_rejects_disallowed_auditor(tmp_path: Path):
    """An auditor_name that is NOT in the v7.4.0 allowlist is not appended."""
    root = _make_root(tmp_path)
    task_dir = tmp_path / "task-dir"
    task_dir.mkdir()

    finding = _make_finding(severity="warning", category="xss", blocking=False)
    # v7.4.0 allowlist: claude-md, dead-code, security, performance, code-quality.
    # Use a name not in that set.
    report_path = _write_auditor_report(task_dir, "ml-executor-auditor", [finding])
    summary = _make_summary(task_dir, [
        {"auditor_name": "ml-executor-auditor", "report_path": str(report_path)}
    ])

    count = ingest_findings(task_dir, root, summary)
    assert count == 0
    q = load_queue(queue_path(root))
    assert q["findings"] == []


def test_filter_blocking_must_be_boolean_false(tmp_path: Path):
    """blocking=0 (falsy int, not False boolean) is rejected per AC-3."""
    root = _make_root(tmp_path)
    task_dir = tmp_path / "task-dir"
    task_dir.mkdir()

    # blocking=0 is falsy but not boolean False — should be rejected
    finding = {
        "id": "F-001",
        "description": "A finding",
        "location": "file.py:1",
        "severity": "warning",
        "category": "dead-code",
        "blocking": 0,  # falsy int, not boolean False
    }
    report_path = _write_auditor_report(task_dir, "claude-md-auditor", [finding])
    summary = _make_summary(task_dir, [
        {"auditor_name": "claude-md-auditor", "report_path": str(report_path)}
    ])

    count = ingest_findings(task_dir, root, summary)
    assert count == 0, "blocking=0 (int) must not be admitted; only blocking==False (bool)"


# ---------------------------------------------------------------------------
# AC-12: Dedup — same fixture twice → exactly one row
# ---------------------------------------------------------------------------


def test_dedup_same_fixture_twice(tmp_path: Path):
    """Calling ingest_findings twice with the same fixture → 1 row, second call returns 0."""
    root = _make_root(tmp_path)
    task_dir = tmp_path / "task-dir"
    task_dir.mkdir()

    finding = _make_finding(id="F-001", severity="warning", category="dead-code", blocking=False)
    report_path = _write_auditor_report(task_dir, "claude-md-auditor", [finding])
    summary = _make_summary(task_dir, [
        {"auditor_name": "claude-md-auditor", "report_path": str(report_path)}
    ])

    count1 = ingest_findings(task_dir, root, summary)
    assert count1 == 1

    count2 = ingest_findings(task_dir, root, summary)
    assert count2 == 0, "second call with same fingerprint must return 0 (dedup)"

    q = load_queue(queue_path(root))
    assert len(q["findings"]) == 1, "queue must contain exactly 1 row after two calls with same fixture"


def test_dedup_allows_done_resurfaced(tmp_path: Path):
    """A fingerprint matching a 'done' row is appended as a new row (resurfaced)."""
    root = _make_root(tmp_path)
    task_dir = tmp_path / "task-dir"
    task_dir.mkdir()

    finding = _make_finding(id="F-001", description="Resurfaced finding", severity="warning",
                            category="dead-code", blocking=False)
    fp = compute_fingerprint("claude-md-auditor", "F-001", "Resurfaced finding")

    # Pre-populate queue with a "done" row having the same fingerprint
    done_row = _make_row(fingerprint=fp, status="done")
    _pre_populate_queue(root, [done_row])

    report_path = _write_auditor_report(task_dir, "claude-md-auditor", [finding])
    summary = _make_summary(task_dir, [
        {"auditor_name": "claude-md-auditor", "report_path": str(report_path)}
    ])

    count = ingest_findings(task_dir, root, summary)
    assert count == 1, "finding with fingerprint matching a 'done' row must be appended"

    q = load_queue(queue_path(root))
    assert len(q["findings"]) == 2


def test_dedup_blocks_pending_duplicate(tmp_path: Path):
    """A fingerprint matching an existing 'pending' row is silently dropped."""
    root = _make_root(tmp_path)
    task_dir = tmp_path / "task-dir"
    task_dir.mkdir()

    finding = _make_finding(id="F-001", description="Existing finding", severity="warning",
                            category="dead-code", blocking=False)
    fp = compute_fingerprint("claude-md-auditor", "F-001", "Existing finding")
    pending_row = _make_row(fingerprint=fp, status="pending")
    _pre_populate_queue(root, [pending_row])

    report_path = _write_auditor_report(task_dir, "claude-md-auditor", [finding])
    summary = _make_summary(task_dir, [
        {"auditor_name": "claude-md-auditor", "report_path": str(report_path)}
    ])

    count = ingest_findings(task_dir, root, summary)
    assert count == 0

    q = load_queue(queue_path(root))
    assert len(q["findings"]) == 1  # still only the pre-existing row


def test_dedup_blocks_in_progress_duplicate(tmp_path: Path):
    """A fingerprint matching an existing 'in_progress' row is silently dropped."""
    root = _make_root(tmp_path)
    task_dir = tmp_path / "task-dir"
    task_dir.mkdir()

    finding = _make_finding(id="F-001", description="In-flight finding", severity="warning",
                            category="dead-code", blocking=False)
    fp = compute_fingerprint("claude-md-auditor", "F-001", "In-flight finding")
    in_progress_row = _make_row(fingerprint=fp, status="in_progress")
    _pre_populate_queue(root, [in_progress_row])

    report_path = _write_auditor_report(task_dir, "claude-md-auditor", [finding])
    summary = _make_summary(task_dir, [
        {"auditor_name": "claude-md-auditor", "report_path": str(report_path)}
    ])

    count = ingest_findings(task_dir, root, summary)
    assert count == 0

    q = load_queue(queue_path(root))
    assert len(q["findings"]) == 1


# ---------------------------------------------------------------------------
# AC-13: Concurrent dedup — two threads, same fingerprint → exactly one row
# ---------------------------------------------------------------------------


def test_concurrent_dedup(tmp_path: Path):
    """Two threads with the same fingerprint produce exactly 1 row after both complete.

    The lock must be acquired on the queue file FD (not on a temp file) or
    this test will fail intermittently.  Run at least once; a flaky pass is
    a symptom of incorrect locking.
    """
    root = _make_root(tmp_path)
    task_dir = tmp_path / "task-dir"
    task_dir.mkdir()

    finding = _make_finding(id="F-concurrent", description="Concurrent finding",
                            severity="warning", category="dead-code", blocking=False)
    report_path = _write_auditor_report(task_dir, "claude-md-auditor", [finding])
    summary = _make_summary(task_dir, [
        {"auditor_name": "claude-md-auditor", "report_path": str(report_path)}
    ])

    errors: list[Exception] = []

    def _call():
        try:
            ingest_findings(task_dir, root, summary)
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=_call)
    t2 = threading.Thread(target=_call)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors, f"ingest_findings raised in a thread: {errors}"

    q = load_queue(queue_path(root))
    assert len(q["findings"]) == 1, (
        f"expected exactly 1 row after concurrent dedup; got {len(q['findings'])}"
    )


# ---------------------------------------------------------------------------
# AC-14: Round-trip status update via update_row_status
# ---------------------------------------------------------------------------


def test_update_row_status_done(tmp_path: Path):
    """Insert in_progress row; call update_row_status(done); reload → status=='done', attempts unchanged."""
    root = _make_root(tmp_path)
    row = _make_row(row_id="row-done-test", status="in_progress", attempts=1)
    _pre_populate_queue(root, [row])

    update_row_status(root, "row-done-test", "done")

    q = load_queue(queue_path(root))
    updated = next(r for r in q["findings"] if r["id"] == "row-done-test")
    assert updated["status"] == "done"
    assert updated["attempts"] == 1, "update_row_status must not change attempts"


def test_update_row_status_pending(tmp_path: Path):
    """Insert in_progress,attempts=2; call update_row_status(pending) → status=='pending', attempts==2."""
    root = _make_root(tmp_path)
    row = _make_row(row_id="row-pending-test", status="in_progress", attempts=2)
    _pre_populate_queue(root, [row])

    update_row_status(root, "row-pending-test", "pending")

    q = load_queue(queue_path(root))
    updated = next(r for r in q["findings"] if r["id"] == "row-pending-test")
    assert updated["status"] == "pending"
    assert updated["attempts"] == 2, "update_row_status must not change attempts"


def test_update_row_status_failed(tmp_path: Path):
    """Insert in_progress,attempts=3; call update_row_status(failed) → status=='failed'."""
    root = _make_root(tmp_path)
    row = _make_row(row_id="row-failed-test", status="in_progress", attempts=3)
    _pre_populate_queue(root, [row])

    update_row_status(root, "row-failed-test", "failed")

    q = load_queue(queue_path(root))
    updated = next(r for r in q["findings"] if r["id"] == "row-failed-test")
    assert updated["status"] == "failed"
    assert updated["attempts"] == 3, "update_row_status must not change attempts"


def test_update_row_status_all_valid_transitions(tmp_path: Path):
    """All four valid status values are accepted without raising."""
    for valid_status in ("pending", "in_progress", "done", "failed"):
        root = _make_root(tmp_path / valid_status)
        row = _make_row(row_id="row-001", status="pending")
        _pre_populate_queue(root, [row])
        update_row_status(root, "row-001", valid_status)  # must not raise
        q = load_queue(queue_path(root))
        updated = q["findings"][0]
        assert updated["status"] == valid_status


# ---------------------------------------------------------------------------
# AC-17: select_next_pending selection logic
# ---------------------------------------------------------------------------


def test_select_next_pending_returns_pending(tmp_path: Path):
    """Queue with one pending (attempts=0) and one in_progress: returns the pending row."""
    root = _make_root(tmp_path)
    pending_row = _make_row(row_id="pending-001", status="pending", attempts=0,
                            created_at="2026-05-04T10:00:00Z")
    in_progress_row = _make_row(row_id="inprog-001", status="in_progress", attempts=1,
                                created_at="2026-05-04T09:00:00Z")
    _pre_populate_queue(root, [in_progress_row, pending_row])

    result = select_next_pending(root)
    assert result is not None
    assert result["id"] == "pending-001"
    assert result["status"] == "pending"


def test_select_next_pending_skips_in_progress(tmp_path: Path):
    """Queue with only an in_progress row: select_next_pending returns None."""
    root = _make_root(tmp_path)
    in_progress_row = _make_row(row_id="inprog-001", status="in_progress", attempts=1)
    _pre_populate_queue(root, [in_progress_row])

    result = select_next_pending(root)
    assert result is None


def test_select_next_pending_skips_attempts_3(tmp_path: Path):
    """Queue with pending,attempts=3: select_next_pending returns None."""
    root = _make_root(tmp_path)
    exhausted_row = _make_row(row_id="exhausted-001", status="pending", attempts=3)
    _pre_populate_queue(root, [exhausted_row])

    result = select_next_pending(root)
    assert result is None


def test_select_next_pending_oldest_first(tmp_path: Path):
    """Two pending rows with different created_at: returns the earlier one."""
    root = _make_root(tmp_path)
    older_row = _make_row(row_id="older-001", status="pending", attempts=0,
                          created_at="2026-05-04T08:00:00Z")
    newer_row = _make_row(row_id="newer-001", status="pending", attempts=0,
                          created_at="2026-05-04T12:00:00Z")
    # Insert in reverse order to make sure sorting is applied, not insertion order
    _pre_populate_queue(root, [newer_row, older_row])

    result = select_next_pending(root)
    assert result is not None
    assert result["id"] == "older-001"


def test_select_next_pending_missing_queue(tmp_path: Path):
    """When the queue file is absent, select_next_pending returns None without raising."""
    root = _make_root(tmp_path)
    # No queue file written
    result = select_next_pending(root)
    assert result is None


def test_select_next_pending_attempts_2_eligible(tmp_path: Path):
    """A pending row with attempts=2 (< 3) is still eligible for selection."""
    root = _make_root(tmp_path)
    row = _make_row(row_id="row-attempts-2", status="pending", attempts=2)
    _pre_populate_queue(root, [row])

    result = select_next_pending(root)
    assert result is not None
    assert result["id"] == "row-attempts-2"


def test_select_next_pending_skips_done_and_failed(tmp_path: Path):
    """done and failed rows are not returned by select_next_pending."""
    root = _make_root(tmp_path)
    done_row = _make_row(row_id="done-001", status="done")
    failed_row = _make_row(row_id="failed-001", status="failed")
    _pre_populate_queue(root, [done_row, failed_row])

    result = select_next_pending(root)
    assert result is None


# ---------------------------------------------------------------------------
# AC-1: queue_path resolution
# ---------------------------------------------------------------------------


def test_queue_path_resolves_correctly(tmp_path: Path):
    """queue_path(root) returns root/.dynos/proactive-findings.json."""
    root = tmp_path / "project"
    result = queue_path(root)
    assert result == root / ".dynos" / "proactive-findings.json"


def test_ingest_findings_missing_task_id_uses_empty_string(tmp_path: Path):
    """If summary lacks 'task_id', ingest_findings uses '' for source_task_id."""
    root = _make_root(tmp_path)
    task_dir = tmp_path / "task-dir"
    task_dir.mkdir()

    finding = _make_finding(severity="warning", category="dead-code", blocking=False)
    report_path = _write_auditor_report(task_dir, "claude-md-auditor", [finding])
    summary = {
        # no task_id key
        "reports": [{"auditor_name": "claude-md-auditor", "report_path": str(report_path)}]
    }

    count = ingest_findings(task_dir, root, summary)
    assert count == 1

    q = load_queue(queue_path(root))
    assert q["findings"][0]["source_task_id"] == ""


# ---------------------------------------------------------------------------
# v7.4.0 — broadened auditor allowlist
# ---------------------------------------------------------------------------


def _ingest_one(tmp_path: Path, auditor_name: str, severity: str = "warning",
                category: str = "perf", blocking: bool = False) -> int:
    root = _make_root(tmp_path)
    task_dir = tmp_path / f"task-{auditor_name}"
    task_dir.mkdir()
    finding = _make_finding(
        severity=severity, category=category, blocking=blocking, id=f"F-{auditor_name}",
    )
    report_path = _write_auditor_report(task_dir, auditor_name, [finding])
    summary = _make_summary(
        task_dir,
        [{"auditor_name": auditor_name, "report_path": str(report_path)}],
    )
    return ingest_findings(task_dir, root, summary)


def test_filter_accepts_security_auditor(tmp_path: Path):
    """v7.4.0: non-blocking security finding is admitted."""
    assert _ingest_one(tmp_path, "security-auditor",
                       severity="warning", category="security") == 1


def test_filter_accepts_performance_auditor(tmp_path: Path):
    """v7.4.0: non-blocking performance finding is admitted.

    task-005 schema-shape filter: severity:minor findings now require a
    non-empty remediation field to be admitted (review notes without
    remediation are dropped). Use severity:major here so the test
    continues to assert the auditor allowlist without pulling in the
    minor-needs-remediation gate.
    """
    assert _ingest_one(tmp_path, "performance-auditor",
                       severity="major", category="performance") == 1


def test_filter_accepts_code_quality_auditor(tmp_path: Path):
    """v7.4.0: non-blocking code-quality finding is admitted."""
    assert _ingest_one(tmp_path, "code-quality-auditor",
                       severity="warning", category="quality") == 1


def test_filter_still_rejects_severity_info_for_new_auditors(tmp_path: Path):
    """v7.4.0: severity=info is still rejected even for newly-allowed auditors."""
    assert _ingest_one(tmp_path, "security-auditor",
                       severity="info", category="security") == 0
    assert _ingest_one(tmp_path, "performance-auditor",
                       severity="info", category="performance") == 0
    assert _ingest_one(tmp_path, "code-quality-auditor",
                       severity="info", category="quality") == 0


# ---------------------------------------------------------------------------
# v7.4.0 — ingest_prevention_rules
# ---------------------------------------------------------------------------


def _make_rule(
    *,
    rule: str = "Reject os.open(... 0o644) in hooks/",
    rationale: str = "Blocks the world-readable queue literal",
    enforcement: str = "lint",
    category: str = "sec",
    source_finding: str = "SEC-002",
) -> dict:
    return {
        "rule": rule,
        "rationale": rationale,
        "enforcement": enforcement,
        "category": category,
        "source_finding": source_finding,
        "executor": "backend-executor",
        "template": "pattern_must_not_appear",
        "params": {"regex": "0o644", "scope": "hooks/**/*.py"},
    }


def test_ingest_prevention_rules_filters_by_enforcement(tmp_path: Path):
    """v7.4.0: rules with actionable enforcement land in queue; advisory rules don't."""
    root = _make_root(tmp_path)
    rules = [
        _make_rule(rule="Test rule 1", enforcement="lint"),
        _make_rule(rule="Test rule 2", enforcement="static-check"),
        _make_rule(rule="Test rule 3", enforcement="ci-gate"),
        _make_rule(rule="Test rule 4", enforcement="test"),
        _make_rule(rule="Test rule 5", enforcement="runtime-guard"),
        _make_rule(rule="Skipped 1", enforcement="advisory"),
        _make_rule(rule="Skipped 2", enforcement="review-checklist"),
        _make_rule(rule="Skipped 3", enforcement="prompt-constraint"),
    ]
    count = lib_residuals.ingest_prevention_rules(root, rules)
    assert count == 5

    q = load_queue(queue_path(root))
    titles = {r["title"] for r in q["findings"]}
    assert "Test rule 1" in titles
    assert "Test rule 5" in titles
    assert "Skipped 1" not in titles
    assert "Skipped 2" not in titles
    assert "Skipped 3" not in titles


def test_ingest_prevention_rules_dedup(tmp_path: Path):
    """v7.4.0: calling twice with the same rule produces one row."""
    root = _make_root(tmp_path)
    rules = [_make_rule(rule="Same rule", rationale="Same rationale")]
    assert lib_residuals.ingest_prevention_rules(root, rules) == 1
    assert lib_residuals.ingest_prevention_rules(root, rules) == 0
    q = load_queue(queue_path(root))
    assert len(q["findings"]) == 1


def test_ingest_prevention_rules_row_shape(tmp_path: Path):
    """v7.4.0: appended rows have correct kind, source_auditor, and AC-2 fields."""
    root = _make_root(tmp_path)
    rules = [_make_rule(rule="Shape test", rationale="Shape rationale",
                        enforcement="lint", category="sec",
                        source_finding="SEC-002")]
    assert lib_residuals.ingest_prevention_rules(root, rules) == 1
    q = load_queue(queue_path(root))
    row = q["findings"][0]
    assert set(row.keys()) == ALLOWED_ROW_KEYS
    assert row["kind"] == "residual"
    assert row["source_auditor"] == "postmortem-analysis"
    assert row["title"] == "Shape test"
    assert "Shape rationale" in row["description"]
    assert "lint" in row["description"]
    assert "SEC-002" in row["description"]
    assert row["status"] == "pending"
    assert row["attempts"] == 0
    assert row["last_attempt_at"] is None
    assert row["source_task_id"] == ""
    assert row["location"] == ""
    # Fingerprint is sha256("postmortem-analysis:" + rule + ":" + rationale)
    expected_fp = compute_fingerprint("postmortem-analysis", "Shape test", "Shape rationale")
    assert row["fingerprint"] == expected_fp


def test_ingest_prevention_rules_skips_malformed(tmp_path: Path):
    """v7.4.0: malformed rule entries are silently skipped, not raised."""
    root = _make_root(tmp_path)
    rules = [
        "not a dict",
        {},  # no rule field
        {"rule": "", "enforcement": "lint"},  # empty rule
        {"rule": "missing enforcement"},  # no enforcement
        _make_rule(rule="Valid rule", enforcement="lint"),
    ]
    assert lib_residuals.ingest_prevention_rules(root, rules) == 1


# ---------------------------------------------------------------------------
# select_next_pending — depends_on dependency gating
# ---------------------------------------------------------------------------


def _make_dep_row(row_id, *, status="pending", attempts=0, depends_on=None,
                  created_at="2026-05-06T20:00:00Z"):
    return {
        "id": row_id,
        "kind": "residual",
        "fingerprint": f"fp-{row_id}",
        "created_at": created_at,
        "source_task_id": "",
        "source_auditor": "test",
        "title": f"row {row_id}",
        "description": f"desc {row_id}",
        "location": "",
        "status": status,
        "attempts": attempts,
        "last_attempt_at": None,
        "depends_on": depends_on if depends_on is not None else [],
    }


def _write_queue(root, rows):
    qp = queue_path(root)
    qp.parent.mkdir(parents=True, exist_ok=True)
    qp.write_text(json.dumps({"findings": rows}, indent=2), encoding="utf-8")


def test_select_skips_row_with_unsatisfied_dependency(tmp_path: Path):
    """A pending row whose dep is still pending must not be selected."""
    root = tmp_path
    _write_queue(root, [
        _make_dep_row("A", created_at="2026-05-06T20:00:00Z"),
        _make_dep_row("B", depends_on=["A"], created_at="2026-05-06T20:01:00Z"),
    ])
    nxt = select_next_pending(root)
    assert nxt is not None
    assert nxt["id"] == "A", "A must be selected first; B blocked on A"


def test_select_unblocks_dependent_after_dep_done(tmp_path: Path):
    """After dep is marked done, the dependent row becomes selectable."""
    root = tmp_path
    _write_queue(root, [
        _make_dep_row("A", status="done", created_at="2026-05-06T20:00:00Z"),
        _make_dep_row("B", depends_on=["A"], created_at="2026-05-06T20:01:00Z"),
    ])
    nxt = select_next_pending(root)
    assert nxt is not None
    assert nxt["id"] == "B"


def test_select_chain_dependency(tmp_path: Path):
    """Phase 1 → Phase 2 → Phase 3 chain: each unblocks the next."""
    root = tmp_path
    rows = [
        _make_dep_row("p1", created_at="2026-05-06T20:00:00Z"),
        _make_dep_row("p2", depends_on=["p1"], created_at="2026-05-06T20:01:00Z"),
        _make_dep_row("p3", depends_on=["p1", "p2"], created_at="2026-05-06T20:02:00Z"),
    ]
    _write_queue(root, rows)
    # All pending → only p1 selectable
    assert select_next_pending(root)["id"] == "p1"
    # Mark p1 done → p2 unblocks
    rows[0]["status"] = "done"
    _write_queue(root, rows)
    assert select_next_pending(root)["id"] == "p2"
    # Mark p2 done → p3 unblocks
    rows[1]["status"] = "done"
    _write_queue(root, rows)
    assert select_next_pending(root)["id"] == "p3"


def test_select_dep_failed_blocks_dependent(tmp_path: Path):
    """A dep with status='failed' (not 'done') keeps the dependent blocked."""
    root = tmp_path
    _write_queue(root, [
        _make_dep_row("A", status="failed", created_at="2026-05-06T20:00:00Z"),
        _make_dep_row("B", depends_on=["A"], created_at="2026-05-06T20:01:00Z"),
    ])
    assert select_next_pending(root) is None, "B blocked because A failed (not done)"


def test_select_missing_dep_id_blocks(tmp_path: Path):
    """A typoed dep id (no matching row) blocks the dependent — fail-closed."""
    root = tmp_path
    _write_queue(root, [
        _make_dep_row("B", depends_on=["NONEXISTENT-ID"],
                       created_at="2026-05-06T20:00:00Z"),
    ])
    assert select_next_pending(root) is None


def test_select_empty_depends_on_unconstrained(tmp_path: Path):
    """An empty depends_on list (or absent) is unconstrained — backward-compat."""
    root = tmp_path
    _write_queue(root, [
        _make_dep_row("A", depends_on=[], created_at="2026-05-06T20:00:00Z"),
    ])
    nxt = select_next_pending(root)
    assert nxt is not None
    assert nxt["id"] == "A"


def test_select_legacy_row_without_depends_on_field(tmp_path: Path):
    """Rows written before this feature (no depends_on field at all) work unchanged."""
    root = tmp_path
    legacy_row = _make_dep_row("legacy", created_at="2026-05-06T20:00:00Z")
    del legacy_row["depends_on"]
    _write_queue(root, [legacy_row])
    nxt = select_next_pending(root)
    assert nxt is not None
    assert nxt["id"] == "legacy"
