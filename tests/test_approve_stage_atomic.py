"""Unit tests for AC26: approve_stage atomically advances manifest stage.

The critical contract: after approve_stage returns (exit 0), manifest.json
must already reflect the new stage. Callers do not depend on the daemon to
observe the receipt and issue the transition.

These tests exercise the SPEC_NORMALIZATION -> SPEC_REVIEW path as described
in AC26, using fast_track=True to bypass the planner-spec receipt gate.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

SPEC_TEMPLATE = (
    "# Normalized Spec\n\n"
    "## Task Summary\nA.\n\n"
    "## User Context\nB.\n\n"
    "## Acceptance Criteria\n1. one\n2. two\n\n"
    "## Implicit Requirements Surfaced\nC.\n\n"
    "## Out of Scope\nD.\n\n"
    "## Assumptions\nsafe assumption: none\n\n"
    "## Risk Notes\nE.\n"
)


def _make_task(
    tmp_path: Path,
    stage: str,
    fast_track: bool = False,
    tdd_required: bool = False,
) -> Path:
    task_dir = tmp_path / ".dynos" / "task-20260423-atomic"
    task_dir.mkdir(parents=True)
    manifest = {
        "task_id": "task-20260423-atomic",
        "created_at": "2026-04-23T00:00:00Z",
        "title": "Atomic approve-stage test",
        "raw_input": "x",
        "stage": stage,
        "classification": {
            "type": "feature",
            "domains": ["backend"],
            "risk_level": "medium",
            "notes": "n",
            "tdd_required": tdd_required,
        },
        "retry_counts": {},
        "blocked_reason": None,
        "completion_at": None,
    }
    if fast_track:
        manifest["fast_track"] = True
    (task_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    (task_dir / "spec.md").write_text(SPEC_TEMPLATE)
    return task_dir


def _run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(ROOT / "hooks")}
    return subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "ctl.py"), *args],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


# ---------------------------------------------------------------------------
# AC26 core assertion: SPEC_NORMALIZATION -> SPEC_REVIEW without daemon
# ---------------------------------------------------------------------------


def test_spec_normalization_advances_to_spec_review_before_return(tmp_path: Path):
    """AC26: approve_stage on a SPEC_NORMALIZATION task advances manifest to
    SPEC_REVIEW before the command exits. No daemon is started.
    """
    task_dir = _make_task(tmp_path, stage="SPEC_NORMALIZATION", fast_track=True)
    r = _run(tmp_path, "approve-stage", str(task_dir), "SPEC_NORMALIZATION")
    assert r.returncode == 0, r.stderr

    # Receipt must exist
    receipt_path = task_dir / "receipts" / "human-approval-SPEC_NORMALIZATION.json"
    assert receipt_path.exists(), "approval receipt not written"
    receipt = json.loads(receipt_path.read_text())
    assert receipt["approver"] == "human"
    assert len(receipt["artifact_sha256"]) == 64

    # Manifest must already reflect the new stage — no daemon required
    manifest = json.loads((task_dir / "manifest.json").read_text())
    assert manifest["stage"] == "SPEC_REVIEW", (
        f"Expected stage SPEC_REVIEW but got {manifest['stage']!r}; "
        "cmd_approve_stage must atomically advance the stage before returning"
    )


def test_spec_normalization_without_fast_track_blocked_by_gate(tmp_path: Path):
    """Gate enforcement: without fast_track and without planner-spec receipt,
    SPEC_NORMALIZATION -> SPEC_REVIEW is refused and command exits 1.
    The stage must remain SPEC_NORMALIZATION.
    """
    task_dir = _make_task(tmp_path, stage="SPEC_NORMALIZATION", fast_track=False)
    r = _run(tmp_path, "approve-stage", str(task_dir), "SPEC_NORMALIZATION")
    assert r.returncode == 1
    assert "planner-spec" in r.stderr or "SPEC_REVIEW" in r.stderr

    manifest = json.loads((task_dir / "manifest.json").read_text())
    assert manifest["stage"] == "SPEC_NORMALIZATION", (
        "Stage must not advance when gate check fails"
    )


# ---------------------------------------------------------------------------
# SPEC_REVIEW -> PLANNING (existing path, now guaranteed atomic too)
# ---------------------------------------------------------------------------


def test_spec_review_advances_to_planning_before_return(tmp_path: Path):
    """approve_stage on SPEC_REVIEW advances to PLANNING before exit."""
    task_dir = _make_task(tmp_path, stage="SPEC_REVIEW")
    r = _run(tmp_path, "approve-stage", str(task_dir), "SPEC_REVIEW")
    assert r.returncode == 0, r.stderr

    receipt_path = task_dir / "receipts" / "human-approval-SPEC_REVIEW.json"
    assert receipt_path.exists()

    manifest = json.loads((task_dir / "manifest.json").read_text())
    assert manifest["stage"] == "PLANNING", (
        f"Expected PLANNING, got {manifest['stage']!r}"
    )


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_unknown_stage_rejected_stage_unchanged(tmp_path: Path):
    """An unknown stage name exits 1 and leaves the manifest stage unchanged."""
    task_dir = _make_task(tmp_path, stage="SPEC_NORMALIZATION", fast_track=True)
    r = _run(tmp_path, "approve-stage", str(task_dir), "BOGUS_APPROVE_STAGE")
    assert r.returncode == 1
    assert "unknown stage" in r.stderr

    manifest = json.loads((task_dir / "manifest.json").read_text())
    assert manifest["stage"] == "SPEC_NORMALIZATION"


def test_missing_artifact_exits_one_stage_unchanged(tmp_path: Path):
    """Missing spec.md exits 1; stage must not change."""
    task_dir = _make_task(tmp_path, stage="SPEC_NORMALIZATION", fast_track=True)
    (task_dir / "spec.md").unlink()
    r = _run(tmp_path, "approve-stage", str(task_dir), "SPEC_NORMALIZATION")
    assert r.returncode == 1
    assert "missing artifact" in r.stderr or "spec.md" in r.stderr

    manifest = json.loads((task_dir / "manifest.json").read_text())
    assert manifest["stage"] == "SPEC_NORMALIZATION"


def test_success_stdout_names_new_stage(tmp_path: Path):
    """Stdout on success must mention the next stage so callers can parse it."""
    task_dir = _make_task(tmp_path, stage="SPEC_NORMALIZATION", fast_track=True)
    r = _run(tmp_path, "approve-stage", str(task_dir), "SPEC_NORMALIZATION")
    assert r.returncode == 0, r.stderr
    assert "SPEC_REVIEW" in r.stdout
