#!/usr/bin/env python3
"""Generate and optionally serve the live dynos-work dashboard."""

from __future__ import annotations

import argparse
import json
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer, HTTPStatus
from pathlib import Path

from dynolineage import build_lineage
from dynoreport import build_report
from dynoslib import validate_generated_html


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>dynos-work | Live Control Center</title>
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
      height: 48px;
      padding: 0 28px;
      background: hsla(214 26% 10% / 0.92);
      backdrop-filter: blur(12px);
      border-bottom: 1px solid var(--line);
    }}
    .topbar-wordmark {{
      font-family: "Inter", "Segoe UI", ui-sans-serif, system-ui, -apple-system, sans-serif;
      font-weight: 800;
      font-size: 15px;
      letter-spacing: 0.04em;
      color: var(--text);
    }}
    .topbar-right {{
      display: flex;
      align-items: center;
      gap: 16px;
    }}
    .live-dot {{
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--mint);
      flex-shrink: 0;
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
      padding-top: 28px;
    }}
    .hero {{
      display: grid;
      grid-template-columns: 2fr 1fr;
      gap: 20px;
      margin-bottom: 20px;
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
    h1 {{
      margin: 0;
      font-size: clamp(2rem, 3vw, 4rem);
      line-height: 0.96;
    }}
    .sub {{
      color: var(--muted);
      margin-top: 16px;
      max-width: 58ch;
    }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 14px;
      margin-top: 22px;
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
    .meta {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      color: var(--muted);
      font-size: 13px;
      margin-top: 20px;
      flex-wrap: wrap;
    }}
    .grid {{
      display: grid;
      grid-template-columns: 1.3fr 1fr;
      gap: 20px;
    }}
    .stack {{
      display: grid;
      gap: 20px;
    }}
    .barlist, .list {{
      display: grid;
      gap: 12px;
      margin-top: 16px;
    }}
    .bar {{
      display: grid;
      gap: 6px;
    }}
    .barhead {{
      display: flex;
      justify-content: space-between;
      gap: 10px;
      font-size: 13px;
    }}
    @keyframes shimmer {{
      0% {{ background-position: -200% center; }}
      100% {{ background-position: 200% center; }}
    }}
    .track {{
      height: 8px;
      border-radius: 999px;
      background: hsla(210 20% 90% / 0.08);
      overflow: hidden;
    }}
    .fill {{
      height: 100%;
      border-radius: inherit;
      background:
        linear-gradient(90deg, transparent, hsla(0 0% 100% / 0.18), transparent) no-repeat,
        linear-gradient(90deg, var(--mint), var(--sky));
      background-size: 200% 100%, 100% 100%;
      animation: shimmer 2.4s ease-in-out infinite;
    }}
    .warning .fill {{
      background:
        linear-gradient(90deg, transparent, hsla(0 0% 100% / 0.18), transparent) no-repeat,
        linear-gradient(90deg, var(--amber), var(--rose));
      background-size: 200% 100%, 100% 100%;
      animation: shimmer 2.4s ease-in-out infinite;
    }}
    .row {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      padding: 12px 14px;
      border-radius: 10px;
      background: var(--panel-2);
      border: 1px solid var(--line);
      align-items: center;
    }}
    .mini {{
      font-size: 12px;
      color: var(--muted);
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
    .empty-state .tag {{
      font-style: normal;
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
    .spark {{
      margin-top: 16px;
      width: 100%;
      height: 160px;
      border-radius: 12px;
      background: linear-gradient(180deg, hsla(198 88% 63% / 0.08), transparent);
      border: 1px solid var(--line);
      position: relative;
      overflow: hidden;
    }}
    .spark svg {{
      width: 100%;
      height: 100%;
      display: block;
    }}
    @keyframes drawLine {{
      from {{ stroke-dashoffset: var(--line-length); }}
      to {{ stroke-dashoffset: 0; }}
    }}
    #sparkline polyline.spark-main {{
      stroke-dasharray: var(--line-length);
      stroke-dashoffset: var(--line-length);
      animation: drawLine 1.2s ease-out forwards;
    }}
    #sparkline .spark-grid-line {{
      stroke: hsla(210 20% 80% / 0.07);
      stroke-width: 1;
    }}
    #sparkline .spark-dot {{
      fill: hsl(198 88% 63%);
      opacity: 0.85;
    }}
    #sparkline .spark-fill {{
      opacity: 0.8;
    }}
    /* -- Entrance animations -- */
    @keyframes fadeSlideIn {{
      from {{ opacity: 0; transform: translateY(12px); }}
      to {{ opacity: 1; transform: translateY(0); }}
    }}
    @keyframes pulse {{
      0%, 100% {{ opacity: 1; transform: scale(1); }}
      50% {{ opacity: 0.5; transform: scale(1.25); }}
    }}
    .panel {{
      opacity: 0;
      transform: translateY(12px);
    }}
    .stat {{
      opacity: 0;
      transform: translateY(12px);
    }}
    body.loaded .panel {{
      animation: fadeSlideIn 0.5s ease-out forwards;
    }}
    body.loaded .stat {{
      animation: fadeSlideIn 0.4s ease-out forwards;
    }}
    /* Staggered delays for panels */
    body.loaded .hero > .panel:nth-child(1) {{ animation-delay: 0s; }}
    body.loaded .hero > .panel:nth-child(2) {{ animation-delay: 0.08s; }}
    body.loaded .grid .stack:nth-child(1) > .panel:nth-child(1) {{ animation-delay: 0.12s; }}
    body.loaded .grid .stack:nth-child(1) > .panel:nth-child(2) {{ animation-delay: 0.18s; }}
    body.loaded .grid .stack:nth-child(1) > .panel:nth-child(3) {{ animation-delay: 0.24s; }}
    body.loaded .grid .stack:nth-child(2) > .panel:nth-child(1) {{ animation-delay: 0.16s; }}
    body.loaded .grid .stack:nth-child(2) > .panel:nth-child(2) {{ animation-delay: 0.22s; }}
    body.loaded .grid .stack:nth-child(2) > .panel:nth-child(3) {{ animation-delay: 0.28s; }}
    /* Staggered delays for stat cards */
    body.loaded .stats .stat:nth-child(1) {{ animation-delay: 0.1s; }}
    body.loaded .stats .stat:nth-child(2) {{ animation-delay: 0.16s; }}
    body.loaded .stats .stat:nth-child(3) {{ animation-delay: 0.22s; }}
    body.loaded .stats .stat:nth-child(4) {{ animation-delay: 0.28s; }}
    /* Live dot pulse */
    .live-dot {{
      animation: pulse 2s ease-in-out infinite;
    }}
    /* -- Hover effects -- */
    .panel {{
      transition: transform 0.22s ease, box-shadow 0.22s ease, border-color 0.22s ease;
    }}
    body.loaded .panel:hover {{
      transform: translateY(-3px);
      box-shadow: 0 24px 72px hsla(220 60% 2% / 0.55), 0 0 0 1px hsla(156 63% 54% / 0.18);
      border-color: hsla(156 63% 54% / 0.28);
    }}
    .stat {{
      transition: transform 0.2s ease, box-shadow 0.2s ease, border-color 0.2s ease;
    }}
    body.loaded .stat:hover {{
      transform: translateY(-2px);
      box-shadow: 0 12px 36px hsla(220 60% 2% / 0.4), 0 0 0 1px hsla(42 94% 64% / 0.2);
      border-color: hsla(42 94% 64% / 0.32);
    }}
    @media (max-width: 980px) {{
      .hero, .grid {{ grid-template-columns: 1fr; }}
      .stats {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .shell {{ padding: 20px; }}
      .topbar {{ padding: 0 16px; }}
      .topbar-updated {{ max-width: 220px; }}
    }}
    @media (max-width: 600px) {{
      .hero, .grid {{ grid-template-columns: 1fr; }}
      .stats {{ grid-template-columns: 1fr; }}
      .shell {{ padding: 12px; }}
      .topbar {{ padding: 0 12px; height: 44px; }}
      .topbar-wordmark {{ font-size: 13px; }}
      .topbar-updated {{ max-width: 140px; font-size: 11px; }}
      .panel {{ padding: 16px; border-radius: 10px; }}
      h1 {{ font-size: clamp(1.4rem, 5vw, 2rem); }}
      .sub {{ font-size: 13px; }}
      .stat .value {{ font-size: 1.5rem; }}
      .row {{ padding: 10px 12px; font-size: 13px; }}
      .row code {{ font-size: 11px; word-break: break-all; }}
      .meta {{ font-size: 12px; }}
      .spark {{ height: 120px; }}
    }}
  </style>
</head>
<body>
  <nav class="topbar" role="navigation" aria-label="Dashboard navigation">
    <span class="topbar-wordmark">dynos-work</span>
    <div class="topbar-right">
      <span class="live-dot" aria-label="Live status indicator"></span>
      <span class="topbar-updated" id="updated" aria-live="polite"></span>
    </div>
  </nav>
  <div class="shell">
    <section class="hero">
      <div class="panel">
        <div class="headline">Real-Time Foundry Control</div>
        <h1>dynos-work live routing, regressions, and challenger flow</h1>
        <div class="sub">Live registry state, benchmark freshness, automation queue pressure, and promotion lineage, refreshed continuously from local runtime data.</div>
        <div class="stats" id="stats"></div>
        <div class="meta">
          <span id="lineage"></span>
        </div>
      </div>
      <div class="panel">
        <div class="headline">Control Actions</div>
        <div class="list">
          <div class="row"><span>Refresh dashboard data</span><code>python3 hooks/dynodashboard.py generate --root .</code></div>
          <div class="row"><span>Run challenger queue</span><code>python3 hooks/dynoauto.py run --root .</code></div>
          <div class="row"><span>Inspect route</span><code>python3 hooks/dynoroute.py backend-executor feature --root .</code></div>
          <div class="row"><span>Serve dashboard</span><code>python3 hooks/dynodashboard.py serve --root .</code></div>
        </div>
      </div>
    </section>
    <section class="grid">
      <div class="stack">
        <div class="panel">
          <div class="headline">Active Routes</div>
          <div class="barlist" id="routes"></div>
        </div>
        <div class="panel">
          <div class="headline">Automation Queue</div>
          <div class="list" id="queue"></div>
        </div>
        <div class="panel">
          <div class="headline">Recent Benchmark Composite</div>
          <div class="spark"><svg id="sparkline" viewBox="0 0 600 160" preserveAspectRatio="none"></svg></div>
        </div>
      </div>
      <div class="stack">
        <div class="panel">
          <div class="headline">Coverage Gaps</div>
          <div class="list" id="gaps"></div>
        </div>
        <div class="panel">
          <div class="headline">Demotions</div>
          <div class="list" id="demotions"></div>
        </div>
        <div class="panel">
          <div class="headline">Recent Runs</div>
          <div class="list" id="runs"></div>
        </div>
      </div>
    </section>
  </div>
  <script>
    const embedded = __EMBEDDED_DATA__;
    async function loadData() {{
      try {{
        const response = await fetch('dashboard-data.json?ts=' + Date.now(), {{ cache: 'no-store' }});
        if (!response.ok) throw new Error('fetch failed');
        return await response.json();
      }} catch (err) {{
        return embedded;
      }}
    }}
    function esc(value) {{
      return String(value ?? '').replace(/[&<>"]/g, (m) => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[m]));
    }}
    function renderStats(summary) {{
      const stats = [
        ['Learned Components', summary.learned_components],
        ['Active Routes', summary.active_routes],
        ['Queue Jobs', summary.queued_automation_jobs],
        ['Coverage Gaps', summary.coverage_gaps],
      ];
      document.getElementById('stats').innerHTML = stats.map(([label, value]) => `
        <div class="stat">
          <div class="label">${{esc(label)}}</div>
          <div class="value">${{esc(value)}}</div>
        </div>
      `).join('');
    }}
    function renderRoutes(items) {{
      const target = document.getElementById('routes');
      if (!items.length) {{
        target.innerHTML = `<div class="empty-state"><span>No live learned routes</span><span class="tag warn">generic fallback</span></div>`;
        return;
      }}
      target.innerHTML = items.map((item) => `
        <div class="bar">
          <div class="barhead">
            <strong>${{esc(item.agent_name)}}</strong>
            <span>${{esc(item.mode)}} · ${{Number(item.composite || 0).toFixed(3)}}</span>
          </div>
          <div class="mini">${{esc(item.role)}} / ${{esc(item.task_type)}}</div>
          <div class="track"><div class="fill" style="width:${{Math.max(5, Math.min(100, (item.composite || 0) * 100))}}%"></div></div>
        </div>
      `).join('');
    }}
    function renderList(id, items, emptyText, formatter) {{
      const target = document.getElementById(id);
      if (!items.length) {{
        target.innerHTML = `<div class="empty-state"><span>${{esc(emptyText)}}</span></div>`;
        return;
      }}
      target.innerHTML = items.map(formatter).join('');
    }}
    function renderRuns(runs) {{
      renderList('runs', runs, 'No benchmark runs yet.', (item) => `
        <div class="row">
          <div>
            <div><strong>${{esc(item.fixture_id || item.run_id)}}</strong></div>
            <div class="mini">${{esc(item.target_name || 'unknown')}} · ${{esc(item.role || 'n/a')}}</div>
          </div>
          <span class="tag">${{esc(item.evaluation?.recommendation || 'recorded')}}</span>
        </div>
      `);
      const svg = document.getElementById('sparkline');
      const values = runs.map((item) => Number(item.evaluation?.candidate?.mean_composite || 0));
      if (!values.length) {{
        svg.innerHTML = '';
        return;
      }}
      const max = Math.max(...values, 1);
      const min = Math.min(...values, 0);
      const points = values.map((value, index) => {{
        const x = values.length === 1 ? 300 : (index / (values.length - 1)) * 600;
        const y = 140 - ((value - min) / Math.max(0.0001, max - min || 1)) * 110;
        return `${{x}},${{y}}`;
      }}).join(' ');
      var polyPoints = values.map((value, index) => {{
        var x = values.length === 1 ? 300 : (index / (values.length - 1)) * 600;
        var y = 140 - ((value - min) / Math.max(0.0001, max - min || 1)) * 110;
        return [x, y];
      }});
      var firstX = polyPoints[0][0];
      var lastX = polyPoints[polyPoints.length - 1][0];
      var polygonPts = points + ` ${{lastX}},160 ${{firstX}},160`;
      var dots = polyPoints.map(function(pt) {{
        return `<circle class="spark-dot" cx="${{pt[0]}}" cy="${{pt[1]}}" r="3" />`;
      }}).join('');
      var lineLen = 0;
      for (var pi = 1; pi < polyPoints.length; pi++) {{
        var dx = polyPoints[pi][0] - polyPoints[pi - 1][0];
        var dy = polyPoints[pi][1] - polyPoints[pi - 1][1];
        lineLen += Math.sqrt(dx * dx + dy * dy);
      }}
      svg.innerHTML = `
        <defs>
          <linearGradient id="sparkFillGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="hsl(200 82% 60%)" stop-opacity="0.32" />
            <stop offset="100%" stop-color="hsl(200 82% 60%)" stop-opacity="0" />
          </linearGradient>
        </defs>
        <line class="spark-grid-line" x1="0" y1="40" x2="600" y2="40" />
        <line class="spark-grid-line" x1="0" y1="80" x2="600" y2="80" />
        <line class="spark-grid-line" x1="0" y1="120" x2="600" y2="120" />
        <polygon class="spark-fill" fill="url(#sparkFillGrad)" points="${{polygonPts}}" />
        <polyline class="spark-main" fill="none" stroke="hsl(198 88% 63%)" stroke-width="4" points="${{points}}" style="--line-length:${{Math.ceil(lineLen)}}" />
        <polyline fill="none" stroke="hsla(198 88% 63% / 0.18)" stroke-width="12" points="${{points}}" />
        ${{dots}}
      `;
    }}
    function render(data) {{
      renderStats(data.summary || {{}});
      renderRoutes(data.active_routes || []);
      renderList('queue', data.automation_queue || [], 'No queued automation work.', (item) => `
        <div class="row">
          <div>
            <div><strong>${{esc(item.agent_name)}}</strong></div>
            <div class="mini">${{esc(item.reason || 'queued')}} · ${{esc(item.fixture_path || '')}}</div>
          </div>
          <span class="tag warn">${{esc(item.status || 'queued')}}</span>
        </div>
      `);
      renderList('gaps', data.coverage_gaps || [], 'No fixture coverage gaps.', (item) => `
        <div class="row">
          <div>
            <div><strong>${{esc(item.target_name)}}</strong></div>
            <div class="mini">${{esc(item.role)}} / ${{esc(item.task_type)}}</div>
          </div>
          <span class="tag danger">missing fixture</span>
        </div>
      `);
      renderList('demotions', data.demotions || [], 'No active regressions.', (item) => `
        <div class="row">
          <div>
            <div><strong>${{esc(item.agent_name)}}</strong></div>
            <div class="mini">${{esc(item.role)}} · ${{esc(item.last_evaluation?.recommendation || 'unknown')}}</div>
          </div>
          <span class="tag danger">demoted</span>
        </div>
      `);
      renderRuns(data.recent_runs || []);
      document.getElementById('updated').textContent = `Updated: ${{data.generated_at || data.registry_updated_at || 'unknown'}}`;
      document.getElementById('lineage').textContent = `Lineage: ${{data.lineage?.nodes || 0}} nodes / ${{data.lineage?.edges || 0}} edges`;
    }}
    function animateCounters() {{
      var elements = document.querySelectorAll('.stat .value');
      for (var i = 0; i < elements.length; i++) {{
        (function(el) {{
          var text = el.textContent.trim();
          var target = parseInt(text, 10);
          if (isNaN(target) || target < 0) return;
          var duration = 800;
          var startTime = null;
          el.textContent = '0';
          function step(timestamp) {{
            if (!startTime) startTime = timestamp;
            var progress = Math.min((timestamp - startTime) / duration, 1);
            var eased = 1 - Math.pow(1 - progress, 3);
            el.textContent = String(Math.round(eased * target));
            if (progress < 1) {{
              requestAnimationFrame(step);
            }}
          }}
          requestAnimationFrame(step);
        }})(elements[i]);
      }}
    }}
    async function tick() {{
      const data = await loadData();
      render(data);
      if (!window.__dynosFirstRender) {{
        window.__dynosFirstRender = true;
        document.body.classList.add('loaded');
        animateCounters();
      }}
    }}
    tick();
    setInterval(tick, 3000);
  </script>
</body>
</html>
"""


def build_dashboard_payload(root: Path) -> dict:
    report = build_report(root)
    lineage = build_lineage(root)
    report["generated_at"] = report.get("registry_updated_at")
    report["lineage"] = {"nodes": len(lineage.get("nodes", [])), "edges": len(lineage.get("edges", []))}
    report["lineage_graph"] = lineage
    return report


def write_dashboard(root: Path) -> dict:
    dynos_dir = root / ".dynos"
    dynos_dir.mkdir(parents=True, exist_ok=True)
    payload = build_dashboard_payload(root)
    data_path = dynos_dir / "dashboard-data.json"
    html_path = dynos_dir / "dashboard.html"
    data_path.write_text(json.dumps(payload, indent=2) + "\n")
    safe_json = json.dumps(payload).replace("</", "<\\/")
    html = HTML_TEMPLATE.replace("{{", "{").replace("}}", "}")
    html = html.replace("__EMBEDDED_DATA__", safe_json)
    html_path.write_text(html)
    validation_errors = validate_generated_html(html_path)
    if validation_errors:
        import sys
        for err in validation_errors:
            print(f"WARNING: {err}", file=sys.stderr)
    return {
        "html_path": str(html_path),
        "data_path": str(data_path),
        "summary": payload.get("summary", {}),
        "validation_errors": validation_errors,
    }


def cmd_generate(args: argparse.Namespace) -> int:
    result = write_dashboard(Path(args.root).resolve())
    print(json.dumps(result, indent=2))
    return 0


ALLOWED_SERVE_FILES = {"dashboard.html", "dashboard-data.json"}


class RestrictedHandler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:
        path = self.path.split("?")[0].lstrip("/")
        if path not in ALLOWED_SERVE_FILES:
            self.send_error(HTTPStatus.FORBIDDEN, "Access denied")
            return
        super().do_GET()

    def log_message(self, format: str, *args: object) -> None:
        pass


def cmd_serve(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    write_dashboard(root)
    os.chdir(root / ".dynos")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), RestrictedHandler)
    print(json.dumps({"url": f"http://127.0.0.1:{args.port}/dashboard.html"}, indent=2))
    server.serve_forever()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate = subparsers.add_parser("generate", help="Generate dashboard HTML and live JSON")
    generate.add_argument("--root", default=".")
    generate.set_defaults(func=cmd_generate)
    serve = subparsers.add_parser("serve", help="Serve live dashboard locally with refreshable JSON")
    serve.add_argument("--root", default=".")
    serve.add_argument("--port", type=int, default=8765)
    serve.set_defaults(func=cmd_serve)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
