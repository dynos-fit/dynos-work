#!/usr/bin/env python3
"""Global state management for dynos multi-project daemon."""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import argparse
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dynoslib import load_json, now_iso, write_json
from dynoslib_core import project_dir, is_pid_running

__all__ = ["merge_policy"]

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_VALID_STATUSES = {"active", "paused", "archived"}


def global_home() -> Path:
    """Returns DYNOS_HOME env var or ~/.dynos/."""
    env = os.environ.get("DYNOS_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / ".dynos"


_GLOBAL_DIRS = ("registry", "patterns", "policy", "logs", "projects")


def ensure_global_dirs() -> None:
    """Creates ~/.dynos/{registry,patterns,policy,logs,projects} on first use."""
    home = global_home()
    for name in _GLOBAL_DIRS:
        (home / name).mkdir(parents=True, exist_ok=True)


def project_slug(root: Path) -> str:
    """Convert a project root path to a safe directory name."""
    return str(root.resolve()).strip("/").replace("/", "-")


def registry_path() -> Path:
    return global_home() / "registry.json"


def global_policy_path() -> Path:
    return global_home() / "policy" / "global-policy.json"


def patterns_dir() -> Path:
    return global_home() / "patterns"


def logs_dir() -> Path:
    return global_home() / "logs"


def sweeps_log_path() -> Path:
    return global_home() / "sweeps.jsonl"


# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------

def _compute_checksum(data: dict) -> str:
    """SHA-256 hex digest of the registry payload (excluding checksum field)."""
    copy = dict(data)
    copy.pop("checksum", None)
    blob = json.dumps(copy, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _empty_registry() -> dict:
    return {"version": 1, "projects": [], "checksum": ""}


def load_registry() -> dict:
    """Load the global registry with checksum verification.

    Returns the registry dict.  If the file is missing or corrupt, returns a
    fresh empty registry.
    """
    ensure_global_dirs()
    path = registry_path()
    if not path.exists():
        reg = _empty_registry()
        reg["checksum"] = _compute_checksum(reg)
        return reg
    try:
        reg = load_json(path)
    except (json.JSONDecodeError, FileNotFoundError, OSError):
        log_global("registry load failed; returning empty registry")
        reg = _empty_registry()
        reg["checksum"] = _compute_checksum(reg)
        return reg

    if not isinstance(reg, dict):
        reg = _empty_registry()
        reg["checksum"] = _compute_checksum(reg)
        return reg

    stored_checksum = reg.get("checksum", "")
    expected = _compute_checksum(reg)
    if stored_checksum != expected:
        log_global(
            f"registry checksum mismatch: stored={stored_checksum[:12]}... "
            f"expected={expected[:12]}... — refusing to operate on corrupted registry"
        )
        return _empty_registry()
    return reg


def save_registry(data: dict) -> None:
    """Increment version, recompute checksum, write atomically."""
    ensure_global_dirs()
    data["version"] = data.get("version", 0) + 1
    data["checksum"] = _compute_checksum(data)
    write_json(registry_path(), data)


def _find_project_entry(reg: dict, root: Path) -> dict | None:
    abs_path = str(root.resolve())
    for entry in reg.get("projects", []):
        if entry.get("path") == abs_path:
            return entry
    return None


def register_project(root: Path) -> dict:
    """Register a project in the global registry. Returns the updated registry."""
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"project path is not a directory: {root}")

    reg = load_registry()
    existing = _find_project_entry(reg, root)
    if existing is not None:
        existing["last_active_at"] = now_iso()
        existing["status"] = "active"
        save_registry(reg)
        log_global(f"re-registered project: {root}")
        return reg

    entry = {
        "path": str(root),
        "registered_at": now_iso(),
        "last_active_at": now_iso(),
        "status": "active",
    }
    reg.setdefault("projects", []).append(entry)
    save_registry(reg)
    log_global(f"registered project: {root}")
    return reg


def unregister_project(root: Path) -> dict:
    """Remove a project from the global registry. Returns the updated registry."""
    root = root.resolve()
    reg = load_registry()
    abs_path = str(root)
    before = len(reg.get("projects", []))
    reg["projects"] = [
        e for e in reg.get("projects", []) if e.get("path") != abs_path
    ]
    after = len(reg["projects"])
    if before != after:
        save_registry(reg)
        log_global(f"unregistered project: {root}")
    else:
        log_global(f"unregister: project not found: {root}")
    return reg


def set_project_active(root: Path) -> dict:
    """Mark project as active and update last_active_at."""
    return set_project_status(root, "active")


def set_project_status(root: Path, status: str) -> dict:
    """Set project status. Must be one of: active, paused, archived."""
    if status not in _VALID_STATUSES:
        raise ValueError(f"invalid status {status!r}; must be one of {_VALID_STATUSES}")
    root = root.resolve()
    reg = load_registry()
    entry = _find_project_entry(reg, root)
    if entry is None:
        raise ValueError(f"project not registered: {root}")
    entry["status"] = status
    if status == "active":
        entry["last_active_at"] = now_iso()
    save_registry(reg)
    log_global(f"set project status: {root} -> {status}")
    return reg


def list_projects() -> list[dict]:
    """Return a list of all registered project entries."""
    reg = load_registry()
    return list(reg.get("projects", []))


# ---------------------------------------------------------------------------
# Policy merge
# ---------------------------------------------------------------------------

def merge_policy(project_root: Path) -> dict:
    """Merge local project policy over global policy.

    Local keys win over global keys; absent local keys fall through to global
    defaults.  A project never sees raw data from another project.
    """
    ensure_global_dirs()
    gp_path = global_policy_path()
    global_policy: dict = {}
    if gp_path.exists():
        try:
            global_policy = load_json(gp_path)
        except (json.JSONDecodeError, FileNotFoundError, OSError):
            global_policy = {}

    local_policy_file = project_dir(project_root) / "policy.json"
    local_policy: dict = {}
    if local_policy_file.exists():
        try:
            local_policy = load_json(local_policy_file)
        except (json.JSONDecodeError, FileNotFoundError, OSError):
            local_policy = {}

    merged = dict(global_policy)
    merged.update(local_policy)
    return merged


# ---------------------------------------------------------------------------
# Statistics extraction (delegated to dynoglobal_stats)
# ---------------------------------------------------------------------------
from dynoglobal_stats import extract_project_stats  # noqa: E402


def aggregate_cross_project_stats() -> dict:
    """Aggregate anonymous stats from all registered projects."""
    from dynoglobal_stats import aggregate_cross_project_stats as _agg
    return _agg(
        list_projects_fn=list_projects,
        ensure_global_dirs_fn=ensure_global_dirs,
        patterns_dir_fn=patterns_dir,
        log_global_fn=log_global,
    )


def promote_prevention_rules() -> dict:
    """Promote prevention rules appearing in 2+ distinct projects."""
    from dynoglobal_stats import promote_prevention_rules as _promote
    return _promote(
        list_projects_fn=list_projects,
        ensure_global_dirs_fn=ensure_global_dirs,
        patterns_dir_fn=patterns_dir,
        log_global_fn=log_global,
    )


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log_global(message: str) -> None:
    """Append a message to today's date-stamped log file in ~/.dynos/logs/."""
    ensure_global_dirs()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_file = logs_dir() / f"{today}.log"
    timestamp = now_iso()
    line = f"[{timestamp}] {message}\n"
    try:
        with open(log_file, "a") as f:
            f.write(line)
    except OSError:
        pass


def cleanup_old_logs(max_age_days: int = 30) -> int:
    """Remove log files older than max_age_days. Returns count of files removed."""
    if max_age_days < 1:
        raise ValueError("max_age_days must be >= 1")
    ensure_global_dirs()
    cutoff = time.time() - (max_age_days * 86400)
    removed = 0
    log_dir = logs_dir()
    try:
        entries = list(log_dir.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if not entry.is_file() or not entry.suffix == ".log":
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed += 1
        except OSError:
            continue
    if removed > 0:
        log_global(f"cleaned up {removed} old log files (max_age_days={max_age_days})")
    return removed


# ---------------------------------------------------------------------------
# Daemon helpers
# ---------------------------------------------------------------------------

def daemon_pid_path() -> Path:
    """PID file for the global daemon."""
    return global_home() / "daemon.pid"


def daemon_stop_path() -> Path:
    """Stop sentinel file for the global daemon."""
    return global_home() / "stop"


def current_daemon_pid() -> int | None:
    """Return the running daemon PID or None."""
    path = daemon_pid_path()
    if not path.exists():
        return None
    try:
        pid = int(path.read_text().strip())
    except (ValueError, OSError):
        return None
    return pid if is_pid_running(pid) else None


def _last_active_sort_key(entry: dict) -> str:
    """Sort key: last_active_at descending (negate by inverting)."""
    return entry.get("last_active_at", "")


def _should_skip_backoff(entry: dict, sweep_count: int) -> bool:
    """Exponential backoff for idle projects.

    >24h idle  -> skip every other sweep
    >72h idle  -> skip 3 out of 4 sweeps
    >7d  idle  -> skip 7 out of 8 sweeps
    """
    last_active = entry.get("last_active_at", "")
    if not last_active:
        return False
    try:
        last_dt = datetime.fromisoformat(last_active)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return False

    now = datetime.now(timezone.utc)
    age_hours = (now - last_dt).total_seconds() / 3600.0

    if age_hours > 168:  # >7 days
        return (sweep_count % 8) != 0
    if age_hours > 72:   # >3 days
        return (sweep_count % 4) != 0
    if age_hours > 24:   # >1 day
        return (sweep_count % 2) != 0
    return False


def _log_project_cycle(project_path: str, cycle_result: dict) -> None:
    """Write structured log entry after a project maintenance cycle."""
    log_entry = {
        "event": "project_cycle",
        "project": project_path,
        "executed_at": cycle_result.get("executed_at", now_iso()),
        "ok": cycle_result.get("ok", False),
        "action_count": len(cycle_result.get("actions", [])),
    }
    log_global(json.dumps(log_entry, separators=(",", ":")))


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------

_SHOULD_STOP = False


def _stop_handler(signum: int, frame: object) -> None:
    del signum, frame
    global _SHOULD_STOP
    _SHOULD_STOP = True


def _run_maintenance_cycle(root: Path) -> dict:
    """Run maintenance_cycle via subprocess to avoid tight coupling.

    Invokes dynomaintain.py run-once and parses the JSON output.
    """
    hooks_dir = Path(__file__).resolve().parent
    result = subprocess.run(
        [sys.executable, str(hooks_dir / "dynomaintain.py"), "run-once", "--root", str(root)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode == 0 and result.stdout.strip():
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            pass
    return {
        "executed_at": now_iso(),
        "ok": False,
        "error": result.stderr.strip() if result.stderr else f"exit code {result.returncode}",
        "actions": [],
    }


# ---------------------------------------------------------------------------
# Daemon commands
# ---------------------------------------------------------------------------

def cmd_run_once(args: argparse.Namespace) -> int:
    """Run a single sweep over all active projects."""
    reg = load_registry()
    projects = [
        e for e in reg.get("projects", []) if e.get("status") == "active"
    ]
    projects.sort(key=_last_active_sort_key, reverse=True)

    results: list[dict] = []
    for entry in projects:
        proj_path = entry.get("path", "")
        root = Path(proj_path)
        if not root.is_dir():
            log_global(f"skipping missing project directory: {proj_path}")
            continue
        try:
            cycle = _run_maintenance_cycle(root)
        except (OSError, subprocess.SubprocessError) as exc:
            log_global(f"_run_maintenance_cycle failed for {proj_path}: {exc}")
            cycle = {"executed_at": now_iso(), "ok": False, "error": str(exc), "actions": []}
        _log_project_cycle(proj_path, cycle)
        results.append({"project": proj_path, "cycle": cycle})

    try:
        aggregate_cross_project_stats()
    except OSError as exc:
        log_global(f"cross-project aggregation failed: {exc}")

    summary = {
        "sweep_at": now_iso(),
        "projects_maintained": len(results),
        "results": results,
    }
    print(json.dumps(summary, indent=2))
    return 0


def cmd_run_loop(args: argparse.Namespace) -> int:
    """Run the global daemon loop in the foreground."""
    ensure_global_dirs()
    poll_seconds = int(getattr(args, "poll_seconds", None) or 3600)

    daemon_pid_path().write_text(f"{os.getpid()}\n")
    if daemon_stop_path().exists():
        daemon_stop_path().unlink()

    signal.signal(signal.SIGTERM, _stop_handler)
    signal.signal(signal.SIGINT, _stop_handler)

    sweep_count = 0
    try:
        while not _SHOULD_STOP and not daemon_stop_path().exists():
            sweep_count += 1
            reg = load_registry()
            projects = [
                e for e in reg.get("projects", []) if e.get("status") == "active"
            ]
            projects.sort(key=_last_active_sort_key, reverse=True)

            maintained = 0
            per_project: list[dict] = []
            for entry in projects:
                if _SHOULD_STOP or daemon_stop_path().exists():
                    break
                if _should_skip_backoff(entry, sweep_count):
                    log_global(
                        f"backoff skip: {entry.get('path', '?')} "
                        f"(sweep #{sweep_count})"
                    )
                    continue
                proj_path = entry.get("path", "")
                root = Path(proj_path)
                if not root.is_dir():
                    log_global(f"skipping missing project directory: {proj_path}")
                    continue
                try:
                    cycle = _run_maintenance_cycle(root)
                except OSError as exc:
                    log_global(f"_run_maintenance_cycle failed for {proj_path}: {exc}")
                    cycle = {
                        "executed_at": now_iso(),
                        "ok": False,
                        "error": str(exc),
                        "actions": [],
                    }
                _log_project_cycle(proj_path, cycle)
                per_project.append({"project": proj_path, "ok": cycle.get("ok", False)})
                maintained += 1

            try:
                aggregate_cross_project_stats()
            except OSError as exc:
                log_global(f"cross-project aggregation failed: {exc}")

            cleanup_old_logs()

            sweep_entry = {
                "sweep": sweep_count,
                "executed_at": now_iso(),
                "projects_total": len(projects),
                "projects_maintained": maintained,
                "per_project": per_project,
                "ok": all(p.get("ok", False) for p in per_project) if per_project else True,
            }
            try:
                with open(sweeps_log_path(), "a") as f:
                    f.write(json.dumps(sweep_entry) + "\n")
            except OSError:
                pass

            log_global(
                f"sweep #{sweep_count} complete: "
                f"{maintained}/{len(projects)} projects maintained"
            )

            # Sleep in 1-second increments to allow clean shutdown
            for _ in range(poll_seconds):
                if _SHOULD_STOP or daemon_stop_path().exists():
                    break
                time.sleep(1)
    finally:
        if daemon_pid_path().exists():
            try:
                daemon_pid_path().unlink()
            except OSError:
                pass
        if daemon_stop_path().exists():
            try:
                daemon_stop_path().unlink()
            except OSError:
                pass
        log_global("global daemon stopped")
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    """Start the global daemon as a background process."""
    ensure_global_dirs()
    lock_file = global_home() / "start.lock"
    lock_fd = open(lock_file, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_fd.close()
        print(json.dumps({"status": "start_in_progress"}, indent=2))
        return 0
    try:
        existing = current_daemon_pid()
        if existing is not None:
            print(json.dumps({"status": "already_running", "pid": existing}, indent=2))
            return 0

        hooks_dir = Path(__file__).resolve().parent
        poll_seconds = int(getattr(args, "poll_seconds", None) or 3600)
        process = subprocess.Popen(
            [
                sys.executable,
                str(hooks_dir / "dynoglobal.py"),
                "run-loop",
                "--poll-seconds",
                str(poll_seconds),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        time.sleep(0.2)
        print(json.dumps({
            "status": "started",
            "pid": process.pid,
            "poll_seconds": poll_seconds,
        }, indent=2))
        return 0
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


def cmd_stop(args: argparse.Namespace) -> int:
    """Stop the global daemon using stop-file + SIGTERM."""
    pid = current_daemon_pid()
    if pid is None:
        print(json.dumps({"status": "not_running"}, indent=2))
        return 0
    ensure_global_dirs()
    try:
        daemon_stop_path().write_text("stop\n")
    except OSError:
        pass
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    print(json.dumps({"status": "stopping", "pid": pid}, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Print JSON status of the global daemon."""
    pid = current_daemon_pid()
    running = pid is not None

    # Determine last_run_at from today's log
    last_run_at = None
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log_file = logs_dir() / f"{today}.log"
        if log_file.exists():
            lines = log_file.read_text().splitlines()
            for line in reversed(lines):
                if "sweep #" in line and "complete" in line:
                    # Extract timestamp from "[TIMESTAMP] ..." format
                    if line.startswith("[") and "]" in line:
                        last_run_at = line[1 : line.index("]")]
                    break
    except OSError:
        pass

    # Build per-project summary
    reg = load_registry()
    active_projects = [
        e for e in reg.get("projects", []) if e.get("status") == "active"
    ]
    per_project_summary = []
    for entry in active_projects:
        per_project_summary.append({
            "path": entry.get("path", ""),
            "last_active_at": entry.get("last_active_at", ""),
            "status": entry.get("status", ""),
        })

    # Sweep history from JSONL
    sweep_count = 0
    total_failures = 0
    recent_sweeps: list[dict] = []
    sp = sweeps_log_path()
    if sp.exists():
        try:
            lines = sp.read_text().strip().splitlines()
            sweeps = [json.loads(l) for l in lines if l.strip()]
            sweep_count = len(sweeps)
            total_failures = sum(1 for s in sweeps if not s.get("ok"))
            recent_sweeps = [
                {
                    "sweep": s.get("sweep"),
                    "executed_at": s.get("executed_at"),
                    "ok": s.get("ok"),
                    "projects_maintained": s.get("projects_maintained"),
                    "projects_total": s.get("projects_total"),
                }
                for s in sweeps[-5:]
            ]
        except (json.JSONDecodeError, OSError):
            pass

    payload = {
        "running": running,
        "pid": pid,
        "last_run_at": last_run_at,
        "sweep_count": sweep_count,
        "total_failures": total_failures,
        "recent_sweeps": recent_sweeps,
        "projects_maintained": len(active_projects),
        "per_project_summary": per_project_summary,
    }
    print(json.dumps(payload, indent=2))
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    """Show recent global daemon sweep logs."""
    sp = sweeps_log_path()
    if not sp.exists():
        print(json.dumps({"sweeps": [], "message": "No sweep logs yet"}))
        return 0
    try:
        lines = sp.read_text().strip().splitlines()
        sweeps = [json.loads(l) for l in lines if l.strip()]
    except (json.JSONDecodeError, OSError) as e:
        print(json.dumps({"error": str(e)}))
        return 1
    n = int(getattr(args, "last", None) or 10)
    recent = sweeps[-n:]
    print(json.dumps({"total_sweeps": len(sweeps), "showing": len(recent), "sweeps": recent}, indent=2))
    return 0


# ---------------------------------------------------------------------------
# CLI parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Build argument parser with daemon subcommands."""
    parser = argparse.ArgumentParser(
        prog="dynoglobal",
        description="Global daemon for multi-project dynos maintenance.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    sp_start = subparsers.add_parser("start", help="Start global daemon in background")
    sp_start.add_argument("--poll-seconds", type=int, default=3600)
    sp_start.set_defaults(func=cmd_start)

    sp_stop = subparsers.add_parser("stop", help="Stop the global daemon")
    sp_stop.set_defaults(func=cmd_stop)

    sp_status = subparsers.add_parser("status", help="Show global daemon status (JSON)")
    sp_status.set_defaults(func=cmd_status)

    sp_run_once = subparsers.add_parser("run-once", help="Run a single maintenance sweep")
    sp_run_once.set_defaults(func=cmd_run_once)

    sp_run_loop = subparsers.add_parser("run-loop", help="Run the daemon loop in foreground")
    sp_run_loop.add_argument("--poll-seconds", type=int, default=3600)
    sp_run_loop.set_defaults(func=cmd_run_loop)

    sp_logs = subparsers.add_parser("logs", help="Show recent global sweep logs")
    sp_logs.add_argument("--last", default="10", help="Number of recent sweeps to show")
    sp_logs.set_defaults(func=cmd_logs)

    sp_dash = subparsers.add_parser("dashboard", help="Generate or serve global dashboard")
    dash_sub = sp_dash.add_subparsers(dest="dash_command")
    dash_gen = dash_sub.add_parser("generate", help="Generate dashboard HTML (default)")
    dash_gen.set_defaults(func=cmd_dashboard_shim)
    dash_serve = dash_sub.add_parser("serve", help="Generate and serve on local HTTP server")
    dash_serve.add_argument("--port", type=int, default=8766, help="Port to serve on")
    dash_serve.set_defaults(func=cmd_serve_shim)
    dash_kill = dash_sub.add_parser("kill", help="Stop the dashboard server")
    dash_kill.set_defaults(func=cmd_kill_shim)
    dash_restart = dash_sub.add_parser("restart", help="Restart the dashboard server")
    dash_restart.add_argument("--port", type=int, default=8766, help="Port to serve on")
    dash_restart.set_defaults(func=cmd_restart_shim)
    sp_dash.set_defaults(func=cmd_dashboard_shim)

    return parser


def cmd_dashboard_shim(args: argparse.Namespace) -> int:
    """Shim that imports and delegates to dynoglobal_dashboard."""
    from dynoglobal_dashboard import cmd_dashboard
    return cmd_dashboard(args)


def cmd_serve_shim(args: argparse.Namespace) -> int:
    """Shim that imports and delegates to dynoglobal_dashboard serve."""
    from dynoglobal_dashboard import cmd_serve
    return cmd_serve(args)


def cmd_kill_shim(args: argparse.Namespace) -> int:
    """Shim that imports and delegates to dynoglobal_dashboard kill."""
    from dynoglobal_dashboard import cmd_kill
    return cmd_kill(args)


def cmd_restart_shim(args: argparse.Namespace) -> int:
    """Shim that imports and delegates to dynoglobal_dashboard restart."""
    from dynoglobal_dashboard import cmd_restart
    return cmd_restart(args)


def main() -> int:
    """Entry point for the global daemon CLI."""
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
