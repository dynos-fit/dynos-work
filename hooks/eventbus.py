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

def _run(cmd: list[str], root: Path) -> bool:
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
            timeout=300,
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
    )


def run_postmortem(root: Path, _payload: dict) -> bool:
    """Generate human-readable postmortem report."""
    return _run(
        ["python3", str(SCRIPT_DIR / "postmortem.py"), "generate", "--root", str(root)],
        root,
    )


def run_dashboard(root: Path, _payload: dict) -> bool:
    """Refresh live dashboard artifacts."""
    return _run(
        ["python3", str(SCRIPT_DIR / "dashboard.py"), "generate", "--root", str(root)],
        root,
    )


def run_register(root: Path, _payload: dict) -> bool:
    """Mark project active in global registry."""
    return _run(
        ["python3", str(SCRIPT_DIR / "registry.py"), "set-active", str(root)],
        root,
    )


def run_improve(root: Path, _payload: dict) -> bool:
    """Run the auto-improvement engine on postmortem data."""
    return _run(
        ["python3", str(SCRIPT_DIR / "postmortem_improve.py"), "improve", "--root", str(root)],
        root,
    )


def run_agent_generator(root: Path, _payload: dict) -> bool:
    """Discover uncovered (role, task_type) slots and generate shadow agents."""
    return _run(
        ["python3", str(SCRIPT_DIR / "agent_generator.py"), "auto", "--root", str(root)],
        root,
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
    """
    summary: dict[str, list[str]] = {}
    iteration = 0
    emitted_follow_ons: set[str] = set()  # track across ALL iterations to prevent duplicates
    completed_task_dirs: list[str] = []  # ALL task dirs from task-completed events

    from lib_core import is_learning_enabled
    learning = is_learning_enabled(root)
    # policy_engine is the only learning handler — skipped when learning is disabled.
    # postmortem, dashboard, register always run regardless.
    _LEARNING_HANDLERS = {"policy_engine", "improve", "agent_generator", "benchmark_scheduler"}

    # Track consumers that failed during this drain call — don't retry them
    # in subsequent iterations. Retries happen on the NEXT drain() invocation.
    failed_this_drain: set[tuple[str, str]] = set()  # {(event_type, consumer)}

    while iteration < max_iterations:
        iteration += 1
        processed_any = False
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
                    if event_type == "task-completed" and isinstance(payload, dict):
                        td = payload.get("task_dir")
                        if td and td not in completed_task_dirs:
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

                        log_event(
                            root,
                            "eventbus_handler",
                            handler=consumer_name,
                            trigger_event=event_type,
                            success=success,
                            duration_s=round(time.monotonic() - t0, 3),
                            error=err_msg if not success else None,
                        )

                    # Track success with AND semantics: any failure sticks
                    prev = handler_all_ok.setdefault(event_type, {}).get(consumer_name, True)
                    handler_all_ok[event_type][consumer_name] = prev and success

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

    # Cleanup old events
    cleanup_old_events(root)

    # Write post-completion receipt for EACH completed task
    if "task-completed" in summary and completed_task_dirs:
        handlers_run = []
        for evt_type, results in summary.items():
            for r in results:
                name, status = r.split(":", 1)
                handlers_run.append({"name": name, "success": status == "ok", "event": evt_type})
        postmortem_ok = any(r.startswith("postmortem:ok") for r in summary.get("calibration-completed", []))
        patterns_ok = any(r.startswith("patterns:ok") for r in summary.get("memory-completed", []))
        for td in completed_task_dirs:
            try:
                from lib_receipts import receipt_post_completion
                task_dir = Path(td)
                if task_dir.exists():
                    receipt_post_completion(
                        task_dir,
                        handlers_run=handlers_run,
                        postmortem_written=postmortem_ok,
                        patterns_updated=patterns_ok,
                    )
            except Exception as exc:
                print(f"  [warn] post-completion receipt failed for {td}: {exc}", file=sys.stderr)

    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_drain(args) -> int:
    """Drain all pending events."""
    root = Path(args.root).resolve()
    summary = drain(root, max_iterations=args.max_iterations)

    if summary:
        for event_type, results in summary.items():
            for result in results:
                print(f"  {event_type}: {result}")
    else:
        print("  No events to process")

    return 0


def build_parser():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    drain_p = sub.add_parser("drain", help="Process all pending events")
    drain_p.add_argument("--root", default=".")
    drain_p.add_argument("--max-iterations", type=int, default=10)
    drain_p.set_defaults(func=cmd_drain)

    return parser


if __name__ == "__main__":
    from cli_base import cli_main
    raise SystemExit(cli_main(build_parser))
