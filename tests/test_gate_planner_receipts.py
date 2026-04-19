"""Tests for the two new planner transition gates (F6, CRITERION 6).

Two new gate clauses in `transition_task`:

1. SPEC_NORMALIZATION → SPEC_REVIEW requires a `planner-spec` receipt
   (skipped on fast-track tasks — fast-track writes only a combined
   planner-plan receipt for the Spec+Plan spawn).
2. PLANNING → PLAN_REVIEW requires a `planner-plan` receipt. This fires
   on both normal and fast-track paths.

Writing a planner receipt requires two things: the sidecar digest file
at `receipts/_injected-planner-prompts/{phase}.sha256` AND a matching
`injected_prompt_sha256` string. We exercise both the refusal path
(no receipt) and the success path (receipt + sidecar both present).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_core import transition_task  # noqa: E402
from lib_receipts import receipt_planner_spawn  # noqa: E402


PLANNER_DIGEST = "a" * 64  # fixed test digest; real callers hash the prompt


def _write_planner_receipt(td: Path, phase: str) -> None:
    """Write the planner sidecar + receipt for the given phase.

    `receipt_planner_spawn` asserts the sidecar file exists AND matches
    the supplied digest; we materialize both in a single helper so tests
    stay readable.
    """
    sidecar_dir = td / "receipts" / "_injected-planner-prompts"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    (sidecar_dir / f"{phase}.sha256").write_text(PLANNER_DIGEST)
    receipt_planner_spawn(
        td,
        phase,
        tokens_used=0,
        injected_prompt_sha256=PLANNER_DIGEST,
    )


def _setup(tmp_path: Path, *, stage: str, fast_track: bool = False,
           classification_fast_track: bool = False,
           slug: str = "PR") -> Path:
    """Build a task at the given stage. Fast-track flag can be set at the
    top level of the manifest OR under `classification.fast_track` — the
    gate tolerates either (see seg-1 evidence)."""
    project = tmp_path / "project"
    td = project / ".dynos" / f"task-20260419-{slug}"
    td.mkdir(parents=True)
    manifest: dict = {
        "task_id": td.name,
        "stage": stage,
        "classification": {"risk_level": "medium"},
    }
    if fast_track:
        manifest["fast_track"] = True
    if classification_fast_track:
        manifest["classification"]["fast_track"] = True
    (td / "manifest.json").write_text(json.dumps(manifest))
    # Artifacts that SPEC_REVIEW / PLAN_REVIEW / downstream gates may touch.
    (td / "spec.md").write_text("# spec\n")
    (td / "plan.md").write_text("# plan\n")
    return td


# ---------------------------------------------------------------------------
# Refusal paths — receipt missing.
# ---------------------------------------------------------------------------


def test_spec_review_requires_planner_spec(tmp_path: Path) -> None:
    """Normal (non-fast-track) path: SPEC_NORMALIZATION → SPEC_REVIEW
    with NO planner-spec receipt must refuse and the error must name the
    missing receipt step (`planner-spec`)."""
    td = _setup(tmp_path, stage="SPEC_NORMALIZATION", slug="PS-NOREC")
    with pytest.raises(ValueError) as excinfo:
        transition_task(td, "SPEC_REVIEW")
    assert "planner-spec" in str(excinfo.value), (
        f"gate error must name planner-spec — got: {excinfo.value!r}"
    )


def test_plan_review_requires_planner_plan(tmp_path: Path) -> None:
    """PLANNING → PLAN_REVIEW without a planner-plan receipt must refuse
    and the error must name the missing receipt step (`planner-plan`)."""
    td = _setup(tmp_path, stage="PLANNING", slug="PP-NOREC")
    with pytest.raises(ValueError) as excinfo:
        transition_task(td, "PLAN_REVIEW")
    assert "planner-plan" in str(excinfo.value), (
        f"gate error must name planner-plan — got: {excinfo.value!r}"
    )


# ---------------------------------------------------------------------------
# Fast-track path — planner-spec requirement skipped.
# ---------------------------------------------------------------------------


def test_fast_track_skips_planner_spec_requirement(tmp_path: Path) -> None:
    """Fast-track tasks (manifest.fast_track=True) combine Spec+Plan
    into one planner spawn and only emit a planner-plan receipt. The
    SPEC_NORMALIZATION → SPEC_REVIEW gate must therefore NOT require a
    planner-spec receipt on the fast-track path. With no planner-spec
    written, the transition must succeed."""
    td = _setup(tmp_path, stage="SPEC_NORMALIZATION",
                fast_track=True, slug="FT-TOP")
    # No planner-spec receipt present. Fast-track = gate skipped.
    transition_task(td, "SPEC_REVIEW")
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == "SPEC_REVIEW"


def test_fast_track_flag_under_classification_also_skips(tmp_path: Path) -> None:
    """Seg-1 accepts `manifest.classification.fast_track=True` as an
    alternate source of the flag (older manifests stored it there).
    This test pins that fallback so future refactors don't silently
    drop it."""
    td = _setup(tmp_path, stage="SPEC_NORMALIZATION",
                classification_fast_track=True, slug="FT-CLS")
    transition_task(td, "SPEC_REVIEW")
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == "SPEC_REVIEW"


# ---------------------------------------------------------------------------
# Success path — receipts written.
# ---------------------------------------------------------------------------


def test_both_gates_pass_when_receipts_present(tmp_path: Path) -> None:
    """End-to-end: write a planner-spec receipt; SPEC_NORMALIZATION →
    SPEC_REVIEW succeeds. Then write a planner-plan receipt; PLANNING
    → PLAN_REVIEW succeeds."""
    td = _setup(tmp_path, stage="SPEC_NORMALIZATION", slug="HAPPY")

    # --- first edge: need planner-spec --------------------------------
    _write_planner_receipt(td, "spec")
    transition_task(td, "SPEC_REVIEW")
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == "SPEC_REVIEW"

    # Move the task through SPEC_REVIEW → PLANNING manually by
    # rewriting the manifest rather than going through transition_task
    # (the human-approval gate between these two stages is out of
    # scope for this file's focus).
    manifest["stage"] = "PLANNING"
    (td / "manifest.json").write_text(json.dumps(manifest))

    # --- second edge: need planner-plan -------------------------------
    _write_planner_receipt(td, "plan")
    transition_task(td, "PLAN_REVIEW")
    manifest_after = json.loads((td / "manifest.json").read_text())
    assert manifest_after["stage"] == "PLAN_REVIEW"


def test_planner_plan_gate_still_fires_under_fast_track(tmp_path: Path) -> None:
    """Fast-track skips planner-spec but NOT planner-plan (fast-track
    still writes planner-plan for the combined spawn). A fast-track task
    with no planner-plan receipt must still be refused at
    PLANNING → PLAN_REVIEW. Regression guard — easy to mistakenly
    gate both fast-track checks together."""
    td = _setup(tmp_path, stage="PLANNING", fast_track=True, slug="FT-PP")
    with pytest.raises(ValueError, match="planner-plan"):
        transition_task(td, "PLAN_REVIEW")
