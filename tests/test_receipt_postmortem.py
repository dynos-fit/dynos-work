"""Tests for the three postmortem receipt writers (AC 18)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_receipts import (  # noqa: E402
    receipt_postmortem_analysis,
    receipt_postmortem_generated,
    receipt_postmortem_skipped,
)


def _task_dir(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260418-PM"
    td.mkdir(parents=True)
    return td


def test_postmortem_generated_happy(tmp_path: Path):
    td = _task_dir(tmp_path)
    out = receipt_postmortem_generated(td, "a" * 64, "b" * 64, 3, 5)
    assert out.exists()
    payload = json.loads(out.read_text())
    assert payload["json_sha256"] == "a" * 64
    assert payload["md_sha256"] == "b" * 64
    assert payload["anomaly_count"] == 3
    assert payload["pattern_count"] == 5


def test_postmortem_generated_rejects_empty_hashes(tmp_path: Path):
    td = _task_dir(tmp_path)
    with pytest.raises(ValueError, match="json_sha256"):
        receipt_postmortem_generated(td, "", "b" * 64, 0, 0)
    with pytest.raises(ValueError, match="md_sha256"):
        receipt_postmortem_generated(td, "a" * 64, "", 0, 0)


def test_postmortem_generated_rejects_negative_counts(tmp_path: Path):
    td = _task_dir(tmp_path)
    with pytest.raises(ValueError, match="anomaly_count"):
        receipt_postmortem_generated(td, "a" * 64, "b" * 64, -1, 0)
    with pytest.raises(ValueError, match="pattern_count"):
        receipt_postmortem_generated(td, "a" * 64, "b" * 64, 0, -2)


def test_postmortem_analysis_happy(tmp_path: Path):
    td = _task_dir(tmp_path)
    out = receipt_postmortem_analysis(td, "c" * 64, 7, "d" * 64)
    assert out.exists()
    payload = json.loads(out.read_text())
    assert payload["analysis_sha256"] == "c" * 64
    assert payload["rules_added"] == 7
    assert payload["rules_sha256_after"] == "d" * 64


def test_postmortem_analysis_rejects_invalid(tmp_path: Path):
    td = _task_dir(tmp_path)
    with pytest.raises(ValueError, match="analysis_sha256"):
        receipt_postmortem_analysis(td, "", 0, "d" * 64)
    with pytest.raises(ValueError, match="rules_added"):
        receipt_postmortem_analysis(td, "c" * 64, -1, "d" * 64)
    with pytest.raises(ValueError, match="rules_sha256_after"):
        receipt_postmortem_analysis(td, "c" * 64, 0, "")


def test_postmortem_skipped_happy(tmp_path: Path):
    td = _task_dir(tmp_path)
    # After task-20260419-002 G1 the enum is reduced to two reasons;
    # `quality-above-threshold` is no longer valid.
    for reason in ("clean-task", "no-findings"):
        out = receipt_postmortem_skipped(td, reason, "e" * 64, subsumed_by=[])
        payload = json.loads(out.read_text())
        assert payload["reason"] == reason
        assert payload["retrospective_sha256"] == "e" * 64
        assert payload["subsumed_by"] == []


def test_postmortem_skipped_rejects_invalid_reason(tmp_path: Path):
    td = _task_dir(tmp_path)
    with pytest.raises(ValueError, match="invalid postmortem skip reason"):
        receipt_postmortem_skipped(td, "made-up-reason", "e" * 64, subsumed_by=[])
    with pytest.raises(ValueError, match="invalid postmortem skip reason"):
        receipt_postmortem_skipped(td, "", "e" * 64, subsumed_by=[])


def test_postmortem_skipped_rejects_empty_retro_sha(tmp_path: Path):
    td = _task_dir(tmp_path)
    with pytest.raises(ValueError, match="retrospective_sha256"):
        receipt_postmortem_skipped(td, "no-findings", "", subsumed_by=[])
