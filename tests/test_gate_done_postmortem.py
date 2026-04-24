"""Tests for require_receipts_for_done postmortem matrix (AC 19).

Migrated for task-20260419-006: AC 6 added registry-eligible auditor
crosscheck. We mock the auditor registry to be empty so the crosscheck
is vacuous and these tests focus on the postmortem-receipt matrix.
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
    receipt_postmortem_generated,
    receipt_postmortem_skipped,
)


@pytest.fixture(autouse=True)
def _empty_auditor_registry(monkeypatch):
    """Make the AC 6 registry cross-check vacuous so this test file focuses
    on the postmortem receipt matrix (which it was originally written for)."""
    import router
    monkeypatch.setattr(router, "_load_auditor_registry", lambda root: {
        "always": [], "fast_track": [], "domain_conditional": {},
    })


def _setup(tmp_path: Path, *, quality: float | None = None) -> Path:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260418-DPM"
    td.mkdir(parents=True)
    if quality is not None:
        (td / "task-retrospective.json").write_text(
            json.dumps({"quality_score": quality})
        )
    receipt_audit_routing(td, [])  # empty auditors so we focus on postmortem
    return td


def _write_postmortem_fixture(td: Path, anomaly_count: int, pattern_count: int) -> Path:
    """v4 self-compute contract: counts come from the on-disk postmortem JSON."""
    pm_json = td / "postmortem.json"
    pm_json.write_text(json.dumps({
        "anomalies": [{"idx": i} for i in range(anomaly_count)],
        "recurring_patterns": [{"idx": i} for i in range(pattern_count)],
    }))
    return pm_json


def test_neither_generated_nor_skipped_is_gap(tmp_path: Path):
    td = _setup(tmp_path, quality=0.95)
    gaps = require_receipts_for_done(td)
    assert any("postmortem-generated or postmortem-skipped missing" in g for g in gaps)


def test_skipped_alone_passes(tmp_path: Path):
    td = _setup(tmp_path, quality=0.95)
    # task-20260419-002 G2: subsumed_by required; empty list is valid
    # because reason is `no-findings`.
    receipt_postmortem_skipped(td, "no-findings", "f" * 64, subsumed_by=[])
    gaps = require_receipts_for_done(td)
    assert gaps == []


def test_generated_clean_passes(tmp_path: Path):
    """generated + zero anomalies + quality>=0.8 -> no analysis needed."""
    td = _setup(tmp_path, quality=0.95)
    pm = _write_postmortem_fixture(td, anomaly_count=0, pattern_count=0)
    receipt_postmortem_generated(td, pm)
    gaps = require_receipts_for_done(td)
    assert gaps == []


def test_generated_with_anomalies_requires_analysis(tmp_path: Path):
    td = _setup(tmp_path, quality=0.95)
    pm = _write_postmortem_fixture(td, anomaly_count=2, pattern_count=0)
    receipt_postmortem_generated(td, pm)
    gaps = require_receipts_for_done(td)
    assert any("postmortem-analysis or postmortem-skipped missing" in g for g in gaps)


def test_generated_with_low_quality_requires_analysis(tmp_path: Path):
    td = _setup(tmp_path, quality=0.5)
    pm = _write_postmortem_fixture(td, anomaly_count=0, pattern_count=0)
    receipt_postmortem_generated(td, pm)
    gaps = require_receipts_for_done(td)
    assert any("postmortem-analysis or postmortem-skipped missing" in g for g in gaps)


def test_generated_with_anomalies_plus_analysis_passes(tmp_path: Path):
    td = _setup(tmp_path, quality=0.95)
    pm = _write_postmortem_fixture(td, anomaly_count=4, pattern_count=0)
    receipt_postmortem_generated(td, pm)
    analysis_file = tmp_path / "analysis.json"
    analysis_file.write_text('{"findings": []}')
    rules_file = tmp_path / "rules.md"
    rules_file.write_text("# rules\n")
    receipt_postmortem_analysis(
        td, analysis_path=analysis_file, rules_path=rules_file, rules_added=1
    )
    gaps = require_receipts_for_done(td)
    assert gaps == []


def test_generated_with_anomalies_plus_skipped_passes(tmp_path: Path):
    td = _setup(tmp_path, quality=0.95)
    pm = _write_postmortem_fixture(td, anomaly_count=4, pattern_count=0)
    receipt_postmortem_generated(td, pm)
    # task-20260419-002 G2: subsumed_by required; empty list is valid
    # because reason is `clean-task`.
    receipt_postmortem_skipped(td, "clean-task", "f" * 64, subsumed_by=[])
    gaps = require_receipts_for_done(td)
    assert gaps == []


def test_skipped_variant_short_circuits_anomaly_branch(tmp_path: Path):
    """Even if generated says anomalies>0, a skipped receipt satisfies (d)."""
    td = _setup(tmp_path, quality=0.4)
    pm = _write_postmortem_fixture(td, anomaly_count=99, pattern_count=0)
    receipt_postmortem_generated(td, pm)
    # task-20260419-002 G2: subsumed_by required; empty list is valid
    # because reason is `no-findings`.
    receipt_postmortem_skipped(td, "no-findings", "f" * 64, subsumed_by=[])
    gaps = require_receipts_for_done(td)
    assert gaps == []
