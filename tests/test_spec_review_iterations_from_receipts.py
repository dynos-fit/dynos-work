"""Tests for spec_review_iterations counted from receipts (F9, CRITERION 9).

Before F9, `compute_reward` walked `execution-log.md` looking for
`[HUMAN] SPEC_REVIEW` lines. Log lines are unsigned, editable, and not
a hash-bound audit record — a malicious or accidental log edit could
manufacture arbitrary iteration counts. After F9, the source of truth
is the glob `receipts/human-approval-SPEC_REVIEW*.json`. Log lines are
ignored.

The efficiency_score penalty formula is unchanged — only the INPUT
source for `spec_review_iterations` moved. A task that has never been
approved has 0 receipts → counter 0 → penalty 0 (formula untouched:
`max(0, s - 1) * 0.1`).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))


def _make_task_dir(tmp_path: Path, *, slug: str = "SRIR") -> Path:
    """Minimal task dir that `compute_reward` can consume without
    exploding on a missing manifest. `compute_reward` reads the
    manifest for classification fields and token-usage for cost; the
    minimum is a manifest with a classification subtree."""
    td = tmp_path / ".dynos" / f"task-20260419-{slug}"
    td.mkdir(parents=True)
    (td / "manifest.json").write_text(json.dumps({
        "task_id": td.name,
        "stage": "DONE",
        "classification": {
            "type": "feature",
            "risk_level": "medium",
            "domains": [],
        },
    }))
    return td


def _write_approval_receipt(td: Path, filename: str) -> None:
    """Write a valid human-approval-SPEC_REVIEW* receipt file.
    Payload shape mirrors real approvals (valid=true, step, ts),
    so `read_receipt` would accept it too. The counter at F9 only
    globs for the file; it does not read the payload."""
    receipts = td / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    payload = {
        "step": filename.removesuffix(".json"),
        "valid": True,
        "stage": "SPEC_REVIEW",
        "artifact_sha256": "d" * 64,
        "approver": "human",
        "ts": "2026-04-19T00:00:00Z",
    }
    (receipts / filename).write_text(json.dumps(payload, indent=2))


def _write_empty_planted_receipt(td: Path, filename: str) -> None:
    """Write an EMPTY file (or JSON-invalid content) matching the glob —
    simulating a SEC-005 attack where someone plants bare files to
    inflate the counter without a real approval. Should NOT count."""
    receipts = td / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    (receipts / filename).write_text("")


def _iterations(td: Path) -> int:
    """Return the `spec_review_iterations` value computed by
    `compute_reward` for the given task dir."""
    from lib_validate import compute_reward
    retro = compute_reward(td)
    return int(retro["spec_review_iterations"])


def test_counts_zero_when_no_receipts(tmp_path: Path) -> None:
    """Fresh task dir with no receipts/ at all → counter is 0."""
    td = _make_task_dir(tmp_path, slug="ZERO")
    # receipts/ intentionally does not exist — tests the `is_dir()`
    # short-circuit in compute_reward.
    assert not (td / "receipts").exists()
    assert _iterations(td) == 0


def test_counts_one_when_single_receipt(tmp_path: Path) -> None:
    """One `human-approval-SPEC_REVIEW.json` present → counter is 1."""
    td = _make_task_dir(tmp_path, slug="ONE")
    _write_approval_receipt(td, "human-approval-SPEC_REVIEW.json")
    assert _iterations(td) == 1


def test_counts_two_with_rotation_suffix(tmp_path: Path) -> None:
    """Two receipts — the base filename and a `-002` rotation suffix —
    both match the glob. The counter returns 2. This pins the
    wildcard behavior documented in the spec as the forward-looking
    hook for revision rotation."""
    td = _make_task_dir(tmp_path, slug="TWO")
    _write_approval_receipt(td, "human-approval-SPEC_REVIEW.json")
    _write_approval_receipt(td, "human-approval-SPEC_REVIEW-002.json")
    assert _iterations(td) == 2


def test_empty_planted_files_do_not_inflate_count(tmp_path: Path) -> None:
    """SEC-005 regression: `touch human-approval-SPEC_REVIEW-fake.json`
    (empty file, no JSON content) must NOT inflate the counter.
    Receipts must parse as JSON objects carrying a matching
    `step == "human-approval-SPEC_REVIEW*"` field AND non-empty
    `artifact_sha256`."""
    td = _make_task_dir(tmp_path, slug="SEC5")
    # One legitimate receipt + three planted files of different shapes.
    _write_approval_receipt(td, "human-approval-SPEC_REVIEW.json")
    _write_empty_planted_receipt(td, "human-approval-SPEC_REVIEW-empty.json")
    # Valid JSON but not a dict.
    (td / "receipts" / "human-approval-SPEC_REVIEW-list.json").write_text("[]")
    # Valid JSON object but wrong step field.
    (td / "receipts" / "human-approval-SPEC_REVIEW-wrong.json").write_text(
        json.dumps({"step": "something-else", "artifact_sha256": "a" * 64})
    )
    # Valid JSON object with empty artifact_sha256.
    (td / "receipts" / "human-approval-SPEC_REVIEW-noart.json").write_text(
        json.dumps({"step": "human-approval-SPEC_REVIEW", "artifact_sha256": ""})
    )
    assert _iterations(td) == 1, (
        f"only the legitimate receipt should count; planted files must be rejected"
    )


def test_log_lines_no_longer_counted(tmp_path: Path) -> None:
    """Deliberately write several `[HUMAN] SPEC_REVIEW` lines into the
    execution log but NO receipts. The old log-scanner would have
    returned 3; the F9 counter MUST return 0. This is the core
    regression — if someone restores the log scanner, this test fires."""
    td = _make_task_dir(tmp_path, slug="LOG")
    log = td / "execution-log.md"
    log.write_text(
        "[2026-04-19T00:00:00Z] [HUMAN] SPEC_REVIEW approved v1\n"
        "[2026-04-19T00:00:01Z] [HUMAN] SPEC_REVIEW approved v2\n"
        "[2026-04-19T00:00:02Z] [HUMAN] SPEC_REVIEW approved v3\n"
    )
    # No receipts directory at all.
    assert not (td / "receipts").exists()
    assert _iterations(td) == 0, (
        "F9 regression: log lines must NOT feed spec_review_iterations"
    )


def test_unrelated_approval_files_not_counted(tmp_path: Path) -> None:
    """Defensive: the glob uses the exact prefix `human-approval-SPEC_REVIEW*`.
    A `human-approval-PLAN_REVIEW.json` must not leak into the counter.
    This pins the glob scope — a wildcard bug turning `*_REVIEW*` into
    the prefix would silently double-count."""
    td = _make_task_dir(tmp_path, slug="UNREL")
    _write_approval_receipt(td, "human-approval-SPEC_REVIEW.json")
    _write_approval_receipt(td, "human-approval-PLAN_REVIEW.json")
    _write_approval_receipt(td, "human-approval-TDD_REVIEW.json")
    assert _iterations(td) == 1, (
        "only human-approval-SPEC_REVIEW* should count — got a different value"
    )
