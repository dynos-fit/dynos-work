"""Tests for receipt_plan_audit hash payload (F2) and plan_audit_matches
helper (F3). CRITERIA 2 and 3.

F2: `receipt_plan_audit(...)` now requires three kw-only string kwargs:
`spec_sha256`, `plan_sha256`, `graph_sha256`. Each must be non-empty.
The written payload carries all three.

F3: `plan_audit_matches(task_dir)` returns:
  - True  when the receipt exists AND all three hashes match disk
  - a descriptive string (e.g. "plan.md hash drift") when the receipt
    is present but one of the artifacts drifted on disk
  - False when the receipt is missing (or legacy — pre-F2 no-hash
    payload; that path is covered indirectly here by the missing case)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_receipts import (  # noqa: E402
    hash_file,
    plan_audit_matches,
    receipt_plan_audit,
)


def _setup_artifacts(tmp_path: Path) -> Path:
    """Create a task dir with the three planning artifacts on disk."""
    td = tmp_path / ".dynos" / "task-audit"
    td.mkdir(parents=True)
    (td / "spec.md").write_text("# spec content\n")
    (td / "plan.md").write_text("# plan content\n")
    (td / "execution-graph.json").write_text('{"segments": []}\n')
    return td


def _write_fresh_receipt(td: Path) -> None:
    """Write a plan-audit-check receipt whose hashes match current disk."""
    receipt_plan_audit(
        td,
        tokens_used=100,
        finding_count=0,
        spec_sha256=hash_file(td / "spec.md"),
        plan_sha256=hash_file(td / "plan.md"),
        graph_sha256=hash_file(td / "execution-graph.json"),
    )


# ---------------------------------------------------------------------------
# F2: receipt_plan_audit payload / signature
# ---------------------------------------------------------------------------


def test_payload_contains_three_hashes(tmp_path: Path) -> None:
    """Write a receipt with known hex digests; read the JSON back; assert
    the three keys are present and match exactly what we passed in."""
    td = _setup_artifacts(tmp_path)
    spec_h = "a" * 64
    plan_h = "b" * 64
    graph_h = "c" * 64
    receipt_path = receipt_plan_audit(
        td,
        tokens_used=0,
        finding_count=0,
        spec_sha256=spec_h,
        plan_sha256=plan_h,
        graph_sha256=graph_h,
    )
    payload = json.loads(receipt_path.read_text())
    assert payload["spec_sha256"] == spec_h
    assert payload["plan_sha256"] == plan_h
    assert payload["graph_sha256"] == graph_h


def test_requires_spec_sha256(tmp_path: Path) -> None:
    """Missing kwarg → TypeError (kw-only required argument).
    Empty string → ValueError (explicit rejection of blank)."""
    td = _setup_artifacts(tmp_path)
    # Omission — kw-only required → TypeError.
    with pytest.raises(TypeError):
        receipt_plan_audit(
            td,
            tokens_used=0,
            finding_count=0,
            plan_sha256="b" * 64,
            graph_sha256="c" * 64,
        )
    # Empty string — explicit ValueError naming the arg.
    with pytest.raises(ValueError, match="spec_sha256"):
        receipt_plan_audit(
            td,
            tokens_used=0,
            finding_count=0,
            spec_sha256="",
            plan_sha256="b" * 64,
            graph_sha256="c" * 64,
        )


def test_requires_plan_sha256(tmp_path: Path) -> None:
    td = _setup_artifacts(tmp_path)
    with pytest.raises(TypeError):
        receipt_plan_audit(
            td,
            tokens_used=0,
            finding_count=0,
            spec_sha256="a" * 64,
            graph_sha256="c" * 64,
        )
    with pytest.raises(ValueError, match="plan_sha256"):
        receipt_plan_audit(
            td,
            tokens_used=0,
            finding_count=0,
            spec_sha256="a" * 64,
            plan_sha256="",
            graph_sha256="c" * 64,
        )


def test_requires_graph_sha256(tmp_path: Path) -> None:
    td = _setup_artifacts(tmp_path)
    with pytest.raises(TypeError):
        receipt_plan_audit(
            td,
            tokens_used=0,
            finding_count=0,
            spec_sha256="a" * 64,
            plan_sha256="b" * 64,
        )
    with pytest.raises(ValueError, match="graph_sha256"):
        receipt_plan_audit(
            td,
            tokens_used=0,
            finding_count=0,
            spec_sha256="a" * 64,
            plan_sha256="b" * 64,
            graph_sha256="",
        )


# ---------------------------------------------------------------------------
# F3: plan_audit_matches helper
# ---------------------------------------------------------------------------


def test_matches_true_on_fresh(tmp_path: Path) -> None:
    """Write a receipt over fresh artifacts; the helper must return True."""
    td = _setup_artifacts(tmp_path)
    _write_fresh_receipt(td)
    assert plan_audit_matches(td) is True


def test_matches_returns_drift_string_on_plan_edit(tmp_path: Path) -> None:
    """Write a receipt, then mutate plan.md. The helper must return a
    descriptive string naming plan.md — specifically `str` and `"plan.md"`
    must appear, so callers (PLAN_AUDIT exit gate) can surface the
    artifact identity in error messages."""
    td = _setup_artifacts(tmp_path)
    _write_fresh_receipt(td)
    (td / "plan.md").write_text("# plan content DRIFTED\n")
    result = plan_audit_matches(td)
    assert isinstance(result, str), f"expected str on drift, got {type(result).__name__}"
    assert "plan.md" in result, f"drift string must name plan.md — got {result!r}"
    # And it must NOT be the boolean True literal — callers key off isinstance.
    assert result is not True
    assert result is not False


def test_matches_returns_drift_string_on_spec_edit(tmp_path: Path) -> None:
    """Parallel drift test for spec.md — ensures the helper reports the
    correct artifact. Regression: a buggy impl could blindly say `plan.md`
    on every drift; this test rules that out."""
    td = _setup_artifacts(tmp_path)
    _write_fresh_receipt(td)
    (td / "spec.md").write_text("# spec content DRIFTED\n")
    result = plan_audit_matches(td)
    assert isinstance(result, str)
    assert "spec.md" in result
    assert "plan.md" not in result, (
        f"drift helper mis-identified which artifact drifted: {result!r}"
    )


def test_matches_returns_drift_string_on_graph_edit(tmp_path: Path) -> None:
    """Parallel drift test for execution-graph.json."""
    td = _setup_artifacts(tmp_path)
    _write_fresh_receipt(td)
    (td / "execution-graph.json").write_text('{"segments": ["drifted"]}\n')
    result = plan_audit_matches(td)
    assert isinstance(result, str)
    assert "execution-graph.json" in result


def test_matches_returns_false_when_missing(tmp_path: Path) -> None:
    """No receipt → False (NOT a drift string). Callers distinguish this
    as the missing-receipt branch."""
    td = _setup_artifacts(tmp_path)
    # Intentionally do not write a plan-audit-check receipt.
    result = plan_audit_matches(td)
    assert result is False, f"missing receipt must return False, got {result!r}"
