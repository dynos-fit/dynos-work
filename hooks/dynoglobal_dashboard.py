#!/usr/bin/env python3
"""Generate a unified HTML dashboard showing all registered dynos-work projects."""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import json
import sys
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dynodashboard import build_dashboard_payload
from dynoglobal import (
    current_daemon_pid,
    extract_project_stats,
    load_registry,
    log_global,
    sweeps_log_path,
)
from dynoslib import now_iso, validate_generated_html, write_json


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
    }


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


def gather_all_projects() -> dict:
    """Load registry, iterate all projects (active/paused/archived).

    Active: full payload + sparkline data.
    Paused/archived: name, path, status, last_active_at only.
    Missing dirs: warning tag. Failed extraction: error tag + log.
    """
    reg = load_registry()
    projects_list = reg.get("projects", [])

    active_projects: list[dict] = []
    inactive_projects: list[dict] = []

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
                "stats": {},
                "quality_scores": [],
                "sparkline_svg": "",
                "task_count": 0,
                "learned_routes": 0,
                "last_cycle_at": "",
            })
            log_global(f"global dashboard: missing directory for project {proj_path_str}")
            continue

        try:
            data = gather_project_data(proj_path)
            stats = data.get("stats", {})
            payload = data.get("payload", {})
            quality_scores = data.get("quality_scores", [])
            summary = payload.get("summary", {})

            sparkline_svg = build_sparkline_svg(quality_scores)
            health = derive_health_tag(data)

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
                "stats": {},
                "quality_scores": [],
                "sparkline_svg": "",
                "task_count": 0,
                "learned_routes": 0,
                "last_cycle_at": "",
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
                "stats": {},
                "quality_scores": [],
                "sparkline_svg": "",
                "task_count": 0,
                "learned_routes": 0,
                "last_cycle_at": "",
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


def compute_aggregate_stats(projects: list[dict]) -> dict:
    """Total tasks, avg quality, total learned routes, active count."""
    total_tasks = 0
    quality_scores: list[float] = []
    total_learned_routes = 0
    active_count = 0

    for proj in projects:
        stats = proj.get("stats", {})
        total_tasks += stats.get("total_tasks", 0)
        avg_q = stats.get("average_quality_score", 0.0)
        if isinstance(avg_q, (int, float)) and avg_q > 0:
            quality_scores.append(float(avg_q))
        total_learned_routes += proj.get("learned_routes", 0)
        if proj.get("health") != "error":
            active_count += 1

    avg_quality = round(sum(quality_scores) / len(quality_scores), 2) if quality_scores else 0.0

    return {
        "total_tasks": total_tasks,
        "avg_quality": avg_quality,
        "total_learned_routes": total_learned_routes,
        "active_count": active_count,
    }


def build_global_payload() -> dict:
    """Combine all data: global daemon, projects, aggregates, generation timestamp."""
    daemon_status = gather_global_daemon_status()
    all_projects = gather_all_projects()
    aggregates = compute_aggregate_stats(all_projects["active"])

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


def _render_active_card(proj: dict) -> str:
    """Render a single active project card HTML fragment."""
    name = _esc(proj.get("name", "unknown"))
    path = _esc(proj.get("path", ""))
    health = proj.get("health", "healthy")
    task_count = proj.get("task_count", 0)
    learned_routes = proj.get("learned_routes", 0)
    last_cycle = _esc(proj.get("last_cycle_at", "n/a"))
    sparkline = proj.get("sparkline_svg", "")
    warning = proj.get("warning", "")
    error = proj.get("error", "")
    daemon_running = proj.get("daemon_running", False)

    health_class = "tag"
    health_label = health
    if health == "warning":
        health_class = "tag warn"
    elif health == "error":
        health_class = "tag danger"

    daemon_badge = (
        '<span class="tag" style="font-size:11px;padding:3px 8px;">daemon running</span>'
        if daemon_running
        else '<span class="tag warn" style="font-size:11px;padding:3px 8px;">daemon stopped</span>'
    )

    extra_info = ""
    if warning:
        extra_info = f'<div class="mini" style="color:hsl(34 82% 64%);">{_esc(warning)}</div>'
    elif error:
        extra_info = f'<div class="mini" style="color:hsl(350 72% 68%);">{_esc(error)}</div>'

    sparkline_html = sparkline if sparkline else (
        '<div style="border:1px dashed hsla(210 30% 80% / 0.14);border-radius:8px;'
        'height:40px;display:flex;align-items:center;justify-content:center;'
        'color:var(--muted);font-size:11px;font-style:italic;">no retrospectives</div>'
    )

    return (
        f'<div class="panel project-card">'
        f'<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;margin-bottom:8px;">'
        f'<div>'
        f'<div style="font-weight:800;font-size:15px;">{name}</div>'
        f'<div class="mini" style="word-break:break-all;">{path}</div>'
        f'</div>'
        f'<span class="{health_class}">{_esc(health_label)}</span>'
        f'</div>'
        f'{extra_info}'
        f'<div style="display:flex;gap:8px;align-items:center;margin:8px 0;">'
        f'{daemon_badge}'
        f'</div>'
        f'<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin:10px 0;">'
        f'<div class="mini"><span style="font-weight:700;color:var(--text);">{task_count}</span> tasks</div>'
        f'<div class="mini"><span style="font-weight:700;color:var(--text);">{learned_routes}</span> routes</div>'
        f'<div class="mini">cycle: {last_cycle}</div>'
        f'</div>'
        f'<div style="height:44px;">{sparkline_html}</div>'
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
        f'<div>'
        f'<div style="font-weight:800;font-size:15px;">{name}</div>'
        f'<div class="mini" style="word-break:break-all;">{path}</div>'
        f'</div>'
        f'<span class="{tag_class}">{_esc(status)}</span>'
        f'</div>'
        f'<div class="mini" style="margin-top:8px;">Last active: {last_active}</div>'
        f'</div>'
    )


def _render_html(payload: dict) -> str:
    """Render the full global dashboard HTML from payload data."""
    daemon = payload.get("daemon", {})
    aggregates = payload.get("aggregates", {})
    active_projects = payload.get("active_projects", [])
    inactive_projects = payload.get("inactive_projects", [])
    generated_at = _esc(payload.get("generated_at", ""))

    daemon_running = daemon.get("running", False)
    last_sweep = _esc(daemon.get("last_sweep_at", "n/a"))
    project_count = len(active_projects) + len(inactive_projects)

    daemon_badge = (
        '<span class="tag" style="font-size:11px;padding:3px 8px;">running</span>'
        if daemon_running
        else '<span class="tag danger" style="font-size:11px;padding:3px 8px;">stopped</span>'
    )

    total_tasks = aggregates.get("total_tasks", 0)
    avg_quality = aggregates.get("avg_quality", 0.0)
    total_learned = aggregates.get("total_learned_routes", 0)
    active_count = aggregates.get("active_count", 0)

    # Build active project cards
    if active_projects:
        active_cards_html = "\n".join(_render_active_card(p) for p in active_projects)
    else:
        active_cards_html = (
            '<div class="empty-state">'
            '<span>No registered projects. Run <code>dynos registry register /path</code> to add one.</span>'
            '</div>'
        )

    # Build inactive section
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

    # Assemble final HTML using the GLOBAL_HTML_TEMPLATE
    html = GLOBAL_HTML_TEMPLATE.replace("{{", "{").replace("}}", "}")
    html = html.replace("__DAEMON_BADGE__", daemon_badge)
    html = html.replace("__LAST_SWEEP__", last_sweep)
    html = html.replace("__PROJECT_COUNT__", str(project_count))
    html = html.replace("__GENERATED_AT__", generated_at)
    html = html.replace("__TOTAL_TASKS__", str(total_tasks))
    html = html.replace("__AVG_QUALITY__", f"{avg_quality:.2f}")
    html = html.replace("__LEARNED_ROUTES__", str(total_learned))
    html = html.replace("__ACTIVE_COUNT__", str(active_count))
    html = html.replace("__ACTIVE_CARDS__", active_cards_html)
    html = html.replace("__INACTIVE_SECTION__", inactive_section)

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
    .stats {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 14px;
      margin-bottom: 24px;
    }}
    .stat {{
      background: hsla(215 20% 16% / 0.72);
      border: 1px solid var(--line);
      border-left: 3px solid hsla(43 90% 62% / 0.40);
      border-radius: 10px;
      padding: 16px;
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
    .mini {{
      font-size: 12px;
      color: var(--muted);
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
    .project-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      gap: 1.2rem;
    }}
    .project-card {{
      transition: transform 0.22s ease, box-shadow 0.22s ease, border-color 0.22s ease;
    }}
    .project-card:hover {{
      transform: translateY(-3px);
      box-shadow: 0 24px 72px hsla(220 60% 2% / 0.55), 0 0 0 1px hsla(156 63% 54% / 0.18);
      border-color: hsla(156 63% 54% / 0.28);
    }}
    .stat {{
      transition: transform 0.2s ease, box-shadow 0.2s ease, border-color 0.2s ease;
    }}
    .stat:hover {{
      transform: translateY(-2px);
      box-shadow: 0 12px 36px hsla(220 60% 2% / 0.4), 0 0 0 1px hsla(42 94% 64% / 0.2);
      border-color: hsla(42 94% 64% / 0.32);
    }}
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
    body.loaded .stats .stat:nth-child(1) {{ animation-delay: 0.1s; }}
    body.loaded .stats .stat:nth-child(2) {{ animation-delay: 0.16s; }}
    body.loaded .stats .stat:nth-child(3) {{ animation-delay: 0.22s; }}
    body.loaded .stats .stat:nth-child(4) {{ animation-delay: 0.28s; }}
    @media (max-width: 980px) {{
      .stats {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .project-grid {{ grid-template-columns: 1fr; }}
      .shell {{ padding: 20px; }}
      .topbar {{ padding: 0 14px; }}
    }}
    @media (max-width: 600px) {{
      .stats {{ grid-template-columns: 1fr; }}
      .project-grid {{ grid-template-columns: 1fr; }}
      .shell {{ padding: 12px; }}
      .topbar {{ padding: 0 12px; height: 38px; }}
      .topbar-wordmark {{ font-size: 13px; }}
      .panel {{ padding: 16px; border-radius: 10px; }}
      .stat .value {{ font-size: 1.5rem; }}
    }}
  </style>
</head>
<body>
  <nav class="topbar" role="navigation" aria-label="Global dashboard navigation">
    <span class="topbar-wordmark">dynos-work Global Dashboard</span>
    <div class="topbar-right">
      __DAEMON_BADGE__
      <span class="topbar-updated">sweep: __LAST_SWEEP__ | projects: __PROJECT_COUNT__</span>
    </div>
  </nav>
  <div class="shell">
    <section class="stats">
      <div class="stat">
        <div class="label">Total Tasks</div>
        <div class="value">__TOTAL_TASKS__</div>
      </div>
      <div class="stat">
        <div class="label">Avg Quality</div>
        <div class="value">__AVG_QUALITY__</div>
      </div>
      <div class="stat">
        <div class="label">Learned Routes</div>
        <div class="value">__LEARNED_ROUTES__</div>
      </div>
      <div class="stat">
        <div class="label">Active Projects</div>
        <div class="value">__ACTIVE_COUNT__</div>
      </div>
    </section>
    <section>
      <div class="headline">Active Projects</div>
      <div class="project-grid" style="margin-top:14px;">
        __ACTIVE_CARDS__
      </div>
    </section>
    __INACTIVE_SECTION__
    <div class="mini" style="text-align:center;margin-top:28px;padding-bottom:16px;">
      Generated: __GENERATED_AT__
    </div>
  </div>
  <!-- Hidden elements to satisfy validate_generated_html() required IDs -->
  <div style="display:none">
    <span id="stats"></span><span id="updated"></span><span id="lineage"></span>
    <span id="routes"></span><span id="queue"></span><span id="sparkline"></span>
    <span id="gaps"></span><span id="demotions"></span><span id="runs"></span>
  </div>
  <script>
    document.body.classList.add('loaded');
  </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# File writing
# ---------------------------------------------------------------------------

def write_global_dashboard(payload: dict) -> dict:
    """Render HTML from template, validate, write files. Return result with paths."""
    from dynoglobal import global_home, ensure_global_dirs
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


def cmd_serve(args: object) -> int:
    """Generate the global dashboard and serve it on a local HTTP server."""
    port: int = getattr(args, "port", 8766)

    try:
        payload = build_global_payload()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 1

    write_global_dashboard(payload)

    from dynoglobal import global_home
    serve_dir = str(global_home())
    handler_cls = _make_restricted_handler(serve_dir)

    url = f"http://127.0.0.1:{port}/global-dashboard.html"
    print(json.dumps({"url": url}, indent=2))
    sys.stdout.flush()

    server = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
