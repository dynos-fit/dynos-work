#!/usr/bin/env python3
"""Event bus drain runner for dynos-work.

Processes events emitted by pipelines. Each handler wraps an existing
subprocess call. Handlers emit follow-on events, which the drain loop
picks up on the next iteration. All errors are swallowed (matching the
previous || true behavior).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib_events import (
    cleanup_old_events,
    consume_events,
    emit_event,
    mark_processed,
)
from lib_log import log_event

SCRIPT_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Handler functions
# ---------------------------------------------------------------------------

# Per-handler timeout budget (seconds). The blanket 300s value covered every
# handler regardless of expected runtime, so a hung lightweight handler could
# block the drain for 5 minutes before a TimeoutExpired surfaced.
#
# Budget rationale (informed by drain telemetry — see _drain_locked's
# summary["_durations"] aggregate):
# - register / dashboard: pure file I/O, p99 well under 5s; tight cap
# - policy_engine: reads retrospectives, computes EMA scores; ~30s headroom
# - improve / agent_generator / postmortem: scan retrospectives + write artifacts;
#   medium-weight learning passes
# - DEFAULT: applied to handlers not explicitly classified
_HANDLER_TIMEOUTS: dict[str, int] = {
    "register": 15,
    "dashboard": 30,
    "policy_engine": 60,
    "postmortem": 60,
    "improve": 90,
    "agent_generator": 90,
}
_DEFAULT_HANDLER_TIMEOUT = 60


def _run(cmd: list[str], root: Path, timeout: int = _DEFAULT_HANDLER_TIMEOUT) -> bool:
    """Run a subprocess. Returns True on success, False on failure.

    Non-zero exit: raise RuntimeError with stderr snippet so the drain loop
    captures a real error message in its log_event call, instead of the
    silent success=False / error=null rows that masked the broken
    registry.py import in 2026-04.
    """
    env = {**os.environ, "PYTHONPATH": f"{SCRIPT_DIR}:{os.environ.get('PYTHONPATH', '')}"}
    try:
        result = subprocess.run(
            cmd,
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError) as e:
        raise RuntimeError(f"{cmd[0]}: {e}") from e
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RuntimeError(f"{cmd[0]} exit={result.returncode}: {stderr[:300]}" if stderr else f"{cmd[0]} exit={result.returncode}")
    return True


def run_policy_engine(root: Path, _payload: dict) -> bool:
    """Compute EMA scores and write routing policies from retrospectives."""
    return _run(
        ["python3", str(SCRIPT_DIR / "patterns.py"), "--root", str(root)],
        root,
        timeout=_HANDLER_TIMEOUTS["policy_engine"],
    )


def run_postmortem(root: Path, _payload: dict) -> bool:
    """Generate human-readable postmortem report."""
    return _run(
        ["python3", str(SCRIPT_DIR / "postmortem.py"), "generate", "--root", str(root)],
        root,
        timeout=_HANDLER_TIMEOUTS["postmortem"],
    )


_DASHBOARD_DEBOUNCE_SECONDS = 30


def run_dashboard(root: Path, _payload: dict) -> bool:
    """Refresh live dashboard artifacts.

    Debounced: dashboard freshness within ~30s is indistinguishable to a
    human refreshing the page. Bursty completions (e.g., a batch of
    related tasks finishing back-to-back) used to regenerate the
    dashboard once per task — pure waste. Skip if the output file was
    modified within _DASHBOARD_DEBOUNCE_SECONDS.
    """
    data_path = root / ".dynos" / "dashboard-data.json"
    if data_path.exists():
        try:
            age = time.time() - data_path.stat().st_mtime
            if age < _DASHBOARD_DEBOUNCE_SECONDS:
                return True  # Skip — recent enough
        except OSError:
            pass
    return _run(
        ["python3", str(SCRIPT_DIR / "dashboard.py"), "generate", "--root", str(root)],
        root,
        timeout=_HANDLER_TIMEOUTS["dashboard"],
    )


def run_register(root: Path, _payload: dict) -> bool:
    """Mark project active in global registry."""
    return _run(
        ["python3", str(SCRIPT_DIR / "registry.py"), "set-active", str(root)],
        root,
        timeout=_HANDLER_TIMEOUTS["register"],
    )


def run_improve(root: Path, payload: dict) -> bool:
    """Run the auto-improvement engine on postmortem data.

    Payload-aware skip: the improvement engine derives prevention rules
    from finding categories and repair patterns. A task with zero findings
    AND zero repair cycles offers no signal to learn from; skip the
    subprocess spawn entirely. Saves ~50-300ms of Python interpreter
    startup + import on the common clean-task path.
    """
    task_dir_str = (payload or {}).get("task_dir") if isinstance(payload, dict) else None
    if task_dir_str:
        try:
            from lib_core import load_json
            retro = load_json(Path(task_dir_str) / "task-retrospective.json")
            findings_total = sum(retro.get("findings_by_auditor", {}).values())
            if findings_total == 0 and retro.get("repair_cycle_count", 0) == 0:
                return True  # Nothing to learn from
        except (FileNotFoundError, OSError, Exception):
            # Any error — fall through to the actual handler. Better to
            # spend the 300ms than silently skip improvement.
            pass
    return _run(
        ["python3", str(SCRIPT_DIR / "postmortem_improve.py"), "improve", "--root", str(root)],
        root,
        timeout=_HANDLER_TIMEOUTS["improve"],
    )


def run_agent_generator(root: Path, _payload: dict) -> bool:
    """Discover uncovered (role, task_type) slots and generate shadow agents."""
    return _run(
        ["python3", str(SCRIPT_DIR / "agent_generator.py"), "auto", "--root", str(root)],
        root,
        timeout=_HANDLER_TIMEOUTS["agent_generator"],
    )


# ---------------------------------------------------------------------------
# Handler registry
# ---------------------------------------------------------------------------
# Flat chain: all handlers fire on task-completed.
# Built-in handlers are defined above. Additional handlers can be
# auto-discovered from hooks/handlers/*.py — each module must export:
#   EVENT_TYPE: str  (e.g. "task-completed")
#   def run(root: Path, payload: dict) -> bool

import importlib.util

HandlerEntry = tuple[str, Callable[[Path, dict], bool]]

_BUILTIN_HANDLERS: dict[str, list[HandlerEntry]] = {
    "task-completed": [
        ("improve", run_improve),
        ("agent_generator", run_agent_generator),
        ("policy_engine", run_policy_engine),
        ("dashboard", run_dashboard),
        ("register", run_register),
    ],
}


def _discover_handlers() -> dict[str, list[HandlerEntry]]:
    """Auto-discover handler modules from hooks/handlers/*.py.

    Each module must export EVENT_TYPE (str) and run(root, payload) -> bool.
    Merges with built-in handlers. Falls back to built-ins only if
    the handlers/ directory doesn't exist or is empty.
    """
    handlers = {k: list(v) for k, v in _BUILTIN_HANDLERS.items()}
    handlers_dir = SCRIPT_DIR / "handlers"
    if not handlers_dir.is_dir():
        return handlers
    for path in sorted(handlers_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(path.stem, path)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            event_type = getattr(mod, "EVENT_TYPE", None)
            run_fn = getattr(mod, "run", None)
            if event_type and callable(run_fn):
                handlers.setdefault(event_type, []).append((path.stem, run_fn))
        except Exception as exc:
            print(f"  [warn] handler discovery: {path.name}: {exc}", file=sys.stderr)
    return handlers


HANDLERS: dict[str, list[HandlerEntry]] = _discover_handlers()

# No follow-on events needed — everything fires from task-completed directly.
FOLLOW_ON: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Drain loop
# ---------------------------------------------------------------------------

def drain(root: Path, max_iterations: int = 10) -> dict:
    """Process all pending events until the queue is drained.

    Returns a summary dict of what ran.

    Concurrency: protected by an exclusive fcntl lock on
    .dynos/events/drain.lock. If another drain process holds the lock,
    this call exits immediately with summary {"skipped": ["lock held"]}.
    The running drain will pick up any events emitted before this call
    on its next iteration. This prevents the duplicate-handler-invocation
    race introduced when _fire_task_completed() became async — without
    the lock, two near-simultaneous DONE transitions would spawn two
    parallel drain processes, both consume_events() the same unprocessed
    event, both invoke the handler, then both mark_processed (which is
    idempotent on the field but the handler still ran twice).
    """
    import fcntl

    lock_dir = root / ".dynos" / "events"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "drain.lock"
    lock_fh = open(lock_path, "w")
    try:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_fh.close()
            return {"skipped": ["another drain is already running; new events will be picked up on its next iteration"]}
        return _drain_locked(root, max_iterations)
    finally:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        except (OSError, ValueError):
            pass
        try:
            lock_fh.close()
        except OSError:
            pass


def _compute_policy_hash(root: Path) -> str:
    """Compute an aggregate sha256 over CALIBRATION_POLICY_FILES.

    For each file in CALIBRATION_POLICY_FILES (evaluated under
    `_persistent_project_dir(root)`):
    - if the file exists: hash its contents with `hash_file`
    - if missing: contribute the literal string ``"none"``

    The final digest is `sha256(concat of per-file hashes)` — concatenating
    the per-file digests in list order so the combined hash is deterministic
    and order-sensitive. Returns a lowercase hex string.
    """
    import hashlib as _hashlib
    from lib_core import _persistent_project_dir
    from lib_receipts import CALIBRATION_POLICY_FILES, hash_file

    project_dir = _persistent_project_dir(root)
    parts: list[str] = []
    for rel in CALIBRATION_POLICY_FILES:
        fp = project_dir / rel
        try:
            if fp.is_file():
                parts.append(hash_file(fp))
            else:
                parts.append("none")
        except (OSError, FileNotFoundError):
            # Race between is_file() and open — treat as missing.
            parts.append("none")
    combined = _hashlib.sha256("".join(parts).encode("utf-8")).hexdigest()
    return combined


def _drain_locked(root: Path, max_iterations: int) -> dict:
    """Drain implementation; runs only when the drain.lock is held."""
    summary: dict[str, list[str]] = {}
    iteration = 0
    emitted_follow_ons: set[str] = set()  # track across ALL iterations to prevent duplicates
    completed_task_dirs: list[str] = []  # ALL task dirs from task-completed events
    # Per-handler duration tracking for telemetry (#M3): each entry is a list
    # of seconds across every invocation in this drain. Aggregated into
    # summary["_durations"] before return.
    handler_durations: dict[str, list[float]] = {}

    from lib_core import is_learning_enabled
    learning = is_learning_enabled(root)
    # Learning handlers — skipped when learning is disabled. Calibration +
    # CALIBRATED transition are only attempted when EVERY handler in this
    # set succeeds for a given task_dir (tracked via handler_all_ok_per_task).
    # postmortem, dashboard, register always run regardless.
    _LEARNING_HANDLERS = {"policy_engine", "improve", "agent_generator"}

    # Per-task success map — AC 22. One drain call can process multiple
    # `task-completed` events (one per task), so a single drain-wide
    # (event_type, consumer) success map is insufficient: a handler may
    # succeed on task A's event and fail on task B's. Key by task_dir string
    # (mirrors payload["task_dir"]) so each task's CALIBRATION gate is
    # evaluated independently.
    handler_all_ok_per_task: dict[str, dict[str, bool]] = {}

    # Track consumers that failed during this drain call — don't retry them
    # in subsequent iterations. Retries happen on the NEXT drain() invocation.
    failed_this_drain: set[tuple[str, str]] = set()  # {(event_type, consumer)}

    # AC 22(a): snapshot the aggregate policy hash BEFORE any learning
    # handlers run. Captured once per drain call. If compute fails (unlikely
    # — all errors are swallowed into the "none" sentinel inside
    # _compute_policy_hash), fall back to a deterministic placeholder so a
    # later receipt can still be written.
    try:
        policy_sha256_before = _compute_policy_hash(root)
    except Exception as exc:
        print(f"  [warn] policy hash (before) failed: {exc}", file=sys.stderr)
        policy_sha256_before = "none"

    while iteration < max_iterations:
        iteration += 1
        processed_any = False
        emitted_count_at_iter_start = len(emitted_follow_ons)
        # Track per-event-type: whether ALL handlers succeeded across ALL events
        # Uses AND semantics: any failure for a consumer sticks (later success doesn't override)
        handler_all_ok: dict[str, dict[str, bool]] = {}  # {event_type: {consumer: all_succeeded}}

        for event_type, handlers in HANDLERS.items():
            for consumer_name, handler_fn in handlers:
                # Skip consumers that already failed this drain — retry on next drain() call
                if (event_type, consumer_name) in failed_this_drain:
                    continue
                # When learning is disabled, skip the handler execution but still
                # consume and mark events processed so the chain continues to
                # non-learning handlers downstream (dashboard, register, postmortem)
                skip_execution = not learning and consumer_name in _LEARNING_HANDLERS
                try:
                    events = consume_events(root, event_type, consumer_name)
                except Exception as e:
                    print(f"  [warn] consume_events({event_type}, {consumer_name}): {e}", file=sys.stderr)
                    continue
                for event_path, event_data in events:
                    processed_any = True
                    payload = event_data.get("payload", {})

                    # Capture task identity from task-completed events
                    task_dir_str: str | None = None
                    if event_type == "task-completed" and isinstance(payload, dict):
                        td = payload.get("task_dir")
                        if isinstance(td, str) and td:
                            task_dir_str = td
                            if td not in completed_task_dirs:
                                completed_task_dirs.append(td)

                    # If this (event_type, consumer) already failed on an
                    # earlier event in this drain, don't re-invoke the handler
                    # for remaining backlog events — same failure mode, same
                    # failed_this_drain entry, just N amplified log rows.
                    if (event_type, consumer_name) in failed_this_drain:
                        break

                    # Run handler (or skip if learning disabled)
                    if skip_execution:
                        success = True
                        err_msg = None
                    else:
                        err_msg = None
                        t0 = time.monotonic()
                        try:
                            success = handler_fn(root, payload)
                        except Exception as e:
                            err_msg = str(e)
                            print(f"  [warn] {consumer_name}: {e}", file=sys.stderr)
                            success = False
                        duration = round(time.monotonic() - t0, 3)
                        handler_durations.setdefault(consumer_name, []).append(duration)

                        log_event(
                            root,
                            "eventbus_handler",
                            handler=consumer_name,
                            trigger_event=event_type,
                            success=success,
                            duration_s=duration,
                            error=err_msg if not success else None,
                        )

                    # Track success with AND semantics: any failure sticks
                    prev = handler_all_ok.setdefault(event_type, {}).get(consumer_name, True)
                    handler_all_ok[event_type][consumer_name] = prev and success

                    # Per-task success map (AC 22). Only meaningful for
                    # task-completed events where payload carries task_dir.
                    # AND-semantics: once a handler fails for a task it stays
                    # failed, even if a later re-run somehow succeeds.
                    if task_dir_str is not None:
                        per_task = handler_all_ok_per_task.setdefault(task_dir_str, {})
                        prev_t = per_task.get(consumer_name, True)
                        per_task[consumer_name] = prev_t and success

                    # Only mark processed on success — failed events stay for retry
                    if success:
                        mark_processed(event_path, consumer_name)
                    else:
                        failed_this_drain.add((event_type, consumer_name))

                    # Track in summary
                    status = "ok" if success else "failed"
                    summary.setdefault(event_type, []).append(f"{consumer_name}:{status}")

            # Emit follow-on only when ALL active handlers for this event type succeeded.
            if event_type in handler_all_ok and event_type in FOLLOW_ON:
                results = handler_all_ok[event_type]
                all_succeeded = all(results.values())
                if all_succeeded:
                    follow_on = FOLLOW_ON[event_type]
                    if follow_on not in emitted_follow_ons:
                        emit_event(root, follow_on, "eventbus")
                        emitted_follow_ons.add(follow_on)
                else:
                    failed = [k for k, v in results.items() if not v]
                    print(f"  [gate] {event_type} follow-on blocked — failed: {', '.join(failed)}", file=sys.stderr)


        if not processed_any:
            break
        # M1: short-circuit further iterations when no new events were
        # emitted during this iteration. Iteration N processes existing
        # events; iteration N+1 only matters if N emitted follow-on events
        # for N+1 to consume. With FOLLOW_ON currently empty, the second
        # iteration is pure overhead — N globs + N event reads to confirm
        # there's nothing left. Break out as soon as the iteration was a
        # no-op for the future.
        if len(emitted_follow_ons) == emitted_count_at_iter_start:
            break

    # M3: aggregate per-handler durations so callers can see actual cost
    # without scraping events.jsonl. Lightweight stats (count/min/median/max)
    # — full distribution lives in the per-handler log_event records.
    if handler_durations:
        durations_summary: dict[str, dict[str, float | int]] = {}
        for handler, samples in handler_durations.items():
            samples_sorted = sorted(samples)
            mid = len(samples_sorted) // 2
            median = (
                samples_sorted[mid] if len(samples_sorted) % 2
                else (samples_sorted[mid - 1] + samples_sorted[mid]) / 2
            )
            durations_summary[handler] = {
                "count": len(samples),
                "min_s": round(samples_sorted[0], 3),
                "median_s": round(median, 3),
                "max_s": round(samples_sorted[-1], 3),
                "total_s": round(sum(samples), 3),
            }
        summary["_durations"] = durations_summary  # type: ignore[assignment]

    # Cleanup old events
    cleanup_old_events(root)

    # Write post-completion receipt for EACH completed task.
    #
    # v2 contract (AC 20): signature is (task_dir, handlers_run) only —
    # postmortem_written / patterns_updated moved to dedicated receipts
    # (receipt_postmortem_* / receipt_calibration_applied below).
    #
    # Calibration gate (AC 22): after the post-completion receipt lands, if
    # ALL _LEARNING_HANDLERS succeeded for this task (per the per-task
    # success map) AND learning is enabled, compute a before/after policy
    # hash snapshot, write a `calibration-applied` receipt, then transition
    # the task to CALIBRATED. Any missing or failed learning handler for
    # this task short-circuits: no receipt, no transition.
    if "task-completed" in summary and completed_task_dirs:
        # Snapshot the aggregate learning-policy hash BEFORE writing any
        # receipts. The handlers already ran by this point — this hash
        # reflects the post-learning state on disk. We capture `before` at
        # drain start (below), `after` here.
        handlers_run = []
        for evt_type, results in summary.items():
            if evt_type.startswith("_"):
                # Reserved keys for non-event metadata (e.g. _durations).
                continue
            for r in results:
                name, status = r.split(":", 1)
                handlers_run.append({"name": name, "success": status == "ok", "event": evt_type})

        # AC 22(a): retros_consumed / scores_updated are derived from the
        # handlers_run list — policy_engine (patterns.py) scores retros and
        # writes the effectiveness/route/skip/model policies; improve
        # aggregates postmortem repair patterns into prevention rules. There
        # is no reliable counter emitted by those subprocess handlers today,
        # so we use conservative binary-derived integers:
        #   retros_consumed  = 1 if policy_engine or improve succeeded else 0
        #   scores_updated   = 1 if policy_engine succeeded else 0
        # This satisfies the "0 is acceptable when indeterminable" clause
        # while still distinguishing the no-retro and fully-skipped paths
        # for audit trail purposes. A future follow-up may plumb real counts
        # through the handler return.
        pe_ok = any(r.startswith("policy_engine:ok") for r in summary.get("task-completed", []))
        improve_ok = any(r.startswith("improve:ok") for r in summary.get("task-completed", []))
        retros_consumed = 1 if (pe_ok or improve_ok) else 0
        scores_updated = 1 if pe_ok else 0

        # Compute `after` policy hash once (cheap: <=10 small-ish files,
        # most hits are missing files returning "none").
        policy_sha256_after: str | None = None
        try:
            policy_sha256_after = _compute_policy_hash(root)
        except Exception as exc:
            print(f"  [warn] policy hash (after) failed: {exc}", file=sys.stderr)

        for td in completed_task_dirs:
            task_dir = Path(td)
            if not task_dir.exists():
                continue
            try:
                from lib_receipts import (
                    receipt_post_completion,
                    receipt_calibration_applied,
                    receipt_calibration_noop,
                )
                receipt_post_completion(
                    task_dir,
                    handlers_run=handlers_run,
                )
            except Exception as exc:
                print(f"  [warn] post-completion receipt failed for {td}: {exc}", file=sys.stderr)
                # Skip calibration for this task — no post-completion base.
                continue

            # AC 22 calibration gate — per-task short-circuit.
            if not learning:
                continue
            per_task = handler_all_ok_per_task.get(td, {})
            all_learning_ok = all(
                per_task.get(h) is True for h in _LEARNING_HANDLERS
            )
            if not all_learning_ok:
                continue
            if policy_sha256_after is None:
                # Hash compute failed earlier — don't fabricate a receipt.
                continue

            # AC 20: per-task retros_count from the task's retrospective.
            # The handler return values are booleans today (they don't
            # thread real consumption counts up), so we derive retros_count
            # from the artifact that policy_engine/improve would actually
            # read: task-retrospective.json under the task dir. This is
            # the simplest signal with no plumbing changes.
            #
            # retros_count > 0  ⇔ retrospective exists with >=1 finding
            #                     OR >=1 repair cycle — i.e. something
            #                     learning handlers could have consumed.
            # retros_count == 0 ⇔ retrospective missing, unreadable, or
            #                     reports zero findings + zero repairs.
            retros_count = 0
            try:
                from lib_core import load_json as _load_json
                retro_path = task_dir / "task-retrospective.json"
                if retro_path.is_file():
                    retro = _load_json(retro_path)
                    if isinstance(retro, dict):
                        fba = retro.get("findings_by_auditor", {})
                        findings_total = (
                            sum(fba.values())
                            if isinstance(fba, dict) else 0
                        )
                        repairs = retro.get("repair_cycle_count", 0)
                        if not isinstance(repairs, int):
                            repairs = 0
                        if findings_total > 0 or repairs > 0:
                            retros_count = 1
            except Exception as exc:
                # Unreadable retrospective — treat as zero signal.
                # Never fabricate a non-zero count from uncertainty.
                print(
                    f"  [warn] retros_count derivation failed for {td}: {exc}",
                    file=sys.stderr,
                )
                retros_count = 0

            # AC 20: branch between no-op and applied.
            # Preconditions already verified above:
            #   - all learning handlers succeeded for THIS task
            #   - policy_sha256_after was computed (non-None)
            hash_unchanged = (policy_sha256_before == policy_sha256_after)

            try:
                if hash_unchanged and retros_count == 0:
                    # No retros to consume AND policy unchanged — the
                    # nominal "nothing to calibrate" path. Write a
                    # calibration-noop receipt so the receipt chain still
                    # proves the learning gate fired and made a decision.
                    receipt_calibration_noop(
                        task_dir,
                        reason="no-retros",
                        policy_sha256=policy_sha256_after,
                    )
                elif hash_unchanged and retros_count > 0:
                    # Retros existed but handlers produced no policy
                    # delta (all-handlers-zero-work). Still a no-op at
                    # the policy level; distinguish the reason so audit
                    # trails can see *why* no change landed.
                    receipt_calibration_noop(
                        task_dir,
                        reason="all-handlers-zero-work",
                        policy_sha256=policy_sha256_after,
                    )
                else:
                    # Real policy delta (or, defensively, any case we
                    # didn't classify as a no-op above). Write the
                    # applied receipt. Seg-1 added refuse-on-no-op to
                    # this writer; branching above guarantees we don't
                    # trigger that refuse.
                    receipt_calibration_applied(
                        task_dir,
                        retros_consumed=retros_consumed,
                        scores_updated=scores_updated,
                        policy_sha256_before=policy_sha256_before,
                        policy_sha256_after=policy_sha256_after,
                    )
            except Exception as exc:
                print(
                    f"  [warn] calibration receipt failed for {td}: {exc}",
                    file=sys.stderr,
                )
                continue

            # AC 22(c): transition to CALIBRATED. Idempotency is enforced by
            # load_json + ALLOWED_STAGE_TRANSITIONS — if already CALIBRATED,
            # transition_task raises and we swallow it.
            try:
                from lib_core import transition_task, load_json
                manifest = load_json(task_dir / "manifest.json")
                if manifest.get("stage") == "CALIBRATED":
                    continue
                transition_task(task_dir, "CALIBRATED")
            except Exception as exc:
                print(
                    f"  [warn] CALIBRATED transition failed for {td}: {exc}",
                    file=sys.stderr,
                )

    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_drain(args) -> int:
    """Drain all pending events.

    --sync --task-dir <dir>  runs the drain in-process (no Popen detach),
    blocks until it returns, and exits non-zero if any _LEARNING_HANDLERS
    failed for that task (AC 24). Idempotent: if the task is already at
    CALIBRATED the drain is a no-op and exits 0.
    """
    task_dir_arg: str | None = getattr(args, "task_dir", None)
    sync_mode: bool = bool(getattr(args, "sync", False))

    # Resolve root from the explicit --root or, when --sync --task-dir is
    # supplied, from the parent of parents (.dynos/task-xxx -> repo root).
    if task_dir_arg:
        task_dir = Path(task_dir_arg).resolve()
        if sync_mode:
            # AC 24(d): idempotency — if already CALIBRATED, no-op return 0.
            try:
                from lib_core import load_json
                manifest_path = task_dir / "manifest.json"
                if manifest_path.is_file():
                    manifest = load_json(manifest_path)
                    if manifest.get("stage") == "CALIBRATED":
                        print(f"  task {task_dir.name} already CALIBRATED — no-op")
                        return 0
            except Exception as exc:
                print(f"  [warn] idempotency check failed: {exc}", file=sys.stderr)
                # Fall through — we'd rather attempt the drain than silently
                # exit 0 on a bad manifest.
        root = task_dir.parent.parent
    else:
        root = Path(args.root).resolve()

    summary = drain(root, max_iterations=args.max_iterations)

    if summary:
        for event_type, results in summary.items():
            for result in results:
                print(f"  {event_type}: {result}")
    else:
        print("  No events to process")

    # AC 24(c): exit non-zero when any _LEARNING_HANDLERS failed for the
    # requested task in the sync run. The learning set is duplicated here
    # so we don't have to reach into _drain_locked's closure.
    if sync_mode and task_dir_arg:
        learning_names = {"policy_engine", "improve", "agent_generator"}
        # summary values are "name:status" strings grouped by event type.
        task_completed_results = summary.get("task-completed", [])
        failed_learning: list[str] = []
        for entry in task_completed_results:
            try:
                name, status = entry.split(":", 1)
            except ValueError:
                continue
            if name in learning_names and status != "ok":
                failed_learning.append(name)
        if failed_learning:
            print(
                f"  [fail] learning handlers failed: {', '.join(sorted(set(failed_learning)))}",
                file=sys.stderr,
            )
            return 1

    return 0


def build_parser():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    drain_p = sub.add_parser("drain", help="Process all pending events")
    drain_p.add_argument("--root", default=".")
    drain_p.add_argument("--max-iterations", type=int, default=10)
    drain_p.add_argument(
        "--sync",
        action="store_true",
        help="Run drain in-process (no Popen detach); blocks until done. "
             "Exits non-zero if any learning handler failed for --task-dir.",
    )
    drain_p.add_argument(
        "--task-dir",
        default=None,
        help="Path to the task directory for --sync idempotency and failure "
             "reporting. Required by --sync semantics.",
    )
    drain_p.set_defaults(func=cmd_drain)

    return parser


if __name__ == "__main__":
    from cli_base import cli_main
    raise SystemExit(cli_main(build_parser))
