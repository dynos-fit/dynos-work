"""Receipt-based contract validation chain for dynos-work.

Every pipeline step writes a structured JSON receipt to
.dynos/task-{id}/receipts/{step-name}.json proving what it did.
The next step refuses to proceed unless the prior receipt exists.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from lib_core import now_iso, append_execution_log, _persistent_project_dir
from lib_log import log_event, verify_signed_events
from lib_models import valid_models_for_host, TIER_FAST, resolve_model_for_tier
from lib_validate import require_nonblank, require_nonblank_str
from write_policy import WriteAttempt, _get_capability_key, require_write_allowed


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
RECEIPT_CONTRACT_VERSION = 7


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
    # v5 -> v6 bump (task-20260505-001): auto-approval receipts allow
    # /dynos-work:residual run-next to drain low-risk residuals overnight
    # without human gates while preserving the same hash-mismatch refusal
    # invariant as human-approval receipts. v5 receipts on disk remain
    # readable; only new auto-approval-* writes carry contract_version=6.
    "auto-approval-*": 6,
    # v6 -> v7 bump (task-20260611-001): spawn receipts now carry
    # {host, tier, resolved_model} v7 fields. The floor ensures consumers
    # that depend on host/tier/resolved_model fields reject pre-v7 spawn
    # receipts that lack these fields. Pre-v7 spawn receipts on disk remain
    # readable by callers that pass min_version=1; only new spawn-* writes
    # carry contract_version=7.
    "spawn-*": 7,
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


# Map receipt steps to human-readable execution-log entries
_LOG_MESSAGES: dict[str, str] = {
    "search-conducted": "[DONE] search conducted — query={query}",
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
    "auto-approval-SPEC_REVIEW": "[DONE] auto-approval SPEC_REVIEW — approver={approver}",
    "auto-approval-PLAN_REVIEW": "[DONE] auto-approval PLAN_REVIEW — approver={approver}",
    "auto-approval-TDD_REVIEW": "[DONE] auto-approval TDD_REVIEW — approver={approver}",
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


_WRITE_ROLE = "receipt-writer"


def _record_tokens(task_dir: Path, agent: str, model: str, tokens: int) -> None:
    """Deprecated compatibility shim for old receipt token recording.

    LLM token usage is now attributed exclusively by the SubagentStop hook,
    which parses the finished transcript and writes the authoritative
    ``spawn`` record into ``token-usage.json``. Recording ``tokens_used`` a
    second time from receipt writers double-counts the same work.

    The helper remains in place so older receipt call sites and tests do not
    need to change shape, but it intentionally performs no write.
    """
    _ = (task_dir, agent, model, tokens)
    return None


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
            role=_WRITE_ROLE,
            task_dir=task_dir,
            path=receipt_path,
            operation="modify" if receipt_path.exists() else "create",
            source=_WRITE_ROLE,
        ),
        capability_key=_get_capability_key(_WRITE_ROLE),
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

    # Task-receipt-chain extension (task-20260503-001). Best-effort: any
    # exception is logged via task_receipt_chain_extension_failed and
    # never propagates. Receipt write durability is already guaranteed
    # above by _atomic_write_text.
    try:
        from lib_chain import extend_chain_for_receipt
        extend_chain_for_receipt(task_dir, step_name, receipt_path)
    except Exception as _chain_exc:
        try:
            log_event(
                root, "task_receipt_chain_extension_failed",
                task=task_id, step=step_name, error=str(_chain_exc),
            )
        except Exception:
            pass

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

    # Conditionally require `search-conducted` for all stages at or past
    # SPEC_REVIEW when the gate wrote search_recommended=true.  run-spec-ready
    # blocks advancement from SPEC_NORMALIZATION without the receipt; this
    # ensures validate_chain retroactively surfaces the gap on tasks that
    # slipped past before the gate enforcement was added.
    gate_path = task_dir / "external-solution-gate.json"
    search_required = False
    if gate_path.exists():
        try:
            gate_data = json.loads(gate_path.read_text())
            search_required = isinstance(gate_data, dict) and gate_data.get("search_recommended") is True
        except Exception:
            pass
    if search_required:
        search_stages = {
            "SPEC_REVIEW",
            "PLANNING",
            "PLAN_REVIEW",
            "PLAN_AUDIT",
            "PRE_EXECUTION_SNAPSHOT",
            "EXECUTION",
            "TEST_EXECUTION",
            "CHECKPOINT_AUDIT",
            "REPAIR_PLANNING",
            "REPAIR_EXECUTION",
            "FINAL_AUDIT",
            "DONE",
        }
        for s in search_stages:
            existing = stage_requires.get(s, [])
            if "search-conducted" not in existing:
                stage_requires[s] = existing + ["search-conducted"]

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


def validate_receipt_model_field(receipt: dict, host: str) -> bool:
    """Validate the ``resolved_model`` field of a spawn receipt against the host.

    Returns True when the receipt's ``resolved_model`` value is consistent with
    ``host``; returns False (validation failure) when the field is inconsistent.

    Rules (mirrors ``lib_models`` host contract):
    - For ``host="codex"``: ``resolved_model`` must be ``None``.  Any non-None
      value (including a valid Claude model name) is a forgery attempt and
      returns False.
    - For ``host="claude"``: ``resolved_model`` must be a member of
      ``valid_models_for_host("claude")``.  ``None`` is not acceptable because
      the Claude routing layer always resolves to a concrete model.
    - For any other host: the check is fail-closed — False is returned unless
      ``resolved_model`` is in ``valid_models_for_host(host)``.  When
      ``valid_models_for_host`` returns an empty frozenset (unknown host), any
      ``resolved_model`` value returns False.

    This function does NOT raise; it returns a plain bool so callers can
    distinguish "bad receipt" from "programmer error" without try/except.
    """
    if not isinstance(receipt, dict):
        return False
    resolved_model = receipt.get("resolved_model")
    # Use the canary-tier approach: if resolve_model_for_tier returns None for
    # TIER_FAST, this host never resolves to a concrete model (e.g. codex),
    # so resolved_model MUST be None. Otherwise it must be in valid_models.
    canary = resolve_model_for_tier(host, TIER_FAST)
    if canary is None:
        # None-model host (e.g. codex): resolved_model must be None.
        return resolved_model is None
    else:
        # Concrete-model host (e.g. claude): resolved_model must be in valid set.
        valid_models = valid_models_for_host(host)
        if not valid_models:
            return False
        return resolved_model in valid_models
