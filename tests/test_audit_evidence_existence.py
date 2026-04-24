"""Tests for AC 10: _parse_audit_report evidence.files_inspected existence check.

Covers ``_parse_audit_report(report_path, root)`` in ``hooks/ctl.py``:

  When ``root`` is provided, every entry in ``evidence.files_inspected`` must
  exist on disk.  Entries containing ``*``, ``?``, or ``[`` are treated as
  glob patterns; all others are treated as literal relative paths.

  The check runs on both the empty-findings branch and the non-empty-findings
  branch.

Tests:
  a. test_literal_path_present_no_raise
  b. test_literal_path_absent_raises_do_not_exist
  c. test_glob_matching_real_file_no_raise
  d. test_glob_matching_nothing_raises
  e. test_mixed_list_one_missing_raise_names_missing_entry
  f. test_question_mark_triggers_glob_semantics
  g. test_bracket_triggers_glob_semantics
  h. test_check_runs_in_empty_findings_branch
  i. test_check_runs_in_nonempty_findings_branch
  j. test_empty_files_inspected_no_raise
  k. test_error_message_contains_report_path
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
except Exception as exc:  # pragma: no cover
    pytest.skip(
        f"hooks/ctl.py could not be imported for audit-evidence tests: {exc}",
        allow_module_level=True,
    )

if not hasattr(ctl, "_parse_audit_report"):
    pytest.skip(
        "ctl._parse_audit_report not present",
        allow_module_level=True,
    )

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_report(
    tmp_path: Path,
    *,
    files_inspected: list[str],
    patterns_checked: list[str] | None = None,
    findings: list[dict] | None = None,
    name: str = "audit.json",
) -> Path:
    """Write a minimal valid audit report JSON and return its path.

    When findings is empty (or None) the empty-findings branch is exercised,
    which requires non-empty evidence fields.  When findings is non-empty the
    non-empty-findings branch is exercised.
    """
    if findings is None:
        findings = []
    if patterns_checked is None:
        # Default to a non-empty list so the empty-findings branch doesn't
        # raise on the *patterns_checked* gate.
        patterns_checked = ["pattern-placeholder"]

    data: dict = {
        "findings": findings,
        "evidence": {
            "files_inspected": files_inspected,
            "patterns_checked": patterns_checked,
        },
    }
    report = tmp_path / name
    report.write_text(json.dumps(data))
    return report


def _make_nonempty_report(
    tmp_path: Path,
    *,
    files_inspected: list[str],
    name: str = "audit.json",
) -> Path:
    """Report with one non-blocking finding — exercises non-empty branch."""
    return _make_report(
        tmp_path,
        files_inspected=files_inspected,
        findings=[{"id": "F1", "title": "minor", "blocking": False}],
        name=name,
    )


# ---------------------------------------------------------------------------
# (a) literal path exists → no raise
# ---------------------------------------------------------------------------

def test_literal_path_present_no_raise(tmp_path: Path) -> None:
    real_file = tmp_path / "src" / "foo.py"
    real_file.parent.mkdir(parents=True)
    real_file.write_text("# present")

    report = _make_report(tmp_path, files_inspected=["src/foo.py"])

    result = ctl._parse_audit_report(report, root=tmp_path)
    assert isinstance(result, tuple) and len(result) == 2


# ---------------------------------------------------------------------------
# (b) literal path missing → ValueError with 'do not exist'
# ---------------------------------------------------------------------------

def test_literal_path_absent_raises_do_not_exist(tmp_path: Path) -> None:
    report = _make_report(tmp_path, files_inspected=["src/missing.py"])

    with pytest.raises(ValueError, match="do not exist"):
        ctl._parse_audit_report(report, root=tmp_path)


# ---------------------------------------------------------------------------
# (c) glob (* in path) matching a real file → no raise
# ---------------------------------------------------------------------------

def test_glob_matching_real_file_no_raise(tmp_path: Path) -> None:
    real_file = tmp_path / "src" / "impl.py"
    real_file.parent.mkdir(parents=True)
    real_file.write_text("# real")

    report = _make_report(tmp_path, files_inspected=["src/*.py"])

    result = ctl._parse_audit_report(report, root=tmp_path)
    assert isinstance(result, tuple) and len(result) == 2


# ---------------------------------------------------------------------------
# (d) glob matching nothing → ValueError
# ---------------------------------------------------------------------------

def test_glob_matching_nothing_raises(tmp_path: Path) -> None:
    report = _make_report(tmp_path, files_inspected=["src/*.py"])
    # No files exist under tmp_path/src/

    with pytest.raises(ValueError, match="do not exist"):
        ctl._parse_audit_report(report, root=tmp_path)


# ---------------------------------------------------------------------------
# (e) mixed list with one missing → ValueError names that entry
# ---------------------------------------------------------------------------

def test_mixed_list_one_missing_raise_names_missing_entry(tmp_path: Path) -> None:
    present = tmp_path / "present.py"
    present.write_text("# here")

    missing_entry = "absolutely_not_here.py"
    report = _make_report(
        tmp_path,
        files_inspected=["present.py", missing_entry],
    )

    with pytest.raises(ValueError) as exc_info:
        ctl._parse_audit_report(report, root=tmp_path)

    assert missing_entry in str(exc_info.value), (
        f"ValueError must name the missing entry '{missing_entry}'; "
        f"got: {exc_info.value!r}"
    )


# ---------------------------------------------------------------------------
# (f) ? in path triggers glob semantics
# ---------------------------------------------------------------------------

def test_question_mark_triggers_glob_semantics(tmp_path: Path) -> None:
    # ? should be treated as a glob wildcard.
    # Case 1: no match → raises
    report_miss = _make_report(
        tmp_path, files_inspected=["src/foo?.py"], name="miss.json"
    )
    with pytest.raises(ValueError, match="do not exist"):
        ctl._parse_audit_report(report_miss, root=tmp_path)

    # Case 2: a matching file exists → no raise
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "fooX.py").write_text("# x")
    report_hit = _make_report(
        tmp_path, files_inspected=["src/foo?.py"], name="hit.json"
    )
    ctl._parse_audit_report(report_hit, root=tmp_path)


# ---------------------------------------------------------------------------
# (g) [ in path triggers glob semantics
# ---------------------------------------------------------------------------

def test_bracket_triggers_glob_semantics(tmp_path: Path) -> None:
    # [ should be treated as a glob wildcard.
    # Case 1: no match → raises
    report_miss = _make_report(
        tmp_path, files_inspected=["src/[abc].py"], name="bmiss.json"
    )
    with pytest.raises(ValueError, match="do not exist"):
        ctl._parse_audit_report(report_miss, root=tmp_path)

    # Case 2: matching file exists → no raise
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "a.py").write_text("# a")
    report_hit = _make_report(
        tmp_path, files_inspected=["src/[abc].py"], name="bhit.json"
    )
    ctl._parse_audit_report(report_hit, root=tmp_path)


# ---------------------------------------------------------------------------
# (h) check runs in empty-findings branch
# ---------------------------------------------------------------------------

def test_check_runs_in_empty_findings_branch(tmp_path: Path) -> None:
    """The file-existence check must run even when findings == [] (passed report)."""
    # Empty findings → the "evidence gate" also fires, so we need
    # files_inspected to be non-empty (already satisfied) and patterns_checked.
    # We deliberately omit the physical file so the existence check fires.
    missing = "nonexistent_in_empty_branch.py"
    report = _make_report(tmp_path, files_inspected=[missing])

    with pytest.raises(ValueError, match="do not exist"):
        ctl._parse_audit_report(report, root=tmp_path)


# ---------------------------------------------------------------------------
# (i) check runs in non-empty-findings branch
# ---------------------------------------------------------------------------

def test_check_runs_in_nonempty_findings_branch(tmp_path: Path) -> None:
    """The file-existence check must also run when findings is non-empty."""
    missing = "nonexistent_in_nonempty_branch.py"
    report = _make_nonempty_report(tmp_path, files_inspected=[missing])

    with pytest.raises(ValueError, match="do not exist"):
        ctl._parse_audit_report(report, root=tmp_path)


# ---------------------------------------------------------------------------
# (j) empty files_inspected list → no raise (nothing to check)
# ---------------------------------------------------------------------------

def test_empty_files_inspected_no_raise(tmp_path: Path) -> None:
    """When files_inspected is empty the existence gate has nothing to check.

    Note: this report also has non-empty findings so the evidence-completeness
    gate (which rejects an empty list on the *empty-findings* branch) is not
    triggered.
    """
    report = _make_nonempty_report(tmp_path, files_inspected=[])

    result = ctl._parse_audit_report(report, root=tmp_path)
    assert isinstance(result, tuple) and len(result) == 2


# ---------------------------------------------------------------------------
# (k) error message references the audit report path
# ---------------------------------------------------------------------------

def test_error_message_contains_report_path(tmp_path: Path) -> None:
    missing = "ghost_file.py"
    report = _make_nonempty_report(tmp_path, files_inspected=[missing])

    with pytest.raises(ValueError) as exc_info:
        ctl._parse_audit_report(report, root=tmp_path)

    assert str(report) in str(exc_info.value), (
        f"ValueError must reference the report path {report}; "
        f"got: {exc_info.value!r}"
    )
