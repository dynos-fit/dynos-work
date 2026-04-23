"""Tests for task-20260423-001 AC23: auditor evidence validation.

Covers ``_parse_audit_report`` in ``hooks/ctl.py``:

  (a) A report with ``findings: []`` and empty ``evidence`` raises
      ``ValueError`` (cannot self-declare passed without evidence).
  (b) A report with ``findings: []`` and populated
      ``evidence.files_inspected`` + ``evidence.patterns_checked`` parses
      to ``(0, 0)``.
  (c) A report with ``findings: [{"blocking": True}]`` parses to
      ``(1, 1)`` regardless of evidence presence (non-empty findings
      short-circuit the evidence gate).

These tests are deliberately written against the public behavior of
``_parse_audit_report`` — no mocks, no inspection of internal state. A
regression that silently accepts a passed-with-no-evidence report must
fail (a) below.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = ROOT / "hooks"
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))


try:
    ctl = importlib.import_module("ctl")
except Exception as exc:  # pragma: no cover - defensive
    pytest.skip(
        f"hooks/ctl.py could not be imported for auditor-report tests: {exc}",
        allow_module_level=True,
    )

if not hasattr(ctl, "_parse_audit_report"):
    pytest.skip(
        "ctl._parse_audit_report not present (TDD-first)",
        allow_module_level=True,
    )


def _write_report(tmp_path: Path, data: dict) -> Path:
    report_path = tmp_path / "audit-report.json"
    report_path.write_text(json.dumps(data))
    return report_path


# ---------------------------------------------------------------------------
# (a) Empty findings AND empty evidence → raise ValueError
# ---------------------------------------------------------------------------
def test_empty_findings_and_empty_evidence_raises(tmp_path: Path) -> None:
    report = _write_report(
        tmp_path,
        {
            "auditor_name": "security-auditor",
            "status": "passed",
            "findings": [],
            "evidence": {},
        },
    )

    with pytest.raises(ValueError):
        ctl._parse_audit_report(report)


# ---------------------------------------------------------------------------
# (a-variant) findings key omitted entirely, empty evidence → also raises
# ---------------------------------------------------------------------------
def test_missing_findings_with_empty_evidence_raises(tmp_path: Path) -> None:
    report = _write_report(
        tmp_path,
        {
            "auditor_name": "security-auditor",
            "status": "passed",
            # no findings key
            "evidence": {},
        },
    )

    with pytest.raises(ValueError):
        ctl._parse_audit_report(report)


# ---------------------------------------------------------------------------
# (a-variant) findings: [] + evidence with empty lists → raises
# ---------------------------------------------------------------------------
def test_empty_findings_with_empty_lists_raises(tmp_path: Path) -> None:
    report = _write_report(
        tmp_path,
        {
            "auditor_name": "security-auditor",
            "status": "passed",
            "findings": [],
            "evidence": {"files_inspected": [], "patterns_checked": []},
        },
    )

    with pytest.raises(ValueError):
        ctl._parse_audit_report(report)


# ---------------------------------------------------------------------------
# (b) Empty findings + populated evidence → (0, 0)
# ---------------------------------------------------------------------------
def test_empty_findings_with_populated_evidence_parses_zero_zero(
    tmp_path: Path,
) -> None:
    report = _write_report(
        tmp_path,
        {
            "auditor_name": "security-auditor",
            "status": "passed",
            "findings": [],
            "evidence": {
                "files_inspected": ["hooks/ctl.py", "hooks/lib_log.py"],
                "patterns_checked": ["subprocess.run", "os.system"],
            },
        },
    )

    result = ctl._parse_audit_report(report)
    assert result == (0, 0), (
        f"findings=[] with populated evidence must parse to (0, 0); got {result!r}"
    )


# ---------------------------------------------------------------------------
# (c) Non-empty findings → (1, 1) regardless of evidence presence
# ---------------------------------------------------------------------------
def test_blocking_finding_parses_to_one_one_without_evidence(tmp_path: Path) -> None:
    report = _write_report(
        tmp_path,
        {
            "auditor_name": "security-auditor",
            "status": "failed",
            "findings": [{"id": "SEC-1", "blocking": True, "severity": "high"}],
            # Evidence omitted entirely — the evidence gate must NOT fire
            # when findings are non-empty (per AC6: "Reports with
            # findings: [...] non-empty are unaffected by this check").
        },
    )

    result = ctl._parse_audit_report(report)
    assert result == (1, 1), (
        f"a single blocking finding must parse to (1, 1) regardless of evidence; "
        f"got {result!r}"
    )


def test_blocking_finding_parses_to_one_one_with_evidence(tmp_path: Path) -> None:
    report = _write_report(
        tmp_path,
        {
            "auditor_name": "security-auditor",
            "status": "failed",
            "findings": [{"id": "SEC-2", "blocking": True, "severity": "high"}],
            "evidence": {
                "files_inspected": ["hooks/ctl.py"],
                "patterns_checked": ["subprocess"],
            },
        },
    )

    result = ctl._parse_audit_report(report)
    assert result == (1, 1), (
        f"blocking finding + evidence must still parse to (1, 1); got {result!r}"
    )
