"""Tests for receipt_audit_done self-verify block (AC 2).

When ``report_path`` points to a real JSON file on disk, the writer re-reads
it and cross-checks ``finding_count`` and ``blocking_count`` against the
actual contents. A mismatch raises ValueError naming the mismatched field
and both values. ``report_sha256`` is computed only on verified match.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_receipts import receipt_audit_done  # noqa: E402


def _make_task(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260419-AS"
    td.mkdir(parents=True)
    return td


def _write_report(task_dir: Path, findings: list[dict]) -> Path:
    """Write a real audit report JSON and return its path."""
    report = task_dir / "audit-reports" / "sec.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({"findings": findings}))
    return report


def test_report_path_none_allows_zero_counts(tmp_path: Path):
    """MA-005 hardening: report_path=None is legal ONLY when finding_count
    and blocking_count are both zero. The legit case is an auditor that
    ran and found nothing. No report file is required for zero findings."""
    td = _make_task(tmp_path)
    out = receipt_audit_done(
        td, "sec", "haiku", 0, 0, None, 100,
        route_mode="generic", agent_path=None, injected_agent_sha256=None,
    )
    payload = json.loads(out.read_text())
    assert payload["finding_count"] == 0
    assert payload["blocking_count"] == 0
    assert payload["report_sha256"] is None


def test_report_path_none_with_nonzero_counts_rejected(tmp_path: Path):
    """MA-005 regression: caller-attested non-zero counts without a
    report file are forbidden. This closes the TOCTOU where a malicious
    or careless caller could pass finding_count=5, blocking_count=0 and
    skip writing a report that would contradict the claim."""
    td = _make_task(tmp_path)
    with pytest.raises(ValueError, match="Caller-attested non-zero counts are forbidden"):
        receipt_audit_done(
            td, "sec", "haiku", 5, 2, None, 100,
            route_mode="generic", agent_path=None, injected_agent_sha256=None,
        )
    with pytest.raises(ValueError, match="Caller-attested non-zero counts are forbidden"):
        receipt_audit_done(
            td, "sec", "haiku", 0, 1, None, 100,
            route_mode="generic", agent_path=None, injected_agent_sha256=None,
        )


def test_report_path_matching_counts_verifies(tmp_path: Path):
    """AC 2: 5 findings written, caller claims 5 → passes, report_sha256 computed."""
    td = _make_task(tmp_path)
    findings = [
        {"id": f"F-{i}", "blocking": True}
        for i in range(5)
    ]
    report = _write_report(td, findings)
    out = receipt_audit_done(
        td, "sec", "haiku", 5, 5, str(report), 100,
        route_mode="generic", agent_path=None, injected_agent_sha256=None,
    )
    payload = json.loads(out.read_text())
    assert payload["finding_count"] == 5
    assert payload["blocking_count"] == 5
    # Report sha256 must be populated (64 hex chars) on verified match.
    assert isinstance(payload["report_sha256"], str)
    assert len(payload["report_sha256"]) == 64


def test_report_path_derives_counts_when_omitted(tmp_path: Path):
    """Regression: callers may omit finding/blocking counts entirely when
    a real report file exists; the writer derives both from disk."""
    td = _make_task(tmp_path)
    findings = [
        {"id": "F-1", "blocking": True},
        {"id": "F-2", "blocking": False},
        {"id": "F-3", "blocking": True},
    ]
    report = _write_report(td, findings)
    out = receipt_audit_done(
        td, "sec", "haiku", report_path=str(report), tokens_used=100,
        route_mode="generic", agent_path=None, injected_agent_sha256=None,
    )
    payload = json.loads(out.read_text())
    assert payload["finding_count"] == 3
    assert payload["blocking_count"] == 2
    assert isinstance(payload["report_sha256"], str)
    assert len(payload["report_sha256"]) == 64


def test_report_path_finding_count_mismatch_refuses(tmp_path: Path):
    """AC 2: caller claims 3 findings when file has 5 → ValueError."""
    td = _make_task(tmp_path)
    findings = [{"id": f"F-{i}", "blocking": False} for i in range(5)]
    report = _write_report(td, findings)
    with pytest.raises(ValueError) as exc_info:
        receipt_audit_done(
            td, "sec", "haiku", 3, 0, str(report), 100,
            route_mode="generic", agent_path=None, injected_agent_sha256=None,
        )
    msg = str(exc_info.value)
    assert "finding_count" in msg
    assert "3" in msg and "5" in msg
    # No receipt should have been written on refusal
    assert not (td / "receipts" / "audit-sec.json").exists()


def test_report_path_blocking_count_mismatch_refuses(tmp_path: Path):
    """AC 2: caller claims 0 blocking when file has 2 blocking → ValueError."""
    td = _make_task(tmp_path)
    findings = [
        {"id": "F-1", "blocking": True},
        {"id": "F-2", "blocking": True},
        {"id": "F-3", "blocking": False},
    ]
    report = _write_report(td, findings)
    with pytest.raises(ValueError) as exc_info:
        receipt_audit_done(
            td, "sec", "haiku", 3, 0, str(report), 100,
            route_mode="generic", agent_path=None, injected_agent_sha256=None,
        )
    msg = str(exc_info.value)
    assert "blocking_count" in msg
    assert "0" in msg and "2" in msg


def test_report_path_missing_file_skips_selfverify(tmp_path: Path):
    """AC 2 docstring: missing report file → self-verify skipped (None path
    preserves pre-escalation ensemble semantics)."""
    td = _make_task(tmp_path)
    # Claim a file that doesn't exist
    missing = str(td / "audit-reports" / "nonexistent.json")
    out = receipt_audit_done(
        td, "sec", "haiku", 99, 99, missing, 100,
        route_mode="generic", agent_path=None, injected_agent_sha256=None,
    )
    payload = json.loads(out.read_text())
    # Self-verify skipped → receipt has caller-supplied counts, no hash.
    assert payload["finding_count"] == 99
    assert payload["blocking_count"] == 99
    assert payload["report_sha256"] is None


def test_report_path_missing_file_refuses_when_counts_omitted(tmp_path: Path):
    """Auto-derive mode requires a real report file; otherwise the caller
    must explicitly pass zero counts with report_path=None."""
    td = _make_task(tmp_path)
    missing = str(td / "audit-reports" / "nonexistent.json")
    with pytest.raises(ValueError, match="cannot derive finding_count/blocking_count automatically"):
        receipt_audit_done(
            td, "sec", "haiku", report_path=missing, tokens_used=100,
            route_mode="generic", agent_path=None, injected_agent_sha256=None,
        )
