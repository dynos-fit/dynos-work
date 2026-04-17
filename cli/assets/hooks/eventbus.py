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
    """Run a subprocess. Returns True on success, False on failure."""
    env = {**os.environ, "PYTHONPATH": f"{SCRIPT_DIR}:{os.environ.get('PYTHONPATH', '')}"}
    try:
        result = subprocess.run(
            cmd,
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            timeout=300,  # 5 minute timeout per handler
        )
        if result.returncode != 0 and result.stderr:
            print(f"  [warn] {cmd[0]}: {result.stderr[:200]}", file=sys.stderr)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError) as e:
        print(f"  [warn] {cmd[0]}: {e}", file=sys.stderr)
        return False


def run_learn(root: Path, _payload: dict) -> bool:
    """Aggregate retrospectives into project memory (deterministic Python)."""
    # patterns.py does everything the learn skill does:
    # EMA effectiveness scores, model policy, skip policy, baseline policy,
    # agent routing table, prevention rules — all written to dynos_patterns.md
    return _run(
        ["python3", str(SCRIPT_DIR / "patterns.py"), "--root", str(root)],
        root,
    )


def run_trajectory(root: Path, _payload: dict) -> bool:
    """Rebuild trajectory store from all retrospectives."""
    return _run(
        ["python3", str(SCRIPT_DIR / "trajectory.py"), "rebuild", "--root", str(root)],
        root,
    )


def run_evolve(root: Path, _payload: dict) -> bool:
    """Deterministic learned agent generation."""
    return _run(
        ["python3", str(SCRIPT_DIR / "evolve.py"), "auto", "--root", str(root)],
        root,
    )


def run_patterns(root: Path, _payload: dict) -> bool:
    """Refresh patterns file from live runtime state."""
    return _run(
        ["python3", str(SCRIPT_DIR / "patterns.py"), "--root", str(root)],
        root,
    )


def run_postmortem(root: Path, _payload: dict) -> bool:
    """Generate automatic postmortem."""
    return _run(
        ["python3", str(SCRIPT_DIR / "postmortem.py"), "generate", "--root", str(root)],
        root,
    )


def run_improve(root: Path, _payload: dict) -> bool:
    """Run improvement cycle (project-local only)."""
    return _run(
        ["python3", str(SCRIPT_DIR / "postmortem.py"), "improve", "--root", str(root)],
        root,
    )


def run_benchmark(root: Path, _payload: dict) -> bool:
    """Auto-benchmark shadow challengers."""
    return _run(
        ["python3", str(SCRIPT_DIR / "auto.py"), "run", "--root", str(root)],
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


# ---------------------------------------------------------------------------
# Handler registry
# ---------------------------------------------------------------------------
# Each event type maps to a list of (consumer_name, handler_fn).
# Follow-on events are defined separately in the FOLLOW_ON dict.

HandlerEntry = tuple[str, Callable[[Path, dict], bool]]

HANDLERS: dict[str, list[HandlerEntry]] = {
    "task-completed": [
        ("learn", run_learn),
        ("trajectory", run_trajectory),
    ],
    "learn-completed": [
        ("evolve", run_evolve),
        ("patterns", run_patterns),
    ],
    "evolve-completed": [
        ("postmortem", run_postmortem),
        ("improve", run_improve),
        ("benchmark", run_benchmark),
    ],
    "benchmark-completed": [
        ("dashboard", run_dashboard),
        ("register", run_register),
    ],
}

# Maps event type to the follow-on event emitted when handlers complete
FOLLOW_ON: dict[str, str] = {
    "task-completed": "learn-completed",
    "learn-completed": "evolve-completed",
    "evolve-completed": "benchmark-completed",
}


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
    # Handlers that are part of the learning layer — skipped when learning is disabled.
    _LEARNING_HANDLERS = {"learn", "trajectory", "evolve", "patterns", "improve", "benchmark"}

    while iteration < max_iterations:
        iteration += 1
        processed_any = False
        # Track per-event-type: which handlers ran and whether each succeeded
        handler_results: dict[str, dict[str, bool]] = {}  # {event_type: {consumer: success}}

        for event_type, handlers in HANDLERS.items():
            for consumer_name, handler_fn in handlers:
                if not learning and consumer_name in _LEARNING_HANDLERS:
                    continue
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

                    # Run handler
                    err_msg = None
                    t0 = time.monotonic()
                    try:
                        success = handler_fn(root, payload)
                    except Exception as e:
                        err_msg = str(e)
                        print(f"  [warn] {consumer_name}: {e}", file=sys.stderr)
                        success = False

                    handler_results.setdefault(event_type, {})[consumer_name] = success

                    log_event(
                        root,
                        "eventbus_handler",
                        handler=consumer_name,
                        trigger_event=event_type,
                        success=success,
                        duration_s=round(time.monotonic() - t0, 3),
                        error=err_msg if not success else None,
                    )

                    # Mark as processed regardless of success
                    mark_processed(event_path, consumer_name)

                    # Track in summary
                    status = "ok" if success else "failed"
                    summary.setdefault(event_type, []).append(f"{consumer_name}:{status}")

            # Emit follow-on only when ALL handlers for this event type succeeded.
            # If learn fails but trajectory succeeds, learn-completed must NOT fire.
            if event_type in handler_results and event_type in FOLLOW_ON:
                results = handler_results[event_type]
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
        postmortem_ok = any(r.startswith("postmortem:ok") for r in summary.get("evolve-completed", []))
        patterns_ok = any(r.startswith("patterns:ok") for r in summary.get("learn-completed", []))
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
