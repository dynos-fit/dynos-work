"""Tests for receipt_plan_audit hash payload and plan_audit_matches
helper. CRITERIA 2 and 3 (F2/F3), with SEC-004 hardening applied.

SEC-004 hardening: the writer no longer accepts caller-supplied hashes.
It re-hashes `spec.md`, `plan.md`, and `execution-graph.json` from the
task directory at write time. This closes the TOCTOU between a caller's
hash read and the receipt write — the writer's own read is the
authoritative source.

F3: `plan_audit_matches(task_dir)` still returns:
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
    """Write a plan-audit-check receipt — the writer re-hashes current
    disk contents (SEC-004: no caller-supplied hashes)."""
    receipt_plan_audit(td, tokens_used=100, finding_count=0)


# ---------------------------------------------------------------------------
# Payload / signature
# ---------------------------------------------------------------------------


def test_payload_contains_three_hashes(tmp_path: Path) -> None:
    """Write a receipt; read the JSON back; assert the three hash keys
    are present AND match what `hash_file` says about the same files.

    SEC-004: writer re-hashes internally — callers no longer supply
    hashes — so the only way for the payload to be right is for the
    writer to read the artifacts itself."""
    td = _setup_artifacts(tmp_path)
    receipt_path = receipt_plan_audit(td, tokens_used=0, finding_count=0)
    payload = json.loads(receipt_path.read_text())
    assert payload["spec_sha256"] == hash_file(td / "spec.md")
    assert payload["plan_sha256"] == hash_file(td / "plan.md")
    assert payload["graph_sha256"] == hash_file(td / "execution-graph.json")


def test_missing_artifact_writes_literal_missing(tmp_path: Path) -> None:
    """When an artifact file is absent at write time, the corresponding
    payload slot carries the literal string `missing`. Downstream
    `plan_audit_matches` interprets that as a drift condition because it
    never equals a real sha256 hex digest."""
    td = _setup_artifacts(tmp_path)
    (td / "plan.md").unlink()
    receipt_path = receipt_plan_audit(td, tokens_used=0, finding_count=0)
    payload = json.loads(receipt_path.read_text())
    assert payload["plan_sha256"] == "missing"
    # Spec and graph are still real hashes.
    assert payload["spec_sha256"] == hash_file(td / "spec.md")
    assert payload["graph_sha256"] == hash_file(td / "execution-graph.json")


def test_signature_rejects_caller_supplied_hashes(tmp_path: Path) -> None:
    """SEC-004 regression: callers that still try to pass the old
    kwargs (spec_sha256/plan_sha256/graph_sha256) hit a TypeError for
    unexpected-kwarg. The three args are no longer part of the
    public signature."""
    td = _setup_artifacts(tmp_path)
    with pytest.raises(TypeError, match="spec_sha256"):
        receipt_plan_audit(
            td,
            tokens_used=0,
            finding_count=0,
            spec_sha256="a" * 64,  # type: ignore[call-arg]
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
