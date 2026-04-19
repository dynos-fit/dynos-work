"""Regression tests for the plan-validated receipt freshness short-circuit.

The receipt now captures artifact content hashes (spec.md, plan.md,
execution-graph.json). Execute preflight can call
plan_validated_receipt_matches(task_dir) to detect whether anything
has drifted since planning validated the artifacts. If nothing
drifted, the full re-validation can be skipped — same correctness,
no work.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))


def _setup_task(tmp_path: Path) -> Path:
    task_dir = tmp_path / ".dynos" / "task-001"
    task_dir.mkdir(parents=True)
    (task_dir / "spec.md").write_text("# Spec\nv1\n")
    (task_dir / "plan.md").write_text("# Plan\nv1\n")
    (task_dir / "execution-graph.json").write_text('{"task_id": "task-001", "segments": []}\n')
    return task_dir


class TestReceiptFreshness:
    def test_no_receipt_returns_false(self, tmp_path: Path):
        task_dir = _setup_task(tmp_path)
        from lib_receipts import plan_validated_receipt_matches
        assert plan_validated_receipt_matches(task_dir) is False

    def test_fresh_receipt_with_matching_hashes_returns_true(self, tmp_path: Path):
        task_dir = _setup_task(tmp_path)
        from lib_receipts import receipt_plan_validated, plan_validated_receipt_matches
        receipt_plan_validated(task_dir, segment_count=0, criteria_coverage=[])
        assert plan_validated_receipt_matches(task_dir) is True

    def test_spec_change_invalidates(self, tmp_path: Path):
        task_dir = _setup_task(tmp_path)
        from lib_receipts import receipt_plan_validated, plan_validated_receipt_matches
        receipt_plan_validated(task_dir, segment_count=0, criteria_coverage=[])
        (task_dir / "spec.md").write_text("# Spec\nv2 changed\n")
        # F1: drift now returns a descriptive string naming the artifact
        # (used by the EXECUTION gate to surface drift-vs-missing
        # distinctly). A bare `False` is reserved for "receipt missing".
        result = plan_validated_receipt_matches(task_dir)
        assert result is not True, "spec drift must invalidate the receipt"
        assert result is not False, "drift must NOT return False (that's the missing-receipt signal)"
        assert isinstance(result, str) and "spec.md" in result, \
            f"expected drift string naming spec.md, got {result!r}"

    def test_plan_change_invalidates(self, tmp_path: Path):
        task_dir = _setup_task(tmp_path)
        from lib_receipts import receipt_plan_validated, plan_validated_receipt_matches
        receipt_plan_validated(task_dir, segment_count=0, criteria_coverage=[])
        (task_dir / "plan.md").write_text("# Plan\nv2\n")
        result = plan_validated_receipt_matches(task_dir)
        assert isinstance(result, str) and "plan.md" in result, \
            f"expected drift string naming plan.md, got {result!r}"

    def test_graph_change_invalidates(self, tmp_path: Path):
        task_dir = _setup_task(tmp_path)
        from lib_receipts import receipt_plan_validated, plan_validated_receipt_matches
        receipt_plan_validated(task_dir, segment_count=0, criteria_coverage=[])
        (task_dir / "execution-graph.json").write_text('{"task_id":"x","segments":[]}\n')
        result = plan_validated_receipt_matches(task_dir)
        assert isinstance(result, str) and "execution-graph.json" in result, \
            f"expected drift string naming execution-graph.json, got {result!r}"

    def test_legacy_receipt_without_hashes_treated_as_drift(self, tmp_path: Path):
        """Old receipts written before this commit don't have artifact_hashes.
        plan_validated_receipt_matches must treat them as "needs revalidation"
        rather than assume they pass."""
        task_dir = _setup_task(tmp_path)
        from lib_receipts import write_receipt, plan_validated_receipt_matches
        # Manually write a receipt without artifact_hashes
        write_receipt(
            task_dir,
            "plan-validated",
            segment_count=0,
            criteria_coverage=[],
            validation_passed=True,
        )
        assert plan_validated_receipt_matches(task_dir) is False, \
            "legacy receipts must force re-validation"

    def test_failed_receipt_does_not_match(self, tmp_path: Path):
        """A receipt where validation_passed=False must never short-circuit."""
        task_dir = _setup_task(tmp_path)
        from lib_receipts import receipt_plan_validated, plan_validated_receipt_matches
        receipt_plan_validated(task_dir, segment_count=0, criteria_coverage=[],
                               validation_passed=False)
        assert plan_validated_receipt_matches(task_dir) is False


class TestUseReceiptCli:
    def test_use_receipt_skips_validation_when_fresh(self, tmp_path: Path):
        task_dir = _setup_task(tmp_path)
        from lib_receipts import receipt_plan_validated
        receipt_plan_validated(task_dir, segment_count=0, criteria_coverage=[])

        with mock.patch("validate_task_artifacts.validate_task_artifacts") as mock_v, \
             mock.patch("sys.argv", ["validate_task_artifacts.py", str(task_dir), "--use-receipt"]):
            from validate_task_artifacts import main
            assert main() == 0
        assert not mock_v.called, "fresh receipt must skip validation entirely"

    def test_use_receipt_falls_through_when_stale(self, tmp_path: Path):
        task_dir = _setup_task(tmp_path)
        from lib_receipts import receipt_plan_validated
        receipt_plan_validated(task_dir, segment_count=0, criteria_coverage=[])
        # Drift the spec
        (task_dir / "spec.md").write_text("# Spec\ndrifted\n")

        with mock.patch("validate_task_artifacts.validate_task_artifacts") as mock_v, \
             mock.patch("sys.argv", ["validate_task_artifacts.py", str(task_dir), "--use-receipt"]):
            mock_v.return_value = []
            from validate_task_artifacts import main
            main()
        assert mock_v.called, "stale receipt must trigger normal validation"
