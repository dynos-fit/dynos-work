"""Tests for the proactive autofix scanner."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from dynoproactive import (
    _compute_file_scores,
    _dedup_finding,
    _description_hash,
    _detect_dead_code,
    _detect_syntax_errors,
    _load_findings,
    _load_scan_coverage,
    _make_finding,
    _process_finding,
    _save_scan_coverage,
    VALID_CATEGORIES,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """Create a minimal git-initialized project with some Python files."""
    import subprocess
    (tmp_path / ".dynos").mkdir()
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)

    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "good.py").write_text("import os\nprint(os.getcwd())\n")
    (hooks / "bad_import.py").write_text("import os\nimport json\nprint(os.getcwd())\n")  # json unused
    (hooks / "syntax_err.py").write_text("def foo(\n")  # syntax error

    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)
    return tmp_path


# ---------------------------------------------------------------------------
# VALID_CATEGORIES
# ---------------------------------------------------------------------------

class TestValidCategories:
    def test_includes_all_scanner_categories(self) -> None:
        expected = {"recurring-audit", "dependency-vuln", "dead-code",
                    "architectural-drift", "syntax-error", "llm-review"}
        assert VALID_CATEGORIES == expected


# ---------------------------------------------------------------------------
# _make_finding
# ---------------------------------------------------------------------------

class TestMakeFinding:
    def test_creates_finding_with_required_fields(self) -> None:
        f = _make_finding(
            finding_id="test-001",
            severity="medium",
            category="dead-code",
            description="test finding",
            evidence={"file": "foo.py"},
        )
        assert f["finding_id"] == "test-001"
        assert f["severity"] == "medium"
        assert f["category"] == "dead-code"
        assert f["status"] == "new"
        assert f["attempt_count"] == 0
        assert f["pr_number"] is None
        assert f["issue_number"] is None

    def test_finding_has_timestamps(self) -> None:
        f = _make_finding("id", "low", "dead-code", "desc", {})
        assert "found_at" in f
        assert f["processed_at"] is None


# ---------------------------------------------------------------------------
# _dedup_finding
# ---------------------------------------------------------------------------

class TestDedupFinding:
    def test_exact_id_match_skips(self) -> None:
        finding = _make_finding("dup-1", "low", "dead-code", "desc", {})
        existing = [{"finding_id": "dup-1", "status": "fixed"}]
        reason = _dedup_finding(finding, existing)
        assert reason is not None
        assert "exact finding_id" in reason

    def test_no_match_returns_none(self) -> None:
        finding = _make_finding("new-1", "low", "dead-code", "desc", {})
        existing = [{"finding_id": "other-1", "status": "fixed",
                     "category": "syntax-error", "description": "different",
                     "evidence": {}}]
        reason = _dedup_finding(finding, existing)
        assert reason is None

    def test_permanently_failed_skips(self) -> None:
        finding = _make_finding("perm-1", "low", "dead-code", "desc", {})
        existing = [{"finding_id": "perm-1", "status": "permanently_failed"}]
        reason = _dedup_finding(finding, existing)
        assert reason is not None


# ---------------------------------------------------------------------------
# _detect_syntax_errors
# ---------------------------------------------------------------------------

class TestDetectSyntaxErrors:
    def test_finds_syntax_error(self, tmp_project: Path) -> None:
        findings = _detect_syntax_errors(tmp_project)
        assert len(findings) == 1
        assert findings[0]["category"] == "syntax-error"
        assert "syntax_err.py" in findings[0]["description"]

    def test_clean_files_no_findings(self, tmp_path: Path) -> None:
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        (hooks / "clean.py").write_text("x = 1\n")
        findings = _detect_syntax_errors(tmp_path)
        assert findings == []

    def test_no_hooks_dir_returns_empty(self, tmp_path: Path) -> None:
        findings = _detect_syntax_errors(tmp_path)
        assert findings == []


# ---------------------------------------------------------------------------
# _detect_dead_code
# ---------------------------------------------------------------------------

class TestDetectDeadCode:
    def test_finds_unused_import(self, tmp_project: Path) -> None:
        findings = _detect_dead_code(tmp_project)
        unused = [f for f in findings if "unused" in f["description"].lower()
                  and "bad_import" in f.get("evidence", {}).get("file", "")]
        assert len(unused) >= 1
        assert "json" in unused[0]["evidence"].get("unused_imports", [])

    def test_no_hooks_dir_returns_empty(self, tmp_path: Path) -> None:
        findings = _detect_dead_code(tmp_path)
        assert findings == []


# ---------------------------------------------------------------------------
# Scan coverage
# ---------------------------------------------------------------------------

class TestScanCoverage:
    def test_load_empty(self, tmp_path: Path) -> None:
        coverage = _load_scan_coverage(tmp_path)
        assert coverage == {"files": {}}

    def test_save_and_load(self, tmp_path: Path) -> None:
        (tmp_path / ".dynos").mkdir()
        coverage = {"files": {"foo.py": {"last_scanned_at": "2026-01-01T00:00:00Z", "last_result": "clean"}}}
        _save_scan_coverage(tmp_path, coverage)
        loaded = _load_scan_coverage(tmp_path)
        assert loaded["files"]["foo.py"]["last_result"] == "clean"


# ---------------------------------------------------------------------------
# File scoring
# ---------------------------------------------------------------------------

class TestFileScoring:
    def test_scores_all_python_files(self, tmp_project: Path) -> None:
        coverage = _load_scan_coverage(tmp_project)
        scores = _compute_file_scores(tmp_project, coverage)
        file_names = [str(f.name) for f, _ in scores]
        assert "good.py" in file_names
        assert "bad_import.py" in file_names

    def test_recently_scanned_gets_cooldown(self, tmp_project: Path) -> None:
        (tmp_project / ".dynos").mkdir(exist_ok=True)
        from dynoproactive import now_iso
        coverage = {"files": {"hooks/good.py": {"last_scanned_at": now_iso(), "last_result": "clean"}}}
        _save_scan_coverage(tmp_project, coverage)
        scores = _compute_file_scores(tmp_project, coverage)
        good_score = next((s for f, s in scores if f.name == "good.py"), None)
        bad_score = next((s for f, s in scores if f.name == "bad_import.py"), None)
        assert good_score is not None and bad_score is not None
        assert good_score < bad_score  # good.py should be deprioritized


# ---------------------------------------------------------------------------
# Findings persistence
# ---------------------------------------------------------------------------

class TestFindingsPersistence:
    def test_load_empty(self, tmp_path: Path) -> None:
        findings = _load_findings(tmp_path)
        assert findings == []

    def test_description_hash_deterministic(self) -> None:
        h1 = _description_hash("same description")
        h2 = _description_hash("same description")
        h3 = _description_hash("different description")
        assert h1 == h2
        assert h1 != h3


# ---------------------------------------------------------------------------
# Risk routing
# ---------------------------------------------------------------------------

class TestProcessFindingRouting:
    def test_all_severities_use_autofix(self, tmp_project: Path) -> None:
        """All findings go through autofix regardless of severity."""
        for sev in ("low", "medium", "high", "critical"):
            finding = _make_finding(f"sev-{sev}", sev, "dead-code", "desc", {})
            with patch("dynoproactive._autofix_finding") as autofix_mock:
                autofix_mock.side_effect = lambda f, root: {**f, "status": "fixed"}
                result = _process_finding(finding, tmp_project)
            autofix_mock.assert_called_once()
            assert result["status"] == "fixed"

    def test_recurring_audit_opens_issue_not_autofix(self, tmp_project: Path) -> None:
        """Recurring audit findings are not fixable code — they open issues."""
        finding = _make_finding("recurring-1", "medium", "recurring-audit", "desc", {})
        with patch("dynoproactive._open_github_issue") as issue_mock, \
             patch("dynoproactive._autofix_finding") as autofix_mock:
            issue_mock.side_effect = lambda f: {**f, "status": "issue-opened"}
            result = _process_finding(finding, tmp_project)
        issue_mock.assert_called_once()
        autofix_mock.assert_not_called()
        assert result["status"] == "issue-opened"

    def test_max_attempts_permanently_fails(self, tmp_project: Path) -> None:
        finding = _make_finding("retry-1", "low", "dead-code", "desc", {})
        finding["attempt_count"] = 2  # already at max
        result = _process_finding(finding, tmp_project)
        assert result["status"] == "permanently_failed"
        assert result["suppressed_until"] is not None
