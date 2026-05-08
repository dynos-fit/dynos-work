"""TDD-first tests for cmd_run_spec_ready cross-check enforcement (task-20260507-005).

AC coverage:
  AC 8 — existing receipt-existence check still runs before new cross-checks
  AC 9 — temporal cross-check: refuses when no qualifying log entry in window
  AC 10 — structure check: refuses when receipt lacks valid urls_consulted/findings_summary
  AC 11 — legacy gate (no written_at): cross-checks skipped, stdout log emitted

These tests are RED until ctl.py is updated with _check_web_tool_evidence and
_check_receipt_structure helpers and the cross-check integration in cmd_run_spec_ready.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))


def _utc_str(offset_seconds: int = 0) -> str:
    """Return an ISO 8601 UTC string offset by `offset_seconds` from now."""
    t = datetime.now(timezone.utc).replace(microsecond=0)
    t = t + timedelta(seconds=offset_seconds)
    return t.isoformat().replace("+00:00", "Z")


VALID_SUMMARY = "x" * 210  # 210 chars, above 200-char minimum
VALID_URL = "https://example.com/evidence"


def _make_task_dir(tmp_path: Path) -> Path:
    task_dir = tmp_path / ".dynos" / "task-20260507-spec-ready-test"
    task_dir.mkdir(parents=True)
    manifest = {
        "task_id": task_dir.name,
        "stage": "SPEC_NORMALIZATION",
        "classification": {"type": "feature", "risk_level": "medium", "domains": []},
    }
    (task_dir / "manifest.json").write_text(json.dumps(manifest))
    (task_dir / "spec.md").write_text("# Spec\n\n## Acceptance Criteria\n\n1. AC one.\n")
    return task_dir


def _write_gate(task_dir: Path, *, written_at: str | None, search_recommended: bool = True) -> None:
    gate: dict = {
        "search_recommended": search_recommended,
        "search_used": False,
        "query_reason": "external search recommended",
        "candidates": [],
        "recommended_choice": None,
        "decision_basis": {},
    }
    if written_at is not None:
        gate["written_at"] = written_at
    (task_dir / "external-solution-gate.json").write_text(json.dumps(gate))


def _write_receipt(
    task_dir: Path,
    *,
    ts: str,
    urls: list[str] | None = None,
    summary: str | None = None,
) -> Path:
    receipts_dir = task_dir / "receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    receipt: dict = {
        "step": "search-conducted",
        "ts": ts,
        "valid": True,
        "query": "test query",
        "search_used": True,
    }
    if urls is not None:
        receipt["urls_consulted"] = urls
    if summary is not None:
        receipt["findings_summary"] = summary
    path = receipts_dir / "search-conducted.json"
    path.write_text(json.dumps(receipt))
    return path


def _write_log(task_dir: Path, entries: list[dict]) -> None:
    lines = "\n".join(json.dumps(e) for e in entries)
    (task_dir / "web-tool-log.jsonl").write_text(lines + "\n" if lines else "")


# ---------------------------------------------------------------------------
# Tests targeting the new private helpers directly
# ---------------------------------------------------------------------------

def test_check_web_tool_evidence_returns_none_when_entry_in_window() -> None:
    """AC 9: _check_web_tool_evidence returns None (pass) when valid entry exists in window."""
    from ctl import _check_web_tool_evidence
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as td:
        task_dir = pathlib.Path(td)
        gate_ts = "2026-05-07T10:00:00Z"
        entry_ts = "2026-05-07T10:30:00Z"
        receipt_ts = "2026-05-07T11:00:00Z"
        _write_log(task_dir, [
            {"ts": entry_ts, "tool": "WebSearch", "query": "test"},
        ])
        gate = {"written_at": gate_ts, "search_recommended": True}
        receipt = {"ts": receipt_ts}
        result = _check_web_tool_evidence(task_dir, gate, receipt)
        assert result is None, f"Expected None (pass) but got: {result}"


def test_check_web_tool_evidence_returns_error_when_log_absent() -> None:
    """AC 9: When web-tool-log.jsonl is absent, returns an error string."""
    from ctl import _check_web_tool_evidence
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as td:
        task_dir = pathlib.Path(td)
        gate = {"written_at": "2026-05-07T10:00:00Z", "search_recommended": True}
        receipt = {"ts": "2026-05-07T11:00:00Z"}
        result = _check_web_tool_evidence(task_dir, gate, receipt)
        assert result is not None, "Must return an error string when log file is absent"
        assert isinstance(result, str)


def test_check_web_tool_evidence_returns_error_when_no_entries_in_window() -> None:
    """AC 9: When log entries exist but none fall in the gate-to-receipt window, returns error."""
    from ctl import _check_web_tool_evidence
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as td:
        task_dir = pathlib.Path(td)
        # Entry is BEFORE gate.written_at — outside window
        _write_log(task_dir, [
            {"ts": "2026-05-07T09:00:00Z", "tool": "WebSearch", "query": "early search"},
        ])
        gate = {"written_at": "2026-05-07T10:00:00Z", "search_recommended": True}
        receipt = {"ts": "2026-05-07T11:00:00Z"}
        result = _check_web_tool_evidence(task_dir, gate, receipt)
        assert result is not None, "Must return error when no qualifying entries in window"
        assert "qualifying" in result.lower() or "0" in result, (
            f"Error message must mention qualifying count: {result}"
        )


def test_check_web_tool_evidence_error_message_states_count_and_window() -> None:
    """AC 9: Error message includes the time window and qualifying count of 0."""
    from ctl import _check_web_tool_evidence
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as td:
        task_dir = pathlib.Path(td)
        gate_ts = "2026-05-07T10:00:00Z"
        receipt_ts = "2026-05-07T11:00:00Z"
        _write_log(task_dir, [])  # empty log
        gate = {"written_at": gate_ts, "search_recommended": True}
        receipt = {"ts": receipt_ts}
        result = _check_web_tool_evidence(task_dir, gate, receipt)
        assert result is not None
        # Must reference the time window and count
        assert "0" in result, f"Error must mention count 0: {result}"


def test_check_receipt_structure_returns_none_for_valid_receipt() -> None:
    """AC 10: _check_receipt_structure returns None when urls_consulted and summary are valid."""
    from ctl import _check_receipt_structure
    receipt = {
        "urls_consulted": ["https://example.com"],
        "findings_summary": "x" * 210,
    }
    result = _check_receipt_structure(receipt)
    assert result is None, f"Expected None (pass) but got: {result}"


def test_check_receipt_structure_returns_error_for_missing_urls() -> None:
    """AC 10: _check_receipt_structure returns error when urls_consulted is missing."""
    from ctl import _check_receipt_structure
    receipt = {
        "findings_summary": "x" * 210,
        # no urls_consulted
    }
    result = _check_receipt_structure(receipt)
    assert result is not None
    assert "urls_consulted" in result.lower(), f"Error must name the field: {result}"


def test_check_receipt_structure_returns_error_for_non_https_url() -> None:
    """AC 10: _check_receipt_structure returns error when a URL doesn't match ^https?://."""
    from ctl import _check_receipt_structure
    receipt = {
        "urls_consulted": ["ftp://bad.example.com"],
        "findings_summary": "x" * 210,
    }
    result = _check_receipt_structure(receipt)
    assert result is not None
    assert "urls_consulted" in result.lower() or "ftp" in result.lower(), (
        f"Error must name the field or the offending value: {result}"
    )


def test_check_receipt_structure_returns_error_for_short_summary() -> None:
    """AC 10: _check_receipt_structure returns error when findings_summary is too short."""
    from ctl import _check_receipt_structure
    receipt = {
        "urls_consulted": ["https://example.com"],
        "findings_summary": "too short",  # 9 chars
    }
    result = _check_receipt_structure(receipt)
    assert result is not None
    assert "findings_summary" in result.lower() or "200" in result, (
        f"Error must name the field or minimum: {result}"
    )


def test_check_receipt_structure_returns_error_for_missing_summary() -> None:
    """AC 10: _check_receipt_structure returns error when findings_summary is absent."""
    from ctl import _check_receipt_structure
    receipt = {
        "urls_consulted": ["https://example.com"],
        # no findings_summary
    }
    result = _check_receipt_structure(receipt)
    assert result is not None
    assert "findings_summary" in result.lower() or "missing" in result.lower(), (
        f"Error must name the field: {result}"
    )


# ---------------------------------------------------------------------------
# AC 8, 9, 10, 11: Subprocess-level integration tests for cmd_run_spec_ready
# ---------------------------------------------------------------------------

def test_run_spec_ready_refuses_when_no_log_entry(tmp_path: Path) -> None:
    """AC 9: When gate.written_at is present and web-tool-log.jsonl absent, exits 1."""
    import subprocess
    task_dir = _make_task_dir(tmp_path)
    gate_ts = "2026-05-07T10:00:00Z"
    receipt_ts = "2026-05-07T11:00:00Z"
    _write_gate(task_dir, written_at=gate_ts, search_recommended=True)
    _write_receipt(task_dir, ts=receipt_ts, urls=[VALID_URL], summary=VALID_SUMMARY)
    # No web-tool-log.jsonl written
    result = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "ctl.py"), "run-spec-ready", str(task_dir)],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    assert result.returncode == 1, (
        f"Expected exit 1 when web-tool-log.jsonl is absent.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "qualifying" in result.stderr.lower() or "search_evidence" in result.stderr.lower(), (
        f"stderr must indicate cross-check failure.\nstderr: {result.stderr}"
    )


def test_run_spec_ready_refuses_when_log_entry_outside_window(tmp_path: Path) -> None:
    """AC 9: When log entry exists but ts < gate.written_at, exits 1."""
    import subprocess
    task_dir = _make_task_dir(tmp_path)
    gate_ts = "2026-05-07T10:00:00Z"
    entry_ts = "2026-05-07T09:00:00Z"   # BEFORE gate — outside window
    receipt_ts = "2026-05-07T11:00:00Z"
    _write_gate(task_dir, written_at=gate_ts, search_recommended=True)
    _write_receipt(task_dir, ts=receipt_ts, urls=[VALID_URL], summary=VALID_SUMMARY)
    _write_log(task_dir, [{"ts": entry_ts, "tool": "WebSearch", "query": "early"}])
    result = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "ctl.py"), "run-spec-ready", str(task_dir)],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    assert result.returncode == 1, (
        f"Expected exit 1 when log entry is before gate.written_at.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_run_spec_ready_refuses_when_urls_missing_in_receipt(tmp_path: Path) -> None:
    """AC 10: When receipt lacks urls_consulted and gate.written_at is present, exits 1."""
    import subprocess
    task_dir = _make_task_dir(tmp_path)
    gate_ts = "2026-05-07T10:00:00Z"
    entry_ts = "2026-05-07T10:30:00Z"
    receipt_ts = "2026-05-07T11:00:00Z"
    _write_gate(task_dir, written_at=gate_ts, search_recommended=True)
    # Receipt WITHOUT urls_consulted
    _write_receipt(task_dir, ts=receipt_ts, urls=None, summary=VALID_SUMMARY)
    _write_log(task_dir, [{"ts": entry_ts, "tool": "WebSearch", "query": "test"}])
    result = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "ctl.py"), "run-spec-ready", str(task_dir)],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    assert result.returncode == 1, (
        f"Expected exit 1 when urls_consulted missing in receipt.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "urls_consulted" in result.stderr.lower() or "receipt_structure" in result.stderr.lower(), (
        f"stderr must name the failing field.\nstderr: {result.stderr}"
    )


def test_run_spec_ready_refuses_when_summary_short(tmp_path: Path) -> None:
    """AC 10: When findings_summary is too short, exits 1."""
    import subprocess
    task_dir = _make_task_dir(tmp_path)
    gate_ts = "2026-05-07T10:00:00Z"
    entry_ts = "2026-05-07T10:30:00Z"
    receipt_ts = "2026-05-07T11:00:00Z"
    _write_gate(task_dir, written_at=gate_ts, search_recommended=True)
    _write_receipt(task_dir, ts=receipt_ts, urls=[VALID_URL], summary="too short")
    _write_log(task_dir, [{"ts": entry_ts, "tool": "WebSearch", "query": "test"}])
    result = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "ctl.py"), "run-spec-ready", str(task_dir)],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    assert result.returncode == 1, (
        f"Expected exit 1 when findings_summary too short.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "findings" in result.stderr.lower() or "summary" in result.stderr.lower(), (
        f"stderr must name the failing field.\nstderr: {result.stderr}"
    )


def test_run_spec_ready_skips_cross_check_on_legacy_gate(tmp_path: Path) -> None:
    """AC 11: Legacy gate (no written_at) skips cross-checks, emits skip message to stdout."""
    import subprocess
    task_dir = _make_task_dir(tmp_path)
    # Gate WITHOUT written_at
    _write_gate(task_dir, written_at=None, search_recommended=True)
    receipt_ts = "2026-05-07T11:00:00Z"
    _write_receipt(task_dir, ts=receipt_ts)  # Old receipt schema, no urls/summary
    # No web-tool-log.jsonl — would fail cross-check if it ran
    result = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "ctl.py"), "run-spec-ready", str(task_dir)],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    assert "legacy gate file" in result.stdout, (
        f"Expected 'legacy gate file' in stdout for legacy gate.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_run_spec_ready_treats_malformed_written_at_as_legacy(tmp_path: Path) -> None:
    """AC 11: Unparseable written_at is treated as absent; cross-checks skipped."""
    import subprocess
    task_dir = _make_task_dir(tmp_path)
    _write_gate(task_dir, written_at="not-a-date", search_recommended=True)
    receipt_ts = "2026-05-07T11:00:00Z"
    _write_receipt(task_dir, ts=receipt_ts)
    # No web-tool-log.jsonl — would fail cross-check if it ran
    result = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "ctl.py"), "run-spec-ready", str(task_dir)],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    assert "malformed written_at" in result.stdout, (
        f"Expected 'malformed written_at' in stdout.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
