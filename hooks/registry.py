#!/usr/bin/env python3
"""Global project registry CLI for dynos multi-project daemon."""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import argparse
import json
import os
import sys
from pathlib import Path

from sweeper import (
    list_projects,
    load_registry,
    log_global,
    register_project,
    save_registry,
    set_project_status,
    unregister_project,
)
from lib_core import now_iso


# ---------------------------------------------------------------------------
# Daemon health helper
# ---------------------------------------------------------------------------

def _daemon_health(project_path: str) -> dict:
    """Check daemon health for a project by inspecting its PID file."""
    root = Path(project_path)
    pid_file = root / ".dynos" / "maintenance" / "daemon.pid"
    status_file = root / ".dynos" / "maintenance" / "status.json"

    health: dict = {"daemon_running": False, "pid": None}

    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
        except (ValueError, OSError):
            return health
        try:
            os.kill(pid, 0)
            health["daemon_running"] = True
            health["pid"] = pid
        except OSError:
            pass

    if status_file.exists():
        try:
            with open(status_file) as f:
                status_data = json.load(f)
            if isinstance(status_data, dict):
                health["last_status"] = status_data
        except (json.JSONDecodeError, OSError):
            pass

    return health


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def cmd_register(args: argparse.Namespace) -> int:
    """Register a project in the global registry."""
    raw_path = args.path
    root = Path(raw_path).resolve()

    if not root.is_dir():
        print(json.dumps({"error": f"path is not a directory: {root}"}, indent=2),
              file=sys.stderr)
        return 1

    # Reject temporary directories (autofix worktrees, test fixtures)
    str_root = str(root)
    if str_root.startswith("/tmp/") or str_root.startswith("/var/tmp/") or "/dynos-autofix-" in str_root:
        print(json.dumps({"error": f"refusing to register temporary directory: {root}"}, indent=2),
              file=sys.stderr)
        return 1

    dynos_dir = root / ".dynos"
    if not dynos_dir.is_dir():
        print(json.dumps({"error": f".dynos/ directory not found in {root}"}, indent=2),
              file=sys.stderr)
        return 1

    try:
        reg = register_project(root)
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 1

    entry = None
    for proj in reg.get("projects", []):
        if proj.get("path") == str(root):
            entry = proj
            break

    print(json.dumps({"registered": str(root), "entry": entry}, indent=2))
    return 0


def cmd_unregister(args: argparse.Namespace) -> int:
    """Unregister a project from the global registry. Does not delete files."""
    raw_path = args.path
    root = Path(raw_path).resolve()

    try:
        unregister_project(root)
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 1

    print(json.dumps({"unregistered": str(root)}, indent=2))
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    """List all registered projects as JSON array sorted by last_active_at descending."""
    try:
        projects = list_projects()
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 1

    projects.sort(key=lambda p: p.get("last_active_at", ""), reverse=True)
    print(json.dumps(projects, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Print registry entry with daemon health per project."""
    try:
        projects = list_projects()
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 1

    if args.path is not None:
        root = Path(args.path).resolve()
        abs_path = str(root)
        entry = None
        for proj in projects:
            if proj.get("path") == abs_path:
                entry = proj
                break
        if entry is None:
            print(json.dumps({"error": f"project not registered: {root}"}, indent=2),
                  file=sys.stderr)
            return 1
        entry["daemon_health"] = _daemon_health(entry["path"])
        print(json.dumps(entry, indent=2))
        return 0

    for proj in projects:
        proj["daemon_health"] = _daemon_health(proj["path"])
    print(json.dumps(projects, indent=2))
    return 0


def cmd_pause(args: argparse.Namespace) -> int:
    """Set project status to paused. Idempotent."""
    root = Path(args.path).resolve()
    try:
        set_project_status(root, "paused")
    except ValueError as exc:
        if "not registered" in str(exc):
            print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
            return 1
        raise
    except OSError as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 1

    log_global(f"CLI: paused project {root}")
    print(json.dumps({"paused": str(root)}, indent=2))
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    """Set project status to active. Idempotent."""
    root = Path(args.path).resolve()
    try:
        set_project_status(root, "active")
    except ValueError as exc:
        if "not registered" in str(exc):
            print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
            return 1
        raise
    except OSError as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 1

    log_global(f"CLI: resumed project {root}")
    print(json.dumps({"resumed": str(root)}, indent=2))
    return 0


def cmd_set_active(args: argparse.Namespace) -> int:
    """Update last_active_at to current ISO timestamp. Silently exits 0 if not registered."""
    root = Path(args.path).resolve()

    try:
        reg = load_registry()
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 1

    abs_path = str(root)
    entry = None
    for proj in reg.get("projects", []):
        if proj.get("path") == abs_path:
            entry = proj
            break

    if entry is None:
        return 0

    entry["last_active_at"] = now_iso()
    try:
        save_registry(reg)
    except OSError as exc:
        print(json.dumps({"error": str(exc)}, indent=2), file=sys.stderr)
        return 1

    log_global(f"CLI: set-active for project {root}")
    print(json.dumps({"set_active": str(root), "last_active_at": entry["last_active_at"]}, indent=2))
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    register_p = subparsers.add_parser("register", help="Register a project in the global registry")
    register_p.add_argument("path", help="Path to the project root")
    register_p.set_defaults(func=cmd_register)

    unregister_p = subparsers.add_parser("unregister", help="Unregister a project (does not delete files)")
    unregister_p.add_argument("path", help="Path to the project root")
    unregister_p.set_defaults(func=cmd_unregister)

    list_p = subparsers.add_parser("list", help="List all registered projects")
    list_p.set_defaults(func=cmd_list)

    status_p = subparsers.add_parser("status", help="Show status with daemon health")
    status_p.add_argument("path", nargs="?", default=None, help="Project path (optional, shows all if omitted)")
    status_p.set_defaults(func=cmd_status)

    pause_p = subparsers.add_parser("pause", help="Pause a registered project")
    pause_p.add_argument("path", help="Path to the project root")
    pause_p.set_defaults(func=cmd_pause)

    resume_p = subparsers.add_parser("resume", help="Resume a paused project")
    resume_p.add_argument("path", help="Path to the project root")
    resume_p.set_defaults(func=cmd_resume)

    set_active_p = subparsers.add_parser("set-active", help="Update last_active_at timestamp")
    set_active_p.add_argument("path", help="Path to the project root")
    set_active_p.set_defaults(func=cmd_set_active)

    return parser


if __name__ == "__main__":
    from cli_base import cli_main
    raise SystemExit(cli_main(build_parser))
