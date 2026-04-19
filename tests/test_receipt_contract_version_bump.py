"""Tests for the v2->v3 contract version bump (AC 28).

RECEIPT_CONTRACT_VERSION is now 3. Every writer's output embeds
contract_version=3.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

import lib_receipts  # noqa: E402
from lib_receipts import (  # noqa: E402
    RECEIPT_CONTRACT_VERSION,
    write_receipt,
)


def _td(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260419-CV3"
    td.mkdir(parents=True)
    return td


def test_contract_version_constant_is_three():
    """AC 28: RECEIPT_CONTRACT_VERSION == 3."""
    assert RECEIPT_CONTRACT_VERSION == 3


def test_write_receipt_embeds_contract_version_three(tmp_path: Path):
    """AC 28: write_receipt embeds 3 in the payload."""
    td = _td(tmp_path)
    p = write_receipt(td, "spec-validated", criteria_count=1, spec_sha256="x" * 64)
    payload = json.loads(p.read_text())
    assert payload["contract_version"] == 3


def _exercise_writer(name: str, td: Path):
    """Invoke each writer with minimal valid args (mirrors test_receipt_contract_version)."""
    sd = td / "receipts" / "_injected-prompts"
    sd.mkdir(parents=True, exist_ok=True)

    if name == "receipt_human_approval":
        return lib_receipts.receipt_human_approval(td, "SPEC_REVIEW", "a" * 64)
    if name == "receipt_spec_validated":
        return lib_receipts.receipt_spec_validated(td, 1, "a" * 64)
    if name == "receipt_tdd_tests":
        return lib_receipts.receipt_tdd_tests(td, ["a.py"], "a" * 64, 0, "haiku")
    if name == "receipt_postmortem_generated":
        return lib_receipts.receipt_postmortem_generated(td, "a" * 64, "b" * 64, 0, 0)
    if name == "receipt_postmortem_analysis":
        return lib_receipts.receipt_postmortem_analysis(td, "a" * 64, 0, "b" * 64)
    if name == "receipt_postmortem_skipped":
        return lib_receipts.receipt_postmortem_skipped(td, "no-findings", "a" * 64, subsumed_by=[])
    if name == "receipt_calibration_applied":
        return lib_receipts.receipt_calibration_applied(td, 0, 0, "a" * 64, "b" * 64)
    if name == "receipt_calibration_noop":
        return lib_receipts.receipt_calibration_noop(td, "no-retros", "a" * 64)
    if name == "receipt_plan_routing":
        return lib_receipts.receipt_plan_routing(td, None, None, "generic", None)
    if name == "receipt_plan_validated":
        return lib_receipts.receipt_plan_validated(td, 0, [])
    if name == "receipt_executor_routing":
        return lib_receipts.receipt_executor_routing(td, [])
    if name == "receipt_executor_done":
        digest = "c" * 64
        (sd / "seg-CV.sha256").write_text(digest)
        return lib_receipts.receipt_executor_done(
            td, "seg-CV", "backend", "haiku",
            injected_prompt_sha256=digest,
            agent_name=None, evidence_path=None, tokens_used=0,
        )
    if name == "receipt_audit_routing":
        return lib_receipts.receipt_audit_routing(td, [])
    if name == "receipt_audit_done":
        return lib_receipts.receipt_audit_done(
            td, "sec", "haiku", 0, 0, None, 0,
            route_mode="generic", agent_path=None, injected_agent_sha256=None,
        )
    if name == "receipt_retrospective":
        return lib_receipts.receipt_retrospective(td, 0.9, 0.9, 0.9, 100)
    if name == "receipt_post_completion":
        return lib_receipts.receipt_post_completion(td, [])
    if name == "receipt_planner_spawn":
        # PR #130 hardened receipt_planner_spawn: legacy None-sidecar path
        # was removed. Write the real planner-prompts sidecar that the
        # writer self-verifies against.
        digest = "p" * 64
        psd = td / "receipts" / lib_receipts.INJECTED_PLANNER_PROMPTS_DIR
        psd.mkdir(parents=True, exist_ok=True)
        (psd / "spec.sha256").write_text(digest)
        return lib_receipts.receipt_planner_spawn(td, "spec", 0, injected_prompt_sha256=digest)
    if name == "receipt_plan_audit":
        return lib_receipts.receipt_plan_audit(td, tokens_used=0, finding_count=0)
    return None


def test_every_writer_in_all_embeds_contract_version_three(tmp_path: Path, monkeypatch):
    """AC 28: every receipt_* writer in __all__ writes contract_version=3."""
    td = _td(tmp_path)
    # receipt_rules_check_passed needs run_checks stubbed.
    import rules_engine
    monkeypatch.setattr(rules_engine, "run_checks", lambda root, mode: [])
    monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "dynos-home"))

    writer_names = [n for n in lib_receipts.__all__ if n.startswith("receipt_")]
    exercised = 0
    for name in writer_names:
        if name == "receipt_rules_check_passed":
            out = lib_receipts.receipt_rules_check_passed(td, "all")
        else:
            out = _exercise_writer(name, td)
        if out is None:
            continue
        exercised += 1
        payload = json.loads(out.read_text())
        assert payload["contract_version"] == 3, (
            f"writer {name} did not embed contract_version=3 (got {payload.get('contract_version')!r})"
        )
    assert exercised >= 14, f"expected at least 14 writers exercised, got {exercised}"


def test_every_required_writer_includes_calibration_noop():
    """AC 28: receipt_calibration_noop is part of the v3 bump."""
    assert "receipt_calibration_noop" in lib_receipts.__all__
    assert callable(lib_receipts.receipt_calibration_noop)
