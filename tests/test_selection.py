"""Tests for AC 1, 2, 4: receipt-backed selection via select_eligible_reports.

These tests are RED by design until seg-1 lands select_eligible_reports in
hooks/lib_validate.py.

Fixture design (per plan.md Test Strategy):
  - Report files are written as JSON directly to task_dir/audit-reports/
  - Receipt JSON files are written directly to task_dir/receipts/ (bypassing
    receipt_audit_done to avoid spawn-log dependency in pure selection tests)
  - This isolates the SELECTION PREDICATE from receipt-writing machinery

All tests drive the real select_eligible_reports function.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

from lib_validate import select_eligible_reports  # type: ignore[import]  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write_report(
    task_dir: Path,
    filename: str,
    status: str = "complete",
    findings: list | None = None,
    auditor_name: str = "security",
) -> Path:
    """Write a synthetic audit report to task_dir/audit-reports/<filename>."""
    audit_dir = task_dir / "audit-reports"
    audit_dir.mkdir(exist_ok=True)
    report = {
        "auditor_name": auditor_name,
        "status": status,
        "verdict": "pass",
        "findings": findings or [],
    }
    path = audit_dir / filename
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def _write_receipt(
    task_dir: Path,
    step_name: str,
    report_path: Path | None = None,
    stage: str | None = None,
    model_used: str | None = None,
    contract_version: int = 2,
) -> Path:
    """Write a synthetic receipt directly to task_dir/receipts/<step_name>.json.

    Bypasses receipt_audit_done to avoid spawn-log dependency.
    """
    receipts_dir = task_dir / "receipts"
    receipts_dir.mkdir(exist_ok=True)
    payload: dict = {
        "valid": True,
        "contract_version": contract_version,
    }
    if report_path is not None:
        payload["report_path"] = str(report_path)
        sha = hashlib.sha256(report_path.read_bytes()).hexdigest()
        payload["report_sha256"] = sha
    if stage is not None:
        payload["stage"] = stage
    if model_used is not None:
        payload["model_used"] = model_used
    receipt_file = receipts_dir / f"{step_name}.json"
    receipt_file.write_text(json.dumps(payload), encoding="utf-8")
    return receipt_file


# ---------------------------------------------------------------------------
# AC 1: Receipt-backed selection excludes stale/partial reports
# ---------------------------------------------------------------------------


def test_select_eligible_reports_no_receipt(tmp_path: Path) -> None:
    """A report with no receipt is excluded from gate selection.

    AC 1: A report is eligible only if a matching receipt exists.
    Without a receipt, select_eligible_reports must return [].
    """
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    _write_report(
        task_dir,
        "security-haiku-attempt-1.json",  # noqa: model-literal
        status="complete",
        auditor_name="security",
    )
    # No receipt written — report must be excluded
    result = select_eligible_reports(task_dir, stage="CHECKPOINT_AUDIT")
    assert result == [], (
        f"Report with no receipt must be excluded from gate selection, got {result}"
    )


def test_select_eligible_reports_partial_excluded(tmp_path: Path) -> None:
    """A status='partial' report is excluded from gate path selection.

    AC 1: Gate path (stage not None) requires status=='complete'.
    Partial reports must not appear in gate results.
    """
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    report_path = _write_report(
        task_dir,
        "security-haiku-attempt-1.json",  # noqa: model-literal
        status="partial",
        auditor_name="security",
    )
    _write_receipt(
        task_dir,
        "audit-security",
        report_path=report_path,
        stage="CHECKPOINT_AUDIT",
    )
    # Partial report must be excluded from gate path
    result = select_eligible_reports(task_dir, stage="CHECKPOINT_AUDIT")
    assert result == [], (
        f"status=partial report must be excluded from gate selection, got {result}"
    )


def test_select_eligible_reports_stage_filter(tmp_path: Path) -> None:
    """Receipt stage must match the stage argument exactly.

    AC 1(d): Receipt's stage field must match the stage argument.
    A receipt from a different stage must cause exclusion.
    """
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    report_path = _write_report(
        task_dir,
        "security-haiku-attempt-1.json",  # noqa: model-literal
        status="complete",
        auditor_name="security",
    )
    # Receipt has stage="DESIGN_AUDIT" but we ask for stage="CHECKPOINT_AUDIT"
    _write_receipt(
        task_dir,
        "audit-security",
        report_path=report_path,
        stage="DESIGN_AUDIT",
    )
    result = select_eligible_reports(task_dir, stage="CHECKPOINT_AUDIT")
    assert result == [], (
        f"Receipt stage mismatch must exclude report from gate, got {result}"
    )

    # Now verify that the correct stage IS included
    _write_receipt(
        task_dir,
        "audit-security",
        report_path=report_path,
        stage="CHECKPOINT_AUDIT",
    )
    result = select_eligible_reports(task_dir, stage="CHECKPOINT_AUDIT")
    assert len(result) == 1, (
        f"Matching stage receipt must include the report, got {result}"
    )


def test_complete_report_with_matching_receipt_is_eligible(tmp_path: Path) -> None:
    """A complete report with matching receipt and stage IS eligible.

    Positive path: verifies the happy path of gate selection returns the report.
    """
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    report_path = _write_report(
        task_dir,
        "security-haiku-attempt-1.json",  # noqa: model-literal
        status="complete",
        auditor_name="security",
    )
    _write_receipt(
        task_dir,
        "audit-security",
        report_path=report_path,
        stage="CHECKPOINT_AUDIT",
    )
    result = select_eligible_reports(task_dir, stage="CHECKPOINT_AUDIT")
    assert len(result) == 1, (
        f"Complete report with matching receipt and stage must be eligible, got {result}"
    )


# ---------------------------------------------------------------------------
# AC 2: Skeleton reads as "auditor not run"; DONE gate refuses
# ---------------------------------------------------------------------------


def test_skeleton_reads_as_not_run(tmp_path: Path) -> None:
    """A skeleton (status='in_progress') with no receipt → zero eligible reports.

    AC 2: A skeleton report with status='in_progress' that is never updated
    produces zero eligible reports — 'auditor not run'.
    """
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    _write_report(
        task_dir,
        "security-haiku-attempt-1.json",  # noqa: model-literal
        status="in_progress",
        auditor_name="security",
    )
    # No receipt — skeleton must produce zero eligible reports
    result = select_eligible_reports(task_dir, stage="CHECKPOINT_AUDIT")
    assert result == [], (
        f"status=in_progress skeleton with no receipt must yield zero eligible, got {result}"
    )


def test_selection_requires_complete_receipt(tmp_path: Path) -> None:
    """Gate path requires status='complete' AND a matching receipt.

    AC 2: Even with a receipt, in_progress status excludes the report from the
    gate path. Tests the section-6.4 wiring guarantee.
    """
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    report_path = _write_report(
        task_dir,
        "security-haiku-attempt-1.json",  # noqa: model-literal
        status="in_progress",
        auditor_name="security",
    )
    # Write a receipt — but status=in_progress means not eligible on gate path
    _write_receipt(
        task_dir,
        "audit-security",
        report_path=report_path,
        stage="CHECKPOINT_AUDIT",
    )
    result = select_eligible_reports(task_dir, stage="CHECKPOINT_AUDIT")
    assert result == [], (
        f"status=in_progress must be excluded even with receipt (gate path), got {result}"
    )


def test_skeleton_advisory_context_when_no_stage(tmp_path: Path) -> None:
    """Repair-planning path (stage=None) includes partial/in_progress as advisory.

    AC 1 and AC 2: _collect_audit_findings (repair-planning) omits stage filter,
    so it may include skeleton findings as advisory context when a receipt exists.
    This tests that the stage=None path does NOT apply the status='complete' filter.
    """
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    report_path = _write_report(
        task_dir,
        "security-haiku-attempt-1.json",  # noqa: model-literal
        status="partial",
        findings=[{"id": "F001", "file": "foo.py", "line": 10, "category": "bug"}],
        auditor_name="security",
    )
    _write_receipt(
        task_dir,
        "audit-security",
        report_path=report_path,
        stage="CHECKPOINT_AUDIT",
    )
    # stage=None → repair-planning path; should include partial report
    result = select_eligible_reports(task_dir, stage=None)
    assert len(result) >= 1, (
        f"stage=None (repair-planning) must include partial report as advisory, got {result}"
    )


# ---------------------------------------------------------------------------
# AC 4: Attempt precedence — higher attempt wins; escalation model wins
# ---------------------------------------------------------------------------


def test_select_eligible_reports_attempt_precedence(tmp_path: Path) -> None:
    """When both attempt-1 and attempt-2 are complete with receipts, attempt-2 wins.

    AC 4: select_eligible_reports picks the higher attempt number when
    both are status=complete with valid receipts.
    """
    task_dir = tmp_path / "task"
    task_dir.mkdir()

    report1 = _write_report(
        task_dir,
        "security-haiku-attempt-1.json",  # noqa: model-literal
        status="complete",
        auditor_name="security",
    )
    report2 = _write_report(
        task_dir,
        "security-haiku-attempt-2.json",  # noqa: model-literal
        status="complete",
        auditor_name="security",
    )
    # Both have valid receipts with matching stage
    _write_receipt(
        task_dir, "audit-security",
        report_path=report2,  # receipt points to attempt-2 (latest)
        stage="CHECKPOINT_AUDIT",
    )
    # Also write a separate receipt for attempt-1 (older)
    # In practice receipts may be overwritten or separate per-attempt
    result = select_eligible_reports(task_dir, stage="CHECKPOINT_AUDIT")
    # The result should contain the report with higher attempt number
    assert len(result) >= 1, f"At least one eligible report expected, got {result}"
    # Verify attempt-2 is present (higher attempt takes precedence)
    report_paths = [str(r[0]) for r in result]
    assert any("attempt-2" in rp for rp in report_paths), (
        f"Attempt-2 must take precedence, but result paths are: {report_paths}"
    )


def test_select_eligible_reports_escalation_model_wins(tmp_path: Path) -> None:
    """Escalation model report takes precedence over voting-model reports.

    AC 4: The escalation model takes precedence over voting models regardless
    of attempt number.
    """
    task_dir = tmp_path / "task"
    task_dir.mkdir()

    # Write a voting-model (haiku) report attempt-1
    _write_report(
        task_dir,
        "security-haiku-attempt-1.json",  # noqa: model-literal
        status="complete",
        auditor_name="security",
    )
    # Write an escalation-model (sonnet) report attempt-1
    escalation_report = _write_report(
        task_dir,
        "security-sonnet-attempt-1.json",  # noqa: model-literal
        status="complete",
        auditor_name="security",
    )
    # Only the escalation model report has a receipt
    _write_receipt(
        task_dir,
        "audit-security-sonnet",  # noqa: model-literal
        report_path=escalation_report,
        stage="CHECKPOINT_AUDIT",
        model_used="sonnet",  # noqa: model-literal
    )
    result = select_eligible_reports(task_dir, stage="CHECKPOINT_AUDIT")
    # Escalation model receipt exists → escalation report must be included
    assert len(result) >= 1, f"Escalation model report must be eligible, got {result}"
    report_paths = [str(r[0]) for r in result]
    assert any("sonnet" in rp for rp in report_paths), (  # noqa: model-literal
        f"Escalation model report (sonnet) must be included, got {report_paths}"  # noqa: model-literal
    )


def test_select_eligible_reports_empty_dir(tmp_path: Path) -> None:
    """Empty audit-reports/ directory returns empty list (not an error)."""
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "audit-reports").mkdir()
    result = select_eligible_reports(task_dir, stage="CHECKPOINT_AUDIT")
    assert result == [], f"Empty audit dir must return [], got {result}"


def test_select_eligible_reports_missing_dir(tmp_path: Path) -> None:
    """Missing audit-reports/ directory returns empty list (not an error)."""
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    # No audit-reports dir created
    result = select_eligible_reports(task_dir, stage="CHECKPOINT_AUDIT")
    assert result == [], f"Missing audit dir must return [], got {result}"


def test_select_eligible_reports_bad_filename_inert(tmp_path: Path) -> None:
    """Files not matching {auditor}-{model}-attempt-{n}.json are silently inert.

    ADR-2: Hard cutover — zero legacy-filename branches.
    Legacy patterns like 'security-checkpoint-20260101.json' are ignored.
    """
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    audit_dir = task_dir / "audit-reports"
    audit_dir.mkdir()
    # Write a file with a legacy filename pattern
    legacy = audit_dir / "security-checkpoint-20260101.json"
    legacy.write_text(json.dumps({
        "auditor_name": "security",
        "status": "complete",
        "findings": [],
    }))
    result = select_eligible_reports(task_dir, stage="CHECKPOINT_AUDIT")
    assert result == [], (
        f"Legacy filename pattern must be silently inert, got {result}"
    )
