"""Tests for fail-CLOSED anomaly_count handling in require_receipts_for_done (AC 11).

Previously the code swallowed any coercion error and defaulted to
anomaly_count=0 — silently accepting a malformed postmortem. The new logic
treats any unknown/non-int anomaly_count as requiring a postmortem-analysis
or postmortem-skipped receipt.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_core import require_receipts_for_done  # noqa: E402
from lib_receipts import (  # noqa: E402
    receipt_audit_routing,
    receipt_postmortem_analysis,
    receipt_postmortem_skipped,
    write_receipt,
)


def _setup(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260419-FC"
    td.mkdir(parents=True)
    (td / "manifest.json").write_text(json.dumps({
        "task_id": td.name,
        "stage": "CHECKPOINT_AUDIT",
        "classification": {"risk_level": "medium"},
    }))
    (td / "task-retrospective.json").write_text(json.dumps({"quality_score": 0.95}))
    receipt_audit_routing(td, [])
    return td


def _write_pm_generated_raw(td: Path, **payload) -> Path:
    """Write a postmortem-generated receipt directly with arbitrary anomaly_count."""
    defaults = {
        "json_sha256": "a" * 64,
        "md_sha256": "b" * 64,
        "pattern_count": 0,
    }
    defaults.update(payload)
    return write_receipt(td, "postmortem-generated", **defaults)


def _mock_empty_registry(monkeypatch):
    import router
    monkeypatch.setattr(router, "_load_auditor_registry", lambda root: {
        "always": [], "fast_track": [], "domain_conditional": {},
    })


def test_anomaly_count_int_positive_requires_analysis(tmp_path: Path, monkeypatch):
    """AC 11: anomaly_count=5 (int>0) requires postmortem-analysis/skipped."""
    _mock_empty_registry(monkeypatch)
    td = _setup(tmp_path)
    _write_pm_generated_raw(td, anomaly_count=5)
    gaps = require_receipts_for_done(td)
    assert any("postmortem-analysis or postmortem-skipped missing" in g for g in gaps), \
        f"expected analysis/skipped gap in {gaps}"


def test_anomaly_count_str_treated_as_unknown_requires_analysis(tmp_path: Path, monkeypatch):
    """AC 11 fail-closed: non-int anomaly_count → require analysis/skipped."""
    _mock_empty_registry(monkeypatch)
    td = _setup(tmp_path)
    _write_pm_generated_raw(td, anomaly_count="garbage")
    gaps = require_receipts_for_done(td)
    assert any("postmortem-analysis or postmortem-skipped missing" in g for g in gaps), \
        f"expected fail-closed gap in {gaps}"


def test_anomaly_count_missing_key_requires_analysis(tmp_path: Path, monkeypatch):
    """AC 11 fail-closed: anomaly_count key absent → require analysis/skipped."""
    _mock_empty_registry(monkeypatch)
    td = _setup(tmp_path)
    # Write receipt WITHOUT anomaly_count.
    write_receipt(
        td, "postmortem-generated",
        json_sha256="a" * 64, md_sha256="b" * 64, pattern_count=0,
    )
    gaps = require_receipts_for_done(td)
    assert any("postmortem-analysis or postmortem-skipped missing" in g for g in gaps), \
        f"expected missing-key gap in {gaps}"


def test_anomaly_count_zero_int_with_high_quality_passes(tmp_path: Path, monkeypatch):
    """AC 11 control: anomaly_count=0 + quality_score>=0.8 → no analysis needed."""
    _mock_empty_registry(monkeypatch)
    td = _setup(tmp_path)
    _write_pm_generated_raw(td, anomaly_count=0)
    gaps = require_receipts_for_done(td)
    assert not any("postmortem-analysis" in g for g in gaps), \
        f"unexpected analysis gap with anomaly=0 and quality>=0.8: {gaps}"


def test_anomaly_count_none_treated_as_unknown(tmp_path: Path, monkeypatch):
    """AC 11: anomaly_count=None (JSON null) triggers fail-closed too."""
    _mock_empty_registry(monkeypatch)
    td = _setup(tmp_path)
    _write_pm_generated_raw(td, anomaly_count=None)
    gaps = require_receipts_for_done(td)
    assert any("postmortem-analysis or postmortem-skipped missing" in g for g in gaps), \
        f"expected fail-closed gap for None anomaly_count: {gaps}"


def test_skipped_satisfies_unknown_anomaly_count(tmp_path: Path, monkeypatch):
    """AC 11: postmortem-skipped satisfies the requirement even with unknown anomaly."""
    _mock_empty_registry(monkeypatch)
    td = _setup(tmp_path)
    _write_pm_generated_raw(td, anomaly_count="unknown")
    receipt_postmortem_skipped(td, "no-findings", "f" * 64, subsumed_by=[])
    gaps = require_receipts_for_done(td)
    assert not any("postmortem-analysis" in g for g in gaps), \
        f"skipped should satisfy requirement: {gaps}"
