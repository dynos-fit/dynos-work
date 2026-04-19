"""Tests for ctl validate-task gating on rules-corrupt sentinel (AC 18).

When .dynos/.rules_corrupt sentinel exists, validate-task refuses with
exit 1 and stderr ERROR message naming the persistent rules path.
Without sentinel → validate-task evaluates the artifacts normally.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CTL = ROOT / "hooks" / "ctl.py"


def _make_task_dir(tmp_path: Path) -> tuple[Path, Path]:
    """Returns (root, task_dir)."""
    root = tmp_path / "project"
    td = root / ".dynos" / "task-20260419-CT"
    td.mkdir(parents=True)
    # Minimal manifest so validate-task does not crash on absent file.
    (td / "manifest.json").write_text(json.dumps({
        "task_id": td.name,
        "stage": "FOUNDRY_INITIALIZED",
    }))
    return root, td


def _run_validate_task(root: Path, task_dir: Path, *,
                       dynos_home: Path) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "DYNOS_HOME": str(dynos_home),
        "PYTHONPATH": str(ROOT / "hooks"),
    }
    return subprocess.run(
        [sys.executable, str(CTL), "validate-task", str(task_dir)],
        capture_output=True, text=True, check=False, env=env,
    )


def test_sentinel_present_blocks_validate_task(tmp_path: Path):
    """AC 18: with sentinel, ctl validate-task exits 1 with stderr ERROR."""
    root, td = _make_task_dir(tmp_path)
    sentinel = root / ".dynos" / ".rules_corrupt"
    sentinel.write_text("2026-04-19T00:00:00Z JSONDecodeError: malformed\n")

    home = tmp_path / "dynos-home"
    home.mkdir()

    proc = _run_validate_task(root, td, dynos_home=home)
    assert proc.returncode == 1, f"unexpected exit {proc.returncode}: {proc.stderr}"
    assert "ERROR" in proc.stderr
    assert "prevention-rules.json" in proc.stderr
    assert "corrupt" in proc.stderr.lower()


def test_no_sentinel_validate_task_proceeds(tmp_path: Path):
    """AC 18: without sentinel, validate-task evaluates artifacts normally
    (may fail validation on its own merits, but NOT due to the sentinel
    block path — exit code is NOT 1 with the rules-corrupt error message)."""
    root, td = _make_task_dir(tmp_path)
    home = tmp_path / "dynos-home"
    home.mkdir()

    proc = _run_validate_task(root, td, dynos_home=home)
    # Even if validation reports artifact issues, the stderr must NOT
    # mention prevention-rules corruption.
    assert "prevention-rules.json" not in proc.stderr or "corrupt" not in proc.stderr.lower()
