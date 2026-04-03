#!/usr/bin/env python3
"""Global state management for dynos multi-project daemon."""

from __future__ import annotations

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

from dynoslib import collect_retrospectives, load_json, now_iso, write_json
from dynomaintain import maintenance_cycle

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


def project_dir(root: Path) -> Path:
    """Returns ~/.dynos/projects/{slug}/ for persistent project-specific state.

    This is the safe home for postmortems, improvements, and other
    project-specific data that should not live in the repo's .dynos/.
    """
    d = global_home() / "projects" / project_slug(root)
    d.mkdir(parents=True, exist_ok=True)
    return d


def registry_path() -> Path:
    return global_home() / "registry.json"


def global_policy_path() -> Path:
    return global_home() / "policy" / "global-policy.json"


def patterns_dir() -> Path:
    return global_home() / "patterns"


def logs_dir() -> Path:
    return global_home() / "logs"


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
# Statistics extraction (anonymous only)
# ---------------------------------------------------------------------------

def extract_project_stats(project_root: Path) -> dict:
    """Extract anonymous abstract statistics from a single project.

    Returns counts by type, executor reliability rates, average quality
    scores, and prevention rule frequencies.  No file paths, task
    descriptions, or project-specific content is included.
    """
    project_root = project_root.resolve()
    retrospectives = collect_retrospectives(project_root)

    task_counts_by_type: dict[str, int] = {}
    quality_scores: list[float] = []
    executor_repair_totals: dict[str, list[float]] = {}
    prevention_rules: dict[str, int] = {}
    rule_executors: dict[str, str] = {}

    for retro in retrospectives:
        task_type = retro.get("task_type")
        if isinstance(task_type, str) and task_type:
            task_counts_by_type[task_type] = task_counts_by_type.get(task_type, 0) + 1

        qs = retro.get("quality_score")
        if isinstance(qs, (int, float)):
            quality_scores.append(float(qs))

        repair_freq = retro.get("executor_repair_frequency", {})
        if isinstance(repair_freq, dict):
            for role, count in repair_freq.items():
                if isinstance(role, str) and isinstance(count, (int, float)):
                    executor_repair_totals.setdefault(role, []).append(float(count))

        rules = retro.get("prevention_rules", [])
        if isinstance(rules, list):
            for rule in rules:
                rule_text = None
                rule_executor = "unknown"
                if isinstance(rule, str) and rule:
                    rule_text = rule
                elif isinstance(rule, dict):
                    candidate = rule.get("rule") or rule.get("text")
                    if isinstance(candidate, str) and candidate:
                        rule_text = candidate
                    rule_executor = str(rule.get("executor", "unknown"))
                if rule_text:
                    prevention_rules[rule_text] = prevention_rules.get(rule_text, 0) + 1
                    rule_executors[rule_text] = rule_executor

    total_tasks = sum(task_counts_by_type.values())
    avg_quality = (sum(quality_scores) / len(quality_scores)) if quality_scores else 0.0

    executor_reliability: dict[str, float] = {}
    for role, counts in executor_repair_totals.items():
        avg_repairs = sum(counts) / len(counts) if counts else 0.0
        executor_reliability[role] = round(max(0.0, 1.0 - avg_repairs * 0.1), 3)

    return {
        "total_tasks": total_tasks,
        "task_counts_by_type": task_counts_by_type,
        "average_quality_score": round(avg_quality, 3),
        "executor_reliability": executor_reliability,
        "prevention_rule_frequencies": prevention_rules,
        "prevention_rule_executors": rule_executors,
    }


def aggregate_cross_project_stats() -> dict:
    """Aggregate anonymous stats from all registered projects.

    Writes results to ~/.dynos/patterns/cross-project-stats.json keyed by
    metric name (not by project).  Returns the aggregated dict.
    """
    ensure_global_dirs()
    projects = list_projects()

    agg_task_counts: dict[str, int] = {}
    agg_quality_scores: list[float] = []
    agg_executor_reliability: dict[str, list[float]] = {}
    agg_prevention_rules: dict[str, int] = {}
    project_count = 0

    for proj in projects:
        proj_path = Path(proj.get("path", ""))
        if not proj_path.is_dir():
            continue
        try:
            stats = extract_project_stats(proj_path)
        except OSError:
            log_global(f"failed to extract stats from project at {proj_path}")
            continue

        project_count += 1

        for task_type, count in stats.get("task_counts_by_type", {}).items():
            agg_task_counts[task_type] = agg_task_counts.get(task_type, 0) + count

        avg_q = stats.get("average_quality_score", 0.0)
        if isinstance(avg_q, (int, float)) and avg_q > 0:
            agg_quality_scores.append(float(avg_q))

        for role, rate in stats.get("executor_reliability", {}).items():
            if isinstance(rate, (int, float)):
                agg_executor_reliability.setdefault(role, []).append(float(rate))

        for rule, freq in stats.get("prevention_rule_frequencies", {}).items():
            agg_prevention_rules[rule] = agg_prevention_rules.get(rule, 0) + freq

    overall_quality = (
        round(sum(agg_quality_scores) / len(agg_quality_scores), 3)
        if agg_quality_scores
        else 0.0
    )
    reliability_means: dict[str, float] = {}
    for role, rates in agg_executor_reliability.items():
        reliability_means[role] = round(sum(rates) / len(rates), 3) if rates else 0.0

    result = {
        "aggregated_at": now_iso(),
        "project_count": project_count,
        "total_tasks": sum(agg_task_counts.values()),
        "task_counts_by_type": agg_task_counts,
        "average_quality_score": overall_quality,
        "executor_reliability": reliability_means,
        "prevention_rule_frequencies": agg_prevention_rules,
    }

    output_path = patterns_dir() / "cross-project-stats.json"
    write_json(output_path, result)
    log_global(json.dumps({
        "action": "aggregate_cross_project_stats",
        "projects_aggregated": project_count,
        "total_tasks": result["total_tasks"],
        "average_quality_score": result["average_quality_score"],
        "timestamp": now_iso(),
    }))
    return result


def promote_prevention_rules() -> dict:
    """Promote prevention rules appearing in 2+ distinct projects.

    Reads per-project stats (via extract_project_stats), finds rules that
    appear in at least 2 distinct projects, and writes them to
    ~/.dynos/patterns/global-prevention-rules.json.

    Returns the promoted rules dict.
    """
    ensure_global_dirs()
    projects = list_projects()

    rule_project_count: dict[str, int] = {}
    rule_executor_map: dict[str, str] = {}

    for proj in projects:
        proj_path = Path(proj.get("path", ""))
        if not proj_path.is_dir():
            continue
        try:
            stats = extract_project_stats(proj_path)
        except OSError:
            continue

        seen_in_project: set[str] = set()
        for rule in stats.get("prevention_rule_frequencies", {}):
            if isinstance(rule, str) and rule and rule not in seen_in_project:
                seen_in_project.add(rule)
                rule_project_count[rule] = rule_project_count.get(rule, 0) + 1
        for rule, executor in stats.get("prevention_rule_executors", {}).items():
            if isinstance(rule, str) and isinstance(executor, str):
                rule_executor_map.setdefault(rule, executor)

    promoted = {
        rule: count
        for rule, count in rule_project_count.items()
        if count >= 2
    }

    promotion_ts = now_iso()
    result = {
        "promoted_at": promotion_ts,
        "threshold": 2,
        "rules": [
            {
                "rule": rule,
                "executor": rule_executor_map.get(rule, "unknown"),
                "project_count": count,
                "promotion_timestamp": promotion_ts,
            }
            for rule, count in sorted(promoted.items(), key=lambda x: -x[1])
        ],
    }

    output_path = patterns_dir() / "global-prevention-rules.json"
    write_json(output_path, result)
    log_global(json.dumps({
        "action": "promote_prevention_rules",
        "rules_promoted": len(result["rules"]),
        "threshold": 2,
        "timestamp": promotion_ts,
    }))
    return result


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


def is_pid_running(pid: int) -> bool:
    """Check whether a PID is alive."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


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
            cycle = maintenance_cycle(root)
        except OSError as exc:
            log_global(f"maintenance_cycle failed for {proj_path}: {exc}")
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
                    cycle = maintenance_cycle(root)
                except OSError as exc:
                    log_global(f"maintenance_cycle failed for {proj_path}: {exc}")
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

    payload = {
        "running": running,
        "pid": pid,
        "last_run_at": last_run_at,
        "projects_maintained": len(active_projects),
        "per_project_summary": per_project_summary,
    }
    print(json.dumps(payload, indent=2))
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

    return parser


def main() -> int:
    """Entry point for the global daemon CLI."""
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
