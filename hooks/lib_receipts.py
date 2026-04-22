"""Receipt-based contract validation chain for dynos-work.

Every pipeline step writes a structured JSON receipt to
.dynos/task-{id}/receipts/{step-name}.json proving what it did.
The next step refuses to proceed unless the prior receipt exists.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from lib_core import now_iso, append_execution_log, _persistent_project_dir
from lib_log import log_event
from write_policy import WriteAttempt, require_write_allowed


# Receipt contract version. Receipts written by this module embed
# `contract_version: 5`. Readers MUST treat receipts without this field
# as v1 and accept them when `valid=true`.
#
# Bump rationale (v4 -> v5): F1 — force-override receipts now require
# human-intent justification. ``receipt_force_override`` accepts
# ``reason`` and ``approver`` as keyword-only non-empty strings and
# writes them as top-level payload fields. These values are the one
# intended caller-supplied exception to the v4 self-compute rule because
# they encode human intent (break-glass rationale + operator identity)
# that is not derivable from on-disk state. Pre-v5 force-override
# receipts are NOT retroactively invalidated — the floor in
# MIN_VERSION_PER_STEP applies to v5+ writes only and existing receipts
# remain readable. F2-F6 findings close alongside F1 as additive
# observability/attribution changes that ride the same v5 bump without
# introducing new caller-supplied fields: F2/F3 are skill-prose and
# template-scope hardening (no receipt-shape impact); F4 adds a
# self-computing ``self_verify`` enum on receipt_post_completion; F5
# tightens receipt-side event-attribution filtering paired with
# eventbus source-side ``task=task_id`` emission; F6 tags unverified
# persistent retrospectives with ``_source: "persistent-unverified"``
# and emits ``retrospective_trusted_without_flush_event``.
#
# Bump rationale (v3 -> v4, prior): caller-falsification hardening. A
# family of receipt writers now self-compute their payload fields from
# on-disk artifacts instead of accepting caller-supplied counts/hashes.
# Callers that still pass the legacy kwargs raise TypeError so an
# out-of-date integration cannot silently ship a stale receipt. Affected
# writers:
#   * receipt_postmortem_generated (reads postmortem JSON for counts +
#     hashes sibling md via hash_file)
#   * receipt_retrospective (invokes lib_validate.compute_reward)
#   * receipt_spec_validated (parses spec.md, hashes it)
#   * receipt_plan_validated (invokes validate_task_artifacts, reads
#     execution-graph.json for segment_count / criteria_coverage)
#   * receipt_plan_audit (finding_count removed from the payload)
#   * receipt_post_completion (cross-checks each handler name against
#     eventbus_handler events in events.jsonl)
# receipt_audit_done additionally requires a real report_path when the
# auditor ran in learned/ensemble mode (see ensemble_context kwarg).
# Older v1/v2/v3 receipts remain readable; the bump signals that new
# semantics are available and MIN_VERSION_PER_STEP floors gate receipts
# whose consumers depend on the hardened fields.
#
# Bump rationale (v2 -> v3, prior): new receipt semantics introduced —
#   * receipt_calibration_noop writer added (no-op calibration path)
#   * receipt_audit_done self-verifies blocking_count vs report.json
#   * receipt_rules_check_passed derives counts internally from rules_engine
# The bump signals "new semantics available"; it does NOT retroactively
# invalidate v2 receipts (see MIN_VERSION_PER_STEP floors below).
RECEIPT_CONTRACT_VERSION = 5


# Files that compose the calibration policy snapshot used by the
# retrospective/calibration pipeline. Receipt writers and downstream
# consumers may use this list when computing aggregate policy hashes.
CALIBRATION_POLICY_FILES = [
    "effectiveness-scores.json",
    "model-policy.json",
    "skip-policy.json",
    "route-policy.json",
    "prevention-rules.json",
    "project_rules.md",
    "policy.json",
    "trajectories.json",
    "learned-agents/registry.json",
    "benchmarks/history.json",
]


# Minimum contract_version floor per receipt step. Readers that call
# ``read_receipt(..., min_version=None)`` trigger an auto-lookup against
# this map — if a receipt's contract_version is below the matched floor,
# ``read_receipt`` returns None (the receipt is treated as missing).
#
# Keys may be exact step names (e.g. ``"rules-check-passed"``) or
# wildcards terminated by ``"*"`` (e.g. ``"executor-*"``). Match
# precedence: exact > longest-prefix wildcard > default floor of 1.
#
# Floors sit at 2 (not 3) because the v2->v3 bump adds *new* semantics
# (see RECEIPT_CONTRACT_VERSION docstring) without invalidating existing
# v2 receipts. Bump individual floors only when the consuming pipeline
# actually depends on v3-only fields.
MIN_VERSION_PER_STEP: dict[str, int] = {
    "executor-*": 2,
    "audit-*": 2,
    "plan-validated": 2,
    "rules-check-passed": 2,
    "calibration-applied": 2,
    "calibration-noop": 2,
    "human-approval-*": 2,
    # v4 -> v5 bump: force-override receipts now carry required
    # ``reason`` / ``approver`` fields. The floor applies only to v5+
    # writes; existing pre-v5 force-override receipts remain readable
    # and are NOT retroactively invalidated.
    "force-override-*": 5,
}


def _resolve_min_version(step_name: str) -> int:
    """Return the minimum contract_version required for a given step name.

    Lookup rules:
      1. Exact key match in MIN_VERSION_PER_STEP wins outright.
      2. Among wildcard keys (ending in ``*``), the longest prefix match wins.
      3. Unknown step names default to floor 1 (accept v1+ receipts).
    """
    if step_name in MIN_VERSION_PER_STEP:
        return MIN_VERSION_PER_STEP[step_name]
    best_prefix_len = -1
    best_floor = 1
    for key, floor in MIN_VERSION_PER_STEP.items():
        if not key.endswith("*"):
            continue
        prefix = key[:-1]
        if step_name.startswith(prefix) and len(prefix) > best_prefix_len:
            best_prefix_len = len(prefix)
            best_floor = floor
    return best_floor


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


# Sidecar directory names. These names are the filename schema for the
# per-spawn injected-prompt sidecars and MUST be used by both the writer
# (router.py CLI subcommands) and the reader/asserter (receipt_* functions
# in this module). Defining them here makes the contract unforgeable from
# a single side — renaming requires updating this constant and every
# importer.
INJECTED_PROMPTS_DIR = "_injected-prompts"
INJECTED_AUDITOR_PROMPTS_DIR = "_injected-auditor-prompts"
INJECTED_PLANNER_PROMPTS_DIR = "_injected-planner-prompts"


# PERF-001: per-process memo for hash_file keyed by (abs_path, mtime_ns,
# size). Repeated hashes of the same unchanged file in one Python process
# (e.g. receipt_plan_audit write + plan_audit_matches gate check) now
# share one disk read. Invalidated automatically when mtime or size
# change, so writes between hashes are picked up. Bounded to 128 entries
# to keep the cache small.
_HASH_CACHE: dict[tuple[str, int, int], str] = {}
_HASH_CACHE_MAX = 128


def hash_file(path: Path) -> str:
    """Return sha256 hex digest of a file's contents.

    Raises FileNotFoundError if path does not exist.

    Cached per (absolute-path, mtime_ns, size) within the Python process.
    Any mutation changes mtime_ns or size and invalidates the entry on
    the next call — no stale-hash risk.
    """
    try:
        st = path.stat()
    except OSError:
        # File missing → let open() raise the canonical error below.
        st = None
    if st is not None:
        key = (str(path.resolve()), st.st_mtime_ns, st.st_size)
        cached = _HASH_CACHE.get(key)
        if cached is not None:
            return cached

    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    digest = h.hexdigest()

    if st is not None:
        # Evict oldest if over cap (FIFO is fine; hot keys churn little).
        if len(_HASH_CACHE) >= _HASH_CACHE_MAX:
            try:
                _HASH_CACHE.pop(next(iter(_HASH_CACHE)))
            except StopIteration:
                pass
        _HASH_CACHE[(str(path.resolve()), st.st_mtime_ns, st.st_size)] = digest
    return digest


__all__ = [
    "write_receipt",
    "read_receipt",
    "require_receipt",
    "validate_chain",
    "hash_file",
    "plan_validated_receipt_matches",
    "plan_audit_matches",
    "receipt_spec_validated",
    "receipt_plan_validated",
    "receipt_executor_routing",
    "receipt_executor_done",
    "receipt_audit_routing",
    "receipt_audit_done",
    "receipt_retrospective",
    "receipt_post_completion",
    "receipt_planner_spawn",
    "receipt_plan_audit",
    "receipt_tdd_tests",
    "receipt_human_approval",
    "receipt_postmortem_generated",
    "receipt_postmortem_analysis",
    "receipt_postmortem_skipped",
    "receipt_calibration_applied",
    "receipt_calibration_noop",
    "receipt_rules_check_passed",
    "receipt_force_override",
    "RECEIPT_CONTRACT_VERSION",
    "MIN_VERSION_PER_STEP",
    "CALIBRATION_POLICY_FILES",
    "INJECTED_PROMPTS_DIR",
    "INJECTED_AUDITOR_PROMPTS_DIR",
    "INJECTED_PLANNER_PROMPTS_DIR",
]

# Map receipt steps to human-readable execution-log entries
_LOG_MESSAGES: dict[str, str] = {
    "spec-validated": "[DONE] spec validated — {criteria_count} acceptance criteria",
    "plan-validated": "[DONE] plan validated — {segment_count} segments, criteria {criteria_coverage}",
    "executor-routing": "[ROUTE] executor plan — {n_segments} segments routed",
    "audit-routing": "[ROUTE] audit plan — {n_auditors} auditors routed",
    "retrospective": "[DONE] retrospective — quality={quality_score} cost={cost_score} efficiency={efficiency_score}",
    "post-completion": "[DONE] post-completion — {n_handlers} handlers",
    "planner-discovery": "[DONE] planner discovery — tokens={tokens_used}",
    "planner-spec": "[DONE] planner spec — tokens={tokens_used}",
    "planner-plan": "[DONE] planner plan — tokens={tokens_used}",
    "plan-audit-check": "[DONE] plan audit — tokens={tokens_used}",
    "tdd-tests": "[DONE] TDD tests — tokens={tokens_used}",
    "human-approval-SPEC_REVIEW": "[DONE] human-approval SPEC_REVIEW — approver={approver}",
    "human-approval-PLAN_REVIEW": "[DONE] human-approval PLAN_REVIEW — approver={approver}",
    "human-approval-TDD_REVIEW": "[DONE] human-approval TDD_REVIEW — approver={approver}",
    "postmortem-generated": "[DONE] postmortem generated — anomalies={anomaly_count} patterns={pattern_count}",
    "postmortem-analysis": "[DONE] postmortem analysis — rules_added={rules_added}",
    "postmortem-skipped": "[DONE] postmortem skipped — reason={reason}",
    "calibration-applied": "[DONE] calibration applied — retros={retros_consumed} scores={scores_updated}",
    "calibration-noop": "[DONE] calibration noop — reason={reason}",
    "rules-check-passed": "[DONE] rules check — {rules_evaluated} rules evaluated, {violations_count} violations",
    # Prefix pattern: force-override-{from_stage}-{to_stage} — handled in
    # write_receipt() via the prefix branch below. The entry here documents
    # the format string for reviewers; the {N} placeholder is filled in by
    # the writer using len(bypassed_gates) at write time.
    "force-override-*": "[FORCE] {from_stage} → {to_stage} — bypassed {N} gate(s)",
}


def _record_tokens(task_dir: Path, agent: str, model: str, tokens: int) -> None:
    """Record token usage to token-usage.json. Called by receipt writers."""
    try:
        from lib_tokens import record_tokens
        record_tokens(
            task_dir=task_dir,
            agent=agent,
            model=model,
            input_tokens=tokens // 2,  # rough split since we only have total
            output_tokens=tokens - tokens // 2,
            phase="receipt",
            stage="receipt",
            event_type="receipt-record",
            detail=f"auto-recorded via receipt for {agent}",
        )
    except Exception:
        # Fallback: write directly if lib_tokens isn't available
        try:
            token_path = task_dir / "token-usage.json"
            require_write_allowed(
                WriteAttempt(
                    role="receipt-writer",
                    task_dir=task_dir,
                    path=token_path,
                    operation="modify" if token_path.exists() else "create",
                    source="receipt-writer",
                )
            )
            data = json.loads(token_path.read_text()) if token_path.exists() else {"agents": {}, "total": 0}
            data["agents"][agent] = data.get("agents", {}).get(agent, 0) + tokens
            data["total"] = sum(v for v in data["agents"].values() if isinstance(v, (int, float)))
            token_path.write_text(json.dumps(data, indent=2))
        except Exception:
            pass


def _receipts_dir(task_dir: Path) -> Path:
    return task_dir / "receipts"


def _atomic_write_text(path: Path, content: str) -> None:
    """Atomically write text to `path` via tempfile + os.replace.

    Avoids partial-write torn-state if the process is killed mid-write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def write_receipt(task_dir: Path, step_name: str, **payload: Any) -> Path:
    """Write a receipt proving a pipeline step completed.

    Returns the path to the written receipt file.

    Every receipt embeds `contract_version: RECEIPT_CONTRACT_VERSION`.
    Readers MUST tolerate older receipts without this field.
    """
    receipts = _receipts_dir(task_dir)
    receipts.mkdir(parents=True, exist_ok=True)

    receipt = {
        "step": step_name,
        "ts": now_iso(),
        "valid": True,
        "contract_version": RECEIPT_CONTRACT_VERSION,
        **payload,
    }

    receipt_path = receipts / f"{step_name}.json"
    require_write_allowed(
        WriteAttempt(
            role="receipt-writer",
            task_dir=task_dir,
            path=receipt_path,
            operation="modify" if receipt_path.exists() else "create",
            source="receipt-writer",
        )
    )
    _atomic_write_text(receipt_path, json.dumps(receipt, indent=2, default=str))

    # Log the receipt write to events.jsonl
    root = task_dir.parent.parent
    task_id = task_dir.name
    log_event(root, "receipt_written", task=task_id, step=step_name)

    # Auto-append to execution-log.md
    template = _LOG_MESSAGES.get(step_name)
    if template:
        try:
            fmt_data = {**payload}
            # Add computed fields for formatting
            if "segments" in payload:
                fmt_data["n_segments"] = len(payload["segments"])
            if "auditors" in payload:
                fmt_data["n_auditors"] = len(payload["auditors"])
            if "handlers_run" in payload:
                fmt_data["n_handlers"] = len(payload["handlers_run"])
            msg = template.format(**{k: v for k, v in fmt_data.items() if isinstance(v, (str, int, float, bool, list))})
            append_execution_log(task_dir, msg)
        except (KeyError, TypeError):
            append_execution_log(task_dir, f"[RECEIPT] {step_name}")
    elif step_name.startswith("executor-seg"):
        agent = payload.get("agent_name", "")
        # Distinguish learned vs generic by agent_name presence (post-v2 contract).
        injected = bool(agent)
        append_execution_log(task_dir, f"[DONE] {step_name} — executor={payload.get('executor_type','')} agent={'learned:' + agent if injected else 'generic'} tokens={payload.get('tokens_used','?')}")
    elif step_name.startswith("audit-") and step_name != "audit-routing":
        append_execution_log(task_dir, f"[DONE] {step_name} — findings={payload.get('finding_count',0)} blocking={payload.get('blocking_count',0)}")
    elif step_name.startswith("force-override-"):
        # Dedicated log line for forced transitions. Format pinned by
        # _LOG_MESSAGES["force-override-*"].
        bypassed = payload.get("bypassed_gates", [])
        n_bypassed = len(bypassed) if isinstance(bypassed, list) else 0
        from_stage = payload.get("from_stage", "?")
        to_stage = payload.get("to_stage", "?")
        append_execution_log(
            task_dir,
            f"[FORCE] {from_stage} → {to_stage} — bypassed {n_bypassed} gate(s)",
        )

    # Synchronous in-process scheduler dispatch. Fires AFTER the atomic
    # receipt write (durable on disk) AND AFTER the log_event observability
    # record (captured in events.jsonl). Ordering is load-bearing: moving
    # this call before _atomic_write_text would expose a window where the
    # scheduler sees a non-existent receipt; moving it before log_event
    # would let a scheduler-dispatch side effect (e.g. a refusal-receipt
    # write) race the observability record.
    #
    # The dispatch is wrapped in a try/except Exception so a scheduler
    # failure can NEVER roll back the already-committed atomic receipt
    # write. All failures are printed to stderr and swallowed. Catch
    # Exception (NOT BaseException) to let KeyboardInterrupt / SystemExit
    # propagate normally.
    #
    # Lazy import (function-local) avoids the circular-import cycle
    # scheduler → lib_receipts → scheduler that would fire at module load
    # if the import sat at the top of this file.
    try:
        from scheduler import handle_receipt_written
        receipt_sha256 = hash_file(receipt_path)
        handle_receipt_written(task_dir, step_name, receipt_sha256)
    except Exception as exc:
        import sys as _sys
        print(
            f"[lib_receipts] scheduler dispatch failed: {exc}",
            file=_sys.stderr,
        )

    return receipt_path


def read_receipt(
    task_dir: Path,
    step_name: str,
    *,
    min_version: int | None = None,
) -> dict | None:
    """Read a receipt. Returns None if missing or invalid.

    Backwards-compat: receipts without `contract_version` are treated as v1
    (contract_version=1) and accepted as long as `valid=true`.

    ``min_version`` enforces a minimum ``contract_version`` floor. When
    ``None`` (the default), the floor is auto-resolved via
    ``_resolve_min_version(step_name)`` against ``MIN_VERSION_PER_STEP``.
    When the receipt's ``contract_version`` is below the floor, the
    receipt is treated as missing and ``None`` is returned — this is how
    callers opt into "refuse to trust stale schemas". Pass
    ``min_version=1`` (or lower) to explicitly disable the check.
    """
    receipt_path = _receipts_dir(task_dir) / f"{step_name}.json"
    if not receipt_path.exists():
        return None
    try:
        data = json.loads(receipt_path.read_text())
        if not isinstance(data, dict) or not data.get("valid"):
            return None
    except (json.JSONDecodeError, OSError):
        return None

    floor = _resolve_min_version(step_name) if min_version is None else min_version
    # Legacy receipts without contract_version are treated as v1.
    actual = data.get("contract_version", 1)
    try:
        actual_int = int(actual)
    except (TypeError, ValueError):
        # Malformed contract_version → treat as missing.
        return None
    if actual_int < floor:
        return None
    return data


def require_receipt(task_dir: Path, step_name: str) -> dict:
    """Read a receipt, raising ValueError if missing or invalid.

    Use this as a gate before proceeding to the next step.
    """
    receipt = read_receipt(task_dir, step_name)
    if receipt is None:
        root = task_dir.parent.parent
        task_id = task_dir.name
        log_event(root, "receipt_missing", task=task_id, step=step_name)
        raise ValueError(
            f"Required receipt missing: {step_name}\n"
            f"  Expected at: {_receipts_dir(task_dir) / f'{step_name}.json'}\n"
            f"  This means the prior pipeline step did not complete correctly."
        )
    return receipt



def validate_chain(task_dir: Path) -> list[str]:
    """Validate the entire receipt chain for a task. Returns list of gaps.

    This is a diagnostic tool — it checks all possible receipts and reports
    which ones are missing, without raising exceptions.

    Backwards-compat: receipts without `contract_version` are treated as v1
    and remain valid (no field is required for legacy reads).
    """
    # Define the expected receipt chain based on task stage
    manifest_path = task_dir / "manifest.json"
    if not manifest_path.exists():
        return ["manifest.json missing"]

    manifest = json.loads(manifest_path.read_text())
    stage = manifest.get("stage", "")

    # All possible receipts in order. ``plan-routing`` was pruned fully
    # (v4 AC 1): writer, __all__ export, and _LOG_MESSAGES entry have all
    # been removed. The receipt was never a gating receipt on any stage
    # transition and kept producing spurious DONE-gap reports.
    all_receipts = [
        "spec-validated",
        "plan-validated",
        "executor-routing",
        # executor-{seg-id} receipts are dynamic
        "audit-routing",
        # audit-{auditor} receipts are dynamic
        "retrospective",
        "post-completion",
    ]

    # What receipts should exist based on stage progression.
    # `audit-routing` is required at DONE — without it we cannot enumerate
    # the dynamic audit receipts that should follow it.
    stage_requires: dict[str, list[str]] = {
        # v4 AC 5: PLAN_REVIEW must carry planner-{discovery,spec,plan}.
        # Missing → gap report so retrospectives flag unreceipted planner
        # invocations before they progress further.
        "PLAN_REVIEW": [
            "planner-discovery",
            "planner-spec",
            "planner-plan",
        ],
        "PRE_EXECUTION_SNAPSHOT": ["plan-validated"],
        "EXECUTION": ["plan-validated"],
        "TEST_EXECUTION": ["plan-validated", "executor-routing"],
        "CHECKPOINT_AUDIT": ["plan-validated", "executor-routing"],
        "REPAIR_PLANNING": ["plan-validated", "executor-routing"],
        "REPAIR_EXECUTION": ["plan-validated", "executor-routing"],
        "DONE": [
            "plan-validated",
            "executor-routing",
            "audit-routing",
            "retrospective",
            "post-completion",
        ],
    }

    # AC 8: conditionally add `tdd-tests` to PRE_EXECUTION_SNAPSHOT (and
    # onwards, through EXECUTION et al) when the manifest declares
    # ``classification.tdd_required == true``. The transition gate in
    # lib_core.transition_task enforces the hash-match; validate_chain
    # only reports it as a static-required receipt so stage-based
    # diagnostics are accurate.
    classification = manifest.get("classification") if isinstance(manifest, dict) else None
    tdd_required = bool(
        isinstance(classification, dict) and classification.get("tdd_required") is True
    )
    if tdd_required:
        tdd_stages = {
            "PRE_EXECUTION_SNAPSHOT",
            "EXECUTION",
            "TEST_EXECUTION",
            "CHECKPOINT_AUDIT",
            "REPAIR_PLANNING",
            "REPAIR_EXECUTION",
            "DONE",
        }
        for s in tdd_stages:
            if s in stage_requires and "tdd-tests" not in stage_requires[s]:
                stage_requires[s] = stage_requires[s] + ["tdd-tests"]

    required = stage_requires.get(stage, [])
    gaps = []

    for receipt_name in required:
        if read_receipt(task_dir, receipt_name) is None:
            gaps.append(receipt_name)

    # Check dynamic executor receipts if we're past EXECUTION
    if stage in ("TEST_EXECUTION", "CHECKPOINT_AUDIT", "REPAIR_PLANNING",
                 "REPAIR_EXECUTION", "DONE"):
        exec_routing = read_receipt(task_dir, "executor-routing")
        if exec_routing:
            for seg in exec_routing.get("segments", []):
                seg_id = seg.get("segment_id", "")
                if seg_id and read_receipt(task_dir, f"executor-{seg_id}") is None:
                    gaps.append(f"executor-{seg_id}")

    # Check audit receipts if we're at DONE.
    # audit-routing absence is already reported via stage_requires above;
    # we still enumerate dynamic audit receipts when routing IS present.
    if stage == "DONE":
        audit_routing = read_receipt(task_dir, "audit-routing")
        if audit_routing:
            for auditor in audit_routing.get("auditors", []):
                if auditor.get("action") == "spawn":
                    name = auditor.get("name", "")
                    if name and read_receipt(task_dir, f"audit-{name}") is None:
                        gaps.append(f"audit-{name}")

        # AC 24: calibration requirement at DONE is satisfied by EITHER
        # ``calibration-applied`` OR ``calibration-noop``. If both are
        # present the later ts wins (the DONE->CALIBRATED gate reads the
        # later receipt); here we only report a gap when neither exists.
        calib_applied = read_receipt(task_dir, "calibration-applied")
        calib_noop = read_receipt(task_dir, "calibration-noop")
        if calib_applied is None and calib_noop is None:
            gaps.append("calibration (applied|noop)")

    # v4 AC 8: gate-parity bridge. For pre-DONE / DONE audit stages,
    # surface the same receipts that ``require_receipts_for_done`` would
    # block the DONE transition on. This lets ``validate_chain`` diagnose
    # ensemble / registry / postmortem gaps without duplicating the
    # detection logic. We ADD the bridge gaps to the existing list so the
    # prior behavior is preserved and the gate view is strictly richer.
    if stage in ("CHECKPOINT_AUDIT", "FINAL_AUDIT", "DONE"):
        try:
            from lib_core import require_receipts_for_done  # noqa: PLC0415
            bridge_gaps = require_receipts_for_done(task_dir)
        except Exception as exc:  # pragma: no cover — diagnostic safety
            bridge_gaps = [f"require_receipts_for_done failed: {exc}"]
        if isinstance(bridge_gaps, list):
            for g in bridge_gaps:
                if g not in gaps:
                    gaps.append(g)

    return gaps



# ---------------------------------------------------------------------------
# Convenience receipt writers for common steps
# ---------------------------------------------------------------------------


def _hash_artifact(path: Path) -> str | None:
    """Return sha256 hex of file content, or None if the file is missing."""
    try:
        return hash_file(path)
    except (FileNotFoundError, OSError):
        return None


def receipt_spec_validated(task_dir: Path, **_legacy: Any) -> Path:
    """Write receipt proving spec.md passed validation.

    Self-computes ``criteria_count`` and ``spec_sha256`` from
    ``task_dir/spec.md`` (v4 contract). Callers no longer supply these
    fields — any legacy kwarg (``criteria_count`` or ``spec_sha256``)
    raises ``TypeError`` so a stale integration cannot silently ship a
    receipt whose counts/hash disagree with the on-disk spec.

    Payload includes {criteria_count, spec_sha256, valid: true}.
    """
    if _legacy:
        raise TypeError(
            "receipt_spec_validated no longer accepts caller-supplied "
            f"{sorted(_legacy)} — counts and hash are now self-computed "
            "from task_dir/spec.md"
        )
    spec_path = task_dir / "spec.md"
    if not spec_path.exists():
        raise ValueError(
            f"receipt_spec_validated: spec.md missing at {spec_path}"
        )
    # Deferred import to avoid an lib_validate <-> lib_receipts cycle
    # (lib_validate.compute_reward imports read_receipt from here).
    from lib_validate import parse_acceptance_criteria  # noqa: PLC0415

    try:
        spec_text = spec_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(
            f"receipt_spec_validated: cannot read spec.md at {spec_path}: {exc}"
        ) from exc
    criteria_count = len(parse_acceptance_criteria(spec_text))
    spec_sha256 = hash_file(spec_path)
    return write_receipt(
        task_dir,
        "spec-validated",
        criteria_count=criteria_count,
        spec_sha256=spec_sha256,
    )


def receipt_plan_validated(
    task_dir: Path,
    validation_passed_override: bool | None = None,
    **_legacy: Any,
) -> Path:
    """Write receipt proving plan + execution graph passed validation.

    Self-computes ``segment_count``, ``criteria_coverage``, and
    ``validation_passed`` (v4 contract) by invoking
    ``lib_validate.validate_task_artifacts`` and reading
    ``execution-graph.json``. Callers no longer supply these fields —
    passing any of ``segment_count``, ``criteria_coverage``, or
    ``validation_passed`` raises ``TypeError``.

    ``validation_passed`` is derived as ``len(errors) == 0``. As an
    escape hatch for tests, ``validation_passed_override`` is honoured
    IFF the environment variable ``DYNOS_ALLOW_TEST_OVERRIDE == "1"``;
    otherwise the override is ignored and the computed value wins.

    Captures content hashes of spec.md, plan.md, and execution-graph.json
    so downstream consumers (e.g. execute preflight) can short-circuit
    re-validation when none of the artifacts have changed since the
    receipt was written.
    """
    if _legacy:
        raise TypeError(
            "receipt_plan_validated no longer accepts caller-supplied "
            f"{sorted(_legacy)} — segment_count, criteria_coverage, and "
            "validation_passed are self-computed from task_dir artifacts"
        )

    # Deferred import: validate_task_artifacts lives in lib_validate,
    # which itself imports read_receipt from this module. Importing at
    # call time keeps the module-load graph acyclic.
    from lib_validate import validate_task_artifacts  # noqa: PLC0415

    errors = validate_task_artifacts(task_dir)
    computed_passed = not errors

    # Honour the test override only when the env knob is explicitly set.
    if (
        validation_passed_override is not None
        and os.environ.get("DYNOS_ALLOW_TEST_OVERRIDE") == "1"
    ):
        validation_passed = bool(validation_passed_override)
    else:
        validation_passed = computed_passed

    # Self-compute segment_count + criteria_coverage from the graph.
    segment_count = 0
    criteria_coverage: list[int] = []
    graph_path = task_dir / "execution-graph.json"
    if graph_path.exists():
        try:
            with graph_path.open("r", encoding="utf-8") as f:
                graph = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"receipt_plan_validated: cannot parse execution-graph.json "
                f"at {graph_path}: {exc}"
            ) from exc
        if isinstance(graph, dict):
            segments = graph.get("segments", [])
            if isinstance(segments, list):
                segment_count = len(segments)
                covered: set[int] = set()
                for seg in segments:
                    if not isinstance(seg, dict):
                        continue
                    for cid in seg.get("acceptance_criteria", []) or []:
                        try:
                            covered.add(int(cid))
                        except (TypeError, ValueError):
                            continue
                criteria_coverage = sorted(covered)

    artifact_hashes = {
        "spec.md": _hash_artifact(task_dir / "spec.md"),
        "plan.md": _hash_artifact(task_dir / "plan.md"),
        "execution-graph.json": _hash_artifact(task_dir / "execution-graph.json"),
    }
    return write_receipt(
        task_dir,
        "plan-validated",
        segment_count=segment_count,
        criteria_coverage=criteria_coverage,
        validation_passed=validation_passed,
        artifact_hashes=artifact_hashes,
    )


def plan_validated_receipt_matches(task_dir: Path) -> "bool | str":
    """Return True if a plan-validated receipt exists AND its captured
    artifact hashes match the current spec.md, plan.md, and
    execution-graph.json content.

    Returns ``False`` when the receipt is missing or malformed (e.g. an
    older receipt without an ``artifact_hashes`` payload). Returns a
    descriptive string naming the drifted artifact (e.g.
    ``"plan.md hash drift"``) when the receipt is present but one of the
    tracked artifacts has changed on disk. Callers distinguish these
    three outcomes to surface drift-vs-missing distinctly.
    """
    receipt = read_receipt(task_dir, "plan-validated")
    if receipt is None or not receipt.get("validation_passed", False):
        return False
    captured = receipt.get("artifact_hashes")
    if not isinstance(captured, dict):
        # Old receipts written before hashes existed — treat as drift,
        # forcing re-validation. Safer than assuming they match.
        return False
    for name in ("spec.md", "plan.md", "execution-graph.json"):
        current = _hash_artifact(task_dir / name)
        if current != captured.get(name):
            return f"{name} hash drift"
    return True


def plan_audit_matches(task_dir: Path) -> "bool | str":
    """Return ``True`` if a ``plan-audit-check`` receipt exists AND its
    captured artifact hashes match the current spec.md, plan.md, and
    execution-graph.json content.

    Returns ``False`` when the receipt is missing entirely. Returns a
    descriptive string naming the drifted artifact (e.g.
    ``"plan.md hash drift"``) when the receipt is present but one of the
    tracked artifacts has changed on disk since the audit ran. Callers
    distinguish the three outcomes to surface drift-vs-missing distinctly
    at the PLAN_AUDIT exit gate.

    Receipts written before the hash-binding landed (pre-F2) do not carry
    ``spec_sha256``/``plan_sha256``/``graph_sha256`` fields. Such receipts
    are treated as ``False`` (missing) so the gate forces a fresh audit
    rather than silently trusting a legacy payload.
    """
    receipt = read_receipt(task_dir, "plan-audit-check")
    if receipt is None:
        return False
    # Legacy receipts (pre-F2) lacked the three hash fields. Without
    # hashes we cannot verify freshness — behave like missing.
    expected_spec = receipt.get("spec_sha256")
    expected_plan = receipt.get("plan_sha256")
    expected_graph = receipt.get("graph_sha256")
    if not (
        isinstance(expected_spec, str) and expected_spec
        and isinstance(expected_plan, str) and expected_plan
        and isinstance(expected_graph, str) and expected_graph
    ):
        return False
    # Hash each artifact on disk. Missing files count as drift with a
    # descriptive string (the audit was computed over a file that is now
    # absent — clearly not fresh).
    current_spec = _hash_artifact(task_dir / "spec.md")
    if current_spec != expected_spec:
        return "spec.md hash drift"
    current_plan = _hash_artifact(task_dir / "plan.md")
    if current_plan != expected_plan:
        return "plan.md hash drift"
    current_graph = _hash_artifact(task_dir / "execution-graph.json")
    if current_graph != expected_graph:
        return "execution-graph.json hash drift"
    return True


def receipt_executor_routing(
    task_dir: Path,
    segments: list[dict],
) -> Path:
    """Write receipt proving all executor routing decisions were made."""
    return write_receipt(
        task_dir,
        "executor-routing",
        segments=segments,
    )


def receipt_executor_done(
    task_dir: Path,
    segment_id: str,
    executor_type: str,
    model_used: str | None,
    injected_prompt_sha256: str,
    agent_name: str | None,
    evidence_path: str | None,
    tokens_used: int | None,
) -> Path:
    """Write receipt proving an executor segment completed.

    Asserts the per-segment injected prompt sidecar at
    ``task_dir / "receipts" / "_injected-prompts" / f"{segment_id}.sha256"``
    exists and matches `injected_prompt_sha256`. Raises:

    - ``ValueError("... injected_prompt_sha256 sidecar missing ...")`` if
      the sidecar file does not exist.
    - ``ValueError("... injected_prompt_sha256 mismatch ...")`` if the
      sidecar contents do not match the supplied digest.

    Also records token usage to token-usage.json — the only reliable path
    for token recording since receipts are gated.
    """
    if not isinstance(injected_prompt_sha256, str) or not injected_prompt_sha256:
        raise ValueError("injected_prompt_sha256 must be a non-empty string")

    sidecar_dir = task_dir / "receipts" / INJECTED_PROMPTS_DIR
    sidecar_file = sidecar_dir / f"{segment_id}.sha256"

    if not sidecar_file.exists():
        raise ValueError(
            f"executor-{segment_id}: injected_prompt_sha256 sidecar missing "
            f"at {sidecar_file}"
        )
    try:
        on_disk = sidecar_file.read_text().strip()
    except OSError as e:
        raise ValueError(
            f"executor-{segment_id}: injected_prompt_sha256 sidecar missing "
            f"(unreadable {sidecar_file}: {e})"
        ) from e
    if on_disk != injected_prompt_sha256:
        raise ValueError(
            f"executor-{segment_id}: injected_prompt_sha256 mismatch "
            f"(sidecar={on_disk!r}, payload={injected_prompt_sha256!r})"
        )

    # Record tokens deterministically
    if tokens_used and tokens_used > 0:
        _record_tokens(task_dir, f"{executor_type}-{segment_id}", model_used or "default", tokens_used)

    return write_receipt(
        task_dir,
        f"executor-{segment_id}",
        segment_id=segment_id,
        executor_type=executor_type,
        model_used=model_used,
        injected_prompt_sha256=injected_prompt_sha256,
        agent_name=agent_name,
        evidence_path=evidence_path,
        tokens_used=tokens_used,
    )


def receipt_audit_routing(
    task_dir: Path,
    auditors: list[dict],
) -> Path:
    """Write receipt proving all auditor routing decisions were made.

    Each entry in `auditors` MUST include the keys:
      - injected_agent_sha256: str | None
          (None only when route_mode == "generic"; otherwise required)
      - agent_path: str | None
    Callers are responsible for populating these; this writer enforces
    presence so downstream consumers can rely on the schema.
    """
    if not isinstance(auditors, list):
        raise ValueError("auditors must be a list")
    # Normalize entries before write: skip entries don't need injection fields
    # (they weren't injected), so we fill missing keys with None rather than
    # forcing callers to pass boilerplate. Spawn entries still require the
    # fields explicitly (so a missing key is a schema violation, not a typo).
    normalized: list[dict] = []
    for idx, entry in enumerate(auditors):
        if not isinstance(entry, dict):
            raise ValueError(f"auditors[{idx}] must be a dict")
        action = entry.get("action")
        if action == "skip":
            # Skip entries: default the injection fields to None if absent.
            entry = {
                **entry,
                "injected_agent_sha256": entry.get("injected_agent_sha256"),
                "agent_path": entry.get("agent_path"),
            }
        else:
            # Spawn (or unknown) entries: require explicit keys (may be None).
            if "injected_agent_sha256" not in entry:
                raise ValueError(
                    f"auditors[{idx}] missing required key 'injected_agent_sha256' "
                    f"(must be str or None)"
                )
            if "agent_path" not in entry:
                raise ValueError(
                    f"auditors[{idx}] missing required key 'agent_path' "
                    f"(must be str or None)"
                )
        route_mode = entry.get("route_mode")
        injected = entry.get("injected_agent_sha256")
        # injected_agent_sha256 may be None only when route_mode is generic OR on skip.
        if injected is None and route_mode != "generic" and action != "skip":
            raise ValueError(
                f"auditors[{idx}] injected_agent_sha256 may be None only "
                f"when route_mode=='generic' or action=='skip' "
                f"(got route_mode={route_mode!r} action={action!r})"
            )
        if injected is not None and not isinstance(injected, str):
            raise ValueError(
                f"auditors[{idx}] injected_agent_sha256 must be str or None"
            )
        agent_path = entry.get("agent_path")
        if agent_path is not None and not isinstance(agent_path, str):
            raise ValueError(
                f"auditors[{idx}] agent_path must be str or None"
            )
        normalized.append(entry)

    return write_receipt(
        task_dir,
        "audit-routing",
        auditors=normalized,
    )


def receipt_audit_done(
    task_dir: Path,
    auditor_name: str,
    model_used: str | None,
    finding_count: int | None = None,
    blocking_count: int | None = None,
    report_path: str | None = None,
    tokens_used: int | None = None,
    *,
    route_mode: str,
    agent_path: str | None,
    injected_agent_sha256: str | None,
    ensemble_context: bool = False,
) -> Path:
    """Write receipt proving an auditor completed.

    Asserts the per-(auditor, model) sidecar at
    ``task_dir / "receipts" / "_injected-auditor-prompts"
    / f"{auditor_name}-{model_used}.sha256"`` matches
    `injected_agent_sha256` when non-null. Per-model disambiguation lets
    ensemble voting compare distinct injected prompts per model.

    Raises ValueError on sidecar mismatch or missing. There is no env
    bypass — sidecar enforcement is unconditional.

    v4 AC 17: when ``route_mode == "learned"`` or ``ensemble_context`` is
    True the caller MUST supply a non-null ``report_path`` pointing at an
    existing file (the voting harness materialises this file before the
    receipt is written). The pre-escalation ensemble-vote bypass is thus
    no longer available for learned/ensemble auditors — ship a real
    report or fail the write.

    Also records token usage — same enforcement path as executor receipts.
    """
    if not isinstance(route_mode, str) or not route_mode:
        raise ValueError("route_mode must be a non-empty string")
    if injected_agent_sha256 is None and route_mode != "generic":
        raise ValueError(
            f"injected_agent_sha256 may be None only when route_mode=='generic' "
            f"(got route_mode={route_mode!r})"
        )
    if injected_agent_sha256 is not None and not isinstance(injected_agent_sha256, str):
        raise ValueError("injected_agent_sha256 must be str or None")
    if agent_path is not None and not isinstance(agent_path, str):
        raise ValueError("agent_path must be str or None")

    # AC 17: learned/ensemble auditors require a real report file.
    if route_mode == "learned" or ensemble_context:
        if not isinstance(report_path, str) or not report_path:
            raise ValueError(
                "report_path required for learned/ensemble auditors "
                f"(auditor={auditor_name!r}, route_mode={route_mode!r}, "
                f"ensemble_context={ensemble_context!r})"
            )
        if not Path(report_path).exists():
            raise ValueError(
                "report_path required for learned/ensemble auditors "
                f"(missing file at {report_path} for auditor "
                f"{auditor_name!r}, route_mode={route_mode!r}, "
                f"ensemble_context={ensemble_context!r})"
            )

    if injected_agent_sha256 is not None:
        sidecar_file = (
            task_dir / "receipts" / INJECTED_AUDITOR_PROMPTS_DIR
            / f"{auditor_name}-{model_used}.sha256"
        )
        if not sidecar_file.exists():
            raise ValueError(
                f"audit-{auditor_name}: injected auditor prompt sidecar "
                f"missing at {sidecar_file}"
            )
        try:
            on_disk = sidecar_file.read_text().strip()
        except OSError as e:
            raise ValueError(
                f"audit-{auditor_name}: injected auditor prompt sidecar "
                f"unreadable at {sidecar_file}: {e}"
            ) from e
        if on_disk != injected_agent_sha256:
            raise ValueError(
                f"audit-{auditor_name}: injected_agent_sha256 mismatch "
                f"(sidecar={on_disk!r}, payload={injected_agent_sha256!r})"
            )

    if tokens_used and tokens_used > 0:
        _record_tokens(task_dir, auditor_name, model_used or "default", tokens_used)

    if (finding_count is None) ^ (blocking_count is None):
        raise TypeError(
            "finding_count and blocking_count must be provided together "
            "or both omitted"
        )

    # MA-005 hardening: when report_path is None we have no way to verify
    # caller-supplied finding_count / blocking_count. Any non-zero count
    # supplied without a corresponding report file is caller-attested and
    # exactly the TOCTOU pattern SEC-004 closed for receipt_plan_audit.
    # Rule: `report_path=None` demands both counts be zero. If the auditor
    # found anything, they must materialise a report file and pass its
    # path. Learned / ensemble callers already hit the AC 17 guard above
    # (report_path REQUIRED). This rule catches the remaining generic
    # case and legacy voting-harness callers that pass None+counts>0.
    if not (isinstance(report_path, str) and report_path):
        if finding_count is None:
            finding_count = 0
            blocking_count = 0
        if finding_count != 0 or blocking_count != 0:
            raise ValueError(
                f"audit-{auditor_name}: report_path is None but "
                f"finding_count={finding_count}, blocking_count={blocking_count}. "
                "Caller-attested non-zero counts are forbidden — "
                "materialise a report file and pass its path, or pass "
                "(0, 0) and None together."
            )

    # AC 2 — self-verify block. When ``report_path`` is a non-null string
    # referring to an existing JSON file, cross-check caller-supplied
    # finding_count / blocking_count against the actual contents of the
    # report and attach a sha256 of the file. Any mismatch aborts the
    # write with ValueError naming the mismatched field and both values.
    #
    # When ``report_path`` is None OR the file does not exist, the MA-005
    # rule above already demands counts=(0, 0). The skip below is now
    # semantically: "no report means literal zero findings."
    report_sha256: str | None = None
    if isinstance(report_path, str) and report_path:
        report_file = Path(report_path)
        # SEC-004 fix: reject report_path that escapes task_dir. A compromised
        # orchestrator could otherwise point report_path at arbitrary files.
        try:
            resolved_report = report_file.resolve()
            resolved_task = Path(task_dir).resolve()
            resolved_report.relative_to(resolved_task)
        except (ValueError, OSError) as exc:
            raise ValueError(
                f"audit-{auditor_name}: report_path must be inside task_dir "
                f"({resolved_task}); got {report_path!r}"
            ) from exc
        if report_file.exists():
            try:
                with report_file.open("r", encoding="utf-8") as f:
                    report_payload = json.load(f)
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f"audit-{auditor_name}: cannot parse report at "
                    f"{report_path}: {exc}"
                ) from exc
            findings = report_payload.get("findings", []) if isinstance(report_payload, dict) else []
            if not isinstance(findings, list):
                findings = []
            actual_finding_count = len(findings)
            actual_blocking_count = sum(
                1 for f in findings
                if isinstance(f, dict) and f.get("blocking") is True
            )
            if finding_count is None:
                finding_count = actual_finding_count
                blocking_count = actual_blocking_count
            if finding_count != actual_finding_count:
                raise ValueError(
                    f"audit-{auditor_name}: finding_count mismatch — "
                    f"caller-supplied={finding_count}, "
                    f"actual (from {report_path})={actual_finding_count}"
                )
            if blocking_count != actual_blocking_count:
                raise ValueError(
                    f"audit-{auditor_name}: blocking_count mismatch — "
                    f"caller-supplied={blocking_count}, "
                    f"actual (from {report_path})={actual_blocking_count}"
                )
            try:
                report_sha256 = hash_file(report_file)
            except (FileNotFoundError, OSError) as exc:
                raise ValueError(
                    f"audit-{auditor_name}: cannot hash report at "
                    f"{report_path}: {exc}"
                ) from exc
        elif finding_count is None:
            raise ValueError(
                f"audit-{auditor_name}: report_path={report_path!r} does not exist; "
                "cannot derive finding_count/blocking_count automatically"
            )

    return write_receipt(
        task_dir,
        f"audit-{auditor_name}",
        auditor_name=auditor_name,
        model_used=model_used,
        finding_count=finding_count,
        blocking_count=blocking_count,
        report_path=report_path,
        report_sha256=report_sha256,
        tokens_used=tokens_used,
        route_mode=route_mode,
        agent_path=agent_path,
        injected_agent_sha256=injected_agent_sha256,
    )


def receipt_retrospective(task_dir: Path, **_legacy: Any) -> Path:
    """Write receipt proving retrospective was computed.

    Self-computes ``quality_score``, ``cost_score``, ``efficiency_score``,
    and ``total_tokens`` (v4 contract) by invoking
    ``lib_validate.compute_reward(task_dir)``. Callers no longer supply
    these fields — any legacy kwarg (``quality_score``, ``cost_score``,
    ``efficiency_score``, ``total_tokens``) raises ``TypeError``.
    """
    if _legacy:
        raise TypeError(
            "receipt_retrospective no longer accepts caller-supplied "
            f"{sorted(_legacy)} — scores and tokens are now computed via "
            "lib_validate.compute_reward(task_dir)"
        )

    # Deferred import to avoid an lib_validate <-> lib_receipts cycle
    # (lib_validate.compute_reward imports read_receipt from this module).
    from lib_validate import compute_reward  # noqa: PLC0415

    result = compute_reward(task_dir)
    if not isinstance(result, dict):
        raise ValueError(
            f"receipt_retrospective: compute_reward returned non-dict "
            f"{type(result).__name__}"
        )
    # compute_reward uses `total_token_usage`; receipt schema uses
    # `total_tokens`. Accept either to stay resilient to refactors.
    total_tokens = result.get("total_tokens")
    if total_tokens is None:
        total_tokens = result.get("total_token_usage", 0)
    return write_receipt(
        task_dir,
        "retrospective",
        quality_score=result.get("quality_score", 0.0),
        cost_score=result.get("cost_score", 0.0),
        efficiency_score=result.get("efficiency_score", 0.0),
        total_tokens=total_tokens,
        retrospective_path=str(task_dir / "task-retrospective.json"),
    )


def receipt_post_completion(
    task_dir: Path,
    handlers_run: list[dict],
) -> Path:
    """Write receipt proving post-completion pipeline ran.

    Per the v2 contract, post-completion no longer carries postmortem or
    pattern-update flags — those concerns belong to dedicated postmortem
    receipts (see `receipt_postmortem_*`).

    v4 self-verify: when ``handlers_run`` is non-empty, cross-check each
    declared handler name against ``eventbus_handler`` events in
    ``events.jsonl``. Both the task-scoped log
    (``task_dir/events.jsonl``) and the repo-level fallback
    (``<root>/.dynos/events.jsonl``) are consulted because the eventbus
    currently writes handler events without a ``task=`` attribution and
    they land in the global file. Each entry in ``handlers_run`` must
    resolve to an event whose ``handler`` or ``name`` field matches; a
    missing handler raises
    ``ValueError("post-completion handler not in events: <name>")``.

    v5 self_verify enum: the receipt payload now carries a top-level
    ``self_verify: str`` field with one of three values:
      * ``"passed"`` — events.jsonl was readable AND every handler name
        in ``handlers_run`` was matched against an ``eventbus_handler``
        record.
      * ``"skipped-no-events-log"`` — no events.jsonl file was readable
        (task-scoped and repo-level both absent / unreadable).
      * ``"skipped-handlers-empty"`` — ``handlers_run`` list was empty
        (no handlers to verify).

    Fail-open on file trouble: if events.jsonl is absent, unreadable, or
    unparseable, emit a single stderr warning and proceed with the write
    so the post-completion pipeline is not held hostage to a missing log.
    """
    if not isinstance(handlers_run, list):
        raise ValueError("handlers_run must be a list")

    # Default: empty handlers_run short-circuits to the skipped enum and
    # no file IO happens.
    self_verify: str = "skipped-handlers-empty"

    if handlers_run:
        root = task_dir.parent.parent
        task_id = task_dir.name
        repo_events = root / ".dynos" / "events.jsonl"
        task_events = task_dir / "events.jsonl"

        seen_handlers: set[str] = set()
        any_file_readable = False
        for path in (repo_events, task_events):
            if not path.exists():
                continue
            try:
                with path.open("r", encoding="utf-8") as f:
                    for raw in f:
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            record = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(record, dict):
                            continue
                        if record.get("event") != "eventbus_handler":
                            continue
                        rec_task = record.get("task")
                        # Post-AC18: the eventbus drain now stamps every
                        # per-task ``eventbus_handler`` emission with
                        # ``task=task_id`` before writing to the repo-
                        # level ``.dynos/events.jsonl``. Attribution-less
                        # records therefore cannot have legitimately come
                        # from another task's drain — they are dropped so
                        # a task's self-verify set cannot pull in handler
                        # events it did not produce. Strict equality only.
                        if rec_task != task_id:
                            continue
                        for key in ("handler", "name"):
                            name = record.get(key)
                            if isinstance(name, str) and name:
                                seen_handlers.add(name)
                any_file_readable = True
            except OSError as exc:
                import sys as _sys
                print(
                    f"post-completion self-verify skipped: events.jsonl "
                    f"unavailable ({path}: {exc})",
                    file=_sys.stderr,
                )

        if not any_file_readable:
            import sys as _sys
            print(
                "post-completion self-verify skipped: events.jsonl "
                "unavailable",
                file=_sys.stderr,
            )
            self_verify = "skipped-no-events-log"
        else:
            for idx, entry in enumerate(handlers_run):
                if not isinstance(entry, dict):
                    raise ValueError(
                        f"handlers_run[{idx}] must be a dict (got "
                        f"{type(entry).__name__})"
                    )
                name = entry.get("name") or entry.get("handler")
                if not isinstance(name, str) or not name:
                    raise ValueError(
                        f"handlers_run[{idx}] missing 'name'/'handler' key"
                    )
                if name not in seen_handlers:
                    raise ValueError(
                        f"post-completion handler not in events: {name}"
                    )
            # All handlers matched and events.jsonl was readable — pass.
            self_verify = "passed"

    return write_receipt(
        task_dir,
        "post-completion",
        handlers_run=handlers_run,
        self_verify=self_verify,
    )


# ---------------------------------------------------------------------------
# Start skill spawn receipts (planner, plan-audit, TDD)
# ---------------------------------------------------------------------------


_INJECTED_PROMPT_SHA256_MISSING = object()


def receipt_planner_spawn(  # called dynamically from skills/start/SKILL.md
    task_dir: Path,
    phase: str,  # "discovery", "spec", or "plan"
    tokens_used: int,
    model_used: str | None = None,
    agent_name: str | None = None,
    injected_prompt_sha256: str = _INJECTED_PROMPT_SHA256_MISSING,  # type: ignore[assignment]
) -> Path:
    """Write receipt proving a planner subagent completed. Also records tokens.

    SEC-004 hardening: ``injected_prompt_sha256`` is REQUIRED at the call
    site. Omitting the kwarg entirely raises ``TypeError`` (the sentinel
    default is a deliberate forced-kwarg pattern so a forgotten sidecar
    assertion cannot silently ship). Passing ``injected_prompt_sha256=None``
    explicitly is ALSO rejected — ``None`` is no longer a legal value and
    raises ``ValueError`` with a message containing the substring
    ``legacy None path removed``. The only valid value is a non-empty
    sha256 hex digest captured from
    ``hooks/router.py planner-inject-prompt --task-id <id> --phase <phase>``.

    The writer asserts that the per-phase planner injected-prompt sidecar
    at ``task_dir / "receipts" / INJECTED_PLANNER_PROMPTS_DIR /
    f"{phase}.sha256"`` exists AND its contents (after stripping trailing
    whitespace) match the supplied digest. On missing file or mismatch
    this function raises ``ValueError`` naming the phase. The mismatch
    message contains the literal substring ``hash mismatch`` so
    downstream tests can pin it.

    The sidecar path is
    ``task_dir/receipts/_injected-planner-prompts/{phase}.sha256`` — both
    writer and reader import the directory name from
    ``INJECTED_PLANNER_PROMPTS_DIR`` so the schema is defined in exactly
    one place. The sidecar itself is written by the
    ``planner-inject-prompt`` CLI subcommand in ``hooks/router.py``.
    """
    if injected_prompt_sha256 is _INJECTED_PROMPT_SHA256_MISSING:
        raise TypeError(
            "receipt_planner_spawn: injected_prompt_sha256 is required. "
            "Pass a non-empty sha256 hex digest obtained from "
            "`hooks/router.py planner-inject-prompt --task-id <id> "
            "--phase <phase>`. The None (no-sidecar) path has been removed."
        )
    if injected_prompt_sha256 is None:
        raise ValueError(
            "injected_prompt_sha256 must be a non-empty sha256 hex string; "
            "legacy None path removed"
        )
    # v4 AC 13: tokens_used MUST be a non-negative int. zero is accepted
    # (signals a free / cached planner invocation) but emits a telemetry
    # event so the anomaly surfaces in retrospectives.
    if not isinstance(tokens_used, int) or isinstance(tokens_used, bool) or tokens_used < 0:
        raise ValueError(
            f"receipt_planner_spawn: tokens_used must be non-negative int "
            f"(got {tokens_used!r})"
        )
    step_name = f"planner-{phase}"

    # Sidecar assertion — unconditional now. Every caller must first run
    # `hooks/router.py planner-inject-prompt` and pass the captured digest.
    if not isinstance(injected_prompt_sha256, str) or not injected_prompt_sha256:
        raise ValueError(
            "receipt_planner_spawn: injected_prompt_sha256 must be a "
            "non-empty string"
        )
    sidecar_file = (
        task_dir / "receipts" / INJECTED_PLANNER_PROMPTS_DIR
        / f"{phase}.sha256"
    )
    if not sidecar_file.exists():
        raise ValueError(
            f"receipt_planner_spawn: planner sidecar missing for phase "
            f"{phase!r} at {sidecar_file}. Run `hooks/router.py "
            f"planner-inject-prompt --task-id <id> --phase {phase}` first."
        )
    try:
        on_disk = sidecar_file.read_text().strip()
    except OSError as e:
        raise ValueError(
            f"receipt_planner_spawn: planner sidecar unreadable for "
            f"phase {phase!r} at {sidecar_file}: {e}"
        ) from e
    if on_disk != injected_prompt_sha256:
        raise ValueError(
            f"receipt_planner_spawn: hash mismatch for phase {phase!r} "
            f"— sidecar={on_disk!r}, payload={injected_prompt_sha256!r}."
        )

    if tokens_used > 0:
        _record_tokens(task_dir, f"planner-{phase}", model_used or "default", tokens_used)
    else:
        # Zero-token planner spawn is a diagnostic signal: emit an event
        # so retrospectives can flag suspected cache-hits, handler
        # stubs, or upstream attribution drift without failing the write.
        root = task_dir.parent.parent
        log_event(
            root,
            "planner_spawn_zero_tokens",
            task=task_dir.name,
            phase=phase,
        )
    return write_receipt(
        task_dir,
        step_name,
        phase=phase,
        tokens_used=tokens_used,
        model_used=model_used,
        agent_name=agent_name,
        injected_prompt_sha256=injected_prompt_sha256,
    )


def receipt_plan_audit(
    task_dir: Path,
    tokens_used: int,
    model_used: str | None = None,
    **_legacy: Any,
) -> Path:
    """Write receipt proving plan audit (spec-completion check) ran.

    Hash-binding (SEC-004 + F2): the writer re-hashes ``spec.md``,
    ``plan.md``, and ``execution-graph.json`` from the task directory at
    write time. Those sha256 hex digests are embedded in the receipt
    payload so the PLAN_AUDIT exit gate can detect artifact drift via
    ``plan_audit_matches(task_dir)`` and refuse to advance when the audit
    was computed over a stale version of the artifacts.

    Callers no longer supply the three hashes (breaking change vs. the
    initial F2 signature). Closes the TOCTOU between a caller's
    hash-read and the receipt write — the writer's own read is the
    authoritative source. Missing artifact files land the literal
    string ``missing`` in the corresponding payload slot, which always
    fails ``plan_audit_matches`` downstream with a distinctive drift
    reason.

    v4 AC 14 (task-007): ``finding_count`` has been removed from the
    signature and the receipt payload. Callers that still pass it raise
    ``TypeError`` — the plan-audit result is surfaced via dedicated audit
    receipts, not embedded in this spawn receipt. ``tokens_used`` must
    be a non-negative int.

    Also records token usage to ``token-usage.json`` when ``tokens_used``
    is positive.
    """
    if _legacy:
        raise TypeError(
            "receipt_plan_audit no longer accepts caller-supplied "
            f"{sorted(_legacy)} — finding_count was removed from the v4 "
            "plan-audit receipt payload"
        )
    if not isinstance(tokens_used, int) or isinstance(tokens_used, bool) or tokens_used < 0:
        raise ValueError(
            f"receipt_plan_audit: tokens_used must be non-negative int "
            f"(got {tokens_used!r})"
        )

    def _hash_or_missing(rel: str) -> str:
        p = task_dir / rel
        if not p.exists():
            return "missing"
        try:
            return hash_file(p)
        except OSError:
            return "missing"

    spec_sha256 = _hash_or_missing("spec.md")
    plan_sha256 = _hash_or_missing("plan.md")
    graph_sha256 = _hash_or_missing("execution-graph.json")

    if tokens_used > 0:
        _record_tokens(task_dir, "plan-audit-check", model_used or "default", tokens_used)
    return write_receipt(
        task_dir,
        "plan-audit-check",
        tokens_used=tokens_used,
        model_used=model_used,
        spec_sha256=spec_sha256,
        plan_sha256=plan_sha256,
        graph_sha256=graph_sha256,
    )


def receipt_tdd_tests(
    task_dir: Path,
    test_file_paths: list[str],
    tests_evidence_sha256: str,
    tokens_used: int,
    model_used: str,
) -> Path:
    """Write receipt proving TDD tests were generated.

    Also records token usage. Validates inputs strictly: paths must be a
    list of strings, the evidence digest must be non-empty.
    """
    if not isinstance(test_file_paths, list) or not all(
        isinstance(p, str) for p in test_file_paths
    ):
        raise ValueError("test_file_paths must be a list[str]")
    if not isinstance(tests_evidence_sha256, str) or not tests_evidence_sha256:
        raise ValueError("tests_evidence_sha256 must be a non-empty string")
    if not isinstance(tokens_used, int) or tokens_used < 0:
        raise ValueError("tokens_used must be a non-negative int")
    if not isinstance(model_used, str) or not model_used:
        raise ValueError("model_used must be a non-empty string")

    if tokens_used > 0:
        _record_tokens(task_dir, "tdd-tests", model_used, tokens_used)

    return write_receipt(
        task_dir,
        "tdd-tests",
        test_file_paths=test_file_paths,
        tests_evidence_sha256=tests_evidence_sha256,
        tokens_used=tokens_used,
        model_used=model_used,
    )


# ---------------------------------------------------------------------------
# Human approval, postmortem, and calibration receipts
# ---------------------------------------------------------------------------


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
    if not isinstance(stage, str) or not stage:
        raise ValueError("stage must be a non-empty string")
    if "/" in stage or "\\" in stage or stage.startswith("."):
        raise ValueError(f"stage must not contain path separators: {stage!r}")
    if not isinstance(artifact_sha256, str) or not artifact_sha256:
        raise ValueError("artifact_sha256 must be a non-empty string")
    if not isinstance(approver, str) or not approver.strip():
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
    analysis_sha256: str,
    rules_added: int,
    rules_sha256_after: str,
) -> Path:
    """Write receipt proving postmortem analysis ran and rules updated."""
    if not isinstance(analysis_sha256, str) or not analysis_sha256:
        raise ValueError("analysis_sha256 must be a non-empty string")
    if not isinstance(rules_added, int) or rules_added < 0:
        raise ValueError("rules_added must be a non-negative int")
    if not isinstance(rules_sha256_after, str) or not rules_sha256_after:
        raise ValueError("rules_sha256_after must be a non-empty string")
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
    if not isinstance(retrospective_sha256, str) or not retrospective_sha256:
        raise ValueError("retrospective_sha256 must be a non-empty string")

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
    if not isinstance(policy_sha256_before, str) or not policy_sha256_before:
        raise ValueError("policy_sha256_before must be a non-empty string")
    if not isinstance(policy_sha256_after, str) or not policy_sha256_after:
        raise ValueError("policy_sha256_after must be a non-empty string")
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
    if not isinstance(policy_sha256, str) or not policy_sha256:
        raise ValueError("policy_sha256 must be a non-empty string")
    return write_receipt(
        task_dir,
        "calibration-noop",
        reason=reason,
        policy_sha256=policy_sha256,
    )


def receipt_rules_check_passed(task_dir: Path, mode: str) -> Path:
    """Write receipt proving a rules-check pass (no error-severity violations).

    Signature (AC 1): takes only ``(task_dir, mode)``. All counts and
    hashes are computed internally from a fresh ``rules_engine.run_checks``
    call — callers no longer supply (and therefore cannot falsify) the
    violation totals.

    Refuses with ValueError if the rules engine reports any
    error-severity violation. Rules-check pipeline must branch to the
    failure path in that case; this writer proves the clean outcome only.

    Payload shape (every value computed here):
      - rules_evaluated:    count of entries in ``rules`` list of
                            prevention-rules.json
      - violations_count:   len(violations) returned by run_checks
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
    from rules_engine import run_checks  # noqa: PLC0415
    from lib_core import _persistent_project_dir  # noqa: PLC0415

    root = task_dir.parent.parent

    # Run the engine. This is the source of truth for counts — callers
    # cannot lie about what the engine found.
    violations = run_checks(root, mode)
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

    # Count rules from the file. If the file is missing, count is 0 and
    # the hash is the literal string "none" (matches legacy schema).
    rules_file = _persistent_project_dir(root) / "prevention-rules.json"
    rules_evaluated = 0
    rules_file_sha256: str = "none"
    if rules_file.exists():
        try:
            rules_file_sha256 = hash_file(rules_file)
        except (FileNotFoundError, OSError) as exc:
            raise ValueError(
                f"receipt_rules_check_passed: cannot hash prevention-rules "
                f"file at {rules_file}: {exc}"
            ) from exc
        try:
            with rules_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"receipt_rules_check_passed: cannot parse prevention-rules "
                f"at {rules_file}: {exc}"
            ) from exc
        if isinstance(data, dict):
            rules = data.get("rules", [])
            if isinstance(rules, list):
                rules_evaluated = len(rules)

    return write_receipt(
        task_dir,
        "rules-check-passed",
        rules_evaluated=rules_evaluated,
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
    if not isinstance(from_stage, str) or not from_stage:
        raise ValueError("from_stage must be a non-empty string")
    if not isinstance(to_stage, str) or not to_stage:
        raise ValueError("to_stage must be a non-empty string")
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
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(
            "reason must be a non-empty string "
            "(whitespace-only values are rejected)"
        )
    if not isinstance(approver, str) or not approver.strip():
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
    if not isinstance(current_stage, str) or not current_stage:
        raise ValueError("current_stage must be a non-empty string")
    if not isinstance(proposed_stage, str) or not proposed_stage:
        raise ValueError("proposed_stage must be a non-empty string")
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



# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_validate_chain(args: Any) -> int:
    """CLI: validate the receipt chain for a task."""
    task_dir = Path(args.task_dir).resolve()
    gaps = validate_chain(task_dir)
    if gaps:
        print(f"Receipt chain gaps ({len(gaps)}):")
        for gap in gaps:
            print(f"  ✗ {gap}")
        return 1
    print("Receipt chain complete — all required receipts present.")
    return 0


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Receipt chain validation")
    parser.add_argument("task_dir", help="Path to task directory")
    args = parser.parse_args()
    raise SystemExit(cmd_validate_chain(args))
