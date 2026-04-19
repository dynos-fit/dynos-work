"""Tests for surviving_blocking as set intersection (AC 15).

quality_score = 1.0 - (surviving_blocking / total_blocking) where
surviving_blocking = |pre_repair_set ∩ current_blocking_set|.

Pre-repair set lives at task_dir/repair/pre-repair-blocking.json (a JSON
list of finding ids). When the file is absent or malformed, fall back to
total_blocking (every blocking finding "survives" because there was no
repair to verify).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_validate import compute_reward  # noqa: E402


def _make_task(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260419-SB"
    td.mkdir(parents=True)
    (td / "manifest.json").write_text(json.dumps({
        "task_id": td.name,
        "stage": "DONE",
        "classification": {"risk_level": "medium", "domains": [], "type": "feature"},
    }))
    return td


def _write_report(td: Path, auditor: str, findings: list[dict]) -> Path:
    rd = td / "audit-reports"
    rd.mkdir(parents=True, exist_ok=True)
    path = rd / f"{auditor}.json"
    path.write_text(json.dumps({"auditor_name": auditor, "findings": findings}))
    return path


def _write_pre_repair(td: Path, ids: list[str]) -> Path:
    rd = td / "repair"
    rd.mkdir(parents=True, exist_ok=True)
    path = rd / "pre-repair-blocking.json"
    path.write_text(json.dumps(ids))
    return path


def test_no_repair_file_means_all_blocking_survive(tmp_path: Path):
    """AC 15: pre-repair file absent → surviving_blocking = total_blocking."""
    td = _make_task(tmp_path)
    _write_report(td, "sec", [
        {"id": "F-1", "blocking": True},
        {"id": "F-2", "blocking": True},
        {"id": "F-3", "blocking": True},
    ])
    result = compute_reward(td)
    # 3 blocking, all surviving → quality_score = 1 - 3/3 = 0.0
    assert result["quality_score"] == 0.0


def test_partial_recovery_intersection(tmp_path: Path):
    """AC 15: pre-repair {A,B,C} + current blocking {A,D,E} → intersection={A}.
    surviving=1, total_blocking=3, quality_score = 1 - 1/3 = 0.6667."""
    td = _make_task(tmp_path)
    _write_pre_repair(td, ["F-A", "F-B", "F-C"])
    _write_report(td, "sec", [
        {"id": "F-A", "blocking": True},
        {"id": "F-D", "blocking": True},
        {"id": "F-E", "blocking": True},
    ])
    result = compute_reward(td)
    # surviving = |{A,B,C} ∩ {A,D,E}| = 1, total_blocking = 3.
    expected = round(1.0 - (1 / 3), 4)
    assert result["quality_score"] == expected, \
        f"expected {expected}, got {result['quality_score']}"


def test_full_recovery_zero_blocking(tmp_path: Path):
    """AC 15: total_blocking=0 → quality_score = 0.9 (clean-task default).
    Even with pre-repair set, the 0-blocking branch dominates."""
    td = _make_task(tmp_path)
    _write_pre_repair(td, ["F-A", "F-B"])
    _write_report(td, "sec", [
        {"id": "F-A", "blocking": False},
        {"id": "F-B", "blocking": False},
    ])
    result = compute_reward(td)
    assert result["quality_score"] == 0.9


def test_all_new_findings_zero_intersection(tmp_path: Path):
    """AC 15: pre-repair {A,B} + current {X,Y} → intersection=0;
    surviving=0, quality_score = 1 - 0/2 = 1.0."""
    td = _make_task(tmp_path)
    _write_pre_repair(td, ["F-A", "F-B"])
    _write_report(td, "sec", [
        {"id": "F-X", "blocking": True},
        {"id": "F-Y", "blocking": True},
    ])
    result = compute_reward(td)
    # total_blocking = 2, surviving = 0, quality_score = 1.0
    assert result["quality_score"] == 1.0


def test_malformed_pre_repair_json_falls_back(tmp_path: Path):
    """AC 15: corrupt JSON in pre-repair file → fall back to total_blocking."""
    td = _make_task(tmp_path)
    # Write malformed JSON
    rd = td / "repair"
    rd.mkdir(parents=True)
    (rd / "pre-repair-blocking.json").write_text("not valid json {{{")
    _write_report(td, "sec", [
        {"id": "F-1", "blocking": True},
        {"id": "F-2", "blocking": True},
    ])
    result = compute_reward(td)
    # Falls back to total_blocking=2 → quality_score = 1 - 2/2 = 0.0
    assert result["quality_score"] == 0.0


def test_wrong_shape_pre_repair_falls_back(tmp_path: Path):
    """AC 15: pre-repair JSON is dict (not list) → fall back."""
    td = _make_task(tmp_path)
    rd = td / "repair"
    rd.mkdir(parents=True)
    (rd / "pre-repair-blocking.json").write_text(json.dumps({"key": "value"}))
    _write_report(td, "sec", [
        {"id": "F-1", "blocking": True},
    ])
    result = compute_reward(td)
    assert result["quality_score"] == 0.0  # 1 surviving / 1 total


def test_pre_repair_strings_only_filtered(tmp_path: Path):
    """AC 15: non-string entries in pre-repair list are filtered out."""
    td = _make_task(tmp_path)
    rd = td / "repair"
    rd.mkdir(parents=True)
    # Mix of valid strings + invalid items
    (rd / "pre-repair-blocking.json").write_text(json.dumps([
        "F-A", 42, None, "", "F-B"
    ]))
    _write_report(td, "sec", [
        {"id": "F-A", "blocking": True},
        {"id": "F-Z", "blocking": True},
    ])
    result = compute_reward(td)
    # pre_repair_ids = {"F-A", "F-B"} (filtered), current_blocking = {"F-A", "F-Z"}
    # intersection = {"F-A"} → surviving=1, total=2 → quality = 1 - 1/2 = 0.5
    assert result["quality_score"] == 0.5
