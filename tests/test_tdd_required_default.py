"""Tests for get_tdd_required defaults and PLAN_AUDIT routing (AC 7, 11)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_core import get_tdd_required, transition_task  # noqa: E402
from lib_receipts import receipt_plan_audit, hash_file  # noqa: E402


def test_missing_classification_returns_false():
    assert get_tdd_required({}) is False


def test_classification_without_tdd_required_returns_false():
    assert get_tdd_required({"classification": {"risk_level": "medium"}}) is False


def test_classification_with_tdd_required_true():
    assert get_tdd_required({"classification": {"tdd_required": True}}) is True


def test_classification_with_tdd_required_false():
    assert get_tdd_required({"classification": {"tdd_required": False}}) is False


def test_non_dict_manifest_returns_false():
    assert get_tdd_required(None) is False
    assert get_tdd_required("not a dict") is False


def test_non_dict_classification_returns_false():
    assert get_tdd_required({"classification": "garbage"}) is False
    assert get_tdd_required({"classification": None}) is False


def _setup(tmp_path: Path, *, risk: str, tdd_required: bool | None = None) -> Path:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260418-D"
    td.mkdir(parents=True)
    # Create artifacts so the PLAN_AUDIT hash-bound freshness gate can
    # match against them.
    (td / "spec.md").write_text("# spec")
    (td / "plan.md").write_text("# plan")
    (td / "execution-graph.json").write_text('{"segments": []}')
    classification: dict = {"risk_level": risk}
    if tdd_required is not None:
        classification["tdd_required"] = tdd_required
    (td / "manifest.json").write_text(json.dumps({
        "task_id": "task-20260418-D",
        "stage": "PLAN_AUDIT",
        "classification": classification,
    }))
    return td


def test_plan_audit_to_pre_exec_refused_when_tdd_required_absent_critical(tmp_path: Path):
    """Test-006 investigator finding #4: critical risk without tdd_required
    must REFUSE, not permit. The previous test asserted the inverse and was
    rewritten as part of task-20260419-006 to enforce the proper
    fail-CLOSED behavior.

    The refusal is enforced at the CLASSIFY_AND_SPEC -> SPEC_NORMALIZATION
    edge (AC 9): critical/high risk tasks MUST have classification.tdd_required
    set explicitly (True or False) before they can leave the classification
    stage. This test verifies that gate fires by setting up a critical-risk
    manifest at CLASSIFY_AND_SPEC and confirming the transition refuses with
    a message naming tdd_required.
    """
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260418-D2"
    td.mkdir(parents=True)
    # critical risk + tdd_required missing → must refuse at the
    # CLASSIFY_AND_SPEC -> SPEC_NORMALIZATION edge.
    (td / "manifest.json").write_text(json.dumps({
        "task_id": "task-20260418-D2",
        "stage": "CLASSIFY_AND_SPEC",
        "classification": {"risk_level": "critical"},
    }))
    with pytest.raises(ValueError, match="tdd_required"):
        transition_task(td, "SPEC_NORMALIZATION")
    # Confirm the manifest stage did NOT advance.
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == "CLASSIFY_AND_SPEC", \
        "stage must remain CLASSIFY_AND_SPEC after refused transition"


def test_plan_audit_to_pre_exec_blocked_when_tdd_required_true(tmp_path: Path):
    td = _setup(tmp_path, risk="medium", tdd_required=True)
    with pytest.raises(ValueError, match="tdd_required"):
        transition_task(td, "PRE_EXECUTION_SNAPSHOT")


def test_plan_audit_to_pre_exec_permitted_when_tdd_required_false(tmp_path: Path):
    td = _setup(tmp_path, risk="medium", tdd_required=False)
    transition_task(td, "PRE_EXECUTION_SNAPSHOT")
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == "PRE_EXECUTION_SNAPSHOT"
