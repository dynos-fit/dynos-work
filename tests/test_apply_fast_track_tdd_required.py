"""Tests for apply_fast_track tdd_required auto-derivation (AC 12).

When classification.tdd_required is absent, it is set to True for
high/critical risk and False otherwise. Explicit values (True OR False)
are preserved. Written alongside fast_track in one atomic write.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_validate import apply_fast_track  # noqa: E402


def _write_manifest(tmp_path: Path, classification: dict) -> Path:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260419-FT"
    td.mkdir(parents=True)
    (td / "manifest.json").write_text(json.dumps({
        "task_id": td.name,
        "stage": "CLASSIFY_AND_SPEC",
        "classification": classification,
    }))
    return td


def test_high_absent_tdd_required_derived_true(tmp_path: Path):
    """AC 12: risk=high, tdd_required absent → persisted True."""
    td = _write_manifest(tmp_path, {"risk_level": "high", "domains": ["backend"]})
    apply_fast_track(td)
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["classification"]["tdd_required"] is True


def test_critical_absent_tdd_required_derived_true(tmp_path: Path):
    """AC 12: risk=critical, tdd_required absent → persisted True."""
    td = _write_manifest(tmp_path, {"risk_level": "critical", "domains": ["ui"]})
    apply_fast_track(td)
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["classification"]["tdd_required"] is True


def test_medium_absent_tdd_required_derived_false(tmp_path: Path):
    """AC 12: risk=medium, tdd_required absent → persisted False."""
    td = _write_manifest(tmp_path, {"risk_level": "medium", "domains": ["backend"]})
    apply_fast_track(td)
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["classification"]["tdd_required"] is False


def test_low_absent_tdd_required_derived_false(tmp_path: Path):
    """AC 12: risk=low, tdd_required absent → False (single-domain fast_track)."""
    td = _write_manifest(tmp_path, {"risk_level": "low", "domains": ["backend"]})
    apply_fast_track(td)
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["classification"]["tdd_required"] is False
    assert manifest["fast_track"] is True


def test_explicit_true_preserved_even_for_low(tmp_path: Path):
    """AC 12: explicit tdd_required=True is preserved regardless of risk."""
    td = _write_manifest(tmp_path, {
        "risk_level": "low", "domains": ["backend"], "tdd_required": True,
    })
    apply_fast_track(td)
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["classification"]["tdd_required"] is True


def test_explicit_false_preserved_even_for_critical(tmp_path: Path):
    """AC 12: explicit tdd_required=False preserved even when risk is critical."""
    td = _write_manifest(tmp_path, {
        "risk_level": "critical", "domains": ["backend"], "tdd_required": False,
    })
    apply_fast_track(td)
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["classification"]["tdd_required"] is False


def test_one_atomic_write_contains_both_fields(tmp_path: Path):
    """AC 12: fast_track AND tdd_required persisted in a single atomic write."""
    td = _write_manifest(tmp_path, {"risk_level": "high", "domains": ["backend"]})
    apply_fast_track(td)
    manifest = json.loads((td / "manifest.json").read_text())
    # Both fields present; tdd_required derived True, fast_track False
    # (high-risk is never fast-track eligible by spec).
    assert "fast_track" in manifest
    assert "tdd_required" in manifest["classification"]
    assert manifest["fast_track"] is False
    assert manifest["classification"]["tdd_required"] is True


def test_non_dict_classification_not_mutated(tmp_path: Path):
    """Defensive: non-dict classification does not cause crash or mutation."""
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260419-FTx"
    td.mkdir(parents=True)
    (td / "manifest.json").write_text(json.dumps({
        "task_id": td.name,
        "stage": "CLASSIFY_AND_SPEC",
        "classification": "not-a-dict",
    }))
    # Should not raise; classification remains unchanged.
    apply_fast_track(td)
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["classification"] == "not-a-dict"
