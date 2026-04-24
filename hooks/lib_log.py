"""Unified structured event logging for dynos-work.

Writes JSONL events to per-task log files at .dynos/task-{id}/events.jsonl.
When no task context is available, falls back to .dynos/events.jsonl (global).
Thread-safe via fcntl advisory locking.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any

from lib_core import _persistent_project_dir, now_iso
from write_policy import WriteAttempt, require_write_allowed


# Event names whose sole purpose is operator visibility / forensic trace.
# These events do NOT participate in receipts, stage transitions, or any
# downstream deterministic gate — they are diagnostic-only. Auditors and
# retrospectives may read them, but nothing in the state machine blocks on
# their presence or absence. Kept as a frozenset so the constant cannot be
# mutated at runtime (tests that want to simulate an extra event-name add
# to a local copy, not this module's constant).
DIAGNOSTIC_ONLY_EVENTS: frozenset[str] = frozenset({
    "gate_refused",
    "receipt_refused",
    "finding_contradiction",
    "auditor_not_in_routing",
    "auditor_cross_check_skipped",
    "prevention_rules_corrupt",
    "prevention_rules_corrupt_bootstrap",
    "prevention_rules_healed",
    "pre_repair_snapshot_failed",
    "learned_agent_missing",
    "learned_agent_error",
    "learned_auditor_error",
    "learned_auditor_missing",
    "learned_agent_applied",
    "learned_auditor_applied",
    "router_cache_write_failed",
    "router_cache_lookup",
    "router_cache_write",
    "router_audit_plan",
    "router_executor_plan",
    "router_model_decision",
    "router_route_decision",
    "plan_audit_skipped_by_risk",
    "planner_spawn_zero_tokens",
    "planner_inject_prompt_sidecar_written",
    "injected_prompt_sidecar_written",
    "injected_auditor_prompt_sidecar_written",
    "write_policy_allowed",
    "write_policy_wrapper_required",
    "write_policy_denied",
    "calibration_recovery_attempted",
    "sidecar_assert_skipped",
    "tdd_required_backfill_failed",
    "receipt_written",
    "receipt_missing",
    "stage_transition",
    "eventbus_handler",
    "maintenance_cycle",
    "scheduler_transition_refused",
    "scheduler_transition_race",
    "verify_signed_events_no_secret",
    "verify_signed_events_mismatch",
    # Per-auditor ensemble routing decision (router.py build_audit_plan).
    # Diagnostic trace — no gate or state machine depends on this event.
    "auditor_ensemble_decision",
    # PreToolUse hook diagnostics (pre_tool_use.py).
    # These are forensic traces — no gate or state machine depends on them.
    "pre_tool_use_role_missing",
    "pre_tool_use_bash_check",
    "pre_tool_use_role_file_missing",
    # Risk level override observability (ctl._normalize_classification_payload).
    # Forensic trace — records when an observed_floor overrides the planner's
    # risk_level upward. No gate or state machine blocks on this event.
    "risk_level_upgrade_blocked",
})


__all__ = [
    "DIAGNOSTIC_ONLY_EVENTS",
    "log_event",
    "sign_event",
    "verify_signed_events",
]

_WRITE_ROLE = "eventbus"

_EVENT_SECRET_CACHE: dict[str, str] = {}


def _resolve_event_secret(root: Path) -> str:
    """Resolve the HMAC-SHA256 secret used to sign events.

    Resolution order:
    (a) DYNOS_EVENT_SECRET env var — if non-empty, return it.
    (b) In-process cache keyed on str(root.resolve()).
    (c) Cache file at _persistent_project_dir(root)/'event-secret' with strict
        0o600 permission check.
    (d) Derive from sha256(f'{root.resolve()}:{platform.node()}')[:32], write
        atomically via os.open(O_WRONLY|O_CREAT|O_EXCL, 0o600), handle
        FileExistsError by re-reading (branch c).
    """
    env_secret = os.environ.get("DYNOS_EVENT_SECRET")
    if env_secret:
        return env_secret

    cache_key = str(root.resolve())

    if cache_key in _EVENT_SECRET_CACHE:
        return _EVENT_SECRET_CACHE[cache_key]

    cache_path = _persistent_project_dir(root) / "event-secret"

    if cache_path.exists():
        st_mode = os.stat(cache_path).st_mode & 0o777
        if st_mode != 0o600:
            raise ValueError(f"event-secret perms unsafe: {cache_path}")
        secret = cache_path.read_text(encoding="utf-8").strip()
        _EVENT_SECRET_CACHE[cache_key] = secret
        return secret

    # Derive a deterministic secret for this project+host combination.
    secret = hashlib.sha256(
        f"{root.resolve()}:{platform.node()}".encode("utf-8")
    ).hexdigest()[:32]

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(cache_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, secret.encode("utf-8"))
        finally:
            os.close(fd)
    except FileExistsError:
        # Another thread/process wrote the file concurrently — re-read it.
        st_mode = os.stat(cache_path).st_mode & 0o777
        if st_mode != 0o600:
            raise ValueError(f"event-secret perms unsafe: {cache_path}")
        secret = cache_path.read_text(encoding="utf-8").strip()

    _EVENT_SECRET_CACHE[cache_key] = secret
    return secret


def sign_event(payload: dict, secret: str) -> str:
    """Return the hex digest of HMAC-SHA256 over the canonical JSON of payload.

    The ``_sig`` key is excluded from the canonical serialization before
    computing the digest. The input payload is never mutated.

    Canonical form: ``json.dumps(filtered, sort_keys=True,
    separators=(",", ":"), ensure_ascii=False, default=str)``.
    """
    without_sig = {k: v for k, v in payload.items() if k != "_sig"}
    canonical = json.dumps(
        without_sig,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    digest = hmac.new(
        secret.encode("utf-8"),
        canonical,
        hashlib.sha256,
    ).hexdigest()
    return digest


def verify_signed_events(
    task_dir: Path,
    secret: str,
    *,
    strict: bool = False,
) -> list[dict]:
    """Read task_dir/events.jsonl and return only records whose _sig is valid.

    Parameters
    ----------
    task_dir:
        Directory containing ``events.jsonl``.
    secret:
        HMAC-SHA256 secret used to verify signatures.
    strict:
        When ``True``, any record with a missing or mismatched ``_sig``
        raises ``ValueError``.  When ``False`` (default), such records are
        silently excluded from the result.

    Special-case: when ``secret`` is empty or ``None``:
    - ``strict=True``: returns an empty list immediately (no records can be
      verified without a secret).
    - ``strict=False``: logs a ``verify_signed_events_no_secret`` event and
      returns all parseable records unchanged (documented fallback).
    """
    if not secret:
        if strict:
            # Cannot verify anything without a secret — return empty list.
            return []
        # Non-strict fallback: read all parseable records first, then log event.
        # Reading before logging ensures the no_secret event itself is not
        # included in the returned records.
        records: list[dict] = []
        events_path = task_dir / "events.jsonl"
        if events_path.exists():
            # OSError propagates to caller.
            with events_path.open("r", encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        record = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(record, dict):
                        records.append(record)
        log_event(
            task_dir.parent.parent,
            "verify_signed_events_no_secret",
            task=task_dir.name,
        )
        return records

    events_path = task_dir / "events.jsonl"
    if not events_path.exists():
        return []

    verified: list[dict] = []
    # OSError propagates to caller — receipt_post_completion wraps it in ValueError.
    with events_path.open("r", encoding="utf-8") as f:
        for n, raw in enumerate(f, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                if strict:
                    raise ValueError(
                        f"event signature invalid at line {n}: unparseable JSON"
                    )
                continue
            if not isinstance(record, dict):
                if strict:
                    raise ValueError(
                        f"event signature invalid at line {n}: not a JSON object"
                    )
                continue

            stored_sig = record.get("_sig")
            if stored_sig is None:
                reason = "missing_sig"
                if strict:
                    raise ValueError(
                        f"event signature invalid at line {n}: {reason}"
                    )
                log_event(
                    task_dir.parent.parent,
                    "verify_signed_events_mismatch",
                    task_dir=str(task_dir),
                    line_number=n,
                    reason=reason,
                )
                continue

            expected = sign_event(record, secret)
            if not hmac.compare_digest(expected, stored_sig):
                reason = "signature_mismatch"
                if strict:
                    raise ValueError(
                        f"event signature invalid at line {n}: {reason}"
                    )
                log_event(
                    task_dir.parent.parent,
                    "verify_signed_events_mismatch",
                    task_dir=str(task_dir),
                    line_number=n,
                    reason=reason,
                )
                continue

            verified.append(record)

    return verified


def _append_jsonl(path: Path, line: str, *, attempt: WriteAttempt) -> None:
    """Thread-safe append a single line to a JSONL file."""
    require_write_allowed(attempt, emit_event=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(line)
            f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def log_event(root: Path, event_type: str, *, task: str | None = None, **payload: Any) -> None:
    """Append one structured JSONL event to the task's events.jsonl.

    If `task` is provided and the task directory exists, writes to
    .dynos/{task}/events.jsonl (task-scoped). Otherwise writes to
    .dynos/events.jsonl (global fallback for daemon/system events).

    Args:
        root: Project root directory (contains .dynos/).
        event_type: Event name (e.g. "stage_transition", "router_model_decision").
        task: Optional task ID. When provided, events go to that task's log.
        **payload: Arbitrary key-value pairs merged into the JSON line.
    """
    try:
        record: dict[str, Any] = {"ts": now_iso(), "event": event_type}
        if task is not None:
            record["task"] = task
        record.update(payload)

        try:
            secret = _resolve_event_secret(root)
            record["_sig"] = sign_event(record, secret)
        except Exception as exc:
            print(
                f"[dynos-log] WARNING: _resolve_event_secret failed: {exc}",
                file=sys.stderr,
            )

        line = json.dumps(record, default=str, ensure_ascii=False) + "\n"

        # Task-scoped log when task ID is known and dir exists
        if task is not None:
            task_dir = root / ".dynos" / task
            if task_dir.is_dir():
                _append_jsonl(
                    task_dir / "events.jsonl",
                    line,
                    attempt=WriteAttempt(
                        role=_WRITE_ROLE,
                        task_dir=task_dir,
                        path=task_dir / "events.jsonl",
                        operation="modify" if (task_dir / "events.jsonl").exists() else "create",
                        source=_WRITE_ROLE,
                    ),
                )
                return

        # Global fallback
        global_path = root / ".dynos" / "events.jsonl"
        _append_jsonl(
            global_path,
            line,
            attempt=WriteAttempt(
                role="system",
                task_dir=None,
                path=global_path,
                operation="modify" if global_path.exists() else "create",
                source="system",
            ),
        )
    except Exception as exc:
        print(f"[dynos-log] WARNING: log_event failed: {exc}", file=sys.stderr)
