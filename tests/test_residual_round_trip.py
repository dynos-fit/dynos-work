"""Integration tests for task-20260504-007.

Covers:
  - AC-15: cmd_run_audit_finish end-to-end with residual round-trip
  - AC-15 (ordering): residual row updated BEFORE transition_task is called
  - AC-6: raw-input.md absent → queue unchanged
  - AC-5: ingest_findings failure does not block DONE transition
  - AC-16: skill list behavior (load_queue + output field verification)

Uses pytest.importorskip for lib_residuals so this file collects cleanly
before the module exists.  The ctl module is also importskipped on
lib_residuals via the same mechanism.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from io import StringIO
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

lib_residuals = pytest.importorskip("lib_residuals")

queue_path = lib_residuals.queue_path
load_queue = lib_residuals.load_queue
compute_fingerprint = lib_residuals.compute_fingerprint


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_root_in(base: Path) -> Path:
    root = base / "project"
    (root / ".dynos").mkdir(parents=True)
    return root


def _make_task_dir(root: Path, task_id: str = "task-test-999") -> Path:
    """Create .dynos/task-{id}/ under root, return task_dir."""
    task_dir = root / ".dynos" / task_id
    task_dir.mkdir(parents=True)
    return task_dir


def _write_manifest(task_dir: Path, stage: str = "FINAL_AUDIT", task_id: str = "task-test-999") -> None:
    manifest = {
        "task_id": task_id,
        "stage": stage,
        "classification": {
            "type": "feature",
            "risk_level": "low",
            "domains": ["backend"],
            "tdd_required": False,
        },
    }
    (task_dir / "manifest.json").write_text(json.dumps(manifest))


def _write_audit_summary(task_dir: Path, task_id: str = "task-test-999") -> None:
    summary = {
        "task_id": task_id,
        "reports": [],
        "audit_result": "pass",
    }
    (task_dir / "audit-summary.json").write_text(json.dumps(summary))


def _write_retro(task_dir: Path) -> None:
    (task_dir / "task-retrospective.json").write_text("{}")


def _write_raw_input(task_dir: Path, residual_id: str) -> None:
    content = f"<!-- residual-id: {residual_id} -->\n\nTask description."
    (task_dir / "raw-input.md").write_text(content)


def _pre_populate_queue(root: Path, rows: list[dict]) -> None:
    qp = queue_path(root)
    qp.parent.mkdir(parents=True, exist_ok=True)
    qp.write_text(json.dumps({"findings": rows}))


def _make_row(
    *,
    row_id: str | None = None,
    status: str = "in_progress",
    attempts: int = 1,
    created_at: str = "2026-05-04T10:00:00Z",
    source_auditor: str = "claude-md-auditor",
) -> dict:
    rid = row_id or str(uuid.uuid4())
    fp = compute_fingerprint(source_auditor, rid, "description")
    return {
        "id": rid,
        "kind": "residual",
        "fingerprint": fp,
        "created_at": created_at,
        "source_task_id": "task-test-999",
        "source_auditor": source_auditor,
        "title": "description",
        "description": "description",
        "location": "some/file.py:1",
        "status": status,
        "attempts": attempts,
        "last_attempt_at": None,
    }


def _import_cmd_run_audit_finish():
    """Import cmd_run_audit_finish from ctl, skipping if ctl fails to import."""
    try:
        import ctl as _ctl
        return _ctl.cmd_run_audit_finish
    except ImportError as e:
        pytest.skip(f"ctl not importable: {e}")


def _call_cmd_run_audit_finish(task_dir: Path, monkeypatch: pytest.MonkeyPatch | None = None):
    """Invoke cmd_run_audit_finish(args) and return exit code."""
    cmd_run_audit_finish = _import_cmd_run_audit_finish()
    args = argparse.Namespace(task_dir=str(task_dir))
    return cmd_run_audit_finish(args)


# ---------------------------------------------------------------------------
# AC-15: cmd_run_audit_finish end-to-end
# ---------------------------------------------------------------------------


def _make_noop_transition_task(task_dir: Path) -> object:
    """Return a patched transition_task that skips receipt-chain integrity checks.

    Updates manifest.json to the requested stage and returns the same
    (prev_stage, manifest) shape as the real transition_task.
    """
    def _noop_transition_task(td, stage, **kwargs):
        manifest = json.loads((td / "manifest.json").read_text())
        prev_stage = manifest.get("stage", "FINAL_AUDIT")
        manifest["stage"] = stage
        (td / "manifest.json").write_text(json.dumps(manifest))
        return prev_stage, manifest
    return _noop_transition_task


def test_cmd_run_audit_finish_updates_residual_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Synthetic task dir with manifest(FINAL_AUDIT), audit-summary, retro, and raw-input.md
    containing <!-- residual-id: {some-id} -->.  Pre-existing in_progress row in queue.
    After cmd_run_audit_finish: row status is 'done'.
    """
    monkeypatch.setenv("DYNOS_EVENT_SECRET", "test-secret-for-residual-tests")

    root = _make_root_in(tmp_path)
    task_dir = _make_task_dir(root)

    _write_manifest(task_dir)
    _write_audit_summary(task_dir)
    _write_retro(task_dir)

    residual_id = "test-id-001"
    _write_raw_input(task_dir, residual_id)

    row = _make_row(row_id=residual_id, status="in_progress", attempts=1)
    _pre_populate_queue(root, [row])

    import ctl as _ctl
    monkeypatch.setattr(_ctl, "transition_task", _make_noop_transition_task(task_dir))

    cmd_run_audit_finish = _import_cmd_run_audit_finish()
    args = argparse.Namespace(task_dir=str(task_dir))
    rc = cmd_run_audit_finish(args)
    assert rc == 0, f"cmd_run_audit_finish returned non-zero: {rc}"

    q = load_queue(queue_path(root))
    updated = next((r for r in q["findings"] if r["id"] == residual_id), None)
    assert updated is not None, "row with residual_id not found in queue after run"
    assert updated["status"] == "done", (
        f"expected status='done', got {updated['status']!r}"
    )


def test_cmd_run_audit_finish_row_updated_before_transition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The residual row must be updated to 'done' BEFORE transition_task is called.

    This is the mechanical proof of the ordering constraint (AC-15, ST-4).
    We patch transition_task in the ctl module to assert that the row is
    already 'done' at the moment transition_task is invoked.
    """
    monkeypatch.setenv("DYNOS_EVENT_SECRET", "test-secret-for-residual-tests")

    root = _make_root_in(tmp_path)
    task_dir = _make_task_dir(root)

    _write_manifest(task_dir)
    _write_audit_summary(task_dir)
    _write_retro(task_dir)

    residual_id = "test-id-ordering-check"
    _write_raw_input(task_dir, residual_id)

    row = _make_row(row_id=residual_id, status="in_progress", attempts=1)
    _pre_populate_queue(root, [row])

    transition_calls: list[str] = []

    def _patched_transition_task(td, stage, **kwargs):
        # At this point, the row MUST already be "done" in the queue
        q = load_queue(queue_path(root))
        match = next((r for r in q["findings"] if r["id"] == residual_id), None)
        assert match is not None, "row disappeared before transition_task was called"
        assert match["status"] == "done", (
            f"ORDERING VIOLATION: row status is {match['status']!r} at "
            f"transition_task call time; expected 'done'. "
            f"update_row_status must execute BEFORE transition_task."
        )
        transition_calls.append(stage)
        # Return the same shape as the real transition_task
        manifest = json.loads((td / "manifest.json").read_text())
        manifest["stage"] = stage
        (td / "manifest.json").write_text(json.dumps(manifest))
        return "FINAL_AUDIT", manifest

    import ctl as _ctl
    monkeypatch.setattr(_ctl, "transition_task", _patched_transition_task)

    cmd_run_audit_finish = _import_cmd_run_audit_finish()
    args = argparse.Namespace(task_dir=str(task_dir))
    rc = cmd_run_audit_finish(args)

    assert transition_calls == ["DONE"], (
        f"transition_task was not called exactly once with 'DONE'; calls={transition_calls}"
    )

    q = load_queue(queue_path(root))
    updated = next((r for r in q["findings"] if r["id"] == residual_id), None)
    assert updated is not None
    assert updated["status"] == "done"


def test_cmd_run_audit_finish_no_raw_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Task dir WITHOUT raw-input.md: function completes successfully, queue is unchanged."""
    monkeypatch.setenv("DYNOS_EVENT_SECRET", "test-secret-for-residual-tests")

    root = _make_root_in(tmp_path)
    task_dir = _make_task_dir(root)

    _write_manifest(task_dir)
    _write_audit_summary(task_dir)
    _write_retro(task_dir)
    # raw-input.md intentionally NOT written

    # Pre-populate queue with an unrelated row
    unrelated_row = _make_row(row_id="unrelated-row-001", status="in_progress")
    _pre_populate_queue(root, [unrelated_row])

    import ctl as _ctl
    monkeypatch.setattr(_ctl, "transition_task", _make_noop_transition_task(task_dir))

    cmd_run_audit_finish = _import_cmd_run_audit_finish()
    args = argparse.Namespace(task_dir=str(task_dir))
    rc = cmd_run_audit_finish(args)
    assert rc == 0

    # Queue must be unchanged (no row was updated)
    q = load_queue(queue_path(root))
    row = q["findings"][0]
    assert row["id"] == "unrelated-row-001"
    assert row["status"] == "in_progress", (
        "row status must be unchanged when raw-input.md is absent"
    )


def test_cmd_run_audit_finish_ingest_failure_does_not_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Patching lib_residuals.ingest_findings to raise: cmd_run_audit_finish still exits 0."""
    monkeypatch.setenv("DYNOS_EVENT_SECRET", "test-secret-for-residual-tests")

    root = _make_root_in(tmp_path)
    task_dir = _make_task_dir(root)

    _write_manifest(task_dir)
    _write_audit_summary(task_dir)
    _write_retro(task_dir)

    def _always_raise(*args, **kwargs):
        raise RuntimeError("simulated ingest failure")

    import ctl as _ctl
    monkeypatch.setattr(_ctl.lib_residuals, "ingest_findings", _always_raise)
    monkeypatch.setattr(_ctl, "transition_task", _make_noop_transition_task(task_dir))

    cmd_run_audit_finish = _import_cmd_run_audit_finish()
    args = argparse.Namespace(task_dir=str(task_dir))
    rc = cmd_run_audit_finish(args)
    assert rc == 0, (
        "cmd_run_audit_finish must exit 0 even when ingest_findings raises"
    )

    # Manifest must have reached DONE
    manifest = json.loads((task_dir / "manifest.json").read_text())
    assert manifest["stage"] == "DONE", (
        f"manifest stage should be DONE; got {manifest['stage']!r}"
    )


def test_cmd_run_audit_finish_no_residual_id_in_raw_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """raw-input.md exists but contains no residual-id comment: queue is unchanged."""
    monkeypatch.setenv("DYNOS_EVENT_SECRET", "test-secret-for-residual-tests")

    root = _make_root_in(tmp_path)
    task_dir = _make_task_dir(root)

    _write_manifest(task_dir)
    _write_audit_summary(task_dir)
    _write_retro(task_dir)
    # raw-input.md with no residual-id comment
    (task_dir / "raw-input.md").write_text("Just a regular task description, no residual ID.")

    row = _make_row(row_id="some-pending-row", status="in_progress")
    _pre_populate_queue(root, [row])

    import ctl as _ctl
    monkeypatch.setattr(_ctl, "transition_task", _make_noop_transition_task(task_dir))

    cmd_run_audit_finish = _import_cmd_run_audit_finish()
    args = argparse.Namespace(task_dir=str(task_dir))
    rc = cmd_run_audit_finish(args)
    assert rc == 0

    q = load_queue(queue_path(root))
    row_after = q["findings"][0]
    assert row_after["status"] == "in_progress", (
        "row must be unchanged when raw-input.md has no residual-id comment"
    )


# ---------------------------------------------------------------------------
# AC-16: Skill list behavior — verified via load_queue + field presence
# (The skill itself is prose; we verify the data contract load_queue exposes)
# ---------------------------------------------------------------------------


def _format_list_output(queue: dict) -> str:
    """Simulate what the 'list' subcommand would print.

    Each row: id  status  attempts  source_auditor  title(<=60)  created_at
    Plain text, no ANSI escapes, no JSON.
    """
    findings = queue.get("findings", [])
    if not findings:
        return "dynos-work: no residuals queued"

    lines = []
    for row in findings:
        title = row.get("title", "")[:60]
        line = (
            f"{row['id']}  {row['status']}  {row['attempts']}  "
            f"{row['source_auditor']}  {title}  {row['created_at']}"
        )
        lines.append(line)
    return "\n".join(lines)


def _make_full_row(row_id: str, status: str = "pending", attempts: int = 0,
                   source_auditor: str = "claude-md-auditor",
                   title: str = "A finding title",
                   created_at: str = "2026-05-04T10:00:00Z") -> dict:
    fp = compute_fingerprint(source_auditor, row_id, title)
    return {
        "id": row_id,
        "kind": "residual",
        "fingerprint": fp,
        "created_at": created_at,
        "source_task_id": "task-test-001",
        "source_auditor": source_auditor,
        "title": title,
        "description": title,
        "location": "file.py:1",
        "status": status,
        "attempts": attempts,
        "last_attempt_at": None,
    }


def test_skill_list_two_rows(tmp_path: Path):
    """load_queue returning two rows: output contains both rows' 6 required fields."""
    root = _make_root_in(tmp_path)
    row1 = _make_full_row("row-id-aaa", status="pending", source_auditor="claude-md-auditor",
                          title="First finding", created_at="2026-05-04T08:00:00Z")
    row2 = _make_full_row("row-id-bbb", status="done", attempts=1,
                          source_auditor="dead-code-auditor",
                          title="Second finding", created_at="2026-05-04T09:00:00Z")

    _pre_populate_queue(root, [row1, row2])

    q = load_queue(queue_path(root))
    output = _format_list_output(q)

    assert "row-id-aaa" in output
    assert "row-id-bbb" in output

    # Both rows' 6 required fields must appear in the output
    for row in [row1, row2]:
        assert row["id"] in output, f"id {row['id']!r} not in output"
        assert row["status"] in output, f"status {row['status']!r} not in output"
        assert str(row["attempts"]) in output, f"attempts not in output for {row['id']}"
        assert row["source_auditor"] in output, f"source_auditor not in output for {row['id']}"
        assert row["title"][:60] in output, f"title not in output for {row['id']}"
        assert row["created_at"] in output, f"created_at not in output for {row['id']}"


def test_skill_list_empty_queue(tmp_path: Path):
    """load_queue returns {"findings":[]}: output is exactly 'dynos-work: no residuals queued'."""
    root = _make_root_in(tmp_path)
    _pre_populate_queue(root, [])

    q = load_queue(queue_path(root))
    output = _format_list_output(q)

    assert output == "dynos-work: no residuals queued"


def test_skill_list_missing_file(tmp_path: Path):
    """Queue file absent: load_queue returns empty findings, output is the empty message."""
    root = _make_root_in(tmp_path)
    qp = queue_path(root)
    assert not qp.exists(), "precondition: queue file must not exist"

    q = load_queue(qp)
    output = _format_list_output(q)

    assert output == "dynos-work: no residuals queued"
    assert not qp.exists(), "load_queue must not create the file when it is absent"


def test_skill_list_all_statuses_shown(tmp_path: Path):
    """All rows regardless of status appear in list output (not just pending)."""
    root = _make_root_in(tmp_path)
    rows = [
        _make_full_row("row-pending", status="pending"),
        _make_full_row("row-in-progress", status="in_progress"),
        _make_full_row("row-done", status="done"),
        _make_full_row("row-failed", status="failed"),
    ]
    _pre_populate_queue(root, rows)

    q = load_queue(queue_path(root))
    output = _format_list_output(q)

    for row in rows:
        assert row["id"] in output, f"row {row['id']!r} missing from list output"


def test_skill_list_title_truncated_to_60(tmp_path: Path):
    """Title in list output is truncated to 60 characters."""
    root = _make_root_in(tmp_path)
    long_title = "T" * 100
    row = _make_full_row("row-long-title", title=long_title)
    _pre_populate_queue(root, [row])

    q = load_queue(queue_path(root))
    output = _format_list_output(q)

    # The full title should NOT appear; only the first 60 chars
    assert long_title not in output
    assert "T" * 60 in output
