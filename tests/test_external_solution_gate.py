"""TDD-first tests for the external-solution gate written_at field (task-20260507-005).

AC coverage:
  AC 5 — _compute_external_solution_gate appends written_at ISO 8601 UTC timestamp
  AC 11 — backward-compat skip fires when written_at absent or malformed

These tests are RED until ctl.py is updated with the written_at field.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))


def _make_task_dir(tmp_path: Path, *, search_recommended: bool = True) -> Path:
    """Create a minimal task dir that satisfies _compute_external_solution_gate."""
    task_dir = tmp_path / ".dynos" / "task-20260507-999"
    task_dir.mkdir(parents=True)
    # Manifest with trigger terms so search_recommended=true
    raw_input = "hystrix circuit breaker external library" if search_recommended else "fix typo in README"
    manifest = {
        "task_id": task_dir.name,
        "stage": "SPEC_NORMALIZATION",
        "classification": {
            "type": "feature",
            "risk_level": "medium",
            "domains": ["backend"],
        },
        "raw_input": raw_input,
    }
    (task_dir / "manifest.json").write_text(json.dumps(manifest))
    return task_dir


# ---------------------------------------------------------------------------
# AC 5: _compute_external_solution_gate includes written_at
# ---------------------------------------------------------------------------

def test_external_solution_gate_includes_written_at(tmp_path: Path) -> None:
    """AC 5: The gate dict returned by _compute_external_solution_gate has written_at."""
    from ctl import _compute_external_solution_gate
    task_dir = _make_task_dir(tmp_path, search_recommended=True)
    gate = _compute_external_solution_gate(task_dir)
    assert "written_at" in gate, (
        "_compute_external_solution_gate must include written_at in the gate dict"
    )


def test_external_solution_gate_written_at_iso8601_utc(tmp_path: Path) -> None:
    """AC 5: The written_at value is a parseable ISO 8601 UTC datetime string."""
    from ctl import _compute_external_solution_gate
    task_dir = _make_task_dir(tmp_path, search_recommended=True)
    gate = _compute_external_solution_gate(task_dir)
    written_at = gate.get("written_at", "")
    assert isinstance(written_at, str), "written_at must be a string"
    assert len(written_at) > 0, "written_at must not be empty"
    # Must parse as UTC ISO 8601
    parsed = datetime.fromisoformat(written_at.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None, "written_at must be timezone-aware (UTC)"


def test_external_solution_gate_written_at_present_in_written_json(tmp_path: Path) -> None:
    """AC 5: written_at is present in the external-solution-gate.json written to disk."""
    import subprocess
    task_dir = _make_task_dir(tmp_path, search_recommended=True)
    result = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "ctl.py"), "run-external-solution-gate", str(task_dir)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"run-external-solution-gate failed: {result.stderr}"
    gate_path = task_dir / "external-solution-gate.json"
    assert gate_path.exists(), "external-solution-gate.json must be written"
    gate = json.loads(gate_path.read_text())
    assert "written_at" in gate, "written_at must be present in written gate JSON"
    parsed = datetime.fromisoformat(gate["written_at"].replace("Z", "+00:00"))
    assert parsed.tzinfo is not None


# ---------------------------------------------------------------------------
# AC 11: backward-compat skip behavior (tests call _check_web_tool_evidence
#         logic indirectly, since the new helpers don't exist yet)
# ---------------------------------------------------------------------------

def test_backward_compat_skip_fires_when_written_at_absent(tmp_path: Path) -> None:
    """AC 11: When gate.written_at is absent, cross-check is skipped and stdout says so."""
    import subprocess
    task_dir = _make_task_dir(tmp_path, search_recommended=True)
    # Write gate WITHOUT written_at (legacy format)
    gate = {
        "search_recommended": True,
        "search_used": False,
        "query_reason": "external search recommended",
        "candidates": [],
        "recommended_choice": None,
        "decision_basis": {},
    }
    (task_dir / "external-solution-gate.json").write_text(json.dumps(gate))
    # Write a minimal receipt so the receipt-existence check passes
    receipts_dir = task_dir / "receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    receipt = {
        "step": "search-conducted",
        "ts": "2026-05-07T12:00:00Z",
        "valid": True,
        "query": "test query",
        "search_used": True,
    }
    (receipts_dir / "search-conducted.json").write_text(json.dumps(receipt))
    # Write minimal spec.md
    (task_dir / "spec.md").write_text("# Spec\n\n## Acceptance Criteria\n\n1. AC one.\n")
    result = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "ctl.py"), "run-spec-ready", str(task_dir)],
        capture_output=True, text=True,
        cwd=str(ROOT),
    )
    # The skip message must appear on stdout
    assert "legacy gate file" in result.stdout, (
        f"Expected 'legacy gate file' in stdout.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_backward_compat_skip_fires_and_warns_when_written_at_malformed(tmp_path: Path) -> None:
    """AC 11: When gate.written_at is unparseable, cross-check skipped with malformed warning."""
    import subprocess
    task_dir = _make_task_dir(tmp_path, search_recommended=True)
    gate = {
        "search_recommended": True,
        "search_used": False,
        "query_reason": "external search recommended",
        "candidates": [],
        "recommended_choice": None,
        "decision_basis": {},
        "written_at": "not-a-date",  # malformed
    }
    (task_dir / "external-solution-gate.json").write_text(json.dumps(gate))
    receipts_dir = task_dir / "receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    receipt = {
        "step": "search-conducted",
        "ts": "2026-05-07T12:00:00Z",
        "valid": True,
        "query": "test query",
        "search_used": True,
    }
    (receipts_dir / "search-conducted.json").write_text(json.dumps(receipt))
    (task_dir / "spec.md").write_text("# Spec\n\n## Acceptance Criteria\n\n1. AC one.\n")
    result = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "ctl.py"), "run-spec-ready", str(task_dir)],
        capture_output=True, text=True,
        cwd=str(ROOT),
    )
    # Must emit malformed_written_at skip message on stdout
    assert "malformed written_at" in result.stdout, (
        f"Expected 'malformed written_at' in stdout.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
