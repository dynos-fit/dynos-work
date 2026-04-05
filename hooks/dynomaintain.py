#!/usr/bin/env python3
"""Persistent maintainer worker for dynos-work."""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from dynoslib import load_json, now_iso, _persistent_project_dir


def maintenance_dir(root: Path) -> Path:
    return root / ".dynos" / "maintenance"


def status_path(root: Path) -> Path:
    return maintenance_dir(root) / "status.json"


def pid_path(root: Path) -> Path:
    return maintenance_dir(root) / "daemon.pid"


def stop_path(root: Path) -> Path:
    return maintenance_dir(root) / "stop"


def log_path(root: Path) -> Path:
    return maintenance_dir(root) / "cycles.jsonl"


def policy_path(root: Path) -> Path:
    return _persistent_project_dir(root) / "policy.json"


def maintainer_policy(root: Path) -> dict:
    default = {
        "maintainer_autostart": False,
        "maintainer_poll_seconds": 3600,
    }
    path = policy_path(root)
    if not path.exists() or not path.read_text().strip():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(default, indent=2) + "\n")
        return default
    try:
        data = load_json(path)
    except (json.JSONDecodeError, FileNotFoundError, OSError):
        path.write_text(json.dumps(default, indent=2) + "\n")
        return default
    merged = dict(default)
    if isinstance(data.get("maintainer_autostart"), bool):
        merged["maintainer_autostart"] = data["maintainer_autostart"]
    if isinstance(data.get("maintainer_poll_seconds"), int) and data["maintainer_poll_seconds"] > 0:
        merged["maintainer_poll_seconds"] = data["maintainer_poll_seconds"]
    if merged != data:
        path.write_text(json.dumps({**data, **merged}, indent=2) + "\n")
    return merged


def is_pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def current_pid(root: Path) -> int | None:
    path = pid_path(root)
    if not path.exists():
        return None
    try:
        pid = int(path.read_text().strip())
    except (ValueError, OSError):
        return None
    return pid if is_pid_running(pid) else None


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
                "pid": current_pid(root),
            },
        )
        return cycle
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


def _autofix_flag_path(root: Path) -> Path:
    return maintenance_dir(root) / "autofix.enabled"


def _is_autofix_enabled(root: Path) -> bool:
    return _autofix_flag_path(root).exists()


def _run_autofix(root: Path) -> dict:
    """Run the autofix scan via the skill's Python entry point."""
    hooks_dir = Path(__file__).resolve().parent
    proactive_path = hooks_dir / "dynoproactive.py"
    if not proactive_path.exists():
        return {"autofix": "skipped", "reason": "dynoproactive.py not found (autofix not yet implemented)"}
    try:
        result = subprocess.run(
            [sys.executable, str(proactive_path), "scan", "--root", str(root)],
            capture_output=True, text=True, timeout=1800,
        )
        if result.returncode == 0 and result.stdout.strip():
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError:
                return {
                    "autofix": "completed",
                    "returncode": result.returncode,
                    "stdout_preview": result.stdout[:500],
                    "parse_error": "stdout was not valid JSON",
                }
        return {
            "autofix": "completed",
            "returncode": result.returncode,
            "stderr_preview": (result.stderr or "")[:500],
            "stdout_preview": (result.stdout or "")[:500],
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "autofix": "timeout",
            "timeout_seconds": 1800,
            "stderr_preview": (exc.stderr or "")[:500] if exc.stderr else "",
        }
    except OSError as exc:
        return {"autofix": "error", "error": str(exc), "errno": getattr(exc, "errno", None)}


def cmd_run_once(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    result = maintenance_cycle(root)
    autofix = getattr(args, "autofix", False)
    if autofix or _is_autofix_enabled(root):
        result["autofix"] = _run_autofix(root)
    print(json.dumps(result, indent=2))
    return 0


def cmd_invoke(args: argparse.Namespace) -> int:
    return cmd_run_once(args)


_SHOULD_STOP = False


def _stop_handler(signum: int, frame: object) -> None:
    del signum, frame
    global _SHOULD_STOP
    _SHOULD_STOP = True


def cmd_run_loop(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    maintenance_dir(root).mkdir(parents=True, exist_ok=True)
    autofix = getattr(args, "autofix", False)
    if autofix:
        _autofix_flag_path(root).write_text("1")
    poll_seconds = int(args.poll_seconds or maintainer_policy(root)["maintainer_poll_seconds"])
    pid_path(root).write_text(f"{os.getpid()}\n")
    if stop_path(root).exists():
        stop_path(root).unlink()
    signal.signal(signal.SIGTERM, _stop_handler)
    signal.signal(signal.SIGINT, _stop_handler)
    try:
        while not _SHOULD_STOP and not stop_path(root).exists():
            cycle = maintenance_cycle(root)
            if _is_autofix_enabled(root):
                cycle["autofix"] = _run_autofix(root)
            try:
                cycle_count = sum(1 for _ in open(log_path(root)))
            except OSError:
                cycle_count = 1
            write_status(
                root,
                {
                    "updated_at": now_iso(),
                    "running": True,
                    "pid": os.getpid(),
                    "poll_seconds": poll_seconds,
                    "last_cycle": cycle,
                    "cycle_count": cycle_count,
                },
            )
            for _ in range(poll_seconds):
                if _SHOULD_STOP or stop_path(root).exists():
                    break
                time.sleep(1)
    finally:
        if pid_path(root).exists():
            pid_path(root).unlink()
        if stop_path(root).exists():
            stop_path(root).unlink()
        write_status(
            root,
            {
                "updated_at": now_iso(),
                "running": False,
                "pid": None,
            },
        )
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    lock_file = maintenance_dir(root) / "start.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = open(lock_file, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_fd.close()
        print(json.dumps({"status": "start_in_progress"}, indent=2))
        return 0
    try:
        existing = current_pid(root)
        if existing is not None:
            print(json.dumps({"status": "already_running", "pid": existing}, indent=2))
            return 0
        hooks_dir = Path(__file__).resolve().parent
        poll_seconds = int(args.poll_seconds or maintainer_policy(root)["maintainer_poll_seconds"])
        autofix = getattr(args, "autofix", False)
        cmd = [
            "python3",
            str(hooks_dir / "dynomaintain.py"),
            "run-loop",
            "--root",
            str(root),
            "--poll-seconds",
            str(poll_seconds),
        ]
        if autofix:
            cmd.append("--autofix")
        process = subprocess.Popen(
            cmd,
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        time.sleep(0.2)
        print(json.dumps({"status": "started", "pid": process.pid, "poll_seconds": poll_seconds, "autofix": autofix}, indent=2))
        return 0
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


def cmd_ensure(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    policy = maintainer_policy(root)
    if not policy.get("maintainer_autostart", False):
        print(json.dumps({"status": "autostart_disabled"}, indent=2))
        return 0
    if current_pid(root) is not None:
        print(json.dumps({"status": "already_running", "pid": current_pid(root)}, indent=2))
        return 0
    start_args = argparse.Namespace(root=str(root), poll_seconds=policy["maintainer_poll_seconds"])
    return cmd_start(start_args)


def cmd_stop(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    pid = current_pid(root)
    if pid is None:
        print(json.dumps({"status": "not_running"}, indent=2))
        return 0
    stop_path(root).parent.mkdir(parents=True, exist_ok=True)
    stop_path(root).write_text("stop\n")
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    print(json.dumps({"status": "stopping", "pid": pid}, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    payload = {"running": False, "pid": None}
    if status_path(root).exists():
        try:
            payload = load_json(status_path(root))
        except (json.JSONDecodeError, FileNotFoundError, OSError):
            payload = {"running": False, "pid": None}
    payload["running"] = current_pid(root) is not None
    payload["pid"] = current_pid(root)
    payload["autofix"] = _is_autofix_enabled(root)
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
    run_once.add_argument("--autofix", action="store_true", help="Also run AI autofix scan after data pipeline (scans for debt, opens PRs)")
    run_once.set_defaults(func=cmd_run_once)

    invoke = subparsers.add_parser("invoke", help="Manual maintainer trigger for one maintenance cycle")
    invoke.add_argument("--root", default=".")
    invoke.set_defaults(func=cmd_invoke)

    run_loop = subparsers.add_parser("run-loop", help="Run the maintainer loop in the foreground")
    run_loop.add_argument("--root", default=".")
    run_loop.add_argument("--poll-seconds", type=int)
    run_loop.add_argument("--autofix", action="store_true", help="Also run AI autofix scan after each cycle")
    run_loop.set_defaults(func=cmd_run_loop)

    start = subparsers.add_parser("start", help="Start the maintainer daemon in the background")
    start.add_argument("--root", default=".")
    start.add_argument("--poll-seconds", type=int)
    start.add_argument("--autofix", action="store_true", help="Also run AI autofix scan after each cycle")
    start.set_defaults(func=cmd_start)

    ensure = subparsers.add_parser("ensure", help="Start the daemon only when policy enables autostart")
    ensure.add_argument("--root", default=".")
    ensure.set_defaults(func=cmd_ensure)

    stop = subparsers.add_parser("stop", help="Stop the maintainer daemon")
    stop.add_argument("--root", default=".")
    stop.set_defaults(func=cmd_stop)

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
