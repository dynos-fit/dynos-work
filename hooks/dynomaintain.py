#!/usr/bin/env python3
"""Maintainer worker for dynos-work."""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import argparse
import fcntl
import json
import subprocess
import sys
import time
from pathlib import Path

from dynoslib_core import load_json, now_iso
from dynoslib_log import log_event


def maintenance_dir(root: Path) -> Path:
    return root / ".dynos" / "maintenance"


def status_path(root: Path) -> Path:
    return maintenance_dir(root) / "status.json"


def log_path(root: Path) -> Path:
    return maintenance_dir(root) / "cycles.jsonl"



def write_status(root: Path, payload: dict) -> None:
    path = status_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def run_python(root: Path, script_name: str, *args: str) -> tuple[subprocess.CompletedProcess[str], dict | None]:
    hooks_dir = Path(__file__).resolve().parent
    completed = subprocess.run(
        ["python3", str(hooks_dir / script_name), *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    payload = None
    if completed.stdout.strip():
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            payload = None
    return completed, payload


def maintenance_cycle(root: Path) -> dict:
    cycle_start = time.monotonic()
    lock_file = maintenance_dir(root) / "cycle.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = open(lock_file, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_fd.close()
        return {
            "executed_at": now_iso(),
            "ok": True,
            "skipped": True,
            "reason": "cycle lock held by another process",
            "actions": [],
        }
    try:
        actions: list[dict] = []
        for script_name, args in (
            ("dynostrajectory.py", ("rebuild", "--root", str(root))),
            ("dynopatterns.py", ("--root", str(root))),
            ("dynoevolve.py", ("auto", "--root", str(root))),
            ("dynopostmortem.py", ("generate-all", "--root", str(root))),
            ("dynopostmortem.py", ("improve", "--root", str(root))),
            ("dynofixture.py", ("sync", "--root", str(root))),
            ("dynoauto.py", ("run", "--root", str(root))),
            ("dynodashboard.py", ("generate", "--root", str(root))),
            ("dynoreport.py", ("--root", str(root))),
        ):
            completed, payload = run_python(root, script_name, *args)
            action = {
                "name": script_name,
                "returncode": completed.returncode,
            }
            if payload is not None:
                action["result"] = payload
            if completed.stderr.strip():
                action["stderr"] = completed.stderr.strip()
            actions.append(action)
        cycle = {
            "executed_at": now_iso(),
            "actions": actions,
            "ok": all(item["returncode"] == 0 for item in actions),
            "failed_steps": [a["name"] for a in actions if a["returncode"] != 0],
            "duration_steps": len(actions),
        }
        log_event(
            root,
            "maintenance_cycle",
            ok=cycle["ok"],
            failed_steps=cycle.get("failed_steps", []),
            step_count=cycle.get("duration_steps", 0),
            duration_s=round(time.monotonic() - cycle_start, 3),
        )
        lp = log_path(root)
        lp.parent.mkdir(parents=True, exist_ok=True)
        with open(lp, "a") as f:
            f.write(json.dumps(cycle) + "\n")
        try:
            cycle_count = sum(1 for _ in open(lp))
        except OSError:
            cycle_count = 1
        write_status(
            root,
            {
                "updated_at": now_iso(),
                "running": False,
                "last_cycle": cycle,
                "cycle_count": cycle_count,
            },
        )
        return cycle
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


def cmd_run_once(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    result = maintenance_cycle(root)
    print(json.dumps(result, indent=2))
    return 0


def cmd_invoke(args: argparse.Namespace) -> int:
    return cmd_run_once(args)


def cmd_status(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    payload = {"running": False, "pid": None}
    if status_path(root).exists():
        try:
            payload = load_json(status_path(root))
        except (json.JSONDecodeError, FileNotFoundError, OSError):
            payload = {"running": False, "pid": None}
    payload["running"] = False
    payload["pid"] = None
    # Summarize recent cycle history
    lp = log_path(root)
    if lp.exists():
        try:
            lines = lp.read_text().strip().splitlines()
            cycles = [json.loads(l) for l in lines if l.strip()]
            payload["cycle_count"] = len(cycles)
            recent = cycles[-5:]
            payload["recent_cycles"] = [
                {
                    "executed_at": c.get("executed_at"),
                    "ok": c.get("ok"),
                    "failed_steps": c.get("failed_steps", []),
                }
                for c in recent
            ]
            failures = sum(1 for c in cycles if not c.get("ok"))
            payload["total_failures"] = failures
        except (json.JSONDecodeError, OSError):
            pass
    print(json.dumps(payload, indent=2))
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    """Show recent maintenance cycle logs."""
    root = Path(args.root).resolve()
    lp = log_path(root)
    if not lp.exists():
        print(json.dumps({"cycles": [], "message": "No cycle logs yet"}))
        return 0
    try:
        lines = lp.read_text().strip().splitlines()
        cycles = [json.loads(l) for l in lines if l.strip()]
    except (json.JSONDecodeError, OSError) as e:
        print(json.dumps({"error": str(e)}))
        return 1
    n = int(args.last or 10)
    recent = cycles[-n:]
    print(json.dumps({"total_cycles": len(cycles), "showing": len(recent), "cycles": recent}, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_once = subparsers.add_parser("run-once", help="Run one maintenance cycle")
    run_once.add_argument("--root", default=".")
    run_once.set_defaults(func=cmd_run_once)

    invoke = subparsers.add_parser("invoke", help="Manual maintainer trigger for one maintenance cycle")
    invoke.add_argument("--root", default=".")
    invoke.set_defaults(func=cmd_invoke)

    status = subparsers.add_parser("status", help="Show maintainer status")
    status.add_argument("--root", default=".")
    status.set_defaults(func=cmd_status)

    logs = subparsers.add_parser("logs", help="Show recent maintenance cycle logs")
    logs.add_argument("--root", default=".")
    logs.add_argument("--last", default="10", help="Number of recent cycles to show (default: 10)")
    logs.set_defaults(func=cmd_logs)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
