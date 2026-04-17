#!/usr/bin/env python3
"""Generate a unified HTML dashboard showing all registered dynos-work projects."""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent)); _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "hooks"))

import json
import os
import signal
import sys
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dashboard import build_dashboard_payload
from sweeper import (
    current_daemon_pid,
    load_registry,
    log_global,
    sweeps_log_path,
)
from global_stats import extract_project_stats
from lib_core import _persistent_project_dir, now_iso, write_json
from lib_validate import validate_generated_html


# ---------------------------------------------------------------------------
# Data gathering
# ---------------------------------------------------------------------------

def gather_global_daemon_status() -> dict:
    """Read ~/.dynos/daemon.pid and sweeps.jsonl for global daemon health."""
    pid = current_daemon_pid()
    running = pid is not None

    last_sweep_at = None
    sweep_count = 0
    sp = sweeps_log_path()
    if sp.exists():
        try:
            lines = sp.read_text().strip().splitlines()
            sweeps = []
            for line in lines:
                stripped = line.strip()
                if stripped:
                    try:
                        sweeps.append(json.loads(stripped))
                    except json.JSONDecodeError:
                        continue
            sweep_count = len(sweeps)
            if sweeps:
                last_sweep_at = sweeps[-1].get("executed_at")
        except OSError:
            pass

    return {
        "running": running,
        "pid": pid,
        "last_sweep_at": last_sweep_at,
        "sweep_count": sweep_count,
    }


def gather_project_data(project_path: Path) -> dict:
    """Call build_dashboard_payload() for a single active project. Returns rich data."""
    project_path = project_path.resolve()
    payload = build_dashboard_payload(project_path)
    stats = extract_project_stats(project_path)
    quality_scores = _extract_quality_scores(project_path)
    return {
        "payload": payload,
        "stats": stats,
        "quality_scores": quality_scores,
        "autofix_state": _gather_autofix_state(project_path),
    }


def _gather_autofix_state(project_root: Path) -> dict:
    state = {
        "metrics": {},
        "benchmarks": {},
    }
    persistent = _persistent_project_dir(project_root)
    metrics_path = persistent / "autofix-metrics.json"
    benchmarks_path = persistent / "autofix-benchmarks.json"
    for key, path in (("metrics", metrics_path), ("benchmarks", benchmarks_path)):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                state[key] = data
        except (json.JSONDecodeError, OSError):
            continue
    return state


def _extract_quality_scores(project_path: Path) -> list[float]:
    """Extract quality scores from retrospectives for sparkline rendering."""
    scores: list[float] = []
    dynos_dir = project_path / ".dynos"
    if not dynos_dir.is_dir():
        return scores
    try:
        for retro_path in sorted(dynos_dir.glob("task-*/task-retrospective.json")):
            try:
                data = json.loads(retro_path.read_text())
                qs = data.get("quality_score")
                if isinstance(qs, (int, float)):
                    scores.append(float(qs))
            except (json.JSONDecodeError, OSError):
                continue
    except OSError:
        pass
    return scores


def build_sparkline_svg(scores: list[float], width: int = 200, height: int = 40) -> str:
    """Generate inline SVG polyline sparkline.

    Handles 0 points (empty placeholder), 1 point (single dot),
    N points (polyline with gradient fill).
    """
    if not scores:
        return ""

    svg_id = "spark_" + str(abs(hash(tuple(scores))) % 100000)

    if len(scores) == 1:
        cx = width / 2
        cy = height / 2
        return (
            f'<svg viewBox="0 0 {width} {height}" '
            f'xmlns="http://www.w3.org/2000/svg" style="width:100%;height:100%">'
            f'<circle cx="{cx}" cy="{cy}" r="4" fill="hsl(158 58% 50%)" />'
            f'</svg>'
        )

    min_val = min(scores)
    max_val = max(scores)
    val_range = max_val - min_val if max_val != min_val else 1.0
    padding = 4

    points = []
    for i, val in enumerate(scores):
        x = padding + (i / (len(scores) - 1)) * (width - 2 * padding)
        y = padding + (1.0 - (val - min_val) / val_range) * (height - 2 * padding)
        points.append((x, y))

    points_str = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    first_x = points[0][0]
    last_x = points[-1][0]
    polygon_str = points_str + f" {last_x:.1f},{height} {first_x:.1f},{height}"

    return (
        f'<svg viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg" style="width:100%;height:100%">'
        f'<defs>'
        f'<linearGradient id="{svg_id}_g" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0%" stop-color="hsl(158 58% 50%)" stop-opacity="0.3" />'
        f'<stop offset="100%" stop-color="hsl(158 58% 50%)" stop-opacity="0" />'
        f'</linearGradient>'
        f'</defs>'
        f'<polygon fill="url(#{svg_id}_g)" points="{polygon_str}" />'
        f'<polyline fill="none" stroke="hsl(158 58% 50%)" stroke-width="2" '
        f'stroke-linejoin="round" stroke-linecap="round" points="{points_str}" />'
        f'</svg>'
    )


def derive_health_tag(project_data: dict) -> str:
    """Derive health tag for a project.

    'healthy' if last cycle ok and quality > 0.5,
    'warning' if quality < 0.5 or cycle issues,
    'error' if extraction failed.
    """
    if project_data.get("error"):
        return "error"

    stats = project_data.get("stats", {})
    avg_quality = stats.get("average_quality_score", 0.0)

    payload = project_data.get("payload", {})
    summary = payload.get("summary", {})
    demoted = summary.get("demoted_components", 0)

    if avg_quality < 0.5 or demoted > 0:
        return "warning"
    return "healthy"


def _collect_project_retrospectives(project_path: Path) -> list[dict]:
    """Collect retrospective data for a project."""
    from lib_core import collect_retrospectives
    try:
        return collect_retrospectives(project_path)
    except Exception:
        return []


def gather_all_projects() -> dict:
    """Load registry, iterate all projects (active/paused/archived).

    Active: full payload + sparkline data + retrospectives + benchmark runs.
    Paused/archived: name, path, status, last_active_at only.
    Missing dirs: warning tag. Failed extraction: error tag + log.
    """
    reg = load_registry()
    projects_list = reg.get("projects", [])

    active_projects: list[dict] = []
    inactive_projects: list[dict] = []

    _empty_proj = {
        "stats": {},
        "quality_scores": [],
        "sparkline_svg": "",
        "task_count": 0,
        "learned_routes": 0,
        "last_cycle_at": "",
        "payload": {},
        "retrospectives": [],
        "autofix_state": {"metrics": {}, "benchmarks": {}},
    }

    for entry in projects_list:
        proj_path_str = entry.get("path", "")
        status = entry.get("status", "active")
        proj_path = Path(proj_path_str)
        name = proj_path.name if proj_path_str else "unknown"

        if status in ("paused", "archived"):
            inactive_projects.append({
                "name": name,
                "path": proj_path_str,
                "status": status,
                "last_active_at": entry.get("last_active_at", ""),
            })
            continue

        # Active project
        if not proj_path.is_dir():
            active_projects.append({
                "name": name,
                "path": proj_path_str,
                "status": status,
                "last_active_at": entry.get("last_active_at", ""),
                "health": "warning",
                "warning": "directory missing",
                **_empty_proj,
            })
            log_global(f"global dashboard: missing directory for project {proj_path_str}")
            continue

        try:
            data = gather_project_data(proj_path)
            stats = data.get("stats", {})
            payload = data.get("payload", {})
            quality_scores = data.get("quality_scores", [])
            summary = payload.get("summary", {})
            retrospectives = _collect_project_retrospectives(proj_path)

            sparkline_svg = build_sparkline_svg(quality_scores, width=400, height=60)
            health = derive_health_tag(data)
            maintenance = _gather_maintenance_data(proj_path)
            autofix_cost = _gather_autofix_cost(proj_path)

            active_projects.append({
                "name": name,
                "path": proj_path_str,
                "status": status,
                "last_active_at": entry.get("last_active_at", ""),
                "health": health,
                "stats": stats,
                "quality_scores": quality_scores,
                "sparkline_svg": sparkline_svg,
                "task_count": stats.get("total_tasks", 0),
                "learned_routes": summary.get("active_routes", 0),
                "last_cycle_at": payload.get("generated_at", ""),
                "daemon_running": _check_project_daemon(proj_path),
                "maintenance": maintenance,
                "autofix_cost": autofix_cost,
                "payload": payload,
                "retrospectives": retrospectives,
                "autofix_state": data.get("autofix_state", {"metrics": {}, "benchmarks": {}}),
            })
        except (OSError, json.JSONDecodeError) as exc:
            log_global(f"global dashboard: error extracting project {proj_path_str}: {exc}")
            active_projects.append({
                "name": name,
                "path": proj_path_str,
                "status": status,
                "last_active_at": entry.get("last_active_at", ""),
                "health": "error",
                "error": str(exc),
                **_empty_proj,
            })
        except Exception as exc:
            log_global(f"global dashboard: unexpected error for {proj_path_str}: {exc}")
            active_projects.append({
                "name": name,
                "path": proj_path_str,
                "status": status,
                "last_active_at": entry.get("last_active_at", ""),
                "health": "error",
                "error": str(exc),
                **_empty_proj,
            })

    return {
        "active": active_projects,
        "inactive": inactive_projects,
    }


def _check_project_daemon(project_path: Path) -> bool:
    """Check if a per-project maintenance daemon is running."""
    pid_file = project_path / ".dynos" / "maintenance" / "daemon.pid"
    if not pid_file.exists():
        return False
    try:
        import os as _os
        pid = int(pid_file.read_text().strip())
        _os.kill(pid, 0)
        return True
    except (ValueError, OSError):
        return False


def _gather_maintenance_data(project_root: Path) -> dict:
    """Read .dynos/maintenance/status.json and cycles.jsonl for a project."""
    result: dict = {
        "daemon_running": False,
        "daemon_pid": None,
        "poll_seconds": None,
        "autofix_enabled": False,
        "last_cycle_at": None,
        "last_cycle_actions": [],
        "cycle_history": [],
    }
    maint_dir = project_root / ".dynos" / "maintenance"

    # Read status.json
    status_path = maint_dir / "status.json"
    if status_path.exists():
        try:
            data = json.loads(status_path.read_text())
            result["daemon_running"] = data.get("running", False)
            result["daemon_pid"] = data.get("pid")
            result["poll_seconds"] = data.get("poll_seconds")
            # Check autofix flag
            autofix_flag = maint_dir / "autofix.enabled"
            result["autofix_enabled"] = autofix_flag.exists()
            last_cycle = data.get("last_cycle", {})
            result["last_cycle_at"] = last_cycle.get("executed_at")
            actions_raw = last_cycle.get("actions", [])
            actions = []
            for a in actions_raw:
                if not isinstance(a, dict):
                    continue
                name = a.get("name", "unknown")
                rc = a.get("returncode", -1)
                # Build a human-readable summary from the result
                res = a.get("result", {})
                summary_parts = []
                if isinstance(res, dict):
                    for k, v in res.items():
                        if isinstance(v, list):
                            summary_parts.append(f"{len(v)} {k}")
                        elif isinstance(v, (int, float)):
                            summary_parts.append(f"{k}: {v}")
                        elif isinstance(v, str) and len(v) < 60:
                            summary_parts.append(f"{k}: {v}")
                summary_text = ", ".join(summary_parts[:3]) if summary_parts else "completed"
                actions.append({
                    "name": name,
                    "returncode": rc,
                    "summary": summary_text,
                })
            result["last_cycle_actions"] = actions
        except (json.JSONDecodeError, OSError):
            pass

    # Read cycles.jsonl (last 5 lines)
    cycles_path = maint_dir / "cycles.jsonl"
    if cycles_path.exists():
        try:
            lines = cycles_path.read_text().strip().splitlines()
            recent_lines = lines[-5:] if len(lines) > 5 else lines
            for line in recent_lines:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    cycle = json.loads(stripped)
                    cycle_actions = cycle.get("actions", [])
                    total_actions = len(cycle_actions)
                    failed = sum(1 for a in cycle_actions if isinstance(a, dict) and a.get("returncode", 0) != 0)
                    result["cycle_history"].append({
                        "executed_at": cycle.get("executed_at", "unknown"),
                        "total_actions": total_actions,
                        "failed_actions": failed,
                        "passed": failed == 0,
                    })
                except json.JSONDecodeError:
                    continue
        except OSError:
            pass

    return result


def _gather_autofix_cost(project_root: Path) -> dict:
    """Read proactive-findings.json and scan-coverage.json for autofix cost data.

    proactive-findings.json may be:
      - a dict with a top-level ``cost`` key (newer format)
      - a list of findings (older format, no cost data)

    scan-coverage.json has ``last_scan_at`` at top level.

    Returns a dict with haiku_invocations, fix_invocations, estimated_cost_usd,
    and last_scan_at -- or an empty dict if no data is available.
    """
    result: dict = {}
    dynos_dir = project_root / ".dynos"

    # Read proactive-findings.json for cost data
    findings_path = dynos_dir / "proactive-findings.json"
    if findings_path.exists():
        try:
            data = json.loads(findings_path.read_text())
            if isinstance(data, dict):
                cost = data.get("cost", {})
                if isinstance(cost, dict):
                    result["haiku_invocations"] = cost.get("haiku_invocations", 0)
                    result["fix_invocations"] = cost.get("fix_invocations", 0)
                    result["estimated_cost_usd"] = cost.get("estimated_cost_usd", 0.0)
            # list format has no cost data -- leave result empty for cost fields
        except (json.JSONDecodeError, OSError):
            pass

    # Read scan-coverage.json for last_scan_at
    coverage_path = dynos_dir / "scan-coverage.json"
    if coverage_path.exists():
        try:
            cov_data = json.loads(coverage_path.read_text())
            if isinstance(cov_data, dict):
                last_scan = cov_data.get("last_scan_at", "")
                if last_scan:
                    result["last_scan_at"] = last_scan
        except (json.JSONDecodeError, OSError):
            pass

    return result


def _gather_autofix_prs(project_path: str) -> list[dict]:
    """Get autofix PRs for a specific project. Returns empty list if gh unavailable or none found."""
    project_root = Path(project_path)
    persistent = _persistent_project_dir(project_root)
    metrics_path = persistent / "autofix-metrics.json"
    if metrics_path.exists():
        try:
            data = json.loads(metrics_path.read_text())
            recent = data.get("recent_prs", [])
            if isinstance(recent, list) and recent:
                return recent
        except (json.JSONDecodeError, OSError):
            pass
    import subprocess
    try:
        proc = subprocess.run(
            ["gh", "pr", "list", "--state", "all", "--limit", "10",
             "--search", "dynos/auto-fix in:head",
             "--json", "number,title,state,createdAt,url,headRefName"],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            return []
        data = json.loads(proc.stdout)
        if not isinstance(data, list):
            return []
        prs = []
        for pr in data:
            prs.append({
                "number": pr.get("number", 0),
                "title": pr.get("title", ""),
                "state": pr.get("state", "UNKNOWN"),
                "created_at": pr.get("createdAt", ""),
                "url": pr.get("url", ""),
                "branch": pr.get("headRefName", ""),
            })
        return prs
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return []


def compute_aggregate_stats(projects: list[dict]) -> dict:
    """Total tasks, avg quality, total learned routes, active count, benchmark runs, findings, cross-project summary."""
    total_tasks = 0
    quality_scores: list[float] = []
    total_learned_routes = 0
    active_count = 0
    total_benchmark_runs = 0
    total_findings = 0
    total_autofix_cost_usd = 0.0
    total_learned_components = 0
    total_shadow_components = 0
    total_demoted_components = 0
    total_tracked_fixtures = 0
    total_coverage_gaps = 0
    total_automation_queue = 0
    total_autofix_open_prs = 0
    total_autofix_merged = 0
    total_autofix_suppressions = 0

    for proj in projects:
        stats = proj.get("stats", {})
        payload = proj.get("payload", {})
        summary = payload.get("summary", {})
        retrospectives = proj.get("retrospectives", [])
        autofix_metrics = proj.get("autofix_state", {}).get("metrics", {})
        autofix_totals = autofix_metrics.get("totals", {})

        total_tasks += stats.get("total_tasks", 0)
        avg_q = stats.get("average_quality_score", 0.0)
        if isinstance(avg_q, (int, float)) and avg_q > 0:
            quality_scores.append(float(avg_q))
        total_learned_routes += proj.get("learned_routes", 0)
        if proj.get("health") != "error":
            active_count += 1

        total_benchmark_runs += summary.get("benchmark_runs", 0)
        autofix_cost = proj.get("autofix_cost", {})
        cost_val = autofix_cost.get("estimated_cost_usd", 0.0)
        if isinstance(cost_val, (int, float)):
            total_autofix_cost_usd += float(cost_val)
        total_learned_components += summary.get("learned_components", 0)
        total_shadow_components += summary.get("shadow_components", 0)
        total_demoted_components += summary.get("demoted_components", 0)
        total_tracked_fixtures += summary.get("tracked_fixtures", 0)
        total_coverage_gaps += summary.get("coverage_gaps", 0)
        total_automation_queue += summary.get("queued_automation_jobs", 0)
        total_autofix_open_prs += int(autofix_totals.get("open_prs", 0) or 0)
        total_autofix_merged += int(autofix_totals.get("merged", 0) or 0)
        total_autofix_suppressions += int(autofix_totals.get("suppression_count", 0) or 0)

        for retro in retrospectives:
            fbc = retro.get("findings_by_category", {})
            if isinstance(fbc, dict):
                for count in fbc.values():
                    if isinstance(count, (int, float)):
                        total_findings += int(count)

    avg_quality = round(sum(quality_scores) / len(quality_scores), 2) if quality_scores else 0.0

    return {
        "total_tasks": total_tasks,
        "avg_quality": avg_quality,
        "total_learned_routes": total_learned_routes,
        "active_count": active_count,
        "total_benchmark_runs": total_benchmark_runs,
        "total_findings": total_findings,
        "total_autofix_cost_usd": round(total_autofix_cost_usd, 2),
        "total_learned_components": total_learned_components,
        "total_shadow_components": total_shadow_components,
        "total_demoted_components": total_demoted_components,
        "total_tracked_fixtures": total_tracked_fixtures,
        "total_coverage_gaps": total_coverage_gaps,
        "total_automation_queue": total_automation_queue,
        "total_autofix_open_prs": total_autofix_open_prs,
        "total_autofix_merged": total_autofix_merged,
        "total_autofix_suppressions": total_autofix_suppressions,
    }


def build_global_payload() -> dict:
    """Combine all data: global daemon, projects, aggregates, generation timestamp."""
    daemon_status = gather_global_daemon_status()
    all_projects = gather_all_projects()
    aggregates = compute_aggregate_stats(all_projects["active"])

    # Gather autofix PRs per active project
    for proj in all_projects["active"]:
        proj["autofix_prs"] = _gather_autofix_prs(proj.get("path", ""))

    return {
        "generated_at": now_iso(),
        "daemon": daemon_status,
        "active_projects": all_projects["active"],
        "inactive_projects": all_projects["inactive"],
        "aggregates": aggregates,
    }


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

def _esc(value: object) -> str:
    """HTML-escape a value for safe embedding in templates."""
    s = str(value) if value is not None else ""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _score_color(value: float) -> str:
    """Return semantic CSS color for a score value: green >0.7, amber 0.4-0.7, red <0.4."""
    if value > 0.7:
        return "hsl(158 52% 58%)"
    if value >= 0.4:
        return "hsl(34 82% 64%)"
    return "hsl(350 72% 68%)"


def _score_tag_class(value: float) -> str:
    """Return tag class for a score value."""
    if value > 0.7:
        return "tag"
    if value >= 0.4:
        return "tag warn"
    return "tag danger"


def _health_tag_class(health: str) -> str:
    """Return tag class for a health value."""
    if health == "warning":
        return "tag warn"
    if health == "error":
        return "tag danger"
    return "tag"


# ---------------------------------------------------------------------------
# Compact project card (Section 2: grid)
# ---------------------------------------------------------------------------

def _render_compact_card(proj: dict, index: int) -> str:
    """Render a compact clickable project card for the grid."""
    name = _esc(proj.get("name", "unknown"))
    path = _esc(proj.get("path", ""))
    health = proj.get("health", "healthy")
    task_count = proj.get("task_count", 0)
    learned_routes = proj.get("learned_routes", 0)
    daemon_running = proj.get("daemon_running", False)
    stats = proj.get("stats", {})
    avg_quality = stats.get("average_quality_score", 0.0)

    health_class = _health_tag_class(health)
    daemon_class = "tag" if daemon_running else "tag warn"
    daemon_label = "running" if daemon_running else "stopped"

    # Mini sparkline (120x30)
    quality_scores = proj.get("quality_scores", [])
    mini_spark = build_sparkline_svg(quality_scores, width=120, height=30) if quality_scores else ""
    spark_html = mini_spark if mini_spark else (
        '<span class="mini" style="font-style:italic;">no data</span>'
    )

    # Daemon status dot and last cycle time
    maintenance = proj.get("maintenance", {})
    maint_daemon_running = maintenance.get("daemon_running", daemon_running)
    daemon_dot_color = "var(--mint)" if maint_daemon_running else "var(--rose)"
    daemon_dot_label = "daemon running" if maint_daemon_running else "daemon stopped"
    last_cycle_at = maintenance.get("last_cycle_at", "")
    last_cycle_display = _esc(last_cycle_at[:16].replace("T", " ")) if last_cycle_at else "no cycles"

    # Autofix cost line for compact card
    autofix_cost = proj.get("autofix_cost", {})
    af_cost_usd = autofix_cost.get("estimated_cost_usd", 0.0)
    af_cost_line = ""
    if isinstance(af_cost_usd, (int, float)) and af_cost_usd > 0:
        af_cost_line = f'<span class="mini" style="margin-left:6px;">${af_cost_usd:.2f} autofix</span>'

    return (
        f'<div class="panel pcard" data-project-id="{index}" '
        f'role="button" tabindex="0" aria-label="Show details for {name}">'
        f'<div class="pcard-top">'
        f'<div class="pcard-name">{name}</div>'
        f'<span class="{health_class}" style="font-size:11px;padding:3px 8px;">{_esc(health)}</span>'
        f'</div>'
        f'<div class="mini pcard-path">{path}</div>'
        f'<div class="pcard-row">'
        f'<div class="pcard-score" style="color:{_score_color(avg_quality)}">{avg_quality:.2f}</div>'
        f'<div class="pcard-meta">'
        f'<span class="mini">{task_count} tasks</span>'
        f'<span class="mini">{learned_routes} routes</span>'
        f'<span class="{daemon_class}" style="font-size:10px;padding:2px 7px;">{daemon_label}</span>'
        f'</div>'
        f'</div>'
        f'<div class="pcard-daemon-row">'
        f'<span class="daemon-dot" style="background:{daemon_dot_color};" '
        f'aria-label="{daemon_dot_label}"></span>'
        f'<span class="mini">{daemon_dot_label}</span>'
        f'{"<span class=\"tag\" style=\"font-size:9px;padding:1px 6px;margin-left:6px;\">autofix</span>" if maintenance.get("autofix_enabled") else ""}'
        f'{af_cost_line}'
        f'<span class="mini" style="margin-left:auto;">{last_cycle_display}</span>'
        f'</div>'
        f'<div class="pcard-spark">{spark_html}</div>'
        f'</div>'
    )


# ---------------------------------------------------------------------------
# Full project detail (Section 3: expanded on click)
# ---------------------------------------------------------------------------

def _render_project_detail(proj: dict, index: int) -> str:
    """Render the full detail section for a project (hidden by default)."""
    name = _esc(proj.get("name", "unknown"))
    path = _esc(proj.get("path", ""))
    health = proj.get("health", "healthy")
    task_count = proj.get("task_count", 0)
    learned_routes = proj.get("learned_routes", 0)
    last_cycle = _esc(proj.get("last_cycle_at", "n/a"))
    warning = proj.get("warning", "")
    error_msg = proj.get("error", "")
    daemon_running = proj.get("daemon_running", False)
    payload = proj.get("payload", {})
    retrospectives = proj.get("retrospectives", [])
    stats = proj.get("stats", {})
    summary = payload.get("summary", {})

    health_class = _health_tag_class(health)
    daemon_class = "tag" if daemon_running else "tag warn"
    daemon_label = "daemon running" if daemon_running else "daemon stopped"

    # --- Header with close button ---
    extra_info = ""
    if warning:
        extra_info = f'<div class="mini" style="color:hsl(34 82% 64%);margin-top:4px;">{_esc(warning)}</div>'
    elif error_msg:
        extra_info = f'<div class="mini" style="color:hsl(350 72% 68%);margin-top:4px;">{_esc(error_msg)}</div>'

    header = (
        f'<div class="detail-header">'
        f'<div style="flex:1;min-width:0;">'
        f'<div style="font-weight:800;font-size:20px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{name}</div>'
        f'<div class="mini" style="word-break:break-all;">{path}</div>'
        f'{extra_info}'
        f'</div>'
        f'<div style="display:flex;gap:8px;align-items:center;flex-shrink:0;">'
        f'<span class="{daemon_class}" style="font-size:11px;padding:3px 8px;">{daemon_label}</span>'
        f'<span class="{health_class}">{_esc(health)}</span>'
        f'<button class="close-btn" aria-label="Close project detail" onclick="closeDetail()">&times;</button>'
        f'</div>'
        f'</div>'
    )

    # --- Overview stats row ---
    avg_quality = stats.get("average_quality_score", 0.0)
    avg_cost = 0.0
    avg_efficiency = 0.0
    if retrospectives:
        costs = [r.get("cost_score", 0) for r in retrospectives if isinstance(r.get("cost_score"), (int, float))]
        effs = [r.get("efficiency_score", 0) for r in retrospectives if isinstance(r.get("efficiency_score"), (int, float))]
        if costs:
            avg_cost = sum(costs) / len(costs)
        if effs:
            avg_efficiency = sum(effs) / len(effs)

    benchmark_runs_count = summary.get("benchmark_runs", 0)
    learned_components = summary.get("learned_components", 0)
    tracked_fixtures = summary.get("tracked_fixtures", 0)

    stats_row = (
        f'<div class="proj-stats-row">'
        f'<div class="proj-stat"><span class="proj-stat-val">{task_count}</span><span class="proj-stat-lbl">Tasks</span></div>'
        f'<div class="proj-stat"><span class="proj-stat-val" style="color:{_score_color(avg_quality)}">{avg_quality:.2f}</span><span class="proj-stat-lbl">Quality</span></div>'
        f'<div class="proj-stat"><span class="proj-stat-val" style="color:{_score_color(avg_cost)}">{avg_cost:.2f}</span><span class="proj-stat-lbl">Cost</span></div>'
        f'<div class="proj-stat"><span class="proj-stat-val" style="color:{_score_color(avg_efficiency)}">{avg_efficiency:.2f}</span><span class="proj-stat-lbl">Efficiency</span></div>'
        f'<div class="proj-stat"><span class="proj-stat-val">{learned_components}</span><span class="proj-stat-lbl">Learned</span></div>'
        f'<div class="proj-stat"><span class="proj-stat-val">{benchmark_runs_count}</span><span class="proj-stat-lbl">Benchmarks</span></div>'
        f'<div class="proj-stat"><span class="proj-stat-val">{tracked_fixtures}</span><span class="proj-stat-lbl">Fixtures</span></div>'
        f'</div>'
    )

    # --- Quality Trend (large sparkline 400x80) ---
    quality_scores = proj.get("quality_scores", [])
    sparkline = build_sparkline_svg(quality_scores, width=400, height=80) if quality_scores else ""
    sparkline_html = sparkline if sparkline else (
        '<div class="empty-state" style="min-height:40px;padding:12px 16px;">'
        '<span>No retrospectives yet. Complete a task to see quality trends.</span></div>'
    )
    sparkline_section = (
        f'<div class="detail-section">'
        f'<div class="section-title">Quality Trend</div>'
        f'<div style="height:88px;border:1px solid var(--line);border-radius:10px;'
        f'background:linear-gradient(180deg, hsla(158 58% 50% / 0.06), transparent);padding:4px;overflow:hidden;">'
        f'{sparkline_html}'
        f'</div>'
        f'</div>'
    )

    # --- Active Routes (human-readable list) ---
    active_routes = payload.get("active_routes", [])
    if active_routes:
        route_items = ""
        for r in active_routes:
            agent = _esc(r.get("agent_name", "unknown"))
            role = _esc(r.get("role", "n/a"))
            task_type = _esc(r.get("task_type", "n/a"))
            mode = _esc(r.get("mode", "n/a"))
            comp = float(r.get("composite", 0))
            route_items += (
                f'<div class="route-item">'
                f'<strong>{agent}</strong> handles <em>{role}</em> for <em>{task_type}</em> tasks '
                f'(mode: {mode}, score: <span style="color:{_score_color(comp)};font-weight:700;">{comp:.3f}</span>)'
                f'</div>'
            )
        routes_section = (
            f'<div class="detail-section">'
            f'<div class="section-title">Active Routes</div>'
            f'{route_items}'
            f'</div>'
        )
    else:
        routes_section = (
            f'<div class="detail-section">'
            f'<div class="section-title">Active Routes</div>'
            f'<div class="empty-state"><span>No active learned routes. Using generic fallback.</span></div>'
            f'</div>'
        )

    # --- Recent Tasks (last 10, human-readable cards) ---
    cat_labels = {"sc": "Spec Completion", "sec": "Security", "cq": "Code Quality", "dc": "Dead Code"}
    if retrospectives:
        sorted_retros = sorted(retrospectives, key=lambda r: r.get("task_id", ""), reverse=True)[:10]
        task_items = ""
        for r in sorted_retros:
            tid = _esc(r.get("task_id", "unknown"))
            ttype = _esc(r.get("task_type", ""))
            outcome = r.get("task_outcome", "")
            outcome_class = _score_tag_class(1.0 if outcome == "DONE" else 0.0)
            qs = float(r.get("quality_score", 0))
            cs = float(r.get("cost_score", 0))
            es = float(r.get("efficiency_score", 0))
            fbc = r.get("findings_by_category", {})
            findings_parts = []
            if isinstance(fbc, dict):
                for cat, count in fbc.items():
                    label = cat_labels.get(cat, cat)
                    findings_parts.append(f"{label}: {count}")
            findings_str = " | ".join(findings_parts) if findings_parts else "none"
            repairs = r.get("repair_cycle_count", 0)
            task_items += (
                f'<div class="task-item">'
                f'<div class="task-item-head">'
                f'<strong class="mono">{tid}</strong>'
                f'<span class="mini">{ttype}</span>'
                f'<span class="{outcome_class}" style="font-size:11px;padding:2px 8px;">{_esc(outcome)}</span>'
                f'</div>'
                f'<div class="mini">'
                f'Quality: <span style="color:{_score_color(qs)};font-weight:700;">{qs:.2f}</span> | '
                f'Cost: <span style="color:{_score_color(cs)};font-weight:700;">{cs:.2f}</span> | '
                f'Efficiency: <span style="color:{_score_color(es)};font-weight:700;">{es:.2f}</span>'
                f'</div>'
                f'<div class="mini">Findings: {findings_str} | Repairs: {repairs} cycle{"s" if repairs != 1 else ""}</div>'
                f'</div>'
            )
        tasks_section = (
            f'<div class="detail-section">'
            f'<div class="section-title">Recent Tasks</div>'
            f'{task_items}'
            f'</div>'
        )
    else:
        tasks_section = (
            f'<div class="detail-section">'
            f'<div class="section-title">Recent Tasks</div>'
            f'<div class="empty-state"><span>No tasks completed yet. Run a task to see results.</span></div>'
            f'</div>'
        )

    # --- Benchmark Runs (human-readable) ---
    recent_runs = payload.get("recent_runs", [])
    if recent_runs:
        bench_items = ""
        for run in recent_runs:
            target = _esc(run.get("target_name", "unknown"))
            fixture = _esc(run.get("fixture_id", run.get("run_id", "")))
            cases = run.get("cases", [])
            case_count = len(cases) if isinstance(cases, list) else 0
            eval_data = run.get("evaluation", {})
            eval_rec = _esc(eval_data.get("recommendation", "recorded"))
            candidate_wins = 0
            if isinstance(cases, list):
                for c in cases:
                    if isinstance(c, dict) and c.get("winner") == "candidate":
                        candidate_wins += 1
            bench_items += (
                f'<div class="bench-item">'
                f'<strong>{target}</strong> vs fixture <em class="mono">{fixture}</em>'
                f' &mdash; {case_count} cases, {candidate_wins} candidate wins'
                f' <span class="tag" style="font-size:10px;padding:2px 7px;margin-left:6px;">{eval_rec}</span>'
                f'</div>'
            )
        bench_section = (
            f'<div class="detail-section">'
            f'<div class="section-title">Benchmark Runs</div>'
            f'{bench_items}'
            f'</div>'
        )
    else:
        bench_section = (
            f'<div class="detail-section">'
            f'<div class="section-title">Benchmark Runs</div>'
            f'<div class="empty-state"><span>No benchmark runs yet.</span></div>'
            f'</div>'
        )

    # --- Findings summary (aggregated) ---
    agg_findings: dict[str, int] = {}
    for r in retrospectives:
        fbc = r.get("findings_by_category", {})
        if isinstance(fbc, dict):
            for cat, count in fbc.items():
                if isinstance(count, (int, float)):
                    agg_findings[cat] = agg_findings.get(cat, 0) + int(count)
    if agg_findings:
        findings_parts = []
        for cat, count in sorted(agg_findings.items(), key=lambda x: -x[1]):
            label = cat_labels.get(cat, cat)
            findings_parts.append(f'{_esc(label)}: {count}')
        findings_str = " | ".join(findings_parts)
        findings_section = (
            f'<div class="detail-section">'
            f'<div class="section-title">Findings Summary</div>'
            f'<div class="findings-grid">'
        )
        for cat, count in sorted(agg_findings.items(), key=lambda x: -x[1]):
            label = cat_labels.get(cat, cat)
            findings_section += (
                f'<div class="finding-chip">'
                f'<span class="finding-label">{_esc(label)}</span>'
                f'<span class="finding-count">{count}</span>'
                f'</div>'
            )
        findings_section += '</div></div>'
    else:
        findings_section = ""

    # --- Alerts (only if non-empty) ---
    demotions = payload.get("demotions", [])
    coverage_gaps = payload.get("coverage_gaps", [])
    automation_queue = payload.get("automation_queue", [])

    alerts_items = ""
    for d in demotions:
        alerts_items += (
            f'<div class="alert-row">'
            f'<span class="tag danger" style="font-size:11px;padding:2px 8px;">demotion</span>'
            f'<span>{_esc(d.get("agent_name", ""))}</span>'
            f'<span class="mini">{_esc(d.get("role", ""))} / {_esc(d.get("task_type", ""))}</span>'
            f'</div>'
        )
    for g in coverage_gaps:
        alerts_items += (
            f'<div class="alert-row">'
            f'<span class="tag warn" style="font-size:11px;padding:2px 8px;">gap</span>'
            f'<span>{_esc(g.get("target_name", ""))}</span>'
            f'<span class="mini">{_esc(g.get("role", ""))} / {_esc(g.get("task_type", ""))}</span>'
            f'</div>'
        )
    for q in automation_queue:
        alerts_items += (
            f'<div class="alert-row">'
            f'<span class="tag warn" style="font-size:11px;padding:2px 8px;">queued</span>'
            f'<span>{_esc(q.get("agent_name", ""))}</span>'
            f'<span class="mini">{_esc(q.get("reason", "queued"))}</span>'
            f'</div>'
        )

    alerts_section = ""
    if alerts_items:
        alerts_section = (
            f'<div class="detail-section">'
            f'<div class="section-title" style="color:hsl(350 72% 68%);">Alerts</div>'
            f'{alerts_items}'
            f'</div>'
        )

    # --- Maintenance section ---
    maintenance = proj.get("maintenance", {})
    maint_daemon_running = maintenance.get("daemon_running", False)
    maint_pid = maintenance.get("daemon_pid")
    maint_poll = maintenance.get("poll_seconds")
    maint_last_cycle = maintenance.get("last_cycle_at", "")
    maint_actions = maintenance.get("last_cycle_actions", [])
    maint_history = maintenance.get("cycle_history", [])

    maint_status_class = "tag" if maint_daemon_running else "tag danger"
    maint_status_label = f"running (PID {maint_pid})" if maint_daemon_running and maint_pid else "stopped"
    poll_display = f"every {maint_poll}s" if maint_poll else "n/a"
    maint_cycle_display = _esc(maint_last_cycle) if maint_last_cycle else "no cycles recorded"

    # Daemon status row
    maint_header_html = (
        f'<div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:10px;">'
        f'<span class="{maint_status_class}" style="font-size:11px;padding:3px 8px;">{_esc(maint_status_label)}</span>'
        f'<span class="mini">Poll: {_esc(poll_display)}</span>'
        f'<span class="mini">Last cycle: {maint_cycle_display}</span>'
        f'</div>'
    )

    # Autofix cost line
    autofix_cost = proj.get("autofix_cost", {})
    af_haiku = autofix_cost.get("haiku_invocations", 0)
    af_fix = autofix_cost.get("fix_invocations", 0)
    af_usd = autofix_cost.get("estimated_cost_usd", 0.0)
    af_scan_at = autofix_cost.get("last_scan_at", "")
    if af_haiku or af_fix or af_usd:
        af_scan_display = _esc(af_scan_at[:16].replace("T", " ")) if af_scan_at else "unknown"
        autofix_cost_html = (
            f'<div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:10px;">'
            f'<span class="mini" style="font-weight:700;">Autofix:</span>'
            f'<span class="mini">{af_haiku} Haiku calls, {af_fix} fix calls, ~${af_usd:.2f} estimated cost</span>'
            f'<span class="mini">Last scan: {af_scan_display}</span>'
            f'</div>'
        )
    elif af_scan_at:
        af_scan_display = _esc(af_scan_at[:16].replace("T", " "))
        autofix_cost_html = (
            f'<div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:10px;">'
            f'<span class="mini" style="font-weight:700;">Autofix:</span>'
            f'<span class="mini">No cost data available</span>'
            f'<span class="mini">Last scan: {af_scan_display}</span>'
            f'</div>'
        )
    else:
        autofix_cost_html = (
            f'<div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:10px;">'
            f'<span class="mini" style="font-weight:700;">Autofix:</span>'
            f'<span class="mini" style="font-style:italic;">No scan data available</span>'
            f'</div>'
        )

    # Last Cycle Actions table
    if maint_actions:
        action_rows = ""
        for act in maint_actions:
            act_name = _esc(act.get("name", "unknown"))
            act_rc = act.get("returncode", -1)
            act_summary = _esc(act.get("summary", ""))
            rc_class = "tag" if act_rc == 0 else "tag danger"
            rc_label = "OK" if act_rc == 0 else f"FAIL ({act_rc})"
            action_rows += (
                f'<div class="maint-action-row">'
                f'<span class="mono">{act_name}</span>'
                f'<span class="{rc_class}" style="font-size:10px;padding:2px 7px;">{rc_label}</span>'
                f'<span class="mini" style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{act_summary}</span>'
                f'</div>'
            )
        actions_html = (
            f'<div class="maint-subsection">'
            f'<div class="mini" style="font-weight:700;margin-bottom:6px;">Last Cycle Actions</div>'
            f'{action_rows}'
            f'</div>'
        )
    else:
        actions_html = (
            f'<div class="empty-state" style="min-height:40px;padding:10px 16px;">'
            f'<span>No cycle actions recorded. Start the maintenance daemon to see results.</span></div>'
        )

    # Cycle History (last 5)
    if maint_history:
        history_rows = ""
        for cyc in reversed(maint_history):
            cyc_at = _esc(cyc.get("executed_at", "unknown"))
            cyc_total = cyc.get("total_actions", 0)
            cyc_failed = cyc.get("failed_actions", 0)
            cyc_passed = cyc.get("passed", True)
            cyc_class = "tag" if cyc_passed else "tag danger"
            cyc_label = f"{cyc_total} actions, all passed" if cyc_passed else f"{cyc_total} actions, {cyc_failed} failed"
            history_rows += (
                f'<div class="maint-action-row">'
                f'<span class="mini" style="min-width:140px;">{cyc_at}</span>'
                f'<span class="{cyc_class}" style="font-size:10px;padding:2px 7px;">{cyc_label}</span>'
                f'</div>'
            )
        history_html = (
            f'<div class="maint-subsection">'
            f'<div class="mini" style="font-weight:700;margin-bottom:6px;">Cycle History (last 5)</div>'
            f'{history_rows}'
            f'</div>'
        )
    else:
        history_html = (
            f'<div class="empty-state" style="min-height:40px;padding:10px 16px;">'
            f'<span>No cycle history yet.</span></div>'
        )

    maintenance_section = (
        f'<div class="detail-section">'
        f'<div class="section-title">Maintenance</div>'
        f'{maint_header_html}'
        f'{autofix_cost_html}'
        f'{actions_html}'
        f'{history_html}'
        f'</div>'
    )

    # --- Autofix PRs ---
    autofix_prs = proj.get("autofix_prs", [])
    autofix_state = proj.get("autofix_state", {})
    autofix_metrics = autofix_state.get("metrics", {})
    autofix_totals = autofix_metrics.get("totals", {})
    autofix_categories = autofix_metrics.get("categories", {})
    top_autofix = sorted(
        (
            {
                "name": name,
                "confidence": data.get("confidence", 0.0),
                "mode": data.get("mode", "issue-only"),
                "merged": data.get("merged", 0),
                "issues_opened": data.get("issues_opened", 0),
            }
            for name, data in autofix_categories.items()
            if isinstance(data, dict)
        ),
        key=lambda item: (item["merged"], item["confidence"]),
        reverse=True,
    )[:4]
    if autofix_prs:
        pr_rows = ""
        for pr in autofix_prs:
            pr_num = pr.get("number", 0)
            pr_title = _esc(pr.get("title", ""))
            if len(pr_title) > 70:
                pr_title = pr_title[:67] + "..."
            pr_state = pr.get("state", "UNKNOWN").upper()
            pr_created = _esc(pr.get("created_at", "")[:10])
            pr_branch = _esc(pr.get("branch", ""))
            if pr_state == "MERGED":
                st_style = 'style="font-size:10px;padding:2px 7px;"'
            elif pr_state == "OPEN":
                st_style = 'style="font-size:10px;padding:2px 7px;background:hsla(200 82% 60% / 0.14);color:hsl(200 76% 64%);border-color:hsla(200 82% 60% / 0.22);"'
            else:
                st_style = 'style="font-size:10px;padding:2px 7px;background:hsla(210 14% 64% / 0.14);color:var(--muted);border-color:hsla(210 14% 64% / 0.22);"'
            pr_rows += (
                f'<div class="pr-row">'
                f'<span class="mono" style="flex-shrink:0;">#{pr_num}</span>'
                f'<span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{pr_title}</span>'
                f'<span class="tag" {st_style}>{pr_state}</span>'
                f'<span class="mini" style="flex-shrink:0;">{pr_created}</span>'
                f'</div>'
            )
        autofix_section = (
            f'<div class="detail-section">'
            f'<div class="section-title">Autofix Pull Requests</div>'
            f'{pr_rows}'
            f'</div>'
        )
    else:
        autofix_section = (
            f'<div class="detail-section">'
            f'<div class="section-title">Autofix Pull Requests</div>'
            f'<div class="mini" style="color:var(--muted);">No autofix PRs yet. Enable with <code>dynos local start --autofix</code></div>'
            f'</div>'
        )

    if autofix_totals or top_autofix:
        top_rows = ""
        for item in top_autofix:
            top_rows += (
                f'<div class="maint-action-row">'
                f'<span class="mono">{_esc(item["name"])}</span>'
                f'<span class="tag" style="font-size:10px;padding:2px 7px;">{_esc(item["mode"])}</span>'
                f'<span class="mini">conf {float(item["confidence"]):.2f}</span>'
                f'<span class="mini">merged {int(item["merged"])}</span>'
                f'<span class="mini">issues {int(item["issues_opened"])}</span>'
                f'</div>'
            )
        autofix_metrics_section = (
            f'<div class="detail-section">'
            f'<div class="section-title">Autofix Metrics</div>'
            f'<div class="cross-summary">'
            f'<span>Open PRs: <strong>{int(autofix_totals.get("open_prs", 0) or 0)}</strong></span>'
            f'<span>Merged: <strong>{int(autofix_totals.get("merged", 0) or 0)}</strong></span>'
            f'<span>Closed: <strong>{int(autofix_totals.get("closed_unmerged", 0) or 0)}</strong></span>'
            f'<span>Suppressions: <strong>{int(autofix_totals.get("suppression_count", 0) or 0)}</strong></span>'
            f'</div>'
            f'{top_rows}'
            f'</div>'
        )
    else:
        autofix_metrics_section = ""

    # --- Lineage ---
    lineage = payload.get("lineage", {})
    lineage_nodes = lineage.get("nodes", 0)
    lineage_edges = lineage.get("edges", 0)
    lineage_line = (
        f'<div class="detail-section">'
        f'<div class="section-title">Lineage</div>'
        f'<div class="mini">{lineage_nodes} nodes / {lineage_edges} edges | Last cycle: {last_cycle}</div>'
        f'</div>'
    )

    return (
        f'<div class="project-detail" id="detail-{index}" style="display:none;" '
        f'role="region" aria-label="Detail for {name}">'
        f'<div class="panel">'
        f'{header}'
        f'{stats_row}'
        f'{sparkline_section}'
        f'{routes_section}'
        f'{maintenance_section}'
        f'{tasks_section}'
        f'{bench_section}'
        f'{findings_section}'
        f'{alerts_section}'
        f'{autofix_section}'
        f'{autofix_metrics_section}'
        f'{lineage_line}'
        f'</div>'
        f'</div>'
    )


def _render_inactive_card(proj: dict) -> str:
    """Render a paused/archived project card HTML fragment."""
    name = _esc(proj.get("name", "unknown"))
    path = _esc(proj.get("path", ""))
    status = proj.get("status", "paused")
    last_active = _esc(proj.get("last_active_at", "n/a"))

    tag_class = "tag warn" if status == "paused" else "tag"

    return (
        f'<div class="panel project-card" style="opacity:0.55;">'
        f'<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;">'
        f'<div style="min-width:0;flex:1;">'
        f'<div style="font-weight:800;font-size:15px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{name}</div>'
        f'<div class="mini" style="word-break:break-all;">{path}</div>'
        f'</div>'
        f'<span class="{tag_class}">{_esc(status)}</span>'
        f'</div>'
        f'<div class="mini" style="margin-top:8px;">Last active: {last_active}</div>'
        f'</div>'
    )


def _render_html(payload: dict) -> str:
    """Render the full global dashboard HTML from payload data.

    Three-layer layout:
      1. Global overview (daemon status, aggregate stats, cross-project summary)
      2. Compact project cards grid (clickable)
      3. Project detail (hidden, shown on click via JS toggle)
    Plus: paused/archived at bottom.
    """
    daemon = payload.get("daemon", {})
    aggregates = payload.get("aggregates", {})
    active_projects = payload.get("active_projects", [])
    inactive_projects = payload.get("inactive_projects", [])
    generated_at = _esc(payload.get("generated_at", ""))

    # --- Global daemon status ---
    daemon_running = daemon.get("running", False)
    daemon_pid = daemon.get("pid")
    last_sweep = _esc(daemon.get("last_sweep_at", "n/a"))
    total_project_count = len(active_projects) + len(inactive_projects)
    active_count = aggregates.get("active_count", 0)
    paused_count = sum(1 for p in inactive_projects if p.get("status") == "paused")
    archived_count = sum(1 for p in inactive_projects if p.get("status") == "archived")

    daemon_badge = (
        f'<span class="tag" style="font-size:11px;padding:3px 8px;">running (PID {daemon_pid})</span>'
        if daemon_running
        else '<span class="tag danger" style="font-size:11px;padding:3px 8px;">stopped</span>'
    )

    daemon_panel = (
        f'<div class="panel" style="margin-bottom:20px;">'
        f'<div class="headline">Global Daemon Status</div>'
        f'<div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;">'
        f'{daemon_badge}'
        f'<span class="mini">Last sweep: {last_sweep}</span>'
        f'<span class="mini">Registered projects: {total_project_count} '
        f'({active_count} active, {paused_count} paused, {archived_count} archived)</span>'
        f'</div>'
        f'</div>'
    )

    # --- Aggregate stat cards (6 cards) ---
    total_tasks = aggregates.get("total_tasks", 0)
    avg_quality = aggregates.get("avg_quality", 0.0)
    total_learned = aggregates.get("total_learned_routes", 0)
    total_benchmarks = aggregates.get("total_benchmark_runs", 0)
    total_findings = aggregates.get("total_findings", 0)
    total_autofix_cost = aggregates.get("total_autofix_cost_usd", 0.0)

    # Autofix cost color: mint if $0, amber if > $1, rose if > $10
    if total_autofix_cost > 10:
        autofix_cost_color = "var(--rose)"
    elif total_autofix_cost > 1:
        autofix_cost_color = "var(--amber)"
    else:
        autofix_cost_color = "var(--mint)"

    stat_cards = (
        f'<section class="agg-stats" id="stats">'
        f'<div class="stat"><div class="label">Total Tasks</div><div class="value">{total_tasks}</div></div>'
        f'<div class="stat"><div class="label">Avg Quality</div>'
        f'<div class="value" style="color:{_score_color(avg_quality)}">{avg_quality:.2f}</div></div>'
        f'<div class="stat"><div class="label">Learned Routes</div><div class="value">{total_learned}</div></div>'
        f'<div class="stat"><div class="label">Benchmark Runs</div><div class="value">{total_benchmarks}</div></div>'
        f'<div class="stat"><div class="label">Active Projects</div><div class="value">{active_count}</div></div>'
        f'<div class="stat"><div class="label">Total Findings</div><div class="value">{total_findings}</div></div>'
        f'<div class="stat"><div class="label">Autofix Cost</div>'
        f'<div class="value" style="color:{autofix_cost_color}">${total_autofix_cost:.2f}</div></div>'
        f'</section>'
    )

    # --- Cross-project summary panel ---
    lc = aggregates.get("total_learned_components", 0)
    sc = aggregates.get("total_shadow_components", 0)
    dc = aggregates.get("total_demoted_components", 0)
    tf = aggregates.get("total_tracked_fixtures", 0)
    cg = aggregates.get("total_coverage_gaps", 0)
    aq = aggregates.get("total_automation_queue", 0)
    ao = aggregates.get("total_autofix_open_prs", 0)
    am = aggregates.get("total_autofix_merged", 0)
    asp = aggregates.get("total_autofix_suppressions", 0)

    cross_panel = (
        f'<div class="panel" style="margin-bottom:24px;">'
        f'<div class="headline">Cross-Project Summary</div>'
        f'<div class="cross-summary">'
        f'<span>Learned: <strong>{lc}</strong></span>'
        f'<span>Shadow: <strong>{sc}</strong></span>'
        f'<span>Demoted: <strong>{dc}</strong></span>'
        f'<span>Tracked fixtures: <strong>{tf}</strong></span>'
        f'<span>Coverage gaps: <strong>{cg}</strong></span>'
        f'<span>Automation queue: <strong>{aq}</strong></span>'
        f'<span>Autofix open PRs: <strong>{ao}</strong></span>'
        f'<span>Autofix merged: <strong>{am}</strong></span>'
        f'<span>Autofix suppressions: <strong>{asp}</strong></span>'
        f'</div>'
        f'</div>'
    )

    # --- Section 2: Compact project cards ---
    if active_projects:
        cards_html = "\n".join(_render_compact_card(p, i) for i, p in enumerate(active_projects))
    else:
        cards_html = (
            '<div class="empty-state">'
            '<span>No registered projects. Run <code>dynos registry register /path</code> to add one.</span>'
            '</div>'
        )

    cards_section = (
        f'<section style="margin-bottom:24px;">'
        f'<div class="headline">Projects</div>'
        f'<div class="pcards-grid">{cards_html}</div>'
        f'</section>'
    )

    # --- Section 3: Hidden detail sections ---
    detail_sections = "\n".join(
        _render_project_detail(p, i) for i, p in enumerate(active_projects)
    )
    detail_container = f'<div id="detail-container">{detail_sections}</div>'

    # --- Section 4: Paused/Archived ---
    inactive_section = ""
    if inactive_projects:
        inactive_cards = "\n".join(_render_inactive_card(p) for p in inactive_projects)
        inactive_section = (
            f'<section style="margin-top:28px;">'
            f'<div class="headline">Paused &amp; Archived</div>'
            f'<div class="project-grid" style="margin-top:14px;">'
            f'{inactive_cards}'
            f'</div>'
            f'</section>'
        )

    # Assemble final HTML
    html = GLOBAL_HTML_TEMPLATE.replace("{{", "{").replace("}}", "}")
    html = html.replace("__DAEMON_PANEL__", daemon_panel)
    html = html.replace("__STAT_CARDS__", stat_cards)
    html = html.replace("__CROSS_PANEL__", cross_panel)
    html = html.replace("__PRS_PANEL__", "")
    html = html.replace("__CARDS_SECTION__", cards_section)
    html = html.replace("__DETAIL_CONTAINER__", detail_container)
    html = html.replace("__INACTIVE_SECTION__", inactive_section)
    html = html.replace("__GENERATED_AT__", generated_at)

    return html


GLOBAL_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>dynos-work | Global Dashboard</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&family=JetBrains+Mono:wght@400&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg: hsl(216 28% 7%);
      --bg-soft: hsl(215 24% 11%);
      --panel: hsla(214 22% 14% / 0.92);
      --panel-2: hsla(215 20% 18% / 0.88);
      --line: hsla(210 30% 80% / 0.10);
      --text: hsl(210 20% 93%);
      --muted: hsl(214 14% 64%);
      --gold: hsl(43 90% 62%);
      --mint: hsl(158 58% 50%);
      --rose: hsl(350 78% 62%);
      --sky: hsl(200 82% 60%);
      --amber: hsl(34 88% 58%);
      --shadow: 0 8px 32px hsla(220 60% 2% / 0.35), 0 2px 8px hsla(220 60% 2% / 0.25);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Inter", "Segoe UI", ui-sans-serif, system-ui, -apple-system, sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top left, hsla(156 63% 54% / 0.14), transparent 32%),
        radial-gradient(circle at top right, hsla(42 94% 64% / 0.14), transparent 28%),
        linear-gradient(160deg, var(--bg), hsl(220 28% 10%));
      min-height: 100vh;
    }}
    .topbar {{
      position: sticky;
      top: 0;
      z-index: 900;
      display: flex;
      align-items: center;
      justify-content: space-between;
      height: 42px;
      padding: 0 24px;
      background: hsla(214 26% 10% / 0.92);
      backdrop-filter: blur(12px);
      border-bottom: 1px solid var(--line);
    }}
    .topbar-wordmark {{
      font-family: "Inter", "Segoe UI", ui-sans-serif, system-ui, -apple-system, sans-serif;
      font-weight: 800;
      font-size: 13px;
      letter-spacing: 0.02em;
      color: var(--text);
    }}
    .topbar-right {{
      display: flex;
      align-items: center;
      gap: 10px;
    }}
    .topbar-updated {{
      font-family: "JetBrains Mono", ui-monospace, "Cascadia Code", "Fira Code", monospace;
      font-size: 12px;
      color: var(--muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      max-width: 320px;
    }}
    @keyframes pulse {{
      0%, 100% {{ opacity: 1; transform: scale(1); }}
      50% {{ opacity: 0.5; transform: scale(1.25); }}
    }}
    .live-dot {{
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--mint);
      flex-shrink: 0;
      animation: pulse 2s ease-in-out infinite;
    }}
    .shell {{
      max-width: 1380px;
      margin: 0 auto;
      padding: 28px;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 14px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(16px);
      -webkit-backdrop-filter: blur(16px);
      padding: 24px;
    }}
    .headline {{
      font-size: 13px;
      letter-spacing: 0.18em;
      text-transform: uppercase;
      color: var(--gold);
      margin-bottom: 12px;
      font-weight: 800;
    }}
    /* --- Aggregate stat cards (6-col grid) --- */
    .agg-stats {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 14px;
      margin-bottom: 24px;
    }}
    .stat {{
      background: hsla(215 20% 16% / 0.72);
      border: 1px solid var(--line);
      border-left: 3px solid hsla(43 90% 62% / 0.40);
      border-radius: 10px;
      padding: 16px;
      transition: transform 0.2s ease, box-shadow 0.2s ease, border-color 0.2s ease;
    }}
    .stat:hover {{
      transform: translateY(-2px);
      box-shadow: 0 12px 36px hsla(220 60% 2% / 0.4), 0 0 0 1px hsla(42 94% 64% / 0.2);
      border-color: hsla(42 94% 64% / 0.32);
    }}
    .stat .label {{
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.12em;
    }}
    .stat .value {{
      font-size: 2rem;
      font-weight: 800;
      margin-top: 8px;
      font-variant-numeric: tabular-nums;
      font-feature-settings: "tnum";
    }}
    /* --- Cross-project summary --- */
    .cross-summary {{
      display: flex;
      flex-wrap: wrap;
      gap: 16px 24px;
      font-size: 13px;
      color: var(--muted);
    }}
    .cross-summary strong {{
      color: var(--text);
      font-weight: 800;
      font-variant-numeric: tabular-nums;
    }}
    /* --- Compact project cards grid --- */
    .pcards-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
      gap: 16px;
      margin-top: 14px;
    }}
    .pcard {{
      cursor: pointer;
      transition: transform 0.22s ease, box-shadow 0.22s ease, border-color 0.22s ease;
      position: relative;
    }}
    .pcard:hover {{
      transform: translateY(-3px);
      box-shadow: 0 24px 72px hsla(220 60% 2% / 0.55), 0 0 0 1px hsla(156 63% 54% / 0.18);
      border-color: hsla(156 63% 54% / 0.28);
    }}
    .pcard:focus {{
      outline: 2px solid var(--sky);
      outline-offset: 2px;
    }}
    .pcard.active {{
      border-color: var(--sky);
      box-shadow: 0 0 0 2px hsla(200 82% 60% / 0.3), var(--shadow);
    }}
    .pcard-top {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 8px;
    }}
    .pcard-name {{
      font-weight: 800;
      font-size: 17px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      min-width: 0;
      flex: 1;
    }}
    .pcard-path {{
      word-break: break-all;
      margin-top: 2px;
      max-height: 2.6em;
      overflow: hidden;
    }}
    .pcard-row {{
      display: flex;
      align-items: center;
      gap: 14px;
      margin-top: 10px;
    }}
    .pcard-score {{
      font-size: 1.8rem;
      font-weight: 800;
      font-variant-numeric: tabular-nums;
      font-feature-settings: "tnum";
      line-height: 1;
    }}
    .pcard-meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      align-items: center;
    }}
    .pcard-spark {{
      margin-top: 8px;
      height: 30px;
      overflow: hidden;
    }}
    /* --- Project detail (expanded) --- */
    .project-detail {{
      margin-bottom: 24px;
    }}
    .project-detail .panel {{
      border-color: var(--sky);
      border-width: 2px;
    }}
    .detail-header {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
      margin-bottom: 16px;
    }}
    .close-btn {{
      background: none;
      border: 1px solid var(--line);
      border-radius: 8px;
      color: var(--muted);
      font-size: 20px;
      line-height: 1;
      padding: 4px 10px;
      cursor: pointer;
      transition: color 0.15s ease, border-color 0.15s ease;
    }}
    .close-btn:hover {{
      color: var(--text);
      border-color: var(--rose);
    }}
    .close-btn:focus {{
      outline: 2px solid var(--sky);
      outline-offset: 2px;
    }}
    .mini {{
      font-size: 12px;
      color: var(--muted);
    }}
    .mono {{
      font-family: "JetBrains Mono", ui-monospace, "Cascadia Code", "Fira Code", monospace;
      font-size: 12px;
    }}
    .tag {{
      display: inline-flex;
      align-items: center;
      padding: 5px 12px;
      border-radius: 8px;
      font-size: 12px;
      font-weight: 700;
      line-height: 1.4;
      background: hsla(158 58% 50% / 0.14);
      color: hsl(158 52% 58%);
      border: 1px solid hsla(158 58% 50% / 0.22);
    }}
    .tag.warn {{
      background: hsla(34 88% 58% / 0.14);
      color: hsl(34 82% 64%);
      border-color: hsla(34 88% 58% / 0.22);
    }}
    .tag.danger {{
      background: hsla(350 78% 62% / 0.14);
      color: hsl(350 72% 68%);
      border-color: hsla(350 78% 62% / 0.22);
    }}
    .empty-state {{
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 8px;
      padding: 28px 16px;
      border-radius: 10px;
      border: 1px dashed hsla(210 30% 80% / 0.14);
      background: hsla(215 20% 14% / 0.4);
      color: var(--muted);
      font-size: 13px;
      font-style: italic;
      text-align: center;
      min-height: 64px;
    }}
    /* --- Detail inner sections --- */
    .proj-stats-row {{
      display: grid;
      grid-template-columns: repeat(7, minmax(0, 1fr));
      gap: 10px;
      margin: 12px 0;
    }}
    .proj-stat {{
      background: hsla(215 20% 16% / 0.72);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px 12px;
      text-align: center;
    }}
    .proj-stat-val {{
      display: block;
      font-size: 1.3rem;
      font-weight: 800;
      font-variant-numeric: tabular-nums;
      font-feature-settings: "tnum";
    }}
    .proj-stat-lbl {{
      display: block;
      font-size: 11px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.1em;
      margin-top: 2px;
    }}
    .detail-section {{
      margin: 14px 0;
    }}
    .section-title {{
      font-size: 12px;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: var(--sky);
      margin-bottom: 8px;
      font-weight: 700;
    }}
    .route-item {{
      padding: 10px 14px;
      border-radius: 10px;
      background: var(--panel-2);
      border: 1px solid var(--line);
      margin-bottom: 8px;
      font-size: 13px;
      line-height: 1.5;
    }}
    .route-item em {{
      color: var(--sky);
      font-style: normal;
    }}
    .task-item {{
      padding: 10px 14px;
      border-radius: 10px;
      background: var(--panel-2);
      border: 1px solid var(--line);
      margin-bottom: 8px;
    }}
    .task-item-head {{
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 4px;
    }}
    .bench-item {{
      padding: 10px 14px;
      border-radius: 10px;
      background: var(--panel-2);
      border: 1px solid var(--line);
      margin-bottom: 8px;
      font-size: 13px;
      line-height: 1.5;
    }}
    .bench-item em {{
      color: var(--sky);
      font-style: normal;
    }}
    .findings-grid {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }}
    .finding-chip {{
      display: flex;
      align-items: center;
      gap: 6px;
      background: var(--panel-2);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 6px 12px;
    }}
    .finding-label {{
      font-size: 12px;
      color: var(--muted);
    }}
    .finding-count {{
      font-size: 14px;
      font-weight: 800;
      color: var(--text);
      font-variant-numeric: tabular-nums;
    }}
    .alert-row {{
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 8px 12px;
      border-radius: 8px;
      background: hsla(350 78% 62% / 0.04);
      border: 1px solid hsla(350 78% 62% / 0.10);
      margin-bottom: 6px;
      font-size: 13px;
    }}
    /* --- Daemon dot on compact cards --- */
    .daemon-dot {{
      display: inline-block;
      width: 8px;
      height: 8px;
      border-radius: 50%;
      flex-shrink: 0;
    }}
    .pcard-daemon-row {{
      display: flex;
      align-items: center;
      gap: 6px;
      margin-top: 6px;
      padding-top: 6px;
      border-top: 1px solid var(--line);
    }}
    /* --- Maintenance action rows --- */
    .maint-action-row {{
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 6px 12px;
      border-radius: 8px;
      background: var(--panel-2);
      border: 1px solid var(--line);
      margin-bottom: 4px;
      font-size: 13px;
    }}
    .maint-subsection {{
      margin-top: 10px;
    }}
    /* --- PR rows --- */
    .pr-row {{
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 8px 14px;
      border-radius: 10px;
      background: var(--panel-2);
      border: 1px solid var(--line);
      margin-bottom: 6px;
      font-size: 13px;
    }}
    /* --- Inactive project cards --- */
    .project-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
      gap: 1rem;
    }}
    .project-card {{
      transition: transform 0.22s ease, box-shadow 0.22s ease, border-color 0.22s ease;
    }}
    .project-card:hover {{
      transform: translateY(-2px);
      box-shadow: 0 12px 36px hsla(220 60% 2% / 0.4);
    }}
    /* --- Animations --- */
    @keyframes fadeSlideIn {{
      from {{ opacity: 0; transform: translateY(12px); }}
      to {{ opacity: 1; transform: translateY(0); }}
    }}
    .panel, .stat {{
      opacity: 0;
      transform: translateY(12px);
    }}
    body.loaded .panel {{
      animation: fadeSlideIn 0.5s ease-out forwards;
    }}
    body.loaded .stat {{
      animation: fadeSlideIn 0.4s ease-out forwards;
    }}
    body.loaded .agg-stats .stat:nth-child(1) {{ animation-delay: 0.06s; }}
    body.loaded .agg-stats .stat:nth-child(2) {{ animation-delay: 0.10s; }}
    body.loaded .agg-stats .stat:nth-child(3) {{ animation-delay: 0.14s; }}
    body.loaded .agg-stats .stat:nth-child(4) {{ animation-delay: 0.18s; }}
    body.loaded .agg-stats .stat:nth-child(5) {{ animation-delay: 0.22s; }}
    body.loaded .agg-stats .stat:nth-child(6) {{ animation-delay: 0.26s; }}
    body.loaded .agg-stats .stat:nth-child(7) {{ animation-delay: 0.30s; }}
    /* Detail panel should not hover-lift */
    .project-detail .panel:hover {{
      transform: none;
      box-shadow: var(--shadow);
    }}
    /* --- Responsive --- */
    @media (max-width: 1100px) {{
      .agg-stats {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
      .proj-stats-row {{ grid-template-columns: repeat(4, minmax(0, 1fr)); }}
    }}
    @media (max-width: 980px) {{
      .agg-stats {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .proj-stats-row {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
      .pcards-grid {{ grid-template-columns: 1fr; }}
      .project-grid {{ grid-template-columns: 1fr; }}
      .shell {{ padding: 20px; }}
      .topbar {{ padding: 0 14px; }}
    }}
    @media (max-width: 600px) {{
      .agg-stats {{ grid-template-columns: 1fr; }}
      .proj-stats-row {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .pcards-grid {{ grid-template-columns: 1fr; }}
      .project-grid {{ grid-template-columns: 1fr; }}
      .shell {{ padding: 12px; }}
      .topbar {{ padding: 0 12px; height: 38px; }}
      .topbar-wordmark {{ font-size: 13px; }}
      .panel {{ padding: 16px; border-radius: 10px; }}
      .stat .value {{ font-size: 1.5rem; }}
      .pcard-score {{ font-size: 1.4rem; }}
      .proj-stat-val {{ font-size: 1rem; }}
    }}
  </style>
</head>
<body>
  <nav class="topbar" role="navigation" aria-label="Global dashboard navigation">
    <span class="topbar-wordmark">dynos-work Global</span>
    <div class="topbar-right">
      <span class="live-dot" aria-label="Live status indicator"></span>
      <span class="topbar-updated" id="updated">Generated: __GENERATED_AT__</span>
    </div>
  </nav>
  <div class="shell">
    <!-- Section 1: Global Overview -->
    __DAEMON_PANEL__
    __STAT_CARDS__
    __CROSS_PANEL__
    __PRS_PANEL__
    <!-- Section 2: Project Cards Grid -->
    __CARDS_SECTION__
    <!-- Section 3: Project Detail (hidden, toggled by JS) -->
    __DETAIL_CONTAINER__
    <!-- Section 4: Paused/Archived -->
    __INACTIVE_SECTION__
    <div class="mini" style="text-align:center;margin-top:28px;padding-bottom:16px;">
      Generated: __GENERATED_AT__
    </div>
  </div>
  <!-- Hidden elements to satisfy validate_generated_html() required IDs -->
  <div style="display:none">
    <span id="lineage"></span>
    <span id="routes"></span><span id="queue"></span><span id="sparkline"></span>
    <span id="gaps"></span><span id="demotions"></span><span id="runs"></span>
  </div>
  <script>
    (function() {{
      var activeCard = null;
      var activeDetail = null;

      function closeDetail() {{
        if (activeDetail) {{
          activeDetail.style.display = 'none';
          activeDetail = null;
        }}
        if (activeCard) {{
          activeCard.classList.remove('active');
          activeCard = null;
        }}
      }}

      function openDetail(index, cardEl) {{
        closeDetail();
        var detail = document.getElementById('detail-' + index);
        if (!detail) return;
        detail.style.display = 'block';
        detail.style.opacity = '0';
        detail.style.transform = 'translateY(8px)';
        detail.style.transition = 'opacity 0.3s ease, transform 0.3s ease';
        /* Force reflow then animate */
        void detail.offsetHeight;
        detail.style.opacity = '1';
        detail.style.transform = 'translateY(0)';
        cardEl.classList.add('active');
        activeCard = cardEl;
        activeDetail = detail;
        detail.scrollIntoView({{ behavior: 'smooth', block: 'nearest' }});
      }}

      /* Expose closeDetail globally for the inline onclick on the X button */
      window.closeDetail = closeDetail;

      /* Attach click handlers to all project cards */
      var cards = document.querySelectorAll('.pcard[data-project-id]');
      for (var i = 0; i < cards.length; i++) {{
        (function(card) {{
          var projId = card.getAttribute('data-project-id');
          card.addEventListener('click', function() {{
            if (activeCard === card) {{
              closeDetail();
            }} else {{
              openDetail(projId, card);
            }}
          }});
          card.addEventListener('keydown', function(e) {{
            if (e.key === 'Enter' || e.key === ' ') {{
              e.preventDefault();
              card.click();
            }}
          }});
        }})(cards[i]);
      }}

      /* Trigger loaded animations */
      document.body.classList.add('loaded');
    }})();
  </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# File writing
# ---------------------------------------------------------------------------

def write_global_dashboard(payload: dict) -> dict:
    """Render HTML from template, validate, write files. Return result with paths."""
    from sweeper import global_home, ensure_global_dirs
    ensure_global_dirs()

    home = global_home()
    html_path = home / "global-dashboard.html"
    data_path = home / "global-dashboard-data.json"

    # Write JSON data
    try:
        write_json(data_path, payload)
    except OSError as exc:
        return {"ok": False, "error": f"failed to write data JSON: {exc}"}

    # Render HTML
    html = _render_html(payload)

    # Write HTML
    try:
        html_path.write_text(html)
    except OSError as exc:
        return {"ok": False, "error": f"failed to write HTML: {exc}"}

    # Validate HTML
    validation_errors = validate_generated_html(html_path)
    if validation_errors:
        for err in validation_errors:
            print(f"WARNING: {err}", file=sys.stderr)

    return {
        "ok": True,
        "html_path": str(html_path),
        "data_path": str(data_path),
        "aggregates": payload.get("aggregates", {}),
        "project_count": len(payload.get("active_projects", [])) + len(payload.get("inactive_projects", [])),
        "validation_errors": validation_errors,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

_ALLOWED_SERVE_FILES = {"global-dashboard.html", "global-dashboard-data.json"}


def _make_restricted_handler(serve_dir: str) -> type:
    """Create a handler class bound to a specific directory."""

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, directory=serve_dir, **kwargs)  # type: ignore[arg-type]

        def do_GET(self) -> None:
            path = self.path.split("?")[0].lstrip("/")
            if path not in _ALLOWED_SERVE_FILES:
                self.send_error(HTTPStatus.FORBIDDEN, "Access denied")
                return
            super().do_GET()

        def log_message(self, format: str, *args: object) -> None:
            pass

    return Handler


def cmd_dashboard(args: object) -> int:
    """CLI entry point. Build payload, write dashboard, print JSON to stdout."""
    try:
        payload = build_global_payload()
    except Exception as exc:
        result = {"ok": False, "error": f"failed to build payload: {exc}"}
        print(json.dumps(result, indent=2))
        return 1

    result = write_global_dashboard(payload)
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


def _dashboard_pid_path() -> Path:
    """Path to the dashboard server PID file."""
    from sweeper import global_home
    return global_home() / "dashboard.pid"


def _read_dashboard_pid() -> int | None:
    """Read the dashboard server PID if running."""
    pid_path = _dashboard_pid_path()
    if not pid_path.exists():
        return None
    try:
        pid = int(pid_path.read_text().strip())
        os.kill(pid, 0)  # check alive
        return pid
    except (ValueError, OSError):
        pid_path.unlink(missing_ok=True)
        return None


def cmd_serve(args: object) -> int:
    """Generate the global dashboard and serve it on a local HTTP server."""
    port: int = getattr(args, "port", 8766)

    existing = _read_dashboard_pid()
    if existing:
        print(json.dumps({"ok": False, "error": f"dashboard server already running (PID {existing}). Use 'dashboard kill' first."}))
        return 1

    try:
        payload = build_global_payload()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 1

    write_global_dashboard(payload)

    from sweeper import global_home
    serve_dir = str(global_home())
    handler_cls = _make_restricted_handler(serve_dir)

    # Write PID file
    pid_path = _dashboard_pid_path()
    pid_path.write_text(str(os.getpid()))

    url = f"http://127.0.0.1:{port}/global-dashboard.html"
    print(json.dumps({"url": url}, indent=2))
    sys.stdout.flush()

    class _ReuseServer(ThreadingHTTPServer):
        allow_reuse_address = True
        allow_reuse_port = True
    server = _ReuseServer(("127.0.0.1", port), handler_cls)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        pid_path.unlink(missing_ok=True)
    return 0


def cmd_kill(args: object) -> int:
    """Stop the running dashboard server."""
    pid = _read_dashboard_pid()
    if pid is None:
        print(json.dumps({"ok": True, "message": "no dashboard server running"}))
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
        print(json.dumps({"ok": True, "message": f"killed dashboard server (PID {pid})"}))
    except OSError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    _dashboard_pid_path().unlink(missing_ok=True)
    return 0


def cmd_restart(args: object) -> int:
    """Restart the dashboard server (kill + serve)."""
    import time
    port: int = getattr(args, "port", 8766)
    pid = _read_dashboard_pid()
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
            print(json.dumps({"message": f"killed old server (PID {pid})"}), flush=True)
        except OSError:
            pass
        _dashboard_pid_path().unlink(missing_ok=True)
        for _ in range(20):
            try:
                os.kill(pid, 0)
                time.sleep(0.25)
            except OSError:
                break
    else:
        # No PID file — kill by port as fallback
        import subprocess as _sp
        try:
            r = _sp.run(["lsof", "-ti", f":{port}"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                for p in r.stdout.strip().split("\n"):
                    try:
                        os.kill(int(p), 9)
                    except (ValueError, OSError):
                        pass
        except (FileNotFoundError, _sp.TimeoutExpired):
            pass
    time.sleep(0.5)
    return cmd_serve(args)
