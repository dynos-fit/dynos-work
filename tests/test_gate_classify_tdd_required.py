"""Tests for CLASSIFY_AND_SPEC -> SPEC_NORMALIZATION gate (AC 9).

High/critical risk tasks must have classification.tdd_required explicitly set
(True OR False) before they can leave CLASSIFY_AND_SPEC. Low/medium risk
tasks can have tdd_required absent without refusal. force=True bypasses.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_core import transition_task  # noqa: E402


def _setup(tmp_path: Path, *, risk: str,
           tdd_required: bool | None = None) -> Path:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260419-CS"
    td.mkdir(parents=True)
    classification: dict = {"risk_level": risk}
    if tdd_required is not None:
        classification["tdd_required"] = tdd_required
    (td / "manifest.json").write_text(json.dumps({
        "task_id": td.name,
        "stage": "CLASSIFY_AND_SPEC",
        "classification": classification,
    }))
    return td


def test_critical_absent_tdd_required_refused(tmp_path: Path):
    """AC 9: critical + tdd_required missing → refuse."""
    td = _setup(tmp_path, risk="critical", tdd_required=None)
    with pytest.raises(ValueError) as exc_info:
        transition_task(td, "SPEC_NORMALIZATION")
    msg = str(exc_info.value)
    assert "tdd_required" in msg
    assert "critical" in msg


def test_high_absent_tdd_required_refused(tmp_path: Path):
    """AC 9: high risk + tdd_required missing → refuse."""
    td = _setup(tmp_path, risk="high", tdd_required=None)
    with pytest.raises(ValueError) as exc_info:
        transition_task(td, "SPEC_NORMALIZATION")
    msg = str(exc_info.value)
    assert "tdd_required" in msg


def test_critical_with_tdd_required_true_passes(tmp_path: Path):
    """AC 9: critical + tdd_required=True → pass."""
    td = _setup(tmp_path, risk="critical", tdd_required=True)
    transition_task(td, "SPEC_NORMALIZATION")
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == "SPEC_NORMALIZATION"


def test_critical_with_tdd_required_false_passes(tmp_path: Path):
    """AC 9: critical + tdd_required=False (explicit) → pass."""
    td = _setup(tmp_path, risk="critical", tdd_required=False)
    transition_task(td, "SPEC_NORMALIZATION")
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == "SPEC_NORMALIZATION"


def test_medium_absent_tdd_required_passes(tmp_path: Path):
    """AC 9: medium risk + tdd_required missing → pass (optional for med/low)."""
    td = _setup(tmp_path, risk="medium", tdd_required=None)
    transition_task(td, "SPEC_NORMALIZATION")
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == "SPEC_NORMALIZATION"


def test_low_absent_tdd_required_passes(tmp_path: Path):
    """AC 9: low risk + tdd_required missing → pass."""
    td = _setup(tmp_path, risk="low", tdd_required=None)
    transition_task(td, "SPEC_NORMALIZATION")
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == "SPEC_NORMALIZATION"


def test_force_true_bypasses_gate(tmp_path: Path):
    """AC 9: force=True bypasses the tdd_required requirement."""
    td = _setup(tmp_path, risk="critical", tdd_required=None)
    transition_task(
        td,
        "SPEC_NORMALIZATION",
        force=True,
        force_reason="test: tdd_required classify gate bypass",
        force_approver="test-suite",
    )
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == "SPEC_NORMALIZATION"
