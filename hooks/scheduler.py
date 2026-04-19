"""Receipt-driven stage-transition scheduler for dynos-work.

The scheduler inverts control for a narrow slice of the state machine:
instead of a caller (prose in a skill, a CLI command, an executor) deciding
when to advance a task, the scheduler observes receipt writes via the
``write_receipt`` chokepoint in ``hooks/lib_receipts.py`` and — when every
required proof is present — drives ``transition_task`` itself.

This module exports two symbols:

* ``compute_next_stage(task_dir)`` — a PURE predicate. Reads the manifest
  and receipts/artifacts, returns a tuple describing either the next
  stage to advance to or the missing proofs blocking advancement.
  Never writes, never emits events, never calls ``transition_task``.

* ``handle_receipt_written(task_dir, receipt_step, receipt_sha256)`` —
  the I/O-capable dispatcher invoked from ``write_receipt`` after each
  receipt is durably on disk. Calls ``compute_next_stage``, routes the
  result to ``transition_task`` on clean advance, emits observability
  events on refusal or race, and NEVER raises — a scheduler failure
  cannot roll back the receipt write that triggered it.

Scope ceiling (task-20260419-008 POC): ``compute_next_stage`` handles
only ``SPEC_REVIEW -> PLANNING``. Every other ``current_stage`` value
returns ``(None, [])`` via an explicit early-return so that future
migrations are adds-only (one more ``elif`` arm) and cannot be
introduced by accident (see AC 8, D6).

Design notes (pinned by plan.md):
* D1 — synchronous in-process dispatch via the ``write_receipt``
  chokepoint; no async queue, no fs watcher, no separate process.
* D5 — purity of ``compute_next_stage`` is load-bearing. It is the
  property that makes the dispatch race-free.
* Circular-import avoidance — ``scheduler.py`` is imported lazily from
  inside ``write_receipt``. Conversely, ``receipt_scheduler_refused``
  is imported lazily from inside ``handle_receipt_written`` so this
  module can load before segment-4 lands the new writer.
"""

from __future__ import annotations

import sys
from pathlib import Path

from lib_core import transition_task
from lib_log import log_event
from lib_receipts import _receipts_dir, hash_file, read_receipt


# Receipt step name used for the refusal receipt written when
# ``compute_next_stage`` returns missing proofs. Short-circuits the
# recursion that would otherwise fire when that receipt's own
# ``write_receipt`` re-enters ``handle_receipt_written`` via the
# chokepoint hook (plan.md Open Question 1).
_SCHEDULER_REFUSED_STEP = "scheduler-refused"


def compute_next_stage(task_dir: Path) -> tuple[str | None, list[str]]:
    """Pure predicate: decide whether the task at ``task_dir`` should advance.

    Returns a 2-tuple ``(next_stage, missing_proofs)`` where:

    * ``(None, [])`` — no advance is due (current stage is outside the
      POC scope ceiling).
    * ``("PLANNING", [])`` — current stage is ``SPEC_REVIEW`` AND every
      predicate required for ``SPEC_REVIEW -> PLANNING`` passes.
    * ``("PLANNING", [reason])`` — current stage is ``SPEC_REVIEW`` but
      at least one predicate fails. The reason string format mirrors
      ``_human_approval_err`` in ``hooks/lib_core.py`` so callers and
      operators see identical wording regardless of whether the
      advancement was gated by ``transition_task`` itself or the
      scheduler.

    Purity contract (AC 6 / D5): this function MUST NOT write any file,
    call ``transition_task``, call ``log_event``, call ``emit_event``,
    issue network requests, mutate the manifest, or invoke any
    subprocess. Its only permitted filesystem operations are READ
    operations on ``task_dir / "manifest.json"``,
    ``task_dir / "receipts" / *.json``, ``task_dir / "spec.md"``,
    ``task_dir / "plan.md"``, and ``task_dir / "execution-graph.json"``.

    Exceptions encountered while reading (missing manifest, malformed
    JSON, hash of a vanished artifact) propagate to the caller —
    ``handle_receipt_written`` classifies them in its outer wrapper.
    """
    # ---- Read current stage from manifest (read-only).
    import json  # Local import keeps the module's top-level import
                 # surface minimal and emphasizes purity — nothing
                 # imported here has any side effect on load.

    manifest_path = task_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    current_stage = manifest.get("stage") if isinstance(manifest, dict) else None

    # ---- AC 8: scope ceiling. Explicit early-return for every stage
    # except SPEC_REVIEW, regardless of what receipts exist on disk.
    # Do NOT replace this with a dict dispatch or a set membership check
    # — the inequality is the contract that future migrations must
    # deliberately extend with a new ``elif`` arm.
    if current_stage != "SPEC_REVIEW":
        return (None, [])

    # ---- AC 7: mirror _check_human_approval("SPEC_REVIEW", spec.md).
    # The wording of each reason string mirrors _human_approval_err in
    # hooks/lib_core.py (lines 728-756) so operators see the same
    # diagnostic surface whether the block was raised by transition_task
    # or reported by the scheduler. Sequential short-circuit: the FIRST
    # predicate failure returns with a single reason, matching
    # _human_approval_err's early-return shape.
    receipt_step = "human-approval-SPEC_REVIEW"
    receipt_path = _receipts_dir(task_dir) / f"{receipt_step}.json"
    artifact_path = task_dir / "spec.md"
    next_stage = "PLANNING"

    # (a) Receipt exists and read_receipt returns non-None (i.e. valid
    # with contract_version >= MIN_VERSION_PER_STEP["human-approval-*"]).
    receipt = read_receipt(task_dir, receipt_step)
    if receipt is None:
        return (
            next_stage,
            [f"missing {receipt_step} at {receipt_path}"],
        )

    # (b) spec.md exists on disk. Use a distinct reason string so callers
    # can tell this apart from (a) receipt-missing — mirrors the three-way
    # split in _human_approval_err (hooks/lib_core.py:728-756).
    if not artifact_path.exists():
        return (
            next_stage,
            [
                f"receipt {receipt_step} present at {receipt_path} but "
                f"artifact missing at {artifact_path}"
            ],
        )

    # (c) Receipt's artifact_sha256 matches the live hash of spec.md.
    expected = receipt.get("artifact_sha256") or ""
    actual = hash_file(artifact_path)
    if not isinstance(expected, str) or expected != actual:
        return (
            next_stage,
            [
                f"hash mismatch for {receipt_step}: "
                f"expected={(expected or '')[:12]} actual={actual[:12]}"
            ],
        )

    # Every predicate passed — clean advance.
    return (next_stage, [])


def handle_receipt_written(
    task_dir: Path,
    receipt_step: str,
    receipt_sha256: str,
) -> None:
    """Scheduler dispatcher invoked by ``write_receipt`` after each receipt.

    Behavior per AC 9:

    * Short-circuits (returns) when ``receipt_step`` is the
      scheduler-owned refusal receipt. This breaks the recursion that
      would otherwise fire when the refusal-receipt's own
      ``write_receipt`` re-enters this function via the chokepoint
      hook (plan.md Open Question 1, resolution approved).
    * Calls ``compute_next_stage(task_dir)`` exactly once.
    * On ``(next_stage, [])`` with ``next_stage is not None``: calls
      ``transition_task(task_dir, next_stage)`` without ``force``. The
      ``stage_transition`` log-line written by ``transition_task`` is
      the observability surface — no additional writes here.
    * On ``ValueError`` from ``transition_task`` (illegal transition —
      e.g. another caller already advanced the stage): swallow the
      exception, emit a ``scheduler_transition_race`` event via
      ``log_event``, and return. The scheduler is the loser of the
      race, not a failure.
    * On ``(next_stage, [missing_proofs])`` with non-empty
      ``missing_proofs``: does NOT call ``transition_task``. Emits a
      ``scheduler_transition_refused`` event via ``log_event`` AND
      writes ``receipts/scheduler-refused.json`` via the
      ``receipt_scheduler_refused`` writer (segment-4).
    * On ``(None, [])``: no-op (no event, no receipt).

    Never raises. Any exception other than the expected ``ValueError``
    from ``transition_task`` is caught by the outer wrapper, logged to
    stderr as ``[scheduler] WARNING: {exc}``, and swallowed. This
    protects the receipt-write caller from ripple-failures: the
    receipt is already durable on disk by the time the scheduler runs
    (AC 10 / Implicit Requirement "Receipt write durability").

    ``receipt_sha256`` is currently unused by this POC — it is captured
    on the calling side to keep the chokepoint signature forward-
    compatible with downstream consumers that may want to audit the
    exact bytes that fired the scheduler (future work).
    """
    # ---- Recursion guard (plan.md OQ 1): swallow the refusal-receipt's
    # own re-entry before any other work. Placed OUTSIDE the try/except
    # so it is the cheapest possible early-return path; also means a
    # broken logging path during a refusal cannot trigger an infinite
    # loop.
    if receipt_step == _SCHEDULER_REFUSED_STEP:
        return

    # Reserved for future audit consumers — see docstring.
    del receipt_sha256  # noqa: F841

    # ---- Inner classification: ValueError from transition_task is the
    # "lost the race" signal (see ALLOWED_STAGE_TRANSITIONS re-entry
    # rejection at hooks/lib_core.py:1165). Any OTHER exception from
    # ANY call path (compute_next_stage, transition_task, log_event,
    # the receipt writer) is caught by the outer except and logged to
    # stderr. Two layers are needed because the inner ValueError
    # handler itself may call log_event, which could raise on disk
    # failure — the outer wrapper absorbs that ripple.
    try:
        result = compute_next_stage(task_dir)
        next_stage, missing_proofs = result

        if next_stage is None:
            # (None, []) path — compute_next_stage said "no advance
            # due"; by contract missing_proofs is empty here.
            return

        # Derive current_stage from the manifest AFTER compute_next_stage
        # has run. This is a read, not a mutation — used only for event
        # payload context. If the manifest is unreadable, fall back to
        # None; the outer wrapper still logs a warning.
        current_stage = _read_current_stage(task_dir)
        task_id = _read_task_id(task_dir)
        root = task_dir.parent.parent

        if not missing_proofs:
            # Clean advance: the scheduler is the caller of
            # transition_task. A ValueError here means
            # ALLOWED_STAGE_TRANSITIONS rejected the edge — treat as a
            # lost race (some other caller already advanced).
            try:
                transition_task(task_dir, next_stage)
            except ValueError as race_exc:
                try:
                    log_event(
                        root,
                        "scheduler_transition_race",
                        task=task_id,
                        task_id=task_id,
                        current_stage=current_stage,
                        proposed_stage=next_stage,
                        error_message=str(race_exc),
                    )
                except Exception as log_exc:  # pragma: no cover — defensive
                    print(
                        f"[scheduler] WARNING: {log_exc}",
                        file=sys.stderr,
                    )
            return

        # Refusal path: missing proofs. Emit event THEN write
        # refusal-receipt. The event is independently durable
        # regardless of whether the receipt write succeeds, so
        # operators always see the reason even if disk is full.
        try:
            log_event(
                root,
                "scheduler_transition_refused",
                task=task_id,
                task_id=task_id,
                current_stage=current_stage,
                proposed_stage=next_stage,
                missing_proofs=list(missing_proofs),
            )
        except Exception as log_exc:  # pragma: no cover — defensive
            print(f"[scheduler] WARNING: {log_exc}", file=sys.stderr)

        # Lazy import of receipt_scheduler_refused (segment-4 owns the
        # writer in lib_receipts.py). Doing this inside the function
        # means module load succeeds even before segment-4 lands; the
        # import resolves at call-time.
        try:
            from lib_receipts import receipt_scheduler_refused  # type: ignore
        except ImportError as imp_exc:
            print(
                f"[scheduler] WARNING: {imp_exc}",
                file=sys.stderr,
            )
            return

        try:
            receipt_scheduler_refused(
                task_dir,
                current_stage or "",
                next_stage,
                list(missing_proofs),
            )
        except Exception as rec_exc:
            # Refusal-receipt write failure is non-fatal: the event
            # above already captured the refusal for observability.
            print(
                f"[scheduler] WARNING: {rec_exc}",
                file=sys.stderr,
            )

    except Exception as exc:
        # AC 9: MUST NOT raise. Any uncaught exception (malformed
        # manifest, missing files, I/O error inside compute_next_stage,
        # unexpected transition_task exception, etc.) is swallowed with
        # a stderr warning. The receipt that triggered the scheduler
        # remains durable on disk.
        print(f"[scheduler] WARNING: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Internal read helpers
# ---------------------------------------------------------------------------
#
# These helpers exist only to make handle_receipt_written's event-payload
# construction robust against a missing/malformed manifest. They do NOT
# participate in compute_next_stage's predicate evaluation and are kept
# at module scope so test doubles can observe them if needed. Both
# return None on any read error rather than propagating — the outer
# wrapper's job is to keep the scheduler from raising.

def _read_current_stage(task_dir: Path) -> str | None:
    import json

    try:
        manifest = json.loads((task_dir / "manifest.json").read_text())
        if isinstance(manifest, dict):
            stage = manifest.get("stage")
            if isinstance(stage, str):
                return stage
    except (OSError, ValueError):
        return None
    return None


def _read_task_id(task_dir: Path) -> str:
    import json

    try:
        manifest = json.loads((task_dir / "manifest.json").read_text())
        if isinstance(manifest, dict):
            tid = manifest.get("task_id")
            if isinstance(tid, str) and tid:
                return tid
    except (OSError, ValueError):
        pass
    return task_dir.name
