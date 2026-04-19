"""Unit tests for ``hooks/scheduler.py::compute_next_stage``.

Covers AC 16 of task-20260419-008 (and indirectly exercises the
signatures/behaviors required by AC 5, 6, 7, 8):

- (a) ``test_returns_none_for_every_non_spec_review_stage`` — parametrized
       over ``STAGE_ORDER \\ {"SPEC_REVIEW"}``.
- (b) ``test_spec_review_missing_receipt_returns_missing_proof``.
- (c) ``test_spec_review_hash_mismatch_returns_missing_proof``.
- (d) ``test_spec_review_valid_receipt_returns_clean_advance``.
- (e) ``test_compute_next_stage_is_pure`` — patches ``transition_task``,
       ``log_event``, ``emit_event``; asserts zero calls; snapshots
       directory byte-for-byte before/after.

The tests deliberately import from ``hooks.scheduler`` which does not yet
exist — the suite is expected to FAIL at collection (ImportError) in TDD
pre-implementation state. Once segment-3 (hooks/scheduler.py) lands the
tests must pass unchanged.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Match the project-wide convention (see existing test files like
# tests/test_receipt_human_approval.py, tests/test_eventbus_drain.py, etc.)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_core import STAGE_ORDER  # noqa: E402
from lib_receipts import hash_file, receipt_human_approval  # noqa: E402

# Import under test. This import will fail in TDD pre-implementation
# state (no hooks/scheduler.py exists yet) — that is CORRECT for
# red-phase TDD; once segment-3 lands the import resolves and all
# tests here run.
from scheduler import compute_next_stage  # noqa: E402


SPEC_MD_CONTENTS = (
    "# Normalized Spec\n\n"
    "## Task Summary\nTesting scheduler.\n\n"
    "## User Context\nCI.\n\n"
    "## Acceptance Criteria\n1. one\n2. two\n\n"
    "## Implicit Requirements Surfaced\nNone.\n\n"
    "## Out of Scope\nNone.\n\n"
    "## Assumptions\nsafe assumption: none\n\n"
    "## Risk Notes\nNone.\n"
)


def _make_manifest(task_dir: Path, stage: str, task_id: str = "task-TEST") -> None:
    """Write a minimal but schema-valid manifest.json at the given stage."""
    task_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "task_id": task_id,
        "created_at": "2026-04-19T00:00:00Z",
        "title": "Scheduler POC test",
        "raw_input": "x",
        "stage": stage,
        "classification": {
            "type": "feature",
            "domains": ["backend"],
            "risk_level": "medium",
            "notes": "n",
        },
        "retry_counts": {},
        "blocked_reason": None,
        "completion_at": None,
    }
    (task_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))


def _make_spec(task_dir: Path, contents: str = SPEC_MD_CONTENTS) -> str:
    """Write a spec.md and return its sha256."""
    spec_path = task_dir / "spec.md"
    spec_path.write_text(contents)
    return hash_file(spec_path)


def _build_task(tmp_path: Path, stage: str, *, with_spec: bool = True) -> Path:
    """Build a minimal <repo>/.dynos/task-TEST/ tree at the given stage."""
    root = tmp_path / "project"
    root.mkdir(parents=True, exist_ok=True)
    (root / ".dynos").mkdir(parents=True, exist_ok=True)
    task_dir = root / ".dynos" / "task-TEST"
    _make_manifest(task_dir, stage)
    if with_spec:
        _make_spec(task_dir)
    return task_dir


def _write_approval_receipt_direct(task_dir: Path, artifact_sha256: str) -> None:
    """Write a valid human-approval-SPEC_REVIEW receipt JSON directly.

    Bypasses ``receipt_human_approval`` — which would fire the
    synchronous scheduler dispatch inside ``write_receipt`` and
    auto-advance the manifest to PLANNING, contaminating the fixture
    state these unit tests measure. Writing the receipt file by hand
    keeps ``manifest.stage`` at ``SPEC_REVIEW`` so
    ``compute_next_stage`` exercises the happy-path branch under test.

    Schema mirrors ``write_receipt``'s output for step
    ``human-approval-SPEC_REVIEW`` (see .dynos/task-*/receipts/ for a
    live example).
    """
    receipts_dir = task_dir / "receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    receipt = {
        "step": "human-approval-SPEC_REVIEW",
        "ts": "2026-04-19T00:00:00Z",
        "valid": True,
        "contract_version": 4,
        "stage": "SPEC_REVIEW",
        "artifact_sha256": artifact_sha256,
        "approver": "human",
    }
    (receipts_dir / "human-approval-SPEC_REVIEW.json").write_text(
        json.dumps(receipt, indent=2)
    )


def _snapshot_tree(task_dir: Path) -> dict[str, tuple[int, int, str]]:
    """Return {relative_path: (size, mtime_ns, sha256)} for every file under task_dir.

    Used to prove purity: a pure function MUST leave every file byte-for-byte
    identical. Comparing sha256 defends against identical-timestamp edits and
    size-preserving edits (both of which mtime alone could miss).
    """
    snap: dict[str, tuple[int, int, str]] = {}
    for base, _dirs, files in os.walk(task_dir):
        for name in sorted(files):
            abs_path = Path(base) / name
            rel = str(abs_path.relative_to(task_dir))
            st = abs_path.stat()
            data = abs_path.read_bytes()
            snap[rel] = (st.st_size, st.st_mtime_ns, hashlib.sha256(data).hexdigest())
    return snap


# ---------------------------------------------------------------------------
# (a) Scope ceiling: every non-SPEC_REVIEW stage returns (None, [])
# ---------------------------------------------------------------------------

NON_SPEC_REVIEW_STAGES = [s for s in STAGE_ORDER if s != "SPEC_REVIEW"]


@pytest.mark.parametrize("stage", NON_SPEC_REVIEW_STAGES)
def test_returns_none_for_every_non_spec_review_stage(
    tmp_path: Path, stage: str
) -> None:
    """AC 8 / AC 16 (a): scope ceiling is in code, not by omission.

    For EVERY stage in STAGE_ORDER except SPEC_REVIEW, compute_next_stage
    MUST return exactly ``(None, [])`` without evaluating any predicate.
    This is the test that prevents an executor from "helpfully" adding a
    second ``elif`` arm beyond the POC scope (D6).
    """
    task_dir = _build_task(tmp_path, stage)
    # Also write a valid-looking human-approval-SPEC_REVIEW receipt to
    # prove that even with proof material sitting on disk, a non-SPEC_REVIEW
    # manifest still short-circuits to (None, []).
    spec_sha = hash_file(task_dir / "spec.md")
    receipt_human_approval(task_dir, "SPEC_REVIEW", spec_sha, approver="human")

    result = compute_next_stage(task_dir)

    assert result == (None, []), (
        f"compute_next_stage must return (None, []) for stage={stage!r}; "
        f"got {result!r}. Scope ceiling (D6/AC 8) violated."
    )


# ---------------------------------------------------------------------------
# (b) SPEC_REVIEW, missing receipt → ("PLANNING", [<missing reason>])
# ---------------------------------------------------------------------------

def test_spec_review_missing_receipt_returns_missing_proof(tmp_path: Path) -> None:
    """AC 7 / AC 16 (b): missing human-approval receipt is reported.

    Reason wording must mirror ``_human_approval_err`` in lib_core.py:
    contains the literal substrings "missing" AND
    "human-approval-SPEC_REVIEW".
    """
    task_dir = _build_task(tmp_path, "SPEC_REVIEW")
    (task_dir / "receipts").mkdir(parents=True, exist_ok=True)
    # NO human-approval-SPEC_REVIEW.json is written.

    next_stage, reasons = compute_next_stage(task_dir)

    assert next_stage == "PLANNING", (
        f"On SPEC_REVIEW, proposed next_stage must be 'PLANNING' even when "
        f"predicates fail; got {next_stage!r}"
    )
    assert isinstance(reasons, list), f"reasons must be list[str]; got {type(reasons)}"
    assert len(reasons) >= 1, (
        f"Missing receipt must yield at least one reason string; got {reasons!r}"
    )
    joined = " || ".join(reasons).lower()
    assert "missing" in joined, (
        f"At least one reason must contain 'missing'; got {reasons!r}"
    )
    assert "human-approval-spec_review" in joined, (
        f"At least one reason must name 'human-approval-SPEC_REVIEW'; "
        f"got {reasons!r}"
    )


# ---------------------------------------------------------------------------
# (c) SPEC_REVIEW, hash mismatch → ("PLANNING", [<hash mismatch reason>])
# ---------------------------------------------------------------------------

def test_spec_review_hash_mismatch_returns_missing_proof(tmp_path: Path) -> None:
    """AC 7 / AC 16 (c): receipt present but artifact_sha256 drift.

    Reason wording must mirror ``_human_approval_err``'s hash-mismatch
    branch: contains the literal substrings "hash mismatch" AND
    "human-approval-SPEC_REVIEW".
    """
    task_dir = _build_task(tmp_path, "SPEC_REVIEW")
    # Write a receipt pinned to a BOGUS hash (stale approval vs. the live spec).
    receipt_human_approval(
        task_dir, "SPEC_REVIEW", "a" * 64, approver="human"
    )
    # Sanity: the live spec's hash is NOT "a" * 64.
    live_sha = hash_file(task_dir / "spec.md")
    assert live_sha != "a" * 64

    next_stage, reasons = compute_next_stage(task_dir)

    assert next_stage == "PLANNING", (
        f"next_stage must be 'PLANNING' on hash-mismatch refusal; got {next_stage!r}"
    )
    assert isinstance(reasons, list), f"reasons must be list[str]; got {type(reasons)}"
    assert len(reasons) >= 1, f"Hash mismatch must yield a reason; got {reasons!r}"
    joined = " || ".join(reasons).lower()
    assert "hash mismatch" in joined, (
        f"At least one reason must contain 'hash mismatch'; got {reasons!r}"
    )
    assert "human-approval-spec_review" in joined, (
        f"At least one reason must name 'human-approval-SPEC_REVIEW'; "
        f"got {reasons!r}"
    )


# ---------------------------------------------------------------------------
# (d) SPEC_REVIEW, valid receipt → ("PLANNING", []) EXACTLY
# ---------------------------------------------------------------------------

def test_spec_review_valid_receipt_returns_clean_advance(tmp_path: Path) -> None:
    """AC 7 / AC 16 (d): happy path returns (PLANNING, []) EXACTLY.

    Every predicate passes:
      - receipt exists with valid=true and contract_version >= 2
        (``write_receipt`` always writes RECEIPT_CONTRACT_VERSION which
        satisfies ``MIN_VERSION_PER_STEP["human-approval-*"] = 2``).
      - spec.md exists.
      - receipt.artifact_sha256 == hash_file(spec.md).
    """
    task_dir = _build_task(tmp_path, "SPEC_REVIEW")
    live_sha = hash_file(task_dir / "spec.md")
    _write_approval_receipt_direct(task_dir, live_sha)

    result = compute_next_stage(task_dir)

    assert result == ("PLANNING", []), (
        f"Valid-proofs happy path must return ('PLANNING', []) EXACTLY; "
        f"got {result!r}"
    )


# ---------------------------------------------------------------------------
# (e) Purity: no writes, no log_event, no emit_event, no transition_task
# ---------------------------------------------------------------------------

def test_compute_next_stage_is_pure(tmp_path: Path) -> None:
    """AC 6 / AC 16 (e): compute_next_stage is load-bearing pure.

    Two axes of verification:

    1. MOCKS on the three "I/O escape hatches" the scheduler module might
       import — ``transition_task``, ``log_event``, ``emit_event``. After
       the call, each mock's ``call_count`` MUST be zero. We patch both
       the definition-site attribute AND the import-site attribute on
       ``scheduler`` (if that attribute exists) because Python resolves
       names at the call site, not the definition site.
    2. BYTE-FOR-BYTE directory snapshot before vs. after. Any write,
       rename, or touch within ``task_dir`` violates purity. Comparing
       (size, mtime_ns, sha256) catches even identical-size content
       rewrites that would slip past an mtime-only check.
    """
    task_dir = _build_task(tmp_path, "SPEC_REVIEW")
    live_sha = hash_file(task_dir / "spec.md")
    _write_approval_receipt_direct(task_dir, live_sha)

    before = _snapshot_tree(task_dir)

    # Patch at the DEFINITION sites. Patching at scheduler.<name> would
    # only cover names that scheduler imports directly; patching at
    # the owning modules covers BOTH direct and re-exported call chains.
    with patch("lib_core.transition_task") as m_transition, \
         patch("lib_log.log_event") as m_log, \
         patch("lib_events.emit_event") as m_emit:

        # Additionally, if scheduler imports these names with
        # ``from lib_core import transition_task`` (etc.), the name is
        # bound on the scheduler module at import time and must be
        # patched THERE too. Use ``create=True`` so the patch works even
        # if the attribute does not yet exist on scheduler.
        with patch("scheduler.transition_task", create=True) as m_s_transition, \
             patch("scheduler.log_event", create=True) as m_s_log, \
             patch("scheduler.emit_event", create=True) as m_s_emit:

            result = compute_next_stage(task_dir)

            # Sanity: the happy-path call produces the expected tuple
            # shape regardless of which names are patched — this asserts
            # we actually exercised a SPEC_REVIEW code path with proof
            # present, not a degenerate early-return.
            assert result == ("PLANNING", []), (
                f"Purity test fixture must hit the happy-path branch; "
                f"got {result!r}"
            )

            # 1. No writer/event/transition call anywhere.
            assert m_transition.call_count == 0, (
                f"compute_next_stage called transition_task "
                f"{m_transition.call_count} times; must be 0. Purity broken."
            )
            assert m_log.call_count == 0, (
                f"compute_next_stage called log_event "
                f"{m_log.call_count} times; must be 0. Purity broken."
            )
            assert m_emit.call_count == 0, (
                f"compute_next_stage called emit_event "
                f"{m_emit.call_count} times; must be 0. Purity broken."
            )
            assert m_s_transition.call_count == 0, (
                f"compute_next_stage called scheduler.transition_task "
                f"{m_s_transition.call_count} times; must be 0."
            )
            assert m_s_log.call_count == 0, (
                f"compute_next_stage called scheduler.log_event "
                f"{m_s_log.call_count} times; must be 0."
            )
            assert m_s_emit.call_count == 0, (
                f"compute_next_stage called scheduler.emit_event "
                f"{m_s_emit.call_count} times; must be 0."
            )

    # 2. Byte-for-byte equality of the directory tree.
    after = _snapshot_tree(task_dir)
    assert set(before.keys()) == set(after.keys()), (
        f"compute_next_stage added or removed files. "
        f"before_only={set(before) - set(after)!r}; "
        f"after_only={set(after) - set(before)!r}"
    )
    for rel in before:
        b_size, b_mtime, b_sha = before[rel]
        a_size, a_mtime, a_sha = after[rel]
        assert b_size == a_size, f"Size changed for {rel}: {b_size} -> {a_size}"
        assert b_sha == a_sha, (
            f"SHA256 changed for {rel}: {b_sha} -> {a_sha} "
            f"(compute_next_stage mutated file contents)"
        )
        assert b_mtime == a_mtime, (
            f"mtime changed for {rel}: {b_mtime} -> {a_mtime} "
            f"(compute_next_stage rewrote or touched the file)"
        )
