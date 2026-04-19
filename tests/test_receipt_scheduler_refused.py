"""Adversarial tests for receipt_scheduler_refused validation.

task-20260419-008 / AC 12: the writer rejects malformed input. Each
``raise ValueError(...)`` in the validation block requires a matching
``pytest.raises(ValueError, match=...)`` so the CI coverage linter
(``tests/test_ci_value_error_coverage.py``) is satisfied.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_receipts import receipt_scheduler_refused  # noqa: E402


def _task_dir(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    td = root / ".dynos" / "task-TEST"
    td.mkdir(parents=True)
    (td / "manifest.json").write_text('{"task_id": "task-TEST"}')
    return td


def test_current_stage_with_invalid_chars_rejected(tmp_path: Path) -> None:
    """Lowercase / punctuation in current_stage triggers _STAGE_RE rejection."""
    td = _task_dir(tmp_path)
    with pytest.raises(ValueError, match="current_stage must match"):
        receipt_scheduler_refused(
            task_dir=td,
            current_stage="spec-review",
            proposed_stage="PLANNING",
            missing_proofs=[],
        )


def test_proposed_stage_with_invalid_chars_rejected(tmp_path: Path) -> None:
    """Path-traversal-like proposed_stage triggers _STAGE_RE rejection."""
    td = _task_dir(tmp_path)
    with pytest.raises(ValueError, match="proposed_stage must match"):
        receipt_scheduler_refused(
            task_dir=td,
            current_stage="SPEC_REVIEW",
            proposed_stage="../../etc/passwd",
            missing_proofs=[],
        )


def test_missing_proofs_non_list_rejected(tmp_path: Path) -> None:
    """tuple/None/str containers for missing_proofs are rejected."""
    td = _task_dir(tmp_path)
    with pytest.raises(ValueError, match="missing_proofs must be a list of strings"):
        receipt_scheduler_refused(
            task_dir=td,
            current_stage="SPEC_REVIEW",
            proposed_stage="PLANNING",
            missing_proofs=("missing receipt",),  # tuple, not list
        )
