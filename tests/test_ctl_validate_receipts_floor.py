"""Tests for ctl validate-receipts floor enforcement (AC 25).

The CLI now reports per-row contract_version, flags floor violations as
'FLOOR_VIOLATION:' lines on stderr, and uses three exit codes:
  0 — clean
  1 — gap (existing semantic)
  2 — floor violation (NEW; takes precedence over gap)
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


def _make_task(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260419-VR"
    td.mkdir(parents=True)
    (td / "manifest.json").write_text(json.dumps({
        "task_id": td.name,
        "stage": "EXECUTION",
    }))
    return td


def _write_raw_receipt(td: Path, step: str, *, version: int, valid: bool = True,
                       **extra):
    rd = td / "receipts"
    rd.mkdir(parents=True, exist_ok=True)
    payload = {
        "step": step,
        "ts": "2025-01-01T00:00:00Z",
        "valid": valid,
        "contract_version": version,
        **extra,
    }
    (rd / f"{step}.json").write_text(json.dumps(payload))


def _run_ctl(td: Path, *args: str, dynos_home: Path | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(ROOT / "hooks")}
    if dynos_home:
        env["DYNOS_HOME"] = str(dynos_home)
    return subprocess.run(
        [sys.executable, str(CTL), "validate-receipts", str(td), *args],
        capture_output=True, text=True, check=False, env=env,
    )


def test_v1_executor_receipt_flagged_floor_violation_exit_2(tmp_path: Path):
    """AC 25: v1 executor-seg-1 receipt → FLOOR_VIOLATION on stderr, exit 2."""
    td = _make_task(tmp_path)
    _write_raw_receipt(td, "executor-seg-1", version=1, segment_id="seg-1")
    proc = _run_ctl(td)
    assert proc.returncode == 2, f"expected exit 2, got {proc.returncode}: {proc.stderr}"
    assert "FLOOR_VIOLATION" in proc.stderr
    assert "executor-seg-1" in proc.stderr
    assert "version=1" in proc.stderr
    assert "required=2" in proc.stderr


def test_v2_executor_receipt_passes_floor(tmp_path: Path):
    """AC 25: v2 receipts at floor → no FLOOR_VIOLATION; may exit 0 or 1 depending on gaps."""
    td = _make_task(tmp_path)
    _write_raw_receipt(td, "executor-seg-1", version=2, segment_id="seg-1")
    proc = _run_ctl(td)
    # No floor violation, so exit code should NOT be 2.
    assert proc.returncode != 2, \
        f"unexpected floor violation: {proc.stderr}"
    assert "FLOOR_VIOLATION" not in proc.stderr


def test_v3_receipt_passes_floor(tmp_path: Path):
    """AC 25: v3 (current contract) passes the floor."""
    td = _make_task(tmp_path)
    _write_raw_receipt(td, "executor-seg-1", version=3, segment_id="seg-1")
    proc = _run_ctl(td)
    assert proc.returncode != 2
    assert "FLOOR_VIOLATION" not in proc.stderr


def test_floor_violation_exit_2_takes_precedence_over_gap(tmp_path: Path):
    """AC 25: even when there are also gaps, exit code 2 wins over 1."""
    td = _make_task(tmp_path)
    # Write a v1 receipt (floor violation) AND have many gaps (no plan-validated etc.).
    _write_raw_receipt(td, "executor-seg-1", version=1, segment_id="seg-1")
    proc = _run_ctl(td)
    assert proc.returncode == 2, f"expected 2 (floor takes precedence), got {proc.returncode}"


def test_stdout_carries_per_row_contract_version(tmp_path: Path):
    """AC 25: stdout JSON has 'receipts' list with contract_version per row."""
    td = _make_task(tmp_path)
    _write_raw_receipt(td, "executor-seg-1", version=2, segment_id="seg-1")
    proc = _run_ctl(td)
    payload = json.loads(proc.stdout)
    assert "receipts" in payload
    assert "floor_violations" in payload
    rows = payload["receipts"]
    assert len(rows) >= 1
    row = next(r for r in rows if r["step"] == "executor-seg-1")
    assert row["contract_version"] == 2
    assert row["required_floor"] == 2
    assert row["floor_violation"] is False


def test_floor_violations_listed_in_stdout_json(tmp_path: Path):
    """AC 25: stdout JSON 'floor_violations' summarises every below-floor receipt."""
    td = _make_task(tmp_path)
    _write_raw_receipt(td, "executor-seg-1", version=1, segment_id="seg-1")
    proc = _run_ctl(td)
    payload = json.loads(proc.stdout)
    fvs = payload["floor_violations"]
    assert len(fvs) == 1
    assert fvs[0]["step"] == "executor-seg-1"
    assert fvs[0]["version"] == 1
    assert fvs[0]["required"] == 2
