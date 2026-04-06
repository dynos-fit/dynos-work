"""Tests for the proactive autofix scanner."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from dynoproactive import (
    _check_category_health,
    _classify_fixability,
    _compute_pr_quality_score,
    _cleanup_merged_branches,
    _compute_file_scores,
    _dedup_finding,
    _description_hash,
    _detect_dead_code,
    _detect_syntax_errors,
    _load_autofix_policy,
    _load_findings,
    _load_scan_coverage,
    _make_finding,
    _process_finding,
    _rate_limit_reason,
    _recompute_category_confidence,
    _prune_findings,
    _save_scan_coverage,
    _suppression_reason,
    _sync_outcomes,
    _verify_fix,
    VALID_CATEGORIES,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a minimal git-initialized project with some Python files."""
    import subprocess
    (tmp_path / ".dynos").mkdir()
    monkeypatch.setenv("DYNOS_HOME", str(tmp_path / ".dynos-home"))
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
    def test_low_confidence_category_routes_to_issue(self, tmp_project: Path) -> None:
        finding = _make_finding("dc-low", "low", "dead-code", "desc", {})
        policy = _load_autofix_policy(tmp_project)
        policy["categories"]["dead-code"]["confidence"] = 0.2
        with patch("dynoproactive._open_github_issue") as issue_mock, \
             patch("dynoproactive._autofix_finding") as autofix_mock:
            issue_mock.side_effect = lambda f, root, policy=None: {**f, "status": "issue-opened"}
            result = _process_finding(finding, tmp_project, policy)
        issue_mock.assert_called_once()
        autofix_mock.assert_not_called()
        assert result["status"] == "issue-opened"

    def test_autofixable_dead_code_uses_autofix(self, tmp_project: Path) -> None:
        finding = _make_finding("dc-ok", "low", "dead-code", "desc", {})
        policy = _load_autofix_policy(tmp_project)
        with patch("dynoproactive._autofix_finding") as autofix_mock:
            autofix_mock.side_effect = lambda f, root, policy=None: {**f, "status": "fixed"}
            result = _process_finding(finding, tmp_project, policy)
        autofix_mock.assert_called_once()
        assert result["status"] == "fixed"

    def test_recurring_audit_opens_issue_not_autofix(self, tmp_project: Path) -> None:
        """Recurring audit findings are not fixable code — they open issues."""
        finding = _make_finding("recurring-1", "medium", "recurring-audit", "desc", {})
        with patch("dynoproactive._open_github_issue") as issue_mock, \
             patch("dynoproactive._autofix_finding") as autofix_mock:
            issue_mock.side_effect = lambda f, root, policy=None: {**f, "status": "issue-opened"}
            result = _process_finding(finding, tmp_project)
        issue_mock.assert_called_once()
        autofix_mock.assert_not_called()
        assert result["status"] == "issue-opened"

    def test_max_attempts_falls_back_to_issue(self, tmp_project: Path) -> None:
        finding = _make_finding("retry-1", "low", "dead-code", "desc", {})
        finding["attempt_count"] = 2  # already at max
        with patch("dynoproactive._open_github_issue") as issue_mock:
            issue_mock.side_effect = lambda f, root, policy=None: {**f, "status": "issue-opened"}
            result = _process_finding(finding, tmp_project)
        issue_mock.assert_called_once()
        assert result["rollout_mode"] == "issue-only"

    def test_suppression_policy_skips_finding(self, tmp_project: Path) -> None:
        finding = _make_finding("sup-1", "low", "dead-code", "desc", {"file": "hooks/good.py"})
        policy = _load_autofix_policy(tmp_project)
        policy["suppressions"] = [{"category": "dead-code", "path_prefix": "hooks/", "reason": "quiet path"}]
        result = _process_finding(finding, tmp_project, policy)
        assert result["status"] == "suppressed-policy"
        assert result["suppression_reason"] == "quiet path"

    def test_rate_limit_blocks_autofix(self, tmp_project: Path) -> None:
        finding = _make_finding("rl-1", "low", "dead-code", "desc", {})
        policy = _load_autofix_policy(tmp_project)
        policy["max_prs_per_day"] = 1
        existing = [_make_finding("done-1", "low", "dead-code", "desc", {})]
        existing[0]["pr_number"] = 10
        existing[0]["processed_at"] = datetime.now(timezone.utc).isoformat()
        result = _process_finding(finding, tmp_project, policy, existing)
        assert result["status"] == "rate-limited"

    def test_high_severity_llm_review_goes_to_autofix(self, tmp_project: Path) -> None:
        """High-severity llm-review findings route to autofix, not issue."""
        finding = _make_finding("llm-high-1", "high", "llm-review", "critical bug", {})
        with patch("dynoproactive._open_github_issue") as issue_mock, \
             patch("dynoproactive._autofix_finding") as autofix_mock:
            autofix_mock.side_effect = lambda f, root, policy=None, **kw: {**f, "status": "fixed"}
            result = _process_finding(finding, tmp_project, _load_autofix_policy(tmp_project))
        autofix_mock.assert_called_once()
        issue_mock.assert_not_called()
        assert result["fixability"] == "likely-safe"

    def test_dependency_vuln_opens_issue(self, tmp_project: Path) -> None:
        """Dependency vulns are review-only, route to issue."""
        finding = _make_finding("dep-1", "medium", "dependency-vuln", "vuln", {})
        with patch("dynoproactive._open_github_issue") as issue_mock, \
             patch("dynoproactive._autofix_finding") as autofix_mock:
            issue_mock.side_effect = lambda f, root, policy=None: {**f, "status": "issue-opened"}
            result = _process_finding(finding, tmp_project, _load_autofix_policy(tmp_project))
        issue_mock.assert_called_once()
        autofix_mock.assert_not_called()
        assert result["fixability"] == "review-only"

    def test_architectural_drift_opens_issue(self, tmp_project: Path) -> None:
        """Architectural drift is review-only, route to issue."""
        finding = _make_finding("drift-1", "medium", "architectural-drift", "drift", {})
        with patch("dynoproactive._open_github_issue") as issue_mock, \
             patch("dynoproactive._autofix_finding") as autofix_mock:
            issue_mock.side_effect = lambda f, root, policy=None: {**f, "status": "issue-opened"}
            result = _process_finding(finding, tmp_project, _load_autofix_policy(tmp_project))
        issue_mock.assert_called_once()
        autofix_mock.assert_not_called()
        assert result["fixability"] == "review-only"


# ---------------------------------------------------------------------------
# Fixability classification
# ---------------------------------------------------------------------------

class TestClassifyFixability:
    def test_syntax_error_is_deterministic(self) -> None:
        finding = _make_finding("se-1", "medium", "syntax-error", "desc", {})
        assert _classify_fixability(finding) == "deterministic"

    def test_dead_code_unused_imports_is_deterministic(self) -> None:
        finding = _make_finding("dc-1", "low", "dead-code", "desc", {"unused_imports": ["json"]})
        assert _classify_fixability(finding) == "deterministic"

    def test_dead_code_unreferenced_func_is_likely_safe(self) -> None:
        finding = _make_finding("dc-2", "low", "dead-code", "desc", {"function": "foo"})
        assert _classify_fixability(finding) == "likely-safe"

    def test_llm_review_low_is_likely_safe(self) -> None:
        finding = _make_finding("llm-1", "low", "llm-review", "desc", {})
        assert _classify_fixability(finding) == "likely-safe"

    def test_llm_review_medium_is_likely_safe(self) -> None:
        finding = _make_finding("llm-2", "medium", "llm-review", "desc", {})
        assert _classify_fixability(finding) == "likely-safe"

    def test_llm_review_high_is_likely_safe(self) -> None:
        finding = _make_finding("llm-3", "high", "llm-review", "desc", {})
        assert _classify_fixability(finding) == "likely-safe"

    def test_llm_review_critical_is_likely_safe(self) -> None:
        finding = _make_finding("llm-4", "critical", "llm-review", "desc", {})
        assert _classify_fixability(finding) == "likely-safe"

    def test_dependency_vuln_is_review_only(self) -> None:
        finding = _make_finding("dv-1", "medium", "dependency-vuln", "desc", {})
        assert _classify_fixability(finding) == "review-only"

    def test_architectural_drift_is_review_only(self) -> None:
        finding = _make_finding("ad-1", "medium", "architectural-drift", "desc", {})
        assert _classify_fixability(finding) == "review-only"

    def test_recurring_audit_is_review_only(self) -> None:
        finding = _make_finding("ra-1", "medium", "recurring-audit", "desc", {})
        assert _classify_fixability(finding) == "review-only"


# ---------------------------------------------------------------------------
# Post-fix verification
# ---------------------------------------------------------------------------

class TestVerifyFix:
    def test_valid_fix_passes(self, tmp_project: Path) -> None:
        """A small, valid change in scope passes verification."""
        import subprocess
        # Create a branch with a valid change
        worktree = str(tmp_project)
        (tmp_project / "hooks" / "good.py").write_text("import os\nprint(os.getcwd())\nx = 1\n")
        subprocess.run(["git", "add", "-A"], cwd=worktree, capture_output=True)
        subprocess.run(["git", "commit", "-m", "test change"], cwd=worktree, capture_output=True)
        finding = _make_finding("vf-1", "low", "dead-code", "desc", {"file": "hooks/good.py"})
        ok, reason, report = _verify_fix(tmp_project, worktree, finding)
        assert ok is True
        assert reason == ""
        assert "hooks/good.py" in report["changed_files"]

    def test_syntax_error_fails(self, tmp_project: Path) -> None:
        """A change that introduces a syntax error fails verification."""
        import subprocess
        worktree = str(tmp_project)
        (tmp_project / "hooks" / "good.py").write_text("def foo(\n")
        subprocess.run(["git", "add", "-A"], cwd=worktree, capture_output=True)
        subprocess.run(["git", "commit", "-m", "break syntax"], cwd=worktree, capture_output=True)
        finding = _make_finding("vf-2", "low", "dead-code", "desc", {"file": "hooks/good.py"})
        ok, reason, _report = _verify_fix(tmp_project, worktree, finding)
        assert ok is False
        assert "syntax error" in reason

    def test_dependency_file_change_fails_for_non_dependency_finding(self, tmp_project: Path) -> None:
        import subprocess
        worktree = str(tmp_project)
        (tmp_project / "requirements.txt").write_text("pytest==8.0.0\n")
        subprocess.run(["git", "add", "-A"], cwd=worktree, capture_output=True)
        subprocess.run(["git", "commit", "-m", "deps"], cwd=worktree, capture_output=True)
        finding = _make_finding("vf-3", "low", "dead-code", "desc", {"file": "hooks/good.py"})
        ok, reason, _report = _verify_fix(tmp_project, worktree, finding)
        assert ok is False
        assert "dependency file change" in reason


class TestPolicyHelpers:
    def test_suppression_reason_matches_category_and_path(self, tmp_project: Path) -> None:
        policy = _load_autofix_policy(tmp_project)
        policy["suppressions"] = [{"category": "dead-code", "path_prefix": "hooks/", "reason": "noise"}]
        finding = _make_finding("sup-2", "low", "dead-code", "desc", {"file": "hooks/good.py"})
        assert _suppression_reason(finding, policy) == "noise"

    def test_rate_limit_reason_detects_open_pr_cap(self, tmp_project: Path) -> None:
        policy = _load_autofix_policy(tmp_project)
        policy["max_open_prs"] = 1
        finding = _make_finding("open-1", "low", "dead-code", "desc", {})
        finding["pr_number"] = 11
        finding["merge_outcome"] = "open"
        assert "max_open_prs" in _rate_limit_reason(policy, [finding])

    def test_confidence_recompute_rewards_merged_history(self, tmp_project: Path) -> None:
        policy = _load_autofix_policy(tmp_project)
        policy["categories"]["dead-code"]["stats"]["merged"] = 5
        policy["categories"]["dead-code"]["stats"]["closed_unmerged"] = 0
        updated = _recompute_category_confidence(policy)
        assert updated["categories"]["dead-code"]["confidence"] > 0.8

    def test_pr_quality_score_rewards_small_verified_change(self) -> None:
        score = _compute_pr_quality_score({
            "changed_files": ["hooks/good.py"],
            "python_files_checked": ["hooks/good.py"],
            "targeted_tests": [{"path": "tests/test_good.py", "returncode": 0}],
            "total_changes": 12,
        })
        assert score > 0.8


class TestOutcomeSync:
    def test_sync_outcomes_updates_merge_state_and_metrics(self, tmp_project: Path) -> None:
        findings = [_make_finding("sync-1", "low", "dead-code", "desc", {})]
        findings[0]["pr_number"] = 12
        findings[0]["status"] = "fixed"
        policy = _load_autofix_policy(tmp_project)

        class Result:
            def __init__(self, stdout: str, returncode: int = 0) -> None:
                self.stdout = stdout
                self.returncode = returncode

        def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
            if cmd[:3] == ["gh", "pr", "view"]:
                return Result(json.dumps({
                    "state": "MERGED",
                    "mergedAt": "2026-04-04T12:00:00Z",
                    "closedAt": "2026-04-04T12:00:00Z",
                    "url": "https://example.test/pr/12",
                }))
            if cmd[:3] == ["gh", "issue", "view"]:
                return Result("{}")
            raise AssertionError(f"unexpected command: {cmd}")

        with patch("dynoproactive.shutil.which", return_value="/usr/bin/gh"), \
             patch("dynoproactive.subprocess.run", side_effect=fake_run):
            updated, metrics = _sync_outcomes(tmp_project, findings, policy)

        assert updated[0]["merge_outcome"] == "merged"
        assert metrics["totals"]["merged"] >= 1


# ---------------------------------------------------------------------------
# Dedup ordering
# ---------------------------------------------------------------------------

class TestDedupOrdering:
    def test_permanently_failed_takes_priority_over_generic_match(self) -> None:
        """permanently_failed should be returned instead of generic ID match."""
        finding = _make_finding("pf-1", "low", "dead-code", "desc", {})
        existing = [{"finding_id": "pf-1", "status": "permanently_failed"}]
        reason = _dedup_finding(finding, existing)
        assert reason == "permanently_failed"

    def test_fixed_with_pr_takes_priority(self) -> None:
        """fixed with PR should be returned instead of generic ID match."""
        finding = _make_finding("fx-1", "low", "dead-code", "desc", {})
        existing = [{"finding_id": "fx-1", "status": "fixed", "pr_number": 42}]
        reason = _dedup_finding(finding, existing)
        assert reason == "fixed with merged PR, permanently suppressed"

    def test_generic_match_still_works(self) -> None:
        """Non-special statuses still return the generic match."""
        finding = _make_finding("gm-1", "low", "dead-code", "desc", {})
        existing = [{"finding_id": "gm-1", "status": "failed"}]
        reason = _dedup_finding(finding, existing)
        assert reason is not None
        assert "exact finding_id" in reason


# ---------------------------------------------------------------------------
# Prune findings (Improvement 2)
# ---------------------------------------------------------------------------

class TestPruneFindings:
    def test_removes_old_entries(self) -> None:
        """Entries older than max_age_days are removed."""
        from datetime import datetime, timezone, timedelta
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        recent_date = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        findings = [
            {"status": "new", "found_at": old_date, "description": "old"},
            {"status": "new", "found_at": recent_date, "description": "recent"},
        ]
        result = _prune_findings(findings, max_age_days=30)
        assert len(result) == 1
        assert result[0]["description"] == "recent"

    def test_preserves_fixed_entries(self) -> None:
        """Entries with status 'fixed' are never pruned regardless of age."""
        from datetime import datetime, timezone, timedelta
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        findings = [
            {"status": "fixed", "found_at": old_date, "description": "old fixed"},
            {"status": "new", "found_at": old_date, "description": "old new"},
        ]
        result = _prune_findings(findings, max_age_days=30)
        assert len(result) == 1
        assert result[0]["status"] == "fixed"

    def test_preserves_issue_opened_entries(self) -> None:
        """Entries with status 'issue-opened' are never pruned."""
        from datetime import datetime, timezone, timedelta
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        findings = [
            {"status": "issue-opened", "found_at": old_date, "description": "old issue"},
        ]
        result = _prune_findings(findings, max_age_days=30)
        assert len(result) == 1

    def test_caps_at_max_entries(self) -> None:
        """When over max_entries after age pruning, keeps newest non-preserved."""
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        findings = []
        for i in range(10):
            findings.append({
                "status": "new",
                "found_at": (now - timedelta(hours=i)).isoformat(),
                "description": f"finding-{i}",
            })
        result = _prune_findings(findings, max_age_days=30, max_entries=5)
        assert len(result) == 5
        # Should keep the 5 newest (hours 0-4)
        descs = [f["description"] for f in result]
        assert "finding-0" in descs
        assert "finding-4" in descs

    def test_empty_list(self) -> None:
        result = _prune_findings([])
        assert result == []


# ---------------------------------------------------------------------------
# Dead code re-export false positive (Improvement 1)
# ---------------------------------------------------------------------------

class TestDeadCodeReexport:
    def test_reexport_not_flagged(self, tmp_path: Path) -> None:
        """An import that is used by another file (re-export) should not be flagged."""
        import subprocess
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        # module_a imports json but doesn't use it locally (re-export)
        (hooks / "module_a.py").write_text("import json\nx = 1\n")
        # module_b imports json from module_a
        (hooks / "module_b.py").write_text("from module_a import json\nprint(json.dumps({}))\n")
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)
        findings = _detect_dead_code(tmp_path)
        # json should NOT be flagged as unused in module_a since module_b imports it
        unused_in_a = [f for f in findings
                       if "module_a" in f.get("evidence", {}).get("file", "")
                       and "unused" in f["description"].lower()
                       and "json" in f.get("evidence", {}).get("unused_imports", [])]
        assert len(unused_in_a) == 0


# ---------------------------------------------------------------------------
# Category health (Improvement 5)
# ---------------------------------------------------------------------------

class TestCategoryHealth:
    def test_healthy_category(self) -> None:
        """A category with few failures is healthy."""
        findings = [
            _make_finding("f1", "low", "dead-code", "desc", {}),
        ]
        findings[0]["status"] = "fixed"
        status, reason = _check_category_health("dead-code", findings)
        assert status == "ok"
        assert reason == ""

    def test_disabled_category(self) -> None:
        """A category with 3+ recent failures is disabled."""
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        findings = []
        for i in range(4):
            f = _make_finding(f"fail-{i}", "low", "dead-code", f"desc-{i}", {})
            f["status"] = "failed"
            f["found_at"] = (now - timedelta(days=i)).isoformat()
            findings.append(f)
        status, reason = _check_category_health("dead-code", findings)
        assert status == "disabled"
        assert "4 failures" in reason

    def test_old_failures_ignored(self) -> None:
        """Failures older than 30 days don't count."""
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        findings = []
        for i in range(5):
            f = _make_finding(f"old-fail-{i}", "low", "dead-code", f"desc-{i}", {})
            f["status"] = "failed"
            f["found_at"] = (now - timedelta(days=60 + i)).isoformat()
            findings.append(f)
        status, reason = _check_category_health("dead-code", findings)
        assert status == "ok"

    def test_disabled_category_blocks_processing(self, tmp_project: Path) -> None:
        """When a category is disabled, _process_finding marks finding as failed."""
        from datetime import datetime, timezone, timedelta
        # Create 3+ recent failures in the findings file
        now = datetime.now(timezone.utc)
        existing = []
        for i in range(3):
            f = _make_finding(f"prev-fail-{i}", "low", "dead-code", f"prev-{i}", {})
            f["status"] = "failed"
            f["found_at"] = (now - timedelta(days=i)).isoformat()
            existing.append(f)
        from dynoproactive import _save_findings
        _save_findings(tmp_project, existing)

        finding = _make_finding("new-dc", "low", "dead-code", "new dead code", {})
        result = _process_finding(finding, tmp_project)
        assert result["status"] == "failed"
        assert "category_disabled" in result.get("fail_reason", "")


# ---------------------------------------------------------------------------
# Cost tracking (Improvement 7)
# ---------------------------------------------------------------------------

class TestCostTracking:
    def test_scan_output_has_cost_field(self, tmp_project: Path) -> None:
        """cmd_scan output JSON includes the cost field."""
        import io
        from unittest.mock import patch
        from dynoproactive import _cmd_scan_locked
        with patch("dynoproactive._detect_llm_review", return_value=[]), \
             patch("dynoproactive._detect_dependency_vulns", return_value=[]), \
             patch("dynoproactive._detect_recurring_audit", return_value=[]), \
             patch("dynoproactive._detect_architectural_drift", return_value=[]), \
             patch("dynoproactive._cleanup_merged_branches"):
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                _cmd_scan_locked(tmp_project, max_findings=3)
            output = json.loads(captured.getvalue())
        assert "cost" in output
        assert "haiku_invocations" in output["cost"]
        assert "fix_invocations" in output["cost"]
        assert "estimated_cost_usd" in output["cost"]
        assert output["cost"]["haiku_invocations"] == 0  # LLM review was mocked out


# ---------------------------------------------------------------------------
# Findings persistence with dict format
# ---------------------------------------------------------------------------

class TestFindingsDictFormat:
    def test_load_dict_format(self, tmp_path: Path) -> None:
        """_load_findings handles the new dict format with category_health."""
        (tmp_path / ".dynos").mkdir()
        data = {
            "findings": [{"finding_id": "x", "status": "new"}],
            "category_health": {"dead-code": {"status": "disabled", "reason": "test"}},
        }
        (tmp_path / ".dynos" / "proactive-findings.json").write_text(json.dumps(data))
        findings = _load_findings(tmp_path)
        assert len(findings) == 1
        assert findings[0]["finding_id"] == "x"
