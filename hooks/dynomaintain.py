#!/usr/bin/env python3
"""Persistent maintainer worker for dynos-work."""

from __future__ import annotations

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
    actions: list[dict] = []
    for script_name, args in (
        ("dynostrajectory.py", ("rebuild", "--root", str(root))),
        ("dynopatterns.py", ("--root", str(root))),
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
    }
    write_status(
        root,
        {
            "updated_at": now_iso(),
            "running": False,
            "last_cycle": cycle,
            "pid": current_pid(root),
        },
    )
    return cycle


def cmd_run_once(args: argparse.Namespace) -> int:
    result = maintenance_cycle(Path(args.root).resolve())
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
    poll_seconds = int(args.poll_seconds or maintainer_policy(root)["maintainer_poll_seconds"])
    pid_path(root).write_text(f"{os.getpid()}\n")
    if stop_path(root).exists():
        stop_path(root).unlink()
    signal.signal(signal.SIGTERM, _stop_handler)
    signal.signal(signal.SIGINT, _stop_handler)
    try:
        while not _SHOULD_STOP and not stop_path(root).exists():
            cycle = maintenance_cycle(root)
            write_status(
                root,
                {
                    "updated_at": now_iso(),
                    "running": True,
                    "pid": os.getpid(),
                    "poll_seconds": poll_seconds,
                    "last_cycle": cycle,
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
        process = subprocess.Popen(
            [
                "python3",
                str(hooks_dir / "dynomaintain.py"),
                "run-loop",
                "--root",
                str(root),
                "--poll-seconds",
                str(poll_seconds),
            ],
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        time.sleep(0.2)
        print(json.dumps({"status": "started", "pid": process.pid, "poll_seconds": poll_seconds}, indent=2))
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
    print(json.dumps(payload, indent=2))
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

    run_loop = subparsers.add_parser("run-loop", help="Run the maintainer loop in the foreground")
    run_loop.add_argument("--root", default=".")
    run_loop.add_argument("--poll-seconds", type=int)
    run_loop.set_defaults(func=cmd_run_loop)

    start = subparsers.add_parser("start", help="Start the maintainer daemon in the background")
    start.add_argument("--root", default=".")
    start.add_argument("--poll-seconds", type=int)
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

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
