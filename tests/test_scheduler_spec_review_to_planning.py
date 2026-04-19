"""Integration test for approve-stage -> receipt -> scheduler -> PLANNING.

Covers AC 15 of task-20260419-008. This is the SINGLE merge-gate test
per the User Context section of the spec:

    "The integration test in AC 15 is the single gate that blocks merge;
     reviewers cannot 'spot-check' a state-machine diff visually."

The test exercises the full in-process dispatch chain:

    cmd_approve_stage
        -> receipt_human_approval
            -> write_receipt
                -> _atomic_write_text (receipt now durable)
                -> log_event("receipt_written", ...)
                -> scheduler.handle_receipt_written(task_dir, step, sha)
                    -> compute_next_stage -> ("PLANNING", [])
                    -> transition_task(task_dir, "PLANNING")
                        -> manifest["stage"] = "PLANNING"
                        -> log_event("stage_transition", ...)

After ``cmd_approve_stage`` returns, the manifest MUST be at PLANNING
(proves synchronous in-process dispatch — an async queued path would
leave the manifest at SPEC_REVIEW at this assertion point).

Pre-implementation (TDD red phase): the test FAILS because
``cmd_approve_stage`` still calls ``transition_task`` directly
(legacy path) AND the ``hooks/scheduler.py`` module does not exist, so
the import-chain fires ImportError inside ``write_receipt``'s lazy
import. Post-implementation (AC 5-12): the test passes cleanly.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Match the project-wide sys.path convention (see
# tests/test_receipt_human_approval.py, tests/test_eventbus_drain.py, etc.)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_receipts import hash_file  # noqa: E402
import ctl as ctl_module  # noqa: E402


SPEC_MD_CONTENTS = (
    "# Normalized Spec\n\n"
    "## Task Summary\nIntegration test.\n\n"
    "## User Context\nCI.\n\n"
    "## Acceptance Criteria\n1. first\n2. second\n\n"
    "## Implicit Requirements Surfaced\nNone.\n\n"
    "## Out of Scope\nNone.\n\n"
    "## Assumptions\nsafe assumption: none\n\n"
    "## Risk Notes\nNone.\n"
)


def _build_task(tmp_path: Path) -> Path:
    """Build a minimal <repo>/.dynos/task-TEST/ tree at SPEC_REVIEW.

    Mirrors the minimal-manifest shape used by tests/test_ctl_approve_stage.py
    so that downstream gates (classification schema, task_id format) accept
    it without complaint.
    """
    root = tmp_path / "project"
    root.mkdir()
    (root / ".dynos").mkdir()
    task_dir = root / ".dynos" / "task-TEST"
    task_dir.mkdir(parents=True)

    (task_dir / "manifest.json").write_text(
        json.dumps(
            {
                "task_id": "task-TEST",
                "created_at": "2026-04-19T00:00:00Z",
                "title": "Scheduler integration test",
                "raw_input": "x",
                "stage": "SPEC_REVIEW",
                "classification": {
                    "type": "feature",
                    "domains": ["backend"],
                    "risk_level": "medium",
                    "notes": "n",
                },
                "retry_counts": {},
                "blocked_reason": None,
                "completion_at": None,
            },
            indent=2,
        )
    )
    (task_dir / "spec.md").write_text(SPEC_MD_CONTENTS)
    (task_dir / "receipts").mkdir()
    return task_dir


def _read_events(task_dir: Path) -> list[dict]:
    """Return all JSONL records from the task-scoped events.jsonl.

    log_event writes to ``<task_dir>/events.jsonl`` when the task dir
    exists (see hooks/lib_log.py:104-109). We also union in the global
    ``.dynos/events.jsonl`` as a fallback in case a rare code path
    writes there instead.
    """
    records: list[dict] = []
    task_events = task_dir / "events.jsonl"
    if task_events.exists():
        for line in task_events.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    global_events = task_dir.parent / "events.jsonl"
    if global_events.exists():
        for line in global_events.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def test_approve_stage_spec_review_advances_to_planning_via_scheduler(
    tmp_path: Path,
) -> None:
    """AC 15: full end-to-end scheduler dispatch chain.

    Invokes ``hooks.ctl.cmd_approve_stage`` in-process via an
    ``argparse.Namespace`` (matching the established harness pattern
    used by ``tests/test_ctl_approve_stage.py`` and
    ``tests/test_ctl.py`` for in-process ctl invocations), then asserts:

    (c) return value == 0
    (d) human-approval-SPEC_REVIEW.json exists and artifact_sha256
        matches hash_file(spec.md)
    (e) manifest.json["stage"] == "PLANNING" EXACTLY (proves
        synchronous in-process dispatch)
    (f) events.jsonl contains a stage_transition SPEC_REVIEW -> PLANNING
    (g) NO scheduler-refused.json was written
    """
    task_dir = _build_task(tmp_path)

    # (b) Invoke cmd_approve_stage in-process with argparse.Namespace.
    # AC 15 (b) + plan OQ4 direct us to use the same shape as existing
    # ctl tests. The existing tests/test_ctl_approve_stage.py drives
    # ctl via subprocess, but AC 15 explicitly says "using
    # argparse.Namespace or the equivalent helper already used by other
    # ctl tests." argparse.Namespace matches the cmd_* function
    # signature expected by ctl.py (see e.g. cmd_approve_stage(args)
    # -> int at hooks/ctl.py:109), so we use it directly.
    #
    # RUTHLESSNESS: we wrap ctl.transition_task in a spy-raise so that
    # if cmd_approve_stage still calls transition_task directly (the
    # legacy path deleted by AC 11), this test fails loudly. Without
    # this spy, the test would spuriously pass in partial-implementation
    # states where segments 3-4 land but segment-5 (ctl.py trim) does
    # not — the legacy transition_task call would advance the manifest
    # and the test would green unjustifiedly. The spy makes AC 11's
    # "ctl.py no longer drives the transition" contract verifiable.
    ns = argparse.Namespace(task_dir=str(task_dir), stage="SPEC_REVIEW")
    with patch.object(
        ctl_module,
        "transition_task",
        side_effect=AssertionError(
            "ctl.cmd_approve_stage MUST NOT call transition_task after "
            "AC 11 — the scheduler owns the SPEC_REVIEW -> PLANNING "
            "transition. Found a direct call from ctl.py."
        ),
    ):
        rc = ctl_module.cmd_approve_stage(ns)

    # (c) return 0.
    assert rc == 0, (
        f"cmd_approve_stage must return 0 on happy path; got {rc}. "
        f"Either the receipt write failed or the scheduler dispatch "
        f"refused the advance."
    )

    # (d) Receipt exists and artifact_sha256 matches live spec hash.
    receipt_path = task_dir / "receipts" / "human-approval-SPEC_REVIEW.json"
    assert receipt_path.exists(), (
        f"human-approval-SPEC_REVIEW receipt MUST exist at {receipt_path}; "
        f"cmd_approve_stage never wrote it."
    )
    receipt = json.loads(receipt_path.read_text())
    live_sha = hash_file(task_dir / "spec.md")
    assert receipt.get("artifact_sha256") == live_sha, (
        f"Receipt artifact_sha256 drift: "
        f"receipt={receipt.get('artifact_sha256')!r}, "
        f"live={live_sha!r}"
    )
    assert receipt.get("valid") is True, (
        f"Receipt must have valid=true; got {receipt!r}"
    )
    # Contract version floor per MIN_VERSION_PER_STEP["human-approval-*"] = 2
    assert int(receipt.get("contract_version", 0)) >= 2, (
        f"Receipt contract_version must be >= 2 to be accepted by "
        f"read_receipt; got {receipt.get('contract_version')!r}"
    )

    # (e) Manifest at PLANNING — THE critical assertion.
    manifest = json.loads((task_dir / "manifest.json").read_text())
    assert manifest.get("stage") == "PLANNING", (
        f"manifest.stage must be EXACTLY 'PLANNING' after cmd_approve_stage "
        f"returns (synchronous scheduler dispatch contract). "
        f"Got {manifest.get('stage')!r}. If this is 'SPEC_REVIEW', the "
        f"scheduler was not wired into write_receipt. If this is anything "
        f"else, something advanced past PLANNING unexpectedly."
    )

    # (f) stage_transition event with from=SPEC_REVIEW, to=PLANNING.
    # transition_task's _auto_log emits log_event with fields
    # ``from_stage`` / ``to_stage`` (see hooks/lib_core.py:1877-1884).
    # We accept either naming convention to be robust:
    # - from_stage/to_stage (actual emission from transition_task)
    # - stage_from/stage_to (naming used elsewhere, e.g. gate_refused)
    events = _read_events(task_dir)
    transitions = [e for e in events if e.get("event") == "stage_transition"]
    assert transitions, (
        f"events.jsonl must contain at least one stage_transition event; "
        f"got events={[e.get('event') for e in events]!r}"
    )

    def _matches(e: dict) -> bool:
        frm = e.get("from_stage") or e.get("stage_from")
        to = e.get("to_stage") or e.get("stage_to")
        return frm == "SPEC_REVIEW" and to == "PLANNING"

    matching = [e for e in transitions if _matches(e)]
    assert matching, (
        f"events.jsonl must contain a stage_transition with "
        f"from=SPEC_REVIEW and to=PLANNING; got transitions={transitions!r}"
    )

    # (g) NO scheduler-refused receipt — the happy path MUST NOT write
    # receipt_scheduler_refused because compute_next_stage returns
    # (PLANNING, []) with no missing proofs.
    refused_path = task_dir / "receipts" / "scheduler-refused.json"
    assert not refused_path.exists(), (
        f"scheduler-refused.json MUST NOT be written on the happy path; "
        f"found receipt at {refused_path}. Scheduler wrongly refused a "
        f"valid advance (check compute_next_stage predicates)."
    )
