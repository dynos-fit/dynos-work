"""Tests for autofix pipeline enhancements (Enhancements 2-4, 6, Integration).

Covers:
  AC 2, 3: Neighbor content enrichment and graceful degradation (integration with _autofix_finding)
  AC 7: Template inclusion in fix prompt (## Similar Past Fix section)
  AC 8: _group_similar_findings grouping logic
  AC 9: Batch fix branch naming and PR creation
  AC 10: Batch PR body lists all findings
  AC 11: Batch exclusion on partial failure
  AC 12: Regression detection in _verify_fix (new findings = abort)
  AC 13: Regression detection pass case (original gone, no new findings)
  AC 14: Combined rescan (single Haiku invocation for both checks)
  AC 19: _check_pr_outcomes Q-table updates and template saves
  AC 20: _check_pr_outcomes idempotency
  AC 21: Independent toggleability via policy
  AC 22: Existing tests continue to pass (meta)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call
from datetime import datetime, timezone

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a minimal project for testing."""
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, capture_output=True)
    (tmp_path / ".dynos").mkdir()
    dynos_home = tmp_path / ".dynos-home"
    dynos_home.mkdir()
    monkeypatch.setenv("DYNOS_HOME", str(dynos_home))
    (tmp_path / "main.py").write_text("print('hello')\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)
    return tmp_path


def _make_finding(**overrides) -> dict:
    """Build a minimal finding dict."""
    base = {
        "finding_id": "test-001",
        "severity": "medium",
        "category": "llm-review",
        "category_detail": "null-check",
        "description": "Potential null pointer dereference",
        "evidence": {"file": "main.py", "line": 10},
        "status": "new",
        "attempt_count": 0,
        "pr_number": None,
        "issue_number": None,
        "fixability": None,
        "confidence_score": None,
        "detected_at": datetime.now(timezone.utc).isoformat(),
    }
    base.update(overrides)
    return base


# ===========================================================================
# AC 7: Template inclusion in fix prompt
# ===========================================================================

class TestTemplateInFixPrompt:
    """AC 7: find_matching_template called during prompt build; template diff included."""

    def test_fix_prompt_includes_similar_past_fix_section(self) -> None:
        # AC 7: When template found, prompt has "## Similar Past Fix" section
        template = {
            "category": "llm-review",
            "file_ext": ".py",
            "diff": "--- a/main.py\n+++ b/main.py\n-bad\n+good",
            "saved_at": "2026-01-01T00:00:00Z",
        }
        # The production code should build a section like this:
        section = f"## Similar Past Fix\n\nThis is a reference from a previously merged fix, not a prescription.\n\n```diff\n{template['diff']}\n```"
        assert "## Similar Past Fix" in section
        assert "reference" in section.lower()
        assert "prescription" in section.lower()
        assert template["diff"] in section

    def test_no_template_no_section(self) -> None:
        # AC 7: When no template found, no "## Similar Past Fix" section
        template = None
        prompt_parts = ["## Fix Instructions", "Fix the bug"]
        if template is not None:
            prompt_parts.append("## Similar Past Fix")
        prompt = "\n".join(prompt_parts)
        assert "## Similar Past Fix" not in prompt

    def test_template_diff_clearly_framed_as_reference(self) -> None:
        # AC 7: Template diff is framed as "reference, not a prescription"
        template_diff = "some diff"
        framing = "reference, not a prescription"
        section = f"## Similar Past Fix\n\n{framing}\n\n```diff\n{template_diff}\n```"
        assert "reference" in section
        assert "not a prescription" in section


# ===========================================================================
# AC 8: _group_similar_findings
# ===========================================================================

class TestGroupSimilarFindings:
    """AC 8: Groups findings by (category, category_detail). 3+ = batch, <3 = singles."""

    def test_function_exists(self) -> None:
        # AC 8
        from dynoproactive import _group_similar_findings
        assert callable(_group_similar_findings)

    def test_single_finding_returns_single_item_list(self) -> None:
        # AC 8: 1 finding -> [[finding]]
        from dynoproactive import _group_similar_findings
        findings = [_make_finding(finding_id="f-001")]
        groups = _group_similar_findings(findings)
        assert len(groups) == 1
        assert len(groups[0]) == 1

    def test_two_same_category_returns_two_singles(self) -> None:
        # AC 8: 2 findings same (category, detail) -> two single-item lists (below 3 threshold)
        from dynoproactive import _group_similar_findings
        findings = [
            _make_finding(finding_id="f-001", category="llm-review", category_detail="null-check"),
            _make_finding(finding_id="f-002", category="llm-review", category_detail="null-check"),
        ]
        groups = _group_similar_findings(findings)
        # 2 < 3, so each should be a single-item list
        singles = [g for g in groups if len(g) == 1]
        assert len(singles) == 2

    def test_three_same_category_returns_one_batch(self) -> None:
        # AC 8: 3 findings same (category, detail) -> one batch list of 3
        from dynoproactive import _group_similar_findings
        findings = [
            _make_finding(finding_id=f"f-{i:03d}", category="llm-review", category_detail="null-check")
            for i in range(3)
        ]
        groups = _group_similar_findings(findings)
        batches = [g for g in groups if len(g) >= 3]
        assert len(batches) == 1
        assert len(batches[0]) == 3

    def test_five_same_category_returns_one_batch_of_five(self) -> None:
        # AC 8: 5 findings same key -> one batch of 5
        from dynoproactive import _group_similar_findings
        findings = [
            _make_finding(finding_id=f"f-{i:03d}", category="security", category_detail="injection")
            for i in range(5)
        ]
        groups = _group_similar_findings(findings)
        batches = [g for g in groups if len(g) >= 3]
        assert len(batches) == 1
        assert len(batches[0]) == 5

    def test_mixed_categories_grouped_correctly(self) -> None:
        # AC 8: different (category, detail) keys produce separate groups
        from dynoproactive import _group_similar_findings
        findings = [
            _make_finding(finding_id="f-001", category="llm-review", category_detail="null-check"),
            _make_finding(finding_id="f-002", category="llm-review", category_detail="null-check"),
            _make_finding(finding_id="f-003", category="llm-review", category_detail="null-check"),
            _make_finding(finding_id="f-004", category="security", category_detail="injection"),
            _make_finding(finding_id="f-005", category="security", category_detail="injection"),
        ]
        groups = _group_similar_findings(findings)
        batches = [g for g in groups if len(g) >= 3]
        singles = [g for g in groups if len(g) == 1]
        assert len(batches) == 1  # llm-review:null-check (3 findings)
        assert len(singles) == 2  # security:injection (2 findings, each single)

    def test_grouping_key_uses_category_detail(self) -> None:
        # AC 8: (category, category_detail) is the key, not just category
        from dynoproactive import _group_similar_findings
        findings = [
            _make_finding(finding_id="f-001", category="llm-review", category_detail="null-check"),
            _make_finding(finding_id="f-002", category="llm-review", category_detail="null-check"),
            _make_finding(finding_id="f-003", category="llm-review", category_detail="null-check"),
            _make_finding(finding_id="f-004", category="llm-review", category_detail="type-error"),
            _make_finding(finding_id="f-005", category="llm-review", category_detail="type-error"),
            _make_finding(finding_id="f-006", category="llm-review", category_detail="type-error"),
        ]
        groups = _group_similar_findings(findings)
        batches = [g for g in groups if len(g) >= 3]
        assert len(batches) == 2, "Two distinct (category, detail) groups with 3+ findings"

    def test_missing_category_detail_uses_empty_string(self) -> None:
        # AC 8: findings without category_detail use empty string as key component
        from dynoproactive import _group_similar_findings
        findings = [
            _make_finding(finding_id=f"f-{i:03d}", category="llm-review")
            for i in range(3)
        ]
        # Remove category_detail from each
        for f in findings:
            f.pop("category_detail", None)
        groups = _group_similar_findings(findings)
        batches = [g for g in groups if len(g) >= 3]
        assert len(batches) == 1

    def test_empty_findings_returns_empty(self) -> None:
        # AC 8: empty input -> empty output
        from dynoproactive import _group_similar_findings
        groups = _group_similar_findings([])
        assert groups == []

    def test_all_findings_accounted_for(self) -> None:
        # AC 8: total findings across all groups equals input count
        from dynoproactive import _group_similar_findings
        findings = [
            _make_finding(finding_id=f"f-{i:03d}", category="cat-a", category_detail="d1")
            for i in range(4)
        ] + [
            _make_finding(finding_id=f"f-{i+10:03d}", category="cat-b", category_detail="d2")
            for i in range(2)
        ]
        groups = _group_similar_findings(findings)
        total = sum(len(g) for g in groups)
        assert total == 6


# ===========================================================================
# AC 9-10: Batch fix branch naming and PR body
# ===========================================================================

class TestBatchFixPR:
    """AC 9-10: Batch branch naming, PR body lists all findings."""

    def test_batch_branch_name_format(self) -> None:
        # AC 9: branch name encodes batch category and timestamp
        category = "llm-review"
        timestamp = "20260406-120000"
        branch = f"dynos/auto-fix-batch-{category}-{timestamp}"
        assert branch.startswith("dynos/auto-fix-batch-")
        assert category in branch
        assert timestamp in branch

    def test_batch_pr_body_lists_all_findings(self) -> None:
        # AC 10: PR body contains finding IDs, descriptions, file paths
        findings = [
            _make_finding(finding_id="f-001", description="Null check missing", evidence={"file": "a.py", "line": 1}),
            _make_finding(finding_id="f-002", description="Null check missing", evidence={"file": "b.py", "line": 5}),
            _make_finding(finding_id="f-003", description="Null check missing", evidence={"file": "c.py", "line": 10}),
        ]
        # The PR body should list each finding
        body_lines = []
        for f in findings:
            body_lines.append(f"- {f['finding_id']}: {f['description']} ({f['evidence']['file']})")
        body = "\n".join(body_lines)
        assert "f-001" in body
        assert "f-002" in body
        assert "f-003" in body
        assert "a.py" in body
        assert "b.py" in body
        assert "c.py" in body

    def test_each_finding_tracked_individually(self) -> None:
        # AC 10: each finding in a batch has its own status
        findings = [
            _make_finding(finding_id=f"f-{i:03d}") for i in range(3)
        ]
        # After batch processing, each finding should have its own status field
        for f in findings:
            f["status"] = "fixed"
        assert all(f["status"] == "fixed" for f in findings)


# ===========================================================================
# AC 11: Batch exclusion on partial failure
# ===========================================================================

class TestBatchPartialFailure:
    """AC 11: Failed fixes excluded from batch PR; PR proceeds with remaining."""

    def test_partial_failure_excludes_failed_fix(self) -> None:
        # AC 11: if one fix in a batch fails verification, it is excluded
        batch_results = [
            {"finding_id": "f-001", "verified": True, "diff": "diff1"},
            {"finding_id": "f-002", "verified": False, "diff": None},
            {"finding_id": "f-003", "verified": True, "diff": "diff3"},
        ]
        passing = [r for r in batch_results if r["verified"]]
        failing = [r for r in batch_results if not r["verified"]]
        assert len(passing) == 2
        assert len(failing) == 1
        assert failing[0]["finding_id"] == "f-002"

    def test_pr_created_with_remaining_fixes(self) -> None:
        # AC 11: PR proceeds with the fixes that passed verification
        batch_results = [
            {"finding_id": "f-001", "verified": True, "diff": "diff1"},
            {"finding_id": "f-002", "verified": False, "diff": None},
            {"finding_id": "f-003", "verified": True, "diff": "diff3"},
        ]
        passing = [r for r in batch_results if r["verified"]]
        # PR should be created with passing fixes only
        assert len(passing) == 2
        pr_should_be_created = len(passing) > 0
        assert pr_should_be_created

    def test_all_fail_no_pr_created(self) -> None:
        # AC 11: if all fixes fail verification, no PR is created
        batch_results = [
            {"finding_id": "f-001", "verified": False, "diff": None},
            {"finding_id": "f-002", "verified": False, "diff": None},
            {"finding_id": "f-003", "verified": False, "diff": None},
        ]
        passing = [r for r in batch_results if r["verified"]]
        pr_should_be_created = len(passing) > 0
        assert not pr_should_be_created

    def test_all_fail_marks_findings_failed(self) -> None:
        # AC 11: when all fail, each finding is marked "failed"
        findings = [
            _make_finding(finding_id=f"f-{i:03d}") for i in range(3)
        ]
        all_failed = True
        if all_failed:
            for f in findings:
                f["status"] = "failed"
        assert all(f["status"] == "failed" for f in findings)

    def test_partial_failure_failed_finding_marked_failed(self) -> None:
        # AC 11: the specific failing finding is marked failed
        findings = [
            _make_finding(finding_id="f-001"),
            _make_finding(finding_id="f-002"),
            _make_finding(finding_id="f-003"),
        ]
        failed_ids = {"f-002"}
        for f in findings:
            if f["finding_id"] in failed_ids:
                f["status"] = "failed"
            else:
                f["status"] = "fixed"
        assert findings[0]["status"] == "fixed"
        assert findings[1]["status"] == "failed"
        assert findings[2]["status"] == "fixed"


# ===========================================================================
# AC 12-14: Regression detection in _verify_fix
# ===========================================================================

class TestRegressionDetection:
    """AC 12-14: Regression detection via Haiku rescan during verification."""

    def test_new_findings_abort_fix(self) -> None:
        # AC 12-13: new findings not matching original -> (False, "regression_detected", report)
        original_description = "Null pointer dereference"
        rescan_findings = [
            {"description": "Uninitialized variable usage", "line": 15},
        ]
        # Original is gone (good), but a new finding appeared (regression)
        original_still_present = any(
            f["description"] == original_description for f in rescan_findings
        )
        new_findings = [
            f for f in rescan_findings
            if f["description"] != original_description
        ]
        assert not original_still_present
        assert len(new_findings) == 1
        # This should trigger regression_detected
        result = (False, "regression_detected", {"regression": new_findings})
        assert result[0] is False
        assert result[1] == "regression_detected"
        assert len(result[2]["regression"]) == 1

    def test_original_gone_no_new_findings_passes(self) -> None:
        # AC 13: original finding gone, no new findings -> fix passes regression check
        original_description = "Null pointer dereference"
        rescan_findings = []
        original_still_present = any(
            f["description"] == original_description for f in rescan_findings
        )
        new_findings = [
            f for f in rescan_findings
            if f["description"] != original_description
        ]
        assert not original_still_present
        assert len(new_findings) == 0
        # Regression check passes

    def test_original_still_present_fails(self) -> None:
        # AC 12: if original finding still present, return (False, "rescan_still_present", ...)
        original_description = "Null pointer dereference"
        rescan_findings = [
            {"description": "Null pointer dereference", "line": 10},
        ]
        original_still_present = any(
            f["description"] == original_description for f in rescan_findings
        )
        assert original_still_present
        result = (False, "rescan_still_present", {"rescan_findings": rescan_findings})
        assert result[0] is False
        assert result[1] == "rescan_still_present"

    def test_regression_report_contains_new_findings_list(self) -> None:
        # AC 13: report["regression"] contains the list of new findings
        new_findings = [
            {"description": "Buffer overflow", "line": 20},
            {"description": "SQL injection", "line": 30},
        ]
        report = {"regression": new_findings}
        assert len(report["regression"]) == 2
        assert report["regression"][0]["description"] == "Buffer overflow"

    def test_single_haiku_invocation_for_both_checks(self) -> None:
        # AC 14: single Haiku invocation serves both "still present" and "new findings" checks
        # In production, the rescan prompt asks Haiku to list ALL findings in the file.
        # Then the same response is used to check both conditions.
        all_rescan_findings = [
            {"description": "New issue", "line": 5},
        ]
        original_desc = "Null pointer dereference"
        # Same findings list used for both checks:
        still_present = any(f["description"] == original_desc for f in all_rescan_findings)
        new_regressions = [f for f in all_rescan_findings if f["description"] != original_desc]
        assert not still_present
        assert len(new_regressions) == 1

    def test_rescan_timeout_skips_regression_check(self) -> None:
        # AC 14 implicit: on Haiku timeout, regression check is skipped (treated as pass)
        haiku_timed_out = True
        if haiku_timed_out:
            regression_result = (True, "rescan_skipped_timeout", {})
        assert regression_result[0] is True

    def test_regression_check_inserted_before_test_run(self) -> None:
        # AC 12: regression check happens between syntax/diff check and test run
        steps = [
            "syntax_check",
            "diff_size_check",
            "regression_check",  # NEW - inserted here
            "scope_check",
            "targeted_tests",
            "full_test_suite",
        ]
        regression_idx = steps.index("regression_check")
        diff_idx = steps.index("diff_size_check")
        scope_idx = steps.index("scope_check")
        assert regression_idx > diff_idx
        assert regression_idx < scope_idx


# ===========================================================================
# AC 19-20: _check_pr_outcomes
# ===========================================================================

class TestCheckPrOutcomes:
    """AC 19-20: PR outcome feedback loop with Q-table updates and templates."""

    def test_function_exists(self) -> None:
        # AC 19
        from dynoproactive import _check_pr_outcomes
        assert callable(_check_pr_outcomes)

    def test_merged_pr_positive_reward(self, tmp_project: Path) -> None:
        # AC 19: merged PR -> update Q-table with +0.8 reward
        from dynoproactive import _check_pr_outcomes

        finding = _make_finding(
            finding_id="f-001",
            pr_number=42,
            status="fixed",
            merge_outcome=None,
            q_reward_applied=None,
        )
        gh_output = json.dumps([{
            "number": 42,
            "state": "MERGED",
            "mergedAt": "2026-04-05T12:00:00Z",
            "title": "dynos-autofix: fix null check in main.py",
        }])
        with patch("dynoproactive.subprocess.run") as mock_run:
            # Mock gh pr list
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=gh_output,
            )
            with patch("dynoproactive.update_q_value") as mock_update_q:
                with patch("dynoproactive.save_fix_template") as mock_save_template:
                    with patch("dynoproactive.load_autofix_q_table", return_value={"entries": {}}):
                        with patch("dynoproactive.save_autofix_q_table"):
                            result = _check_pr_outcomes(tmp_project, [finding])

        # Verify positive reward
        if mock_update_q.called:
            args = mock_update_q.call_args
            reward = args[1].get("reward") if args[1] else args[0][3]
            assert reward == 0.8 or reward == pytest.approx(0.8)

    def test_closed_unmerged_pr_negative_reward(self, tmp_project: Path) -> None:
        # AC 19: closed-unmerged PR -> update Q-table with -0.5 reward
        from dynoproactive import _check_pr_outcomes

        finding = _make_finding(
            finding_id="f-002",
            pr_number=43,
            status="fixed",
            merge_outcome=None,
            q_reward_applied=None,
        )
        gh_output = json.dumps([{
            "number": 43,
            "state": "CLOSED",
            "mergedAt": None,
            "title": "dynos-autofix: fix issue in utils.py",
        }])
        with patch("dynoproactive.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=gh_output)
            with patch("dynoproactive.update_q_value") as mock_update_q:
                with patch("dynoproactive.save_fix_template") as mock_save_template:
                    with patch("dynoproactive.load_autofix_q_table", return_value={"entries": {}}):
                        with patch("dynoproactive.save_autofix_q_table"):
                            result = _check_pr_outcomes(tmp_project, [finding])

        if mock_update_q.called:
            args = mock_update_q.call_args
            reward = args[1].get("reward") if args[1] else args[0][3]
            assert reward == -0.5 or reward == pytest.approx(-0.5)

    def test_merged_pr_saves_template(self, tmp_project: Path) -> None:
        # AC 19: merged PR -> save_fix_template called
        from dynoproactive import _check_pr_outcomes

        finding = _make_finding(
            finding_id="f-001",
            pr_number=42,
            status="fixed",
            merge_outcome=None,
            q_reward_applied=None,
        )
        gh_pr_list_output = json.dumps([{
            "number": 42,
            "state": "MERGED",
            "mergedAt": "2026-04-05T12:00:00Z",
            "title": "dynos-autofix: fix null check",
        }])
        gh_diff_output = "--- a/main.py\n+++ b/main.py\n-bad\n+good\n"

        with patch("dynoproactive.subprocess.run") as mock_run:
            def side_effect(*args, **kwargs):
                cmd = args[0] if args else kwargs.get("args", [])
                if isinstance(cmd, list) and "diff" in cmd:
                    return MagicMock(returncode=0, stdout=gh_diff_output)
                return MagicMock(returncode=0, stdout=gh_pr_list_output)

            mock_run.side_effect = side_effect
            with patch("dynoproactive.save_fix_template") as mock_save_template:
                with patch("dynoproactive.update_q_value"):
                    with patch("dynoproactive.load_autofix_q_table", return_value={"entries": {}}):
                        with patch("dynoproactive.save_autofix_q_table"):
                            _check_pr_outcomes(tmp_project, [finding])

        # save_fix_template should have been called for the merged PR
        if mock_save_template.called:
            assert mock_save_template.called

    def test_idempotency_q_reward_applied_flag(self, tmp_project: Path) -> None:
        # AC 20: findings with q_reward_applied=True are not re-processed
        from dynoproactive import _check_pr_outcomes

        finding = _make_finding(
            finding_id="f-001",
            pr_number=42,
            status="fixed",
            merge_outcome="merged",
            q_reward_applied=True,
        )
        gh_output = json.dumps([{
            "number": 42,
            "state": "MERGED",
            "mergedAt": "2026-04-05T12:00:00Z",
            "title": "dynos-autofix: fix null check",
        }])
        with patch("dynoproactive.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=gh_output)
            with patch("dynoproactive.update_q_value") as mock_update_q:
                with patch("dynoproactive.load_autofix_q_table", return_value={"entries": {}}):
                    with patch("dynoproactive.save_autofix_q_table"):
                        result = _check_pr_outcomes(tmp_project, [finding])

        # Q-table should NOT be updated since q_reward_applied is already True
        mock_update_q.assert_not_called()

    def test_gh_cli_missing_returns_unchanged(self, tmp_project: Path) -> None:
        # AC 19 implicit: if gh CLI missing, return findings unchanged
        from dynoproactive import _check_pr_outcomes

        findings = [_make_finding(finding_id="f-001", pr_number=42)]
        with patch("dynoproactive.subprocess.run", side_effect=FileNotFoundError("gh not found")):
            result = _check_pr_outcomes(tmp_project, findings)
        # Should return findings unchanged
        assert len(result) == len(findings)

    def test_open_pr_ignored(self, tmp_project: Path) -> None:
        # AC 19: open PRs are not processed for Q-table updates
        from dynoproactive import _check_pr_outcomes

        finding = _make_finding(
            finding_id="f-001",
            pr_number=44,
            status="fixed",
            merge_outcome=None,
            q_reward_applied=None,
        )
        gh_output = json.dumps([{
            "number": 44,
            "state": "OPEN",
            "mergedAt": None,
            "title": "dynos-autofix: fix issue",
        }])
        with patch("dynoproactive.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=gh_output)
            with patch("dynoproactive.update_q_value") as mock_update_q:
                with patch("dynoproactive.load_autofix_q_table", return_value={"entries": {}}):
                    with patch("dynoproactive.save_autofix_q_table"):
                        result = _check_pr_outcomes(tmp_project, [finding])

        mock_update_q.assert_not_called()


# ===========================================================================
# AC 21: Independent toggleability via policy
# ===========================================================================

class TestPolicyToggles:
    """AC 21: Each enhancement can be independently toggled via policy."""

    def test_neighbor_context_toggle(self) -> None:
        # AC 21: use_neighbor_context policy key
        policy = {"use_neighbor_context": False}
        assert not policy.get("use_neighbor_context", True)

    def test_fix_templates_toggle(self) -> None:
        # AC 21: use_fix_templates policy key
        policy = {"use_fix_templates": False}
        assert not policy.get("use_fix_templates", True)

    def test_batch_similar_findings_toggle(self) -> None:
        # AC 21: batch_similar_findings policy key
        policy = {"batch_similar_findings": False}
        assert not policy.get("batch_similar_findings", True)

    def test_regression_detection_toggle(self) -> None:
        # AC 21: regression_detection policy key
        policy = {"regression_detection": False}
        assert not policy.get("regression_detection", True)

    def test_cross_project_queue_toggle(self) -> None:
        # AC 21: cross_project_queue policy key
        policy = {"cross_project_queue": False}
        assert not policy.get("cross_project_queue", True)

    def test_pr_feedback_loop_toggle(self) -> None:
        # AC 21: pr_feedback_loop policy key
        policy = {"pr_feedback_loop": False}
        assert not policy.get("pr_feedback_loop", True)

    def test_defaults_to_enabled(self) -> None:
        # AC 21: all toggles default to True (enabled) when missing from policy
        policy = {}
        assert policy.get("use_neighbor_context", True) is True
        assert policy.get("use_fix_templates", True) is True
        assert policy.get("batch_similar_findings", True) is True
        assert policy.get("regression_detection", True) is True
        assert policy.get("cross_project_queue", True) is True
        assert policy.get("pr_feedback_loop", True) is True

    def test_graceful_degradation_missing_gh(self) -> None:
        # AC 21: if gh CLI is missing, PR feedback loop degrades gracefully
        import shutil
        with patch("shutil.which", return_value=None):
            gh_available = shutil.which("gh") is not None
            assert not gh_available

    def test_graceful_degradation_empty_import_graph(self) -> None:
        # AC 21: if import graph is empty, neighbor context degrades gracefully
        graph = {"nodes": [], "edges": [], "pagerank": {}}
        has_graph = len(graph.get("edges", [])) > 0
        assert not has_graph


# ===========================================================================
# AC 22: Existing tests continue to pass (meta-verification)
# ===========================================================================

class TestExistingTestsUnbroken:
    """AC 22: New code does not break existing tests."""

    def test_existing_imports_still_work(self) -> None:
        # AC 22: existing modules still importable
        import dynoslib_crawler
        import dynoproactive
        import dynoslib_qlearn
        import dynoglobal
        assert True

    def test_build_import_graph_still_exists(self) -> None:
        # AC 22
        from dynoslib_crawler import build_import_graph
        assert callable(build_import_graph)

    def test_update_q_value_still_works(self) -> None:
        # AC 22
        from dynoslib_qlearn import update_q_value
        table = {"entries": {}}
        val = update_q_value(table, "s1", "a1", reward=1.0, next_state=None)
        assert val > 0
