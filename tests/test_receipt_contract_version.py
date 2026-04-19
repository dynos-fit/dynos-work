"""Tests for contract_version=2 in every writer + v1 backwards compat (AC 30)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

import lib_receipts  # noqa: E402
from lib_receipts import (  # noqa: E402
    RECEIPT_CONTRACT_VERSION,
    read_receipt,
    receipt_audit_done,
    receipt_audit_routing,
    receipt_calibration_applied,
    receipt_executor_done,
    receipt_executor_routing,
    receipt_human_approval,
    receipt_plan_audit,
    receipt_plan_routing,
    receipt_plan_validated,
    receipt_planner_spawn,
    receipt_post_completion,
    receipt_postmortem_analysis,
    receipt_postmortem_generated,
    receipt_postmortem_skipped,
    receipt_retrospective,
    receipt_spec_validated,
    receipt_tdd_tests,
    validate_chain,
    write_receipt,
)


def _td(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260418-CV"
    td.mkdir(parents=True)
    return td


def test_contract_version_constant_is_two():
    assert RECEIPT_CONTRACT_VERSION == 2


def test_write_receipt_embeds_contract_version(tmp_path: Path):
    td = _td(tmp_path)
    p = write_receipt(td, "spec-validated", criteria_count=1)
    payload = json.loads(p.read_text())
    assert payload["contract_version"] == 2


def _exercise_writer(name: str, td: Path):
    """Invoke each writer with minimal valid args. Returns the receipt path."""
    sd = td / "receipts" / "_injected-prompts"
    sd.mkdir(parents=True, exist_ok=True)
    aud_sd = td / "receipts" / "_injected-auditor-prompts"
    aud_sd.mkdir(parents=True, exist_ok=True)

    if name == "receipt_human_approval":
        return receipt_human_approval(td, "SPEC_REVIEW", "a" * 64)
    if name == "receipt_spec_validated":
        return receipt_spec_validated(td, 1, "a" * 64)
    if name == "receipt_tdd_tests":
        return receipt_tdd_tests(td, ["a.py"], "a" * 64, 0, "haiku")
    if name == "receipt_postmortem_generated":
        return receipt_postmortem_generated(td, "a" * 64, "b" * 64, 0, 0)
    if name == "receipt_postmortem_analysis":
        return receipt_postmortem_analysis(td, "a" * 64, 0, "b" * 64)
    if name == "receipt_postmortem_skipped":
        return receipt_postmortem_skipped(td, "no-findings", "a" * 64)
    if name == "receipt_calibration_applied":
        return receipt_calibration_applied(td, 0, 0, "a" * 64, "b" * 64)
    if name == "receipt_plan_routing":
        return receipt_plan_routing(td, None, None, "generic", None)
    if name == "receipt_plan_validated":
        return receipt_plan_validated(td, 0, [])
    if name == "receipt_executor_routing":
        return receipt_executor_routing(td, [])
    if name == "receipt_executor_done":
        digest = "c" * 64
        (sd / "seg-CV.sha256").write_text(digest)
        return receipt_executor_done(
            td, "seg-CV", "backend", "haiku",
            injected_prompt_sha256=digest,
            agent_name=None, evidence_path=None, tokens_used=0,
        )
    if name == "receipt_audit_routing":
        return receipt_audit_routing(td, [])
    if name == "receipt_audit_done":
        return receipt_audit_done(
            td, "sec", "haiku", 0, 0, None, 0,
            route_mode="generic", agent_path=None,
            injected_agent_sha256=None,
        )
    if name == "receipt_retrospective":
        return receipt_retrospective(td, 0.9, 0.9, 0.9, 100)
    if name == "receipt_post_completion":
        return receipt_post_completion(td, [])
    if name == "receipt_planner_spawn":
        # Post-F5: the None legacy path is rejected. Write the sidecar
        # so the writer's unconditional hash-match assertion passes.
        planner_sd = td / "receipts" / "_injected-planner-prompts"
        planner_sd.mkdir(parents=True, exist_ok=True)
        digest = "d" * 64
        (planner_sd / "spec.sha256").write_text(digest)
        return receipt_planner_spawn(
            td, "spec", 0, injected_prompt_sha256=digest
        )
    if name == "receipt_plan_audit":
        return receipt_plan_audit(
            td,
            tokens_used=0,
            finding_count=0,
            spec_sha256="a" * 64,
            plan_sha256="b" * 64,
            graph_sha256="c" * 64,
        )
    if name == "receipt_force_override":
        from lib_receipts import receipt_force_override
        return receipt_force_override(
            td,
            from_stage="PLANNING",
            to_stage="PLAN_REVIEW",
            bypassed_gates=[],
        )
    return None


def test_every_writer_in_all_embeds_contract_version_two(tmp_path: Path):
    td = _td(tmp_path)
    writer_names = [n for n in lib_receipts.__all__ if n.startswith("receipt_")]
    exercised = 0
    for name in writer_names:
        out = _exercise_writer(name, td)
        if out is None:
            continue  # writer not exercised; skip with a soft pass
        exercised += 1
        payload = json.loads(out.read_text())
        assert payload["contract_version"] == 2, (
            f"writer {name} did not embed contract_version=2"
        )
    assert exercised >= 12, f"expected at least 12 writers exercised, got {exercised}"


def test_v1_receipt_without_contract_version_is_readable(tmp_path: Path):
    td = _td(tmp_path)
    receipts = td / "receipts"
    receipts.mkdir()
    legacy = {
        "step": "spec-validated",
        "ts": "2025-01-01T00:00:00Z",
        "valid": True,
        "criteria_count": 4,
        "spec_sha256": "deadbeef",
    }
    (receipts / "spec-validated.json").write_text(json.dumps(legacy))
    out = read_receipt(td, "spec-validated")
    assert out is not None
    assert out.get("valid") is True
    assert "contract_version" not in out


def test_v1_receipt_validate_chain_treats_as_valid(tmp_path: Path):
    """An EXECUTION-stage manifest with a v1 plan-validated receipt
    (no contract_version field) must show no gaps from validate_chain."""
    td = _td(tmp_path)
    (td / "manifest.json").write_text(json.dumps({
        "task_id": td.name,
        "stage": "EXECUTION",
    }))
    receipts = td / "receipts"
    receipts.mkdir()
    legacy = {
        "step": "plan-validated",
        "ts": "2025-01-01T00:00:00Z",
        "valid": True,
        "segment_count": 2,
        "criteria_coverage": [1, 2],
        "validation_passed": True,
    }
    (receipts / "plan-validated.json").write_text(json.dumps(legacy))
    gaps = validate_chain(td)
    assert "plan-validated" not in gaps
