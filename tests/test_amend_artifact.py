"""Unit tests for AC27: amend-artifact subcommand.

Tests assert:
- The command succeeds when --reason is provided and the artifact exists.
- The canonical receipt's artifact_sha256 reflects the new file hash.
- An amendment record is appended to canonical receipt amendments list.
- The command fails with exit 1 when --reason is absent.
- The command fails with exit 1 when the artifact file does not exist.
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

SPEC_AMENDED = SPEC_TEMPLATE + "\n## Amendment Notes\nFixed typo.\n"


def _make_task(tmp_path: Path) -> Path:
    """Create a minimal task directory with spec.md and a spec-validated receipt."""
    task_dir = tmp_path / ".dynos" / "task-20260423-amend"
    task_dir.mkdir(parents=True)
    manifest = {
        "task_id": "task-20260423-amend",
        "created_at": "2026-04-23T00:00:00Z",
        "title": "Amend artifact test",
        "raw_input": "x",
        "stage": "SPEC_REVIEW",
        "classification": {
            "type": "feature",
            "domains": ["backend"],
            "risk_level": "low",
            "notes": "n",
            "tdd_required": False,
        },
        "retry_counts": {},
        "blocked_reason": None,
        "completion_at": None,
    }
    (task_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    (task_dir / "spec.md").write_text(SPEC_TEMPLATE)

    # Write a minimal spec-validated receipt to update in-place.
    receipts_dir = task_dir / "receipts"
    receipts_dir.mkdir(parents=True)
    import hashlib
    sha = hashlib.sha256(SPEC_TEMPLATE.encode()).hexdigest()
    receipt = {
        "step": "spec-validated",
        "ts": "2026-04-23T00:00:00Z",
        "valid": True,
        "contract_version": 5,
        "criteria_count": 2,
        "spec_sha256": sha,
    }
    (receipts_dir / "spec-validated.json").write_text(json.dumps(receipt, indent=2))
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
# AC27a-c-d-e-f: success path
# ---------------------------------------------------------------------------

def test_amend_artifact_success(tmp_path: Path) -> None:
    """amend-artifact exits 0 when --reason is provided and artifact exists."""
    task_dir = _make_task(tmp_path)
    # Overwrite spec.md with amended content so hash changes.
    (task_dir / "spec.md").write_text(SPEC_AMENDED)

    result = _run(
        tmp_path,
        "amend-artifact",
        str(task_dir),
        "spec",
        "--reason",
        "Fixed a typo in acceptance criteria",
    )
    assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"


def test_amend_artifact_canonical_receipt_sha256_updated(tmp_path: Path) -> None:
    """The canonical receipt's artifact_sha256 reflects the new file hash after amendment."""
    import hashlib

    task_dir = _make_task(tmp_path)
    (task_dir / "spec.md").write_text(SPEC_AMENDED)
    expected_sha = hashlib.sha256(SPEC_AMENDED.encode()).hexdigest()

    result = _run(
        tmp_path,
        "amend-artifact",
        str(task_dir),
        "spec",
        "--reason",
        "Updating spec post approval",
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"

    canonical = json.loads((task_dir / "receipts" / "spec-validated.json").read_text())
    assert canonical.get("artifact_sha256") == expected_sha, (
        f"Expected artifact_sha256={expected_sha!r}, got {canonical.get('artifact_sha256')!r}"
    )


def test_amend_artifact_amendments_appended(tmp_path: Path) -> None:
    """An amendment record is appended to the canonical receipt's amendments list."""
    task_dir = _make_task(tmp_path)
    (task_dir / "spec.md").write_text(SPEC_AMENDED)

    result = _run(
        tmp_path,
        "amend-artifact",
        str(task_dir),
        "spec",
        "--reason",
        "Clarified scope section",
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"

    canonical = json.loads((task_dir / "receipts" / "spec-validated.json").read_text())
    amendments = canonical.get("amendments", [])
    assert isinstance(amendments, list), "amendments must be a list"
    assert len(amendments) == 1, f"Expected 1 amendment, got {len(amendments)}"

    record = amendments[0]
    assert record["artifact_name"] == "spec"
    assert record["amended_by"] == "human"
    assert record["reason"] == "Clarified scope section"
    assert "artifact_sha256_before" in record
    assert "artifact_sha256_after" in record
    assert "amended_at" in record


def test_amend_artifact_amendment_receipt_written(tmp_path: Path) -> None:
    """An amendment receipt file is written to receipts/amend-spec-<ts>.json."""
    task_dir = _make_task(tmp_path)
    (task_dir / "spec.md").write_text(SPEC_AMENDED)

    result = _run(
        tmp_path,
        "amend-artifact",
        str(task_dir),
        "spec",
        "--reason",
        "Post-approval correction",
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"

    receipts = list((task_dir / "receipts").glob("amend-spec-*.json"))
    assert len(receipts) == 1, f"Expected 1 amendment receipt, found: {[r.name for r in receipts]}"

    amend_data = json.loads(receipts[0].read_text())
    assert amend_data["artifact_name"] == "spec"
    assert amend_data["amended_by"] == "human"
    assert amend_data["reason"] == "Post-approval correction"


# ---------------------------------------------------------------------------
# AC27b: failure paths
# ---------------------------------------------------------------------------

def test_amend_artifact_fails_without_reason(tmp_path: Path) -> None:
    """amend-artifact exits 1 when --reason is absent."""
    task_dir = _make_task(tmp_path)

    result = _run(
        tmp_path,
        "amend-artifact",
        str(task_dir),
        "spec",
        # No --reason argument.
    )
    assert result.returncode == 1, (
        f"Expected exit 1 when --reason is absent, got {result.returncode}\n"
        f"stderr: {result.stderr}\nstdout: {result.stdout}"
    )
    assert "reason" in result.stderr.lower(), (
        f"Expected error message mentioning 'reason', got: {result.stderr!r}"
    )


def test_amend_artifact_fails_with_blank_reason(tmp_path: Path) -> None:
    """amend-artifact exits 1 when --reason is blank (whitespace only)."""
    task_dir = _make_task(tmp_path)

    result = _run(
        tmp_path,
        "amend-artifact",
        str(task_dir),
        "spec",
        "--reason",
        "   ",
    )
    assert result.returncode == 1, (
        f"Expected exit 1 when --reason is blank, got {result.returncode}\n"
        f"stderr: {result.stderr}\nstdout: {result.stdout}"
    )
    assert "reason" in result.stderr.lower(), (
        f"Expected error message mentioning 'reason', got: {result.stderr!r}"
    )


def test_amend_artifact_fails_when_artifact_missing(tmp_path: Path) -> None:
    """amend-artifact exits 1 when the artifact file does not exist."""
    task_dir = _make_task(tmp_path)
    # Remove the spec.md so the artifact is missing.
    (task_dir / "spec.md").unlink()

    result = _run(
        tmp_path,
        "amend-artifact",
        str(task_dir),
        "spec",
        "--reason",
        "This should fail because file is gone",
    )
    assert result.returncode == 1, (
        f"Expected exit 1 when artifact is missing, got {result.returncode}\n"
        f"stderr: {result.stderr}\nstdout: {result.stdout}"
    )
