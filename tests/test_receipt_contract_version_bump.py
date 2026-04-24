"""Tests for the contract version bump (was AC 28 v2->v3; migrated for
task-20260419-009 AC 24 v4->v5 force-override reason/approver bump).

RECEIPT_CONTRACT_VERSION is now 5. Every writer's output embeds
contract_version=5.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

import lib_receipts  # noqa: E402
from lib_receipts import (  # noqa: E402
    RECEIPT_CONTRACT_VERSION,
    hash_file,
    receipt_postmortem_analysis,
    write_receipt,
)


def _td(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260419-CV3"
    td.mkdir(parents=True)
    return td


def test_contract_version_constant_is_five():
    """AC 24 (task-009): RECEIPT_CONTRACT_VERSION == 5. Renamed from
    _is_four to _is_five so the current floor is obvious to future
    readers rather than hidden behind a stale name."""
    assert RECEIPT_CONTRACT_VERSION == 5


def test_write_receipt_embeds_contract_version_five(tmp_path: Path):
    """AC 24 (task-009): write_receipt embeds 5 in the payload."""
    td = _td(tmp_path)
    p = write_receipt(td, "spec-validated", criteria_count=1, spec_sha256="x" * 64)
    payload = json.loads(p.read_text())
    assert payload["contract_version"] == 5


def _exercise_writer(name: str, td: Path, tmp_path: Path | None = None):
    """Invoke each writer with minimal valid args (mirrors test_receipt_contract_version)."""
    if tmp_path is None:
        tmp_path = td.parent
    sd = td / "receipts" / "_injected-prompts"
    sd.mkdir(parents=True, exist_ok=True)

    # Task-007: minimal fixture for self-compute writers.
    # Manifest needed for receipt_retrospective's compute_reward call.
    (td / "manifest.json").write_text(json.dumps({
        "task_id": td.name,
        "stage": "DONE",
        "created_at": "2026-04-19T00:00:00Z",
        "raw_input": "test",
        "classification": {"type": "refactor", "risk_level": "medium", "domains": ["backend"]},
    }))
    (td / "spec.md").write_text(
        "## Task Summary\n\ntest\n## User Context\n\ntest\n"
        "## Acceptance Criteria\n\n1. one\n\n## Implicit Requirements Surfaced\n\ntest\n"
        "## Out of Scope\n\ntest\n## Assumptions\n\ntest\n## Risk Notes\n\ntest\n"
    )
    (td / "plan.md").write_text(
        "## Technical Approach\n\nt\n## Reference Code\n\nt\n## Components / Modules\n\nt\n"
        "## Data Flow\n\nt\n## Error Handling Strategy\n\nt\n## Test Strategy\n\nt\n"
        "## Dependency Graph\n\nt\n## Open Questions\n\nt\n"
    )
    (td / "execution-graph.json").write_text('{"task_id":"test","segments":[]}')
    pm_json = td / "postmortem.json"
    pm_json.write_text('{"anomalies":[],"recurring_patterns":[]}')

    if name == "receipt_human_approval":
        return lib_receipts.receipt_human_approval(td, "SPEC_REVIEW", "a" * 64)
    if name == "receipt_spec_validated":
        return lib_receipts.receipt_spec_validated(td)
    if name == "receipt_tdd_tests":
        return lib_receipts.receipt_tdd_tests(td, ["a.py"], "a" * 64, 0, "haiku")
    if name == "receipt_postmortem_generated":
        return lib_receipts.receipt_postmortem_generated(td, pm_json)
    if name == "receipt_postmortem_analysis":
        analysis_file = tmp_path / "analysis.json"
        analysis_file.write_text("{}")
        rules_file = tmp_path / "rules.md"
        rules_file.write_text("# rules")
        return lib_receipts.receipt_postmortem_analysis(
            td, analysis_path=analysis_file, rules_path=rules_file, rules_added=1
        )
    if name == "receipt_postmortem_skipped":
        return lib_receipts.receipt_postmortem_skipped(td, "no-findings", "a" * 64, subsumed_by=[])
    if name == "receipt_calibration_applied":
        return lib_receipts.receipt_calibration_applied(td, 0, 0, "a" * 64, "b" * 64)
    if name == "receipt_calibration_noop":
        return lib_receipts.receipt_calibration_noop(td, "no-retros", "a" * 64)
    if name == "receipt_plan_validated":
        return lib_receipts.receipt_plan_validated(td, validation_passed_override=True)
    if name == "receipt_executor_routing":
        return lib_receipts.receipt_executor_routing(td, [])
    if name == "receipt_executor_done":
        digest = "c" * 64
        (sd / "seg-CV.sha256").write_text(digest)
        return lib_receipts.receipt_executor_done(
            td, "seg-CV", "backend", "haiku",
            injected_prompt_sha256=digest,
            agent_name=None, evidence_path=None, tokens_used=0,
            diff_verified_files=[], no_op_justified=False,
        )
    if name == "receipt_audit_routing":
        return lib_receipts.receipt_audit_routing(td, [])
    if name == "receipt_audit_done":
        return lib_receipts.receipt_audit_done(
            td, "sec", "haiku", 0, 0, None, 0,
            route_mode="generic", agent_path=None, injected_agent_sha256=None,
        )
    if name == "receipt_retrospective":
        # Self-computing; compute_reward runs on the fixture task_dir.
        return lib_receipts.receipt_retrospective(td)
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
        return lib_receipts.receipt_plan_audit(td, tokens_used=0)
    if name == "receipt_force_override":
        # v5 bump: reason + approver are required kwargs. Exercise with
        # minimal valid args so the writer is included in the every-writer
        # version-embed check (not silently skipped).
        return lib_receipts.receipt_force_override(
            td, "SPEC_REVIEW", "PLANNING", [],
            reason="test: exercise for contract-version embed check",
            approver="test-suite",
        )
    return None


def test_every_writer_in_all_embeds_contract_version_five(tmp_path: Path, monkeypatch):
    """AC 24 (task-009): every receipt_* writer in __all__ writes
    contract_version=5."""
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
            out = _exercise_writer(name, td, tmp_path)
        if out is None:
            continue
        exercised += 1
        payload = json.loads(out.read_text())
        assert payload["contract_version"] == 5, (
            f"writer {name} did not embed contract_version=5 (got {payload.get('contract_version')!r})"
        )
    assert exercised >= 14, f"expected at least 14 writers exercised, got {exercised}"


def test_every_required_writer_includes_calibration_noop():
    """AC 24 (task-009): receipt_calibration_noop is part of the v5 writer set."""
    assert "receipt_calibration_noop" in lib_receipts.__all__
    assert callable(lib_receipts.receipt_calibration_noop)


# ---------------------------------------------------------------------------
# AC 13, 14, 15 — new keyword-only path-based API for receipt_postmortem_analysis
# ---------------------------------------------------------------------------

def test_postmortem_analysis_keyword_path_happy(tmp_path: Path):
    """AC 13/14: keyword-only path form produces receipt with all required payload keys."""
    td = tmp_path / ".dynos" / "task-test"
    td.mkdir(parents=True)
    analysis_file = tmp_path / "analysis.json"
    analysis_file.write_text('{"findings": []}')
    rules_file = tmp_path / "rules.md"
    rules_file.write_text("# rules\n- rule one\n")

    receipt_path = receipt_postmortem_analysis(
        td,
        analysis_path=analysis_file,
        rules_path=rules_file,
        rules_added=2,
    )
    payload = json.loads(receipt_path.read_text())

    # AC 14: all required keys present
    assert "analysis_sha256" in payload
    assert "rules_added" in payload
    assert "rules_sha256_after" in payload
    assert "contract_version" in payload

    # AC 13: hash is derived from the actual file content
    assert payload["analysis_sha256"] == hash_file(analysis_file)
    assert payload["rules_sha256_after"] == hash_file(rules_file)
    assert payload["rules_added"] == 2
    # AC 18: contract_version still 5 (no bump)
    assert payload["contract_version"] == 5


def test_postmortem_analysis_missing_analysis_path_raises(tmp_path: Path):
    """AC 13: receipt_postmortem_analysis raises ValueError when analysis_path does not exist."""
    td = tmp_path / ".dynos" / "task-test"
    td.mkdir(parents=True)
    missing = tmp_path / "does_not_exist.json"
    rules_file = tmp_path / "rules.md"
    rules_file.write_text("# rules")

    with pytest.raises(ValueError, match="analysis_path does not exist"):
        receipt_postmortem_analysis(
            td,
            analysis_path=missing,
            rules_path=rules_file,
            rules_added=0,
        )


def test_postmortem_analysis_missing_rules_path_gives_zero_hash(tmp_path: Path):
    """AC 15: when rules_path is absent, rules_sha256_after == '0' * 64."""
    td = tmp_path / ".dynos" / "task-test"
    td.mkdir(parents=True)
    analysis_file = tmp_path / "analysis.json"
    analysis_file.write_text("{}")
    missing_rules = tmp_path / "no_rules.md"  # does not exist

    receipt_path = receipt_postmortem_analysis(
        td,
        analysis_path=analysis_file,
        rules_path=missing_rules,
        rules_added=0,
    )
    payload = json.loads(receipt_path.read_text())

    assert payload["rules_sha256_after"] == "0" * 64
