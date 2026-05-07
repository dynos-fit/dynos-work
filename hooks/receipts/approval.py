"""Human-approval / postmortem / calibration / scheduler receipt writers.

Each function in this module writes a structured JSON receipt for an
approval-domain pipeline step (human approval, postmortem
generated/analyzed/skipped, calibration applied/noop, rules-check pass,
force-override, scheduler refusal). Function bodies are copied
byte-for-byte from ``hooks/lib_receipts.py`` during the receipts package
split — see ``hooks/receipts/__init__.py`` for the public re-export
surface.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from lib_core import now_iso, _persistent_project_dir
from lib_validate import require_nonblank, require_nonblank_str

from .core import (
    hash_file,
    write_receipt,
)


# Allowed reasons for receipt_postmortem_skipped. Enum-validated at write
# time so callers cannot silently drift the skip taxonomy.
#
# A prior "quality-over-gate" skip reason was removed
# (task-20260419-002 G1): it was being used to silently skip LLM
# postmortems on tasks that had real non-blocking findings. The
# remaining two reasons ONLY permit a skip when there is literally
# nothing to learn (clean-task) or nothing the auditors found
# (no-findings). Every other skip path must cite prior postmortem work
# via the required `subsumed_by` argument on
# `receipt_postmortem_skipped` (see G2).
_POSTMORTEM_SKIP_REASONS = frozenset({
    "clean-task",
    "no-findings",
})

# Regex for the task_id slug shape — matches SEC-001's regex from PR #126.
# Used to validate entries in the `subsumed_by` list on
# `receipt_postmortem_skipped`.
_SUBSUMED_BY_TASK_ID_RE = re.compile(r"^task-[A-Za-z0-9][A-Za-z0-9_.-]*$")


def receipt_human_approval(
    task_dir: Path,
    stage: str,
    artifact_sha256: str,
    approver: str = "human",
) -> Path:
    """Write receipt proving a human approved an artifact at `stage`.

    Writes ``receipts/human-approval-{stage}.json``.

    Validates inputs: `stage` must be a non-empty identifier-safe string
    (no path separators), `artifact_sha256` must be a non-empty string,
    `approver` defaults to "human".
    """
    require_nonblank_str(stage, field_name="stage")
    if "/" in stage or "\\" in stage or stage.startswith("."):
        raise ValueError(f"stage must not contain path separators: {stage!r}")
    require_nonblank_str(artifact_sha256, field_name="artifact_sha256")
    try:
        require_nonblank(approver, field_name="approver")
    except (TypeError, ValueError):
        raise ValueError(
            "approver must be a non-empty string "
            "(whitespace-only values are rejected — whitespace carries no "
            "human identity)"
        )

    return write_receipt(
        task_dir,
        f"human-approval-{stage}",
        stage=stage,
        artifact_sha256=artifact_sha256,
        approver=approver,
    )


def receipt_auto_approval(
    task_dir: Path,
    stage: str,
    artifact_sha256: str,
    *,
    approver: str = "auto:residual-skill",
) -> Path:
    """Write receipt proving an auto-approval (low-risk residual drain) at `stage`.

    Writes ``receipts/auto-approval-{stage}.json``. Identical validation
    surface to ``receipt_human_approval`` so auto-approval receipts go
    through the same hash-mismatch refusal at transition time (preserving
    the 2026-04-30 audit-chain forgery hardening).

    The ``kind="auto"`` payload field distinguishes auto-approval receipts
    from human-approval receipts in greppable form for retrospectives and
    audit trails.
    """
    require_nonblank_str(stage, field_name="stage")
    if "/" in stage or "\\" in stage or stage.startswith("."):
        raise ValueError(f"stage must not contain path separators: {stage!r}")
    require_nonblank_str(artifact_sha256, field_name="artifact_sha256")
    try:
        require_nonblank(approver, field_name="approver")
    except (TypeError, ValueError):
        raise ValueError(
            "approver must be a non-empty string "
            "(whitespace-only values are rejected — whitespace carries no "
            "approver identity)"
        )

    return write_receipt(
        task_dir,
        f"auto-approval-{stage}",
        stage=stage,
        artifact_sha256=artifact_sha256,
        approver=approver,
        kind="auto",
    )


def receipt_postmortem_generated(
    task_dir: Path,
    postmortem_json_path: Path | str,
    **_legacy: Any,
) -> Path:
    """Write receipt proving a postmortem (json+md pair) was generated.

    v4 self-compute contract: counts and hashes are derived from the
    on-disk postmortem JSON. Callers pass the path to the postmortem
    JSON; the writer:
      * reads it to count ``anomalies`` and ``recurring_patterns``
      * hashes the JSON via ``hash_file``
      * locates the sibling ``<stem>.md`` and hashes it when present
        (writes the literal string ``"none"`` when absent)
    Legacy kwargs (``json_sha256``, ``md_sha256``, ``anomaly_count``,
    ``pattern_count``) raise ``TypeError`` so a stale integration cannot
    silently ship counts that disagree with the file on disk.
    """
    if _legacy:
        raise TypeError(
            "receipt_postmortem_generated no longer accepts caller-supplied "
            f"{sorted(_legacy)} — counts and hashes are self-computed from "
            "postmortem_json_path"
        )
    json_path = Path(postmortem_json_path)
    # SEC-001 (task-20260506-003): bounds-check the postmortem JSON path.
    # The postmortem JSON canonically lives at _persistent_project_dir(root)
    # / "postmortems" / <task-id>.json (OUTSIDE task_dir). A compromised
    # caller could otherwise point postmortem_json_path at arbitrary files.
    # Mirrors the SEC-004 defense in stage.py::_build_audit_receipt_payload.
    safe_postmortems_dir = _persistent_project_dir(task_dir.parent.parent) / "postmortems"
    try:
        resolved_json = Path(json_path).resolve()
        resolved_safe = safe_postmortems_dir.resolve()
        resolved_json.relative_to(resolved_safe)
    except ValueError as exc:
        raise ValueError(
            f"receipt_postmortem_generated: postmortem_json_path must be "
            f"inside {resolved_safe}; got {resolved_json}"
        ) from exc
    if not json_path.exists():
        raise ValueError(
            f"postmortem JSON missing at {json_path}"
        )
    try:
        with json_path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"receipt_postmortem_generated: cannot parse postmortem JSON at "
            f"{json_path}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError(
            f"receipt_postmortem_generated: postmortem JSON at {json_path} "
            f"must be an object (got {type(payload).__name__})"
        )
    anomalies = payload.get("anomalies", [])
    patterns = payload.get("recurring_patterns", [])
    anomaly_count = len(anomalies) if isinstance(anomalies, list) else 0
    pattern_count = len(patterns) if isinstance(patterns, list) else 0

    json_sha256 = hash_file(json_path)
    md_path = json_path.with_suffix(".md")
    md_sha256 = hash_file(md_path) if md_path.exists() else "none"

    return write_receipt(
        task_dir,
        "postmortem-generated",
        json_sha256=json_sha256,
        md_sha256=md_sha256,
        anomaly_count=anomaly_count,
        pattern_count=pattern_count,
    )


def receipt_postmortem_analysis(
    task_dir: Path,
    *,
    analysis_path: Path,
    rules_path: Path | None = None,
    rules_added: int,
) -> Path:
    """Write receipt proving postmortem analysis ran and rules updated.

    Hashes are derived from on-disk files — no caller-supplied hashes accepted.
    Raises ValueError when analysis_path does not exist or rules_added is invalid.
    rules_sha256_after is computed from rules_path when it exists, else "0"*64.

    On-disk payload key set: analysis_sha256, rules_added, rules_sha256_after,
    contract_version.
    """
    if not analysis_path.exists():
        raise ValueError(f"analysis_path does not exist: {analysis_path}")
    if not isinstance(rules_added, int) or isinstance(rules_added, bool) or rules_added < 0:
        raise ValueError("rules_added must be a non-negative int")
    analysis_sha256 = hash_file(analysis_path)
    rules_sha256_after = (
        hash_file(rules_path)
        if (rules_path is not None and rules_path.exists())
        else "0" * 64
    )
    return write_receipt(
        task_dir,
        "postmortem-analysis",
        analysis_sha256=analysis_sha256,
        rules_added=rules_added,
        rules_sha256_after=rules_sha256_after,
    )


def receipt_postmortem_skipped(
    task_dir: Path,
    reason: str,
    retrospective_sha256: str,
    subsumed_by: list[str],
) -> Path:
    """Write receipt proving postmortem was deliberately skipped.

    `reason` is enum-validated against {"clean-task", "no-findings"}.
    A prior quality-over-gate skip reason has been removed
    (task-20260419-002 G1) — callers that previously used it must
    either (a) pass `"clean-task"` when the task genuinely had zero
    findings, or (b) stop skipping and let the LLM postmortem run.

    `subsumed_by` is REQUIRED (task-20260419-002 G2): every skip must
    cite specific prior postmortem task_ids whose derived rules cover
    this task's finding categories. Empty list `[]` is valid ONLY when
    `reason` is `"clean-task"` or `"no-findings"`.

    Validation rules, applied IN ORDER; the first failure short-circuits:

      (a) `subsumed_by` must be a list of strings (any non-list shape
          raises `ValueError("subsumed_by must be a list of task_id
          strings")`).
      (b) Each entry MUST match the task_id regex
          `^task-[A-Za-z0-9][A-Za-z0-9_.-]*$`; failures raise
          `ValueError` whose message contains `subsumed_by[<i>]` and
          `must match`.
      (c) If `reason` is NOT in `{"clean-task", "no-findings"}`, the
          list MUST be non-empty; empty raises `ValueError` whose
          message contains `subsumed_by must be non-empty when reason=`.
          After G1 this case is unreachable via the `reason` enum check
          above, but the rule is retained for defensive coverage.
      (d) For each entry in a non-empty list, the expected postmortem
          file at `_persistent_project_dir(root) / "postmortems" /
          f"{entry}.json"` (where `root = task_dir.parent.parent`) MUST
          exist; missing files raise `ValueError` whose message contains
          `missing postmortem file for` and the offending task_id.

    `subsumed_by` is written verbatim into the receipt payload
    alongside `reason` so downstream consumers can audit which prior
    work the skip rests on.
    """
    if reason not in _POSTMORTEM_SKIP_REASONS:
        raise ValueError(
            f"invalid postmortem skip reason: {reason!r} "
            f"(allowed: {', '.join(sorted(_POSTMORTEM_SKIP_REASONS))})"
        )
    require_nonblank_str(retrospective_sha256, field_name="retrospective_sha256")

    # Rule (a): subsumed_by must be a list. `isinstance(..., list)`
    # rejects tuples, sets, dicts, strings, None, ints, etc. — the
    # message pins the expected shape so callers know the contract.
    if not isinstance(subsumed_by, list):
        raise ValueError("subsumed_by must be a list of task_id strings")

    # Rule (b): every entry must match the task_id slug regex. We
    # iterate with index so the failure message can cite the specific
    # offending position. Non-string entries fail the regex match
    # (re.match rejects non-str input via TypeError, which we preempt
    # by coercing to the regex-based check — `re.match` called on
    # non-str raises TypeError; convert that to ValueError with the
    # same bracket+must-match shape so the test pattern still holds).
    for i, entry in enumerate(subsumed_by):
        if not isinstance(entry, str) or not _SUBSUMED_BY_TASK_ID_RE.match(entry):
            raise ValueError(
                f"subsumed_by[{i}] must match "
                f"^task-[A-Za-z0-9][A-Za-z0-9_.-]*$ (got {entry!r})"
            )

    # Rule (c): non-empty required when reason is not one of the
    # "nothing to cite" reasons. After G1 this branch is effectively
    # unreachable because the reason enum above rejects any other
    # value, but the defensive rule stays — if the enum grows back we
    # want subsumed_by enforcement to follow immediately.
    if reason not in {"clean-task", "no-findings"} and not subsumed_by:
        raise ValueError(
            f"subsumed_by must be non-empty when reason={reason!r}"
        )

    # Rule (d): every cited task_id must have a corresponding
    # postmortem file on disk under the project-persistent postmortems
    # directory. This is the "cite something real" rule — callers
    # cannot hand-wave arbitrary task_ids. The path resolution mirrors
    # `receipt_postmortem_generated`'s write path (see lib_core.py).
    if subsumed_by:
        root = task_dir.parent.parent
        postmortems_dir = _persistent_project_dir(root) / "postmortems"
        for entry in subsumed_by:
            pm_path = postmortems_dir / f"{entry}.json"
            if not pm_path.exists():
                raise ValueError(
                    f"missing postmortem file for {entry!r} at {pm_path}"
                )

    return write_receipt(
        task_dir,
        "postmortem-skipped",
        reason=reason,
        retrospective_sha256=retrospective_sha256,
        subsumed_by=list(subsumed_by),
    )


# Allowed reasons for receipt_calibration_noop. Enum-validated at write
# time so callers cannot silently drift the no-op taxonomy.
_CALIBRATION_NOOP_REASONS = frozenset({
    "no-retros",
    "all-handlers-zero-work",
})


def receipt_calibration_applied(
    task_dir: Path,
    retros_consumed: int,
    scores_updated: int,
    policy_sha256_before: str,
    policy_sha256_after: str,
) -> Path:
    """Write receipt proving calibration policy update was applied.

    Calibration is deterministic — this writer does NOT call
    ``_record_tokens``; no model invocation is involved.

    AC 4 refusal: when ``retros_consumed > 0`` AND
    ``policy_sha256_before == policy_sha256_after`` the writer refuses —
    retros were consumed yet the policy did not move, which means the
    calibration cycle was actually a no-op and the caller must use
    ``receipt_calibration_noop`` instead. The refusal message names the
    alternative writer so the diagnostic is self-evident.
    """
    if not isinstance(retros_consumed, int) or retros_consumed < 0:
        raise ValueError("retros_consumed must be a non-negative int")
    if not isinstance(scores_updated, int) or scores_updated < 0:
        raise ValueError("scores_updated must be a non-negative int")
    require_nonblank_str(policy_sha256_before, field_name="policy_sha256_before")
    require_nonblank_str(policy_sha256_after, field_name="policy_sha256_after")
    if retros_consumed > 0 and policy_sha256_before == policy_sha256_after:
        raise ValueError(
            f"receipt_calibration_applied REFUSES to write: retros_consumed="
            f"{retros_consumed} but policy_sha256_before == policy_sha256_after "
            f"({policy_sha256_before!r}). This is a no-op calibration — use "
            f"calibration-noop (receipt_calibration_noop) instead."
        )
    return write_receipt(
        task_dir,
        "calibration-applied",
        retros_consumed=retros_consumed,
        scores_updated=scores_updated,
        policy_sha256_before=policy_sha256_before,
        policy_sha256_after=policy_sha256_after,
    )


def receipt_calibration_noop(
    task_dir: Path,
    reason: str,
    policy_sha256: str,
) -> Path:
    """Write receipt proving calibration ran but was a deliberate no-op.

    ``reason`` is enum-validated against
    {"no-retros", "all-handlers-zero-work"} — any other value raises
    ValueError. ``policy_sha256`` is the policy hash at the time of the
    no-op (same before/after by construction).

    Step name: ``"calibration-noop"`` — the DONE->CALIBRATED gate
    accepts this OR ``"calibration-applied"`` as satisfying the
    calibration requirement (see ``validate_chain``).
    """
    if reason not in _CALIBRATION_NOOP_REASONS:
        raise ValueError(
            f"invalid calibration-noop reason: {reason!r} "
            f"(allowed: {sorted(_CALIBRATION_NOOP_REASONS)})"
        )
    require_nonblank_str(policy_sha256, field_name="policy_sha256")
    return write_receipt(
        task_dir,
        "calibration-noop",
        reason=reason,
        policy_sha256=policy_sha256,
    )


def receipt_rules_check_passed(task_dir: Path, mode: str) -> Path:
    """Write receipt proving a rules-check pass (no error-severity violations).

    Signature (AC 1): takes only ``(task_dir, mode)``. All counts and
    hashes are computed internally from a fresh
    ``rules_engine.run_checks_with_stats`` call — callers no longer
    supply (and therefore cannot falsify) the violation totals,
    rules_loaded, or rules_skipped.

    Refuses with ValueError if the rules engine reports any
    error-severity violation. Rules-check pipeline must branch to the
    failure path in that case; this writer proves the clean outcome only.

    Payload shape (every value computed here):
      - rules_evaluated:    rules_loaded + rules_skipped (total raw entries)
      - rules_loaded:       Rule objects successfully constructed (AC 11)
      - rules_skipped:      raw entries for which _rule_from_dict returned
                            None (AC 11)
      - violations_count:   len(violations) returned by run_checks_with_stats
      - error_violations:   count where Violation.severity == "error"
      - advisory_violations: count where Violation.severity == "warn"
      - engine_version:     "1" (bootstrap-safe hardcode)
      - rules_file_sha256:  hash_file of the prevention-rules.json path
                            (or "none" if the file does not exist)
      - checked_at:         now_iso()
      - mode:               pass-through
    """
    if mode not in ("staged", "all"):
        raise ValueError(
            f"mode must be 'staged' or 'all' (got mode={mode!r})"
        )

    # Deferred import: rules_engine imports lib_core, and lib_core is
    # imported at module load of this file. Importing rules_engine here
    # (at call time) keeps the import graph acyclic.
    import rules_engine as _rules_engine  # noqa: PLC0415
    from lib_core import _persistent_project_dir  # noqa: PLC0415

    root = task_dir.parent.parent

    # Run the engine via run_checks_with_stats — single authoritative call
    # that returns violations plus loaded/skipped rule counts.
    # Callers cannot supply (and therefore cannot falsify) any of these
    # values. Using run_checks_with_stats avoids a second file read in
    # this function for the loaded/skipped counts (TOCTOU-safe at the
    # caller boundary — AC 12).
    violations, rules_loaded, rules_skipped = _rules_engine.run_checks_with_stats(root, mode)
    violations_count = len(violations)
    error_violations = sum(1 for v in violations if getattr(v, "severity", None) == "error")
    advisory_violations = sum(1 for v in violations if getattr(v, "severity", None) == "warn")

    # Refuse-by-construction: this writer proves the clean outcome only.
    if error_violations > 0:
        raise ValueError(
            f"receipt_rules_check_passed REFUSES to write: error_violations="
            f"{error_violations} (must be 0 — this receipt is a passed-receipt "
            f"by construction; use a failure-path receipt when errors exist). "
            f"violations_count={violations_count}, mode={mode!r}"
        )

    # Hash the rules file for the receipt payload. If the file is missing,
    # the hash is the literal string "none" (matches legacy schema).
    # rules_evaluated is the total raw entry count (loaded + skipped) —
    # AC 13 invariant: rules_evaluated == rules_loaded + rules_skipped.
    rules_file = _persistent_project_dir(root) / "prevention-rules.json"
    rules_evaluated = rules_loaded + rules_skipped
    rules_file_sha256: str = "none"
    if rules_file.exists():
        try:
            rules_file_sha256 = hash_file(rules_file)
        except (FileNotFoundError, OSError) as exc:
            raise ValueError(
                f"receipt_rules_check_passed: cannot hash prevention-rules "
                f"file at {rules_file}: {exc}"
            ) from exc

    return write_receipt(
        task_dir,
        "rules-check-passed",
        rules_evaluated=rules_evaluated,
        rules_loaded=rules_loaded,
        rules_skipped=rules_skipped,
        violations_count=violations_count,
        error_violations=error_violations,
        advisory_violations=advisory_violations,
        engine_version="1",
        rules_file_sha256=rules_file_sha256,
        checked_at=now_iso(),
        mode=mode,
    )


def receipt_force_override(
    task_dir: Path,
    from_stage: str,
    to_stage: str,
    bypassed_gates: list[str],
    *,
    reason: str,
    approver: str,
) -> Path:
    """Write receipt proving a forced stage transition occurred.

    Emitted by ``transition_task`` when invoked with ``force=True``. The
    payload enumerates the gate errors that would have been raised if
    ``force`` were ``False`` (``bypassed_gates``) so the audit chain
    records not just that force was used but which guardrails it
    bypassed.

    Writes ``receipts/force-override-{from_stage}-{to_stage}.json``. One
    force per edge — subsequent forced transitions over the same edge
    overwrite via the atomic write path.

    Validation:
      - ``from_stage`` and ``to_stage`` MUST be non-empty strings; empty
        or non-string values raise ``ValueError`` naming the arg.
      - ``bypassed_gates`` MUST be a list (possibly empty). Every entry
        MUST be a string. Any other container type or non-string entry
        raises ``ValueError``.
      - ``reason`` and ``approver`` are keyword-only, required, and MUST
        be non-empty ``str``. Empty / non-string values raise
        ``ValueError`` naming the offending arg. Mirrors the validation
        pattern from ``receipt_human_approval``. These are the only
        caller-supplied payload fields on this writer because they
        encode human intent (break-glass rationale + operator identity)
        that is not derivable from on-disk state.
    """
    require_nonblank_str(from_stage, field_name="from_stage")
    require_nonblank_str(to_stage, field_name="to_stage")
    # SEC-002 hardening: stage names MUST be strict uppercase identifier
    # slugs. Prevents path traversal via crafted manifest["stage"] values
    # like "../../etc/x" reaching the receipt filename.
    import re as _re_stage
    _STAGE_RE = r"^[A-Z][A-Z0-9_]*$"
    if not _re_stage.match(_STAGE_RE, from_stage):
        raise ValueError(
            f"from_stage must match {_STAGE_RE} (got {from_stage!r})"
        )
    if not _re_stage.match(_STAGE_RE, to_stage):
        raise ValueError(
            f"to_stage must match {_STAGE_RE} (got {to_stage!r})"
        )
    if not isinstance(bypassed_gates, list):
        raise ValueError("bypassed_gates must be a list of strings")
    for idx, entry in enumerate(bypassed_gates):
        if not isinstance(entry, str):
            raise ValueError(
                f"bypassed_gates[{idx}] must be a string (got {type(entry).__name__})"
            )
    # F1 (v4 -> v5): reason + approver required. Validation mirrors
    # receipt_human_approval — non-empty strings only. Whitespace-only
    # values are rejected (they carry no human-readable justification and
    # defeat the break-glass audit purpose).
    try:
        require_nonblank(reason, field_name="reason")
    except (TypeError, ValueError):
        raise ValueError(
            "reason must be a non-empty string "
            "(whitespace-only values are rejected)"
        )
    try:
        require_nonblank(approver, field_name="approver")
    except (TypeError, ValueError):
        raise ValueError(
            "approver must be a non-empty string "
            "(whitespace-only values are rejected)"
        )

    step_name = f"force-override-{from_stage}-{to_stage}"
    return write_receipt(
        task_dir,
        step_name,
        from_stage=from_stage,
        to_stage=to_stage,
        bypassed_gates=list(bypassed_gates),
        bypassed_count=len(bypassed_gates),
        reason=reason,
        approver=approver,
        forced_at=now_iso(),
    )


def receipt_scheduler_refused(
    task_dir: Path,
    current_stage: str,
    proposed_stage: str,
    missing_proofs: list[str],
) -> Path:
    """Write receipt proving the scheduler refused to transition.

    Emitted by ``scheduler.handle_receipt_written`` when
    ``compute_next_stage(task_dir)`` returns a non-None ``next_stage``
    with a non-empty ``missing_proofs`` list. The receipt is purely
    observational — no gate reads it; it parallels the
    ``scheduler_transition_refused`` event and lets the audit trail
    record refusal reasons on disk rather than only in events.jsonl.

    Writes ``receipts/scheduler-refused.json``. Subsequent refusals
    overwrite via the atomic write path — the most recent refusal wins.

    Validation:
      - ``current_stage`` and ``proposed_stage`` MUST be non-empty
        strings matching ``^[A-Z][A-Z0-9_]*$``. The regex shape mirrors
        ``receipt_force_override``'s ``_STAGE_RE`` hardening
        (SEC-002): stage names become part of event payloads and log
        lines, so crafted manifest values like ``"../../etc/x"`` must
        be rejected at the writer boundary. Empty or non-matching
        values raise ``ValueError`` naming the arg.
      - ``missing_proofs`` MUST be a list (possibly empty). Every
        entry MUST be a string. Any other container type or non-string
        entry raises ``ValueError``.
    """
    require_nonblank_str(current_stage, field_name="current_stage")
    require_nonblank_str(proposed_stage, field_name="proposed_stage")
    # SEC-002 hardening: stage names MUST be strict uppercase identifier
    # slugs. Prevents path traversal / event-payload injection via crafted
    # manifest stage values reaching the receipt/event surface.
    import re as _re_stage
    _STAGE_RE = r"^[A-Z][A-Z0-9_]*$"
    if not _re_stage.match(_STAGE_RE, current_stage):
        raise ValueError(
            f"current_stage must match {_STAGE_RE} (got {current_stage!r})"
        )
    if not _re_stage.match(_STAGE_RE, proposed_stage):
        raise ValueError(
            f"proposed_stage must match {_STAGE_RE} (got {proposed_stage!r})"
        )
    if not isinstance(missing_proofs, list):
        raise ValueError("missing_proofs must be a list of strings")
    for idx, entry in enumerate(missing_proofs):
        if not isinstance(entry, str):
            raise ValueError(
                f"missing_proofs[{idx}] must be a string "
                f"(got {type(entry).__name__})"
            )

    return write_receipt(
        task_dir,
        "scheduler-refused",
        current_stage=current_stage,
        proposed_stage=proposed_stage,
        missing_proofs=list(missing_proofs),
        refused_at=now_iso(),
    )
