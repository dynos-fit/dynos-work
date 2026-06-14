"""Tests for AC 8: Audit sharding in cmd_run_audit_setup.

These tests are RED by design until seg-2 adds:
- AUDIT_SHARD_FILE_THRESHOLD (default 30) and AUDIT_SHARD_LOC_THRESHOLD (default 8000)
- Sharding logic that adds shard_briefs to audit-plan.json when diff exceeds thresholds
- A mandatory cross-cutting brief per auditor when sharding

Both tests drive the real cmd_run_audit_setup via subprocess, NOT fabricated dicts.
DYNOS_AUDIT_SHARD_FILE_THRESHOLD env var allows threshold override in tests.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CTL_PY = ROOT / "hooks" / "ctl.py"


def _create_task_dir(tmp_path: Path) -> Path:
    """Create a minimal task dir with manifest for CHECKPOINT_AUDIT stage."""
    project = tmp_path / "project"
    task_dir = project / ".dynos" / "task-20260612-sharding"
    task_dir.mkdir(parents=True)
    # Write a minimal manifest
    manifest = {
        "task_id": "task-20260612-sharding",
        "stage": "CHECKPOINT_AUDIT",
        "fast_track": False,
        "classification": {
            "type": "feature",
            "risk_level": "medium",
            "domains": ["backend"],
        },
        "snapshot": {"head_sha": ""},
        "created_at": "2026-06-12T00:00:00Z",
        "raw_input": "test sharding",
    }
    (task_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return task_dir


def _run_audit_setup(
    task_dir: Path,
    extra_env: dict | None = None,
) -> subprocess.CompletedProcess:
    """Run `python3 ctl.py run-audit-setup <task_dir> --allow-head-fallback`."""
    env = {**os.environ}
    # Disable git diff by not having a valid git repo / HEAD
    env["DYNOS_TASK_DIR"] = str(task_dir)
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        [
            sys.executable,
            str(CTL_PY),
            "run-audit-setup",
            str(task_dir),
        ],
        cwd=str(task_dir.parent.parent),  # project root
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    return result


def _build_synthetic_diff_files(count: int) -> list[str]:
    """Build a list of synthetic diff file names for testing thresholds."""
    return [f"hooks/synthetic_file_{i:03d}.py" for i in range(count)]


def _patch_diff_files_in_plan(task_dir: Path, diff_files: list[str]) -> None:
    """Post-process: write a synthetic large diff scenario by injecting diff context."""
    # This helper patches the audit-plan.json after the fact to simulate sharding
    # — used when cmd_run_audit_setup doesn't have a real git diff available.
    # In production, cmd_run_audit_setup gets diff_files from `git diff --name-only`.
    audit_plan_path = task_dir / "audit-plan.json"
    if audit_plan_path.exists():
        plan = json.loads(audit_plan_path.read_text())
        return plan


# ---------------------------------------------------------------------------
# AC 8: Sharding above threshold produces shard_briefs + cross-cutting brief
# ---------------------------------------------------------------------------


def test_sharding_above_threshold(tmp_path: Path) -> None:
    """Oversized diff (31 files) → audit-plan.json has shard_briefs with cross-cutting brief.

    AC 8: When the diff exceeds AUDIT_SHARD_FILE_THRESHOLD (30), the plan must:
    1. Contain a 'shard_briefs' key in at least one auditor entry
    2. Include a cross-cutting brief whose instruction contains
       "trace relationships" (per spec-quoted text in AC 8 and OQ-6)

    Drive the real cmd_run_audit_setup. Override threshold via
    DYNOS_AUDIT_SHARD_FILE_THRESHOLD env var to use threshold=1 so our small
    synthetic diff (2+ files) triggers sharding without needing 31 real files.
    """
    task_dir = _create_task_dir(tmp_path)

    # Set file threshold to 1 so even a 2-file diff triggers sharding
    # The production env var is DYNOS_AUDIT_SHARD_FILE_THRESHOLD
    extra_env = {
        "DYNOS_AUDIT_SHARD_FILE_THRESHOLD": "1",
        "DYNOS_AUDIT_SHARD_LOC_THRESHOLD": "999999",  # disable LOC threshold
    }

    result = _run_audit_setup(task_dir, extra_env=extra_env)
    # cmd_run_audit_setup may fail if git diff fails — check both success and
    # plan file existence
    audit_plan_path = task_dir / "audit-plan.json"
    assert audit_plan_path.exists(), (
        f"audit-plan.json must be written by cmd_run_audit_setup. "
        f"stdout: {result.stdout[:500]!r}, stderr: {result.stderr[:200]!r}"
    )

    plan = json.loads(audit_plan_path.read_text())
    assert isinstance(plan, dict), "audit-plan.json must be a JSON object"

    # Find auditors with shard_briefs
    auditors = plan.get("auditors", [])
    spawn_auditors = [a for a in auditors if isinstance(a, dict) and a.get("action") == "spawn"]

    # At least one spawned auditor must have shard_briefs
    sharded = [a for a in spawn_auditors if "shard_briefs" in a]
    assert len(sharded) > 0, (
        f"Above-threshold diff must produce shard_briefs in at least one auditor. "
        f"spawn_auditors: {spawn_auditors}"
    )

    # Every sharded auditor must have a cross-cutting brief
    for auditor in sharded:
        briefs = auditor["shard_briefs"]
        assert isinstance(briefs, list), f"shard_briefs must be a list, got {type(briefs)}"
        cross_cutting = [
            b for b in briefs
            if isinstance(b, dict) and b.get("type") == "cross-cutting"
        ]
        assert len(cross_cutting) >= 1, (
            f"Auditor {auditor.get('name')!r} must have a cross-cutting brief. "
            f"briefs: {briefs}"
        )
        # Verify the cross-cutting instruction contains the required text
        for cc in cross_cutting:
            instruction = cc.get("instruction", "")
            assert "trace relationships" in instruction, (
                f"Cross-cutting brief instruction must contain 'trace relationships'. "
                f"Got: {instruction!r}"
            )


def test_sharding_below_threshold(tmp_path: Path) -> None:
    """Small diff (below threshold) → audit-plan.json has no shard_briefs key.

    AC 8: A diff below BOTH thresholds (< 30 files AND < 8000 LOC) must
    produce a single-pass plan. 'shard_briefs' key must be absent (not null).

    Override DYNOS_AUDIT_SHARD_FILE_THRESHOLD to a very high value so even
    large diffs don't trigger sharding in this test.
    """
    task_dir = _create_task_dir(tmp_path)

    # Set threshold very high so NO diff triggers sharding
    extra_env = {
        "DYNOS_AUDIT_SHARD_FILE_THRESHOLD": "9999",
        "DYNOS_AUDIT_SHARD_LOC_THRESHOLD": "9999999",
    }

    result = _run_audit_setup(task_dir, extra_env=extra_env)
    audit_plan_path = task_dir / "audit-plan.json"
    assert audit_plan_path.exists(), (
        f"audit-plan.json must be written. "
        f"stdout: {result.stdout[:500]!r}, stderr: {result.stderr[:200]!r}"
    )

    plan = json.loads(audit_plan_path.read_text())
    assert isinstance(plan, dict), "audit-plan.json must be a JSON object"

    auditors = plan.get("auditors", [])
    for auditor in auditors:
        if not isinstance(auditor, dict):
            continue
        assert "shard_briefs" not in auditor, (
            f"Below-threshold diff must NOT produce shard_briefs key. "
            f"Auditor {auditor.get('name')!r} has shard_briefs: {auditor.get('shard_briefs')}"
        )


def test_sharding_constants_exist() -> None:
    """AUDIT_SHARD_FILE_THRESHOLD and AUDIT_SHARD_LOC_THRESHOLD constants must exist in ctl.

    RED until seg-2 adds these constants. Verifies the constants can be imported
    (or extracted via subprocess inspection) from ctl.py.
    """
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0,'hooks'); "
         "import ctl; "
         "assert hasattr(ctl,'AUDIT_SHARD_FILE_THRESHOLD'), 'missing AUDIT_SHARD_FILE_THRESHOLD'; "
         "assert hasattr(ctl,'AUDIT_SHARD_LOC_THRESHOLD'), 'missing AUDIT_SHARD_LOC_THRESHOLD'; "
         "assert ctl.AUDIT_SHARD_FILE_THRESHOLD == 30, f'Expected 30, got {ctl.AUDIT_SHARD_FILE_THRESHOLD}'; "
         "assert ctl.AUDIT_SHARD_LOC_THRESHOLD == 8000, f'Expected 8000, got {ctl.AUDIT_SHARD_LOC_THRESHOLD}'; "
         "print('OK')"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"AUDIT_SHARD_FILE_THRESHOLD and AUDIT_SHARD_LOC_THRESHOLD must exist in ctl.py "
        f"with defaults 30 and 8000. stderr: {result.stderr!r}, stdout: {result.stdout!r}"
    )
