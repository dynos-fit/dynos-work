"""Tests for the per-segment CHECKPOINT_AUDIT gate (CRITERION 1, Fix A).

Covers the new `transition_task` block that, for transitions from
TEST_EXECUTION or REPAIR_EXECUTION into CHECKPOINT_AUDIT, reads the
`executor-routing` receipt and requires a matching
`executor-{segment_id}` receipt for every planned segment.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_core import transition_task  # noqa: E402
from lib_receipts import (  # noqa: E402
    receipt_executor_done,
    receipt_executor_routing,
)


def _setup_task(tmp_path: Path, stage: str) -> Path:
    """Create a task dir whose manifest sits at `stage` and is otherwise
    ready to attempt a CHECKPOINT_AUDIT transition."""
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260418-GX"
    td.mkdir(parents=True)
    (td / "manifest.json").write_text(json.dumps({
        "task_id": "task-20260418-GX",
        "stage": stage,
        "classification": {"risk_level": "medium"},
        "snapshot": {"head_sha": "0000000000000000000000000000000000000000"},
    }))
    return td


def _write_exec_sidecar(td: Path, segment_id: str, digest: str) -> None:
    """Create the injected-prompt sidecar that receipt_executor_done asserts."""
    sd = td / "receipts" / "_injected-prompts"
    sd.mkdir(parents=True, exist_ok=True)
    (sd / f"{segment_id}.sha256").write_text(digest)


def _write_exec_receipt(td: Path, segment_id: str) -> None:
    """Write a valid executor-{seg} receipt (with matching sidecar)."""
    digest = hashlib.sha256(f"prompt-for-{segment_id}".encode()).hexdigest()
    _write_exec_sidecar(td, segment_id, digest)
    receipt_executor_done(
        td,
        segment_id,
        "backend-executor",
        "haiku",
        injected_prompt_sha256=digest,
        agent_name=None,
        evidence_path=None,
        tokens_used=0,
        diff_verified_files=[],
        no_op_justified=False,
    )


def test_blocks_when_executor_receipt_missing(tmp_path: Path):
    """Two planned segments, only one executor receipt — gate must refuse
    and the error message must name the missing per-segment receipt."""
    td = _setup_task(tmp_path, "TEST_EXECUTION")
    receipt_executor_routing(td, [
        {"segment_id": "seg-1", "executor": "backend-executor"},
        {"segment_id": "seg-2", "executor": "backend-executor"},
    ])
    # Only seg-a completes; seg-b has no receipt.
    _write_exec_receipt(td, "seg-1")

    with pytest.raises(ValueError) as excinfo:
        transition_task(td, "CHECKPOINT_AUDIT")
    assert "executor-seg-2" in str(excinfo.value)
    # The stage must remain unchanged on refusal.
    assert json.loads((td / "manifest.json").read_text())["stage"] == "TEST_EXECUTION"


def test_passes_when_all_executor_receipts_present(tmp_path: Path):
    """Every planned segment has its executor receipt — gate must pass
    and the manifest must advance to CHECKPOINT_AUDIT."""
    td = _setup_task(tmp_path, "TEST_EXECUTION")
    receipt_executor_routing(td, [
        {"segment_id": "seg-1", "executor": "backend-executor"},
        {"segment_id": "seg-2", "executor": "backend-executor"},
    ])
    _write_exec_receipt(td, "seg-1")
    _write_exec_receipt(td, "seg-2")

    transition_task(td, "CHECKPOINT_AUDIT")
    assert json.loads((td / "manifest.json").read_text())["stage"] == "CHECKPOINT_AUDIT"


def test_parity_for_repair_execution_source(tmp_path: Path):
    """The same gate fires when the source stage is REPAIR_EXECUTION."""
    td = _setup_task(tmp_path, "REPAIR_EXECUTION")
    receipt_executor_routing(td, [
        {"segment_id": "seg-1", "executor": "backend-executor"},
        {"segment_id": "seg-2", "executor": "backend-executor"},
    ])
    _write_exec_receipt(td, "seg-1")
    # seg-b missing

    with pytest.raises(ValueError) as excinfo:
        transition_task(td, "CHECKPOINT_AUDIT")
    assert "executor-seg-2" in str(excinfo.value)


def test_empty_segments_list_passes(tmp_path: Path):
    """executor-routing receipt with segments=[] requires no per-segment
    receipts — the transition must succeed."""
    td = _setup_task(tmp_path, "TEST_EXECUTION")
    receipt_executor_routing(td, [])

    transition_task(td, "CHECKPOINT_AUDIT")
    assert json.loads((td / "manifest.json").read_text())["stage"] == "CHECKPOINT_AUDIT"


def test_truncated_routing_cannot_bypass_graph_required_segments(tmp_path: Path):
    """SEC-002 regression: execution-graph.json is authoritative. A
    tampered/truncated executor-routing receipt listing FEWER segments
    than the graph MUST NOT satisfy the gate — the union of both sources
    is enforced."""
    td = _setup_task(tmp_path, "TEST_EXECUTION")
    # The authoritative plan has two segments.
    (td / "execution-graph.json").write_text(json.dumps({
        "task_id": "task-20260418-GX",
        "segments": [
            {"id": "seg-1", "executor": "backend-executor"},
            {"id": "seg-2", "executor": "backend-executor"},
        ],
    }))
    # But the routing receipt only records ONE segment (simulating
    # tampered/buggy routing state).
    receipt_executor_routing(td, [
        {"segment_id": "seg-1", "executor": "backend-executor"},
    ])
    _write_exec_receipt(td, "seg-1")
    # seg-2 has no receipt AND is not in the routing receipt — but the
    # graph still requires it. Gate must refuse.
    with pytest.raises(ValueError) as excinfo:
        transition_task(td, "CHECKPOINT_AUDIT")
    assert "executor-seg-2" in str(excinfo.value)


def test_graph_absent_falls_back_to_routing(tmp_path: Path):
    """If execution-graph.json is missing (legacy task dir), the gate
    falls back to the routing-receipt segments list alone — existing
    behavior is preserved."""
    td = _setup_task(tmp_path, "TEST_EXECUTION")
    # No execution-graph.json on disk.
    receipt_executor_routing(td, [
        {"segment_id": "seg-1", "executor": "backend-executor"},
    ])
    _write_exec_receipt(td, "seg-1")

    transition_task(td, "CHECKPOINT_AUDIT")
    assert json.loads((td / "manifest.json").read_text())["stage"] == "CHECKPOINT_AUDIT"


def test_missing_executor_routing_does_not_double_complain(tmp_path: Path):
    """When executor-routing itself is missing, only the single
    routing-missing error is emitted — no per-segment errors piggyback."""
    td = _setup_task(tmp_path, "TEST_EXECUTION")
    # Do NOT write executor-routing at all.

    with pytest.raises(ValueError) as excinfo:
        transition_task(td, "CHECKPOINT_AUDIT")
    msg = str(excinfo.value)
    assert "executor-routing (executor routing was never recorded)" in msg
    # And — crucially — no per-segment noise piggybacks on top.
    assert "executor-seg-" not in msg
