"""Tests for symlink exclusion in _collect_latest_audit_reports (task-20260504-003).

Covers AC 7 (sec-r2-002):
  _collect_latest_audit_reports must skip any .json entry that is a symlink.
  A symlink with a .json extension inside audit-reports/ must not appear in
  the return value, preventing path-injection via crafted symlink names.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "hooks"))

from ctl import _collect_latest_audit_reports  # noqa: E402


def _write_real_report(audit_dir: Path, filename: str, auditor_name: str) -> Path:
    """Write a real JSON audit report and return its path."""
    p = audit_dir / filename
    p.write_text(json.dumps({"auditor_name": auditor_name, "findings": []}))
    return p


def test_symlink_excluded_from_collection(tmp_path: Path) -> None:
    """A symlink .json file in audit-reports/ must not appear in the collected dict (AC 7).

    The real file is collected; the symlink is silently skipped.
    """
    audit_dir = tmp_path / "audit-reports"
    audit_dir.mkdir()

    # Real checkpoint file for security-auditor
    real_file = _write_real_report(
        audit_dir,
        "security-auditor-checkpoint-2026-05-01T00:00:00.json",
        auditor_name="security-auditor",
    )

    # Symlink with a .json extension — injection attempt
    symlink_target = tmp_path / "fake-injected-report.json"
    symlink_target.write_text(
        json.dumps({"auditor_name": "spec-completion-auditor", "findings": []})
    )
    symlink_path = audit_dir / "spec-completion-auditor-checkpoint-2026-05-02T00:00:00.json"
    symlink_path.symlink_to(symlink_target)

    result = _collect_latest_audit_reports(audit_dir)

    # The symlink must not be among the winning paths.
    collected_paths = set(result.values())
    assert symlink_path not in collected_paths, (
        "symlink must be excluded from _collect_latest_audit_reports result; "
        f"collected paths: {collected_paths}"
    )
    assert symlink_path.resolve() not in collected_paths, (
        "resolved symlink target must also not appear in result via indirect collection"
    )

    # The real file must still be collected.
    assert real_file in collected_paths or real_file.resolve() in collected_paths, (
        "real (non-symlink) report must still be collected; "
        f"collected paths: {collected_paths}"
    )

    # Exactly one auditor key returned (the real file's auditor only).
    assert len(result) == 1, (
        f"expected exactly 1 auditor in result, got {len(result)}: {list(result.keys())}"
    )
    assert "security-auditor" in result, (
        f"security-auditor key must be present; keys: {list(result.keys())}"
    )


def test_symlink_only_dir_returns_empty(tmp_path: Path) -> None:
    """When audit-reports/ contains only symlinks, the result is empty.

    This is the all-symlink edge case: no real reports → empty dict.
    """
    audit_dir = tmp_path / "audit-reports"
    audit_dir.mkdir()

    symlink_target = tmp_path / "external.json"
    symlink_target.write_text(
        json.dumps({"auditor_name": "security-auditor", "findings": []})
    )
    symlink_path = audit_dir / "security-auditor-checkpoint-2026-05-01T00:00:00.json"
    symlink_path.symlink_to(symlink_target)

    result = _collect_latest_audit_reports(audit_dir)

    assert result == {}, (
        "all-symlink audit-reports/ must yield empty dict; "
        f"got: {result}"
    )


def test_real_files_collected_normally_without_symlinks(tmp_path: Path) -> None:
    """Baseline: real files continue to be collected correctly when no symlinks are present."""
    audit_dir = tmp_path / "audit-reports"
    audit_dir.mkdir()

    _write_real_report(
        audit_dir,
        "security-auditor-checkpoint-2026-05-01T00:00:00.json",
        auditor_name="security-auditor",
    )
    _write_real_report(
        audit_dir,
        "code-quality-auditor-checkpoint-2026-05-01T00:00:00.json",
        auditor_name="code-quality-auditor",
    )

    result = _collect_latest_audit_reports(audit_dir)

    assert len(result) == 2, f"expected 2 auditors; got {len(result)}: {list(result.keys())}"
    assert "security-auditor" in result
    assert "code-quality-auditor" in result
