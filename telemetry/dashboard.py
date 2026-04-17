#!/usr/bin/env python3
"""Serve the Vite SPA dashboard with a Python API backend."""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent)); _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "hooks"))

import argparse
import json
import os
import re
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer, HTTPStatus
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from lineage import build_lineage
from report import build_report
from lib_validate import validate_generated_html


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TASK_ID_PATTERN = re.compile(r"^task-\d{8}-\d{3}$")

STAGE_ORDER = {
    "FOUNDRY_INITIALIZED": 0, "DISCOVERY": 1, "SPEC_NORMALIZATION": 2,
    "SPEC_REVIEW": 3, "PLANNING": 4, "PLAN_REVIEW": 5, "PLAN_AUDIT": 6,
    "PRE_EXECUTION_SNAPSHOT": 7, "EXECUTION": 8, "TEST_EXECUTION": 9,
    "CHECKPOINT_AUDIT": 10, "AUDITING": 11, "FINAL_AUDIT": 12, "DONE": 13,
}

RATES_PER_MILLION = {
    "haiku": {"input": 0.80, "output": 4.00},
    "sonnet": {"input": 3.00, "output": 15.00},
    "opus": {"input": 15.00, "output": 75.00},
}


def compute_slug(project_path: str) -> str:
    return project_path.lstrip("/").replace("/", "-")


def persistent_dir(slug: str) -> Path:
    return Path.home() / ".dynos" / "projects" / slug


def local_dynos_dir(project_path: str) -> Path:
    return Path(project_path) / ".dynos"


def persistent_project_dir(project_path: str) -> Path:
    return persistent_dir(compute_slug(project_path))


def read_json_file(path: Path) -> object:
    with open(path) as f:
        return json.load(f)


def read_json_or_default(path: Path, default: object) -> object:
    try:
        return read_json_file(path)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def read_text_file(path: Path) -> str:
    with open(path) as f:
        return f.read()


def list_task_dirs(project_path: str) -> list[str]:
    try:
        entries = os.listdir(local_dynos_dir(project_path))
        return sorted(e for e in entries if TASK_ID_PATTERN.match(e))
    except OSError:
        return []


def get_registry() -> dict:
    registry_path = Path.home() / ".dynos" / "registry.json"
    return read_json_file(registry_path)


def is_registered_project(project_path: str) -> bool:
    try:
        registry = get_registry()
        normalized = os.path.realpath(project_path)
        return any(os.path.realpath(p.get("path", "")) == normalized for p in registry.get("projects", []))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False


def collect_from_all_projects(collector):
    """Run collector(project_path, slug) for every registered project, aggregate results."""
    try:
        registry = get_registry()
        results = []
        for proj in registry.get("projects", []):
            try:
                pp = proj.get("path", "")
                slug = compute_slug(pp)
                results.extend(collector(pp, slug))
            except Exception:
                pass
        return results
    except Exception:
        return []


def reconcile_stage(task_dir: Path, manifest: dict) -> dict:
    """Reconcile manifest stage with execution log."""
    stage = manifest.get("stage", "")
    if stage == "DONE" or (isinstance(stage, str) and "FAIL" in stage):
        return manifest

    try:
        log_path = task_dir / "execution-log.md"
        log_content = log_path.read_text()
        if any(marker in log_content for marker in (
            "\u2192 DONE", "[ADVANCE] EXECUTION \u2192 DONE", "[ADVANCE] AUDITING \u2192 DONE",
        )):
            return {**manifest, "stage": "DONE"}

        stage_lines = [l for l in log_content.split("\n") if "[STAGE]" in l or "[ADVANCE]" in l]
        if stage_lines:
            last = stage_lines[-1]
            m = re.search(r"\u2192\s*(\S+)", last)
            if not m:
                m = re.search(r"→\s*(\S+)", last)
            if m:
                log_stage = m.group(1)
                if STAGE_ORDER.get(log_stage, 0) > STAGE_ORDER.get(stage, 0):
                    return {**manifest, "stage": log_stage}
    except (OSError, UnicodeDecodeError):
        pass
    return manifest


def collect_retrospectives_for_project(project_path: str) -> list[dict]:
    results = []
    for td in list_task_dirs(project_path):
        try:
            data = read_json_file(local_dynos_dir(project_path) / td / "task-retrospective.json")
            if isinstance(data, dict):
                data["task_id"] = td
                results.append(data)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
    return results


# ---------------------------------------------------------------------------
# Computed endpoint builders
# ---------------------------------------------------------------------------

def build_repo_report(project_path: str) -> dict:
    pdir = persistent_project_dir(project_path)
    registry = read_json_or_default(pdir / "learned-agents" / "registry.json", {})
    queue = read_json_or_default(pdir / "automation-queue.json", {})
    history = read_json_or_default(pdir / "benchmark-history.json", {})
    index = read_json_or_default(pdir / "benchmark-index.json", {})

    agents = registry.get("agents", []) if isinstance(registry, dict) else []
    if not isinstance(agents, list):
        agents = []
    active = [a for a in agents if a.get("route_allowed")]
    shadow = [a for a in agents if a.get("mode") == "shadow"]
    demoted = [a for a in agents if a.get("status") == "demoted_on_regression"]
    queue_items = queue.get("items", []) if isinstance(queue, dict) else []
    if not isinstance(queue_items, list):
        queue_items = []
    runs = history.get("runs", []) if isinstance(history, dict) else []
    if not isinstance(runs, list):
        runs = []
    fixtures = index.get("fixtures", []) if isinstance(index, dict) else []
    if not isinstance(fixtures, list):
        fixtures = []

    fixture_ids = set()
    for f in fixtures:
        fid = f.get("fixture_id")
        if isinstance(fid, str):
            fixture_ids.add(fid)

    uncovered = []
    for a in shadow:
        fid = f"{a.get('item_kind', 'agent')}-{a.get('agent_name', 'unknown')}-{a.get('task_type', 'unknown')}"
        if fid not in fixture_ids:
            uncovered.append({
                "target_name": str(a.get("agent_name", "unknown")),
                "role": str(a.get("role", "unknown")),
                "task_type": str(a.get("task_type", "unknown")),
                "item_kind": str(a.get("item_kind", "agent")),
            })

    return {
        "registry_updated_at": registry.get("updated_at") if isinstance(registry, dict) else None,
        "summary": {
            "learned_components": len(agents),
            "active_routes": len(active),
            "shadow_components": len(shadow),
            "demoted_components": len(demoted),
            "queued_automation_jobs": len(queue_items),
            "benchmark_runs": len(runs),
            "tracked_fixtures": len(fixtures),
            "coverage_gaps": len(uncovered),
        },
        "active_routes": [{
            "agent_name": str(a.get("agent_name", "unknown")),
            "role": str(a.get("role", "unknown")),
            "task_type": str(a.get("task_type", "unknown")),
            "item_kind": str(a.get("item_kind", "agent")),
            "mode": str(a.get("mode", "unknown")),
            "composite": a.get("benchmark_summary", {}).get("mean_composite", 0) if isinstance(a.get("benchmark_summary"), dict) else 0,
        } for a in active],
        "demotions": [{
            "agent_name": str(a.get("agent_name", "unknown")),
            "role": str(a.get("role", "unknown")),
            "task_type": str(a.get("task_type", "unknown")),
            "last_evaluation": a.get("last_evaluation", {}),
        } for a in demoted],
        "automation_queue": queue_items,
        "coverage_gaps": uncovered,
        "recent_runs": runs[-5:],
    }


def build_project_stats(project_path: str) -> dict:
    retrospectives = collect_retrospectives_for_project(project_path)
    task_counts_by_type: dict[str, int] = {}
    quality_scores: list[float] = []
    executor_repair_totals: dict[str, list[float]] = {}
    prevention_rules: dict[str, int] = {}
    prevention_rule_executors: dict[str, str] = {}

    for retro in retrospectives:
        task_type = retro.get("task_type", "")
        if isinstance(task_type, str) and task_type:
            task_counts_by_type[task_type] = task_counts_by_type.get(task_type, 0) + 1

        qs = retro.get("quality_score")
        if isinstance(qs, (int, float)):
            quality_scores.append(float(qs))

        repair_frequency = retro.get("executor_repair_frequency")
        if isinstance(repair_frequency, dict):
            for role, count in repair_frequency.items():
                if isinstance(count, (int, float)):
                    executor_repair_totals.setdefault(role, []).append(float(count))

        rules = retro.get("prevention_rules", [])
        if not isinstance(rules, list):
            rules = []
        for rule in rules:
            if isinstance(rule, str) and rule:
                prevention_rules[rule] = prevention_rules.get(rule, 0) + 1
                prevention_rule_executors.setdefault(rule, "unknown")
            elif isinstance(rule, dict):
                candidate = rule.get("rule", "") or rule.get("text", "")
                if isinstance(candidate, str) and candidate:
                    prevention_rules[candidate] = prevention_rules.get(candidate, 0) + 1
                    if candidate not in prevention_rule_executors:
                        prevention_rule_executors[candidate] = rule.get("executor", "unknown") if isinstance(rule.get("executor"), str) else "unknown"

    executor_reliability = {}
    for role, counts in executor_repair_totals.items():
        avg = sum(counts) / len(counts) if counts else 0
        executor_reliability[role] = round(max(0.0, 1 - avg * 0.1), 3)

    total_tasks = sum(task_counts_by_type.values())
    avg_quality = round(sum(quality_scores) / len(quality_scores), 3) if quality_scores else 0.0

    return {
        "total_tasks": total_tasks,
        "task_counts_by_type": task_counts_by_type,
        "average_quality_score": avg_quality,
        "executor_reliability": executor_reliability,
        "prevention_rule_frequencies": prevention_rules,
        "prevention_rule_executors": prevention_rule_executors,
    }


def build_cost_summary(project_path: str) -> dict:
    by_model: dict[str, dict] = {}
    by_agent: dict[str, dict] = {}
    total_tokens = 0
    total_input_tokens = 0
    total_output_tokens = 0
    total_usd = 0.0

    for td in list_task_dirs(project_path):
        try:
            usage = read_json_file(local_dynos_dir(project_path) / td / "token-usage.json")
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
        if not isinstance(usage, dict):
            continue

        models = usage.get("by_model", {})
        if isinstance(models, dict):
            for model, info in models.items():
                if not isinstance(info, dict):
                    continue
                input_tok = info.get("input_tokens", 0) if isinstance(info.get("input_tokens"), (int, float)) else 0
                output_tok = info.get("output_tokens", 0) if isinstance(info.get("output_tokens"), (int, float)) else 0
                tokens = info.get("tokens", input_tok + output_tok) if isinstance(info.get("tokens"), (int, float)) else (input_tok + output_tok)
                key = model.lower()
                if key not in by_model:
                    by_model[key] = {"input_tokens": 0, "output_tokens": 0, "tokens": 0, "estimated_usd": 0.0}
                by_model[key]["input_tokens"] += input_tok
                by_model[key]["output_tokens"] += output_tok
                by_model[key]["tokens"] += tokens
                rates = RATES_PER_MILLION.get(key, {"input": 3.00, "output": 15.00})
                cost = (input_tok / 1_000_000) * rates["input"] + (output_tok / 1_000_000) * rates["output"]
                by_model[key]["estimated_usd"] += cost
                total_tokens += tokens
                total_input_tokens += input_tok
                total_output_tokens += output_tok
                total_usd += cost

        agents = usage.get("by_agent", {})
        if isinstance(agents, dict):
            for agent, info in agents.items():
                if not isinstance(info, dict):
                    continue
                input_tok = info.get("input_tokens", 0) if isinstance(info.get("input_tokens"), (int, float)) else 0
                output_tok = info.get("output_tokens", 0) if isinstance(info.get("output_tokens"), (int, float)) else 0
                tokens = info.get("tokens", input_tok + output_tok) if isinstance(info.get("tokens"), (int, float)) else (input_tok + output_tok)
                if agent not in by_agent:
                    by_agent[agent] = {"input_tokens": 0, "output_tokens": 0, "tokens": 0}
                by_agent[agent]["input_tokens"] += input_tok
                by_agent[agent]["output_tokens"] += output_tok
                by_agent[agent]["tokens"] += tokens

    return {
        "by_model": by_model,
        "by_agent": by_agent,
        "total_tokens": total_tokens,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_estimated_usd": round(total_usd * 100) / 100,
    }


def build_control_plane(project_path: str, slug: str) -> dict:
    ld = local_dynos_dir(project_path)
    pdir = persistent_dir(slug)

    maintainer = read_json_or_default(ld / "maintenance" / "status.json", {"running": False})
    autofix_enabled = (ld / "maintenance" / "autofix.enabled").exists()
    queue = read_json_or_default(ld / "automation" / "queue.json",
                                 {"version": 0, "updated_at": "", "items": []})
    automation_status = read_json_or_default(ld / "automation" / "status.json",
                                             {"updated_at": "", "queued_before": 0, "executed": 0, "pending_after": 0})

    registry = read_json_or_default(pdir / "learned-agents" / "registry.json", {"agents": []})
    agents = registry.get("agents", []) if isinstance(registry, dict) else []
    if not isinstance(agents, list):
        agents = []

    history = read_json_or_default(pdir / "benchmarks" / "history.json", {"runs": []})
    all_runs = history.get("runs", []) if isinstance(history, dict) else []
    if not isinstance(all_runs, list):
        all_runs = []
    recent_runs = all_runs[-10:]

    bench_index = read_json_or_default(pdir / "benchmarks" / "index.json", {"fixtures": []})
    fixtures = bench_index.get("fixtures", []) if isinstance(bench_index, dict) else []
    if not isinstance(fixtures, list):
        fixtures = []

    agent_summary = {
        "total": len(agents),
        "routeable": sum(1 for a in agents if a.get("route_allowed")),
        "shadow": sum(1 for a in agents if a.get("mode") == "shadow"),
        "alongside": sum(1 for a in agents if a.get("mode") == "alongside"),
        "replace": sum(1 for a in agents if a.get("mode") == "replace"),
        "demoted": sum(1 for a in agents if a.get("mode") == "demoted"),
    }

    bucket_map = {"Fresh": [], "Recent": [], "Aging": [], "Stale": [], "Unbenchmarked": []}
    for agent in agents:
        bs = agent.get("benchmark_summary")
        if not isinstance(bs, dict) or (isinstance(bs.get("sample_count"), (int, float)) and bs["sample_count"] == 0):
            bucket_map["Unbenchmarked"].append(agent.get("agent_name", ""))
        else:
            offset = agent.get("last_benchmarked_task_offset", 999)
            if not isinstance(offset, (int, float)):
                offset = 999
            if offset == 0:
                bucket_map["Fresh"].append(agent.get("agent_name", ""))
            elif offset <= 2:
                bucket_map["Recent"].append(agent.get("agent_name", ""))
            elif offset <= 5:
                bucket_map["Aging"].append(agent.get("agent_name", ""))
            else:
                bucket_map["Stale"].append(agent.get("agent_name", ""))

    freshness_buckets = [
        {"label": label, "count": len(arr), "agents": arr}
        for label, arr in bucket_map.items() if arr
    ]

    agent_names = {a.get("agent_name", "") for a in agents}
    coverage_gaps = [
        {
            "target_name": f.get("target_name", ""),
            "role": f.get("role", ""),
            "task_type": f.get("task_type", ""),
            "item_kind": f.get("item_kind", ""),
        }
        for f in fixtures if f.get("target_name") not in agent_names
    ]

    urgency_order = ["demoted on regression", "unbenchmarked", "stale benchmark", "coverage gap"]
    attention_items = []

    for agent in agents:
        if agent.get("mode") == "demoted":
            last_eval = agent.get("last_evaluation", {})
            if not isinstance(last_eval, dict):
                last_eval = {}
            attention_items.append({
                "agent_name": agent.get("agent_name"),
                "reason": "demoted on regression",
                "mode": agent.get("mode"),
                "status": agent.get("status"),
                "recommendation": last_eval.get("recommendation"),
                "delta_composite": last_eval.get("delta_composite"),
            })

    for agent in agents:
        bs = agent.get("benchmark_summary")
        if (not isinstance(bs, dict) or (isinstance(bs.get("sample_count"), (int, float)) and bs["sample_count"] == 0)):
            if agent.get("mode") != "demoted":
                attention_items.append({
                    "agent_name": agent.get("agent_name"),
                    "reason": "unbenchmarked",
                    "mode": agent.get("mode"),
                    "status": agent.get("status"),
                    "recommendation": None,
                    "delta_composite": None,
                })

    for agent in agents:
        bs = agent.get("benchmark_summary")
        is_benchmarked = isinstance(bs, dict) and isinstance(bs.get("sample_count"), (int, float)) and bs["sample_count"] > 0
        offset = agent.get("last_benchmarked_task_offset", 0)
        if not isinstance(offset, (int, float)):
            offset = 0
        if is_benchmarked and offset > 5 and agent.get("mode") != "demoted":
            attention_items.append({
                "agent_name": agent.get("agent_name"),
                "reason": "stale benchmark",
                "mode": agent.get("mode"),
                "status": agent.get("status"),
                "recommendation": None,
                "delta_composite": None,
            })

    for gap in coverage_gaps:
        attention_items.append({
            "agent_name": gap["target_name"],
            "reason": "coverage gap",
            "mode": "",
            "status": "",
            "recommendation": None,
            "delta_composite": None,
        })

    def urgency_key(item):
        reason = item.get("reason", "")
        return urgency_order.index(reason) if reason in urgency_order else len(urgency_order)
    attention_items.sort(key=urgency_key)

    return {
        "maintainer": maintainer,
        "autofix_enabled": autofix_enabled,
        "queue": queue,
        "automation_status": automation_status,
        "agents": agents,
        "freshness_buckets": freshness_buckets,
        "coverage_gaps": coverage_gaps,
        "attention_items": attention_items,
        "recent_runs": recent_runs,
        "agent_summary": agent_summary,
    }


# ---------------------------------------------------------------------------
# SPA + API HTTP Handler
# ---------------------------------------------------------------------------

def _resolve_dist_dir(root: Path) -> Path | None:
    """Find the Vite SPA dist directory."""
    candidates = [
        root / "hooks" / "dashboard-ui" / "dist",
    ]
    plugin_hooks = os.environ.get("PLUGIN_HOOKS")
    if plugin_hooks:
        candidates.append(Path(plugin_hooks) / "dashboard-ui" / "dist")
    candidates.append(Path(__file__).resolve().parent / "dashboard-ui" / "dist")
    for c in candidates:
        if c.is_dir() and (c / "index.html").is_file():
            return c
    return None


MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".eot": "application/vnd.ms-fontobject",
    ".map": "application/json",
    ".txt": "text/plain; charset=utf-8",
    ".webp": "image/webp",
}


def _content_type(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return MIME_TYPES.get(ext, "application/octet-stream")


class DashboardHandler(SimpleHTTPRequestHandler):
    """Serves SPA static files and API endpoints."""

    project_root: str = "."
    dist_dir: str = ""

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        pathname = parsed.path
        query = parse_qs(parsed.query)

        if pathname.startswith("/api/"):
            self._handle_api(pathname, query)
            return

        self._serve_static(pathname)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        pathname = parsed.path
        query = parse_qs(parsed.query)

        if pathname.startswith("/api/"):
            self._handle_api_post(pathname, query)
            return

        self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)

    def log_message(self, format: str, *args: object) -> None:
        pass

    # ---------------------------------------------------------------
    # Static file serving
    # ---------------------------------------------------------------

    def _serve_static(self, pathname: str) -> None:
        dist = Path(self.dist_dir)
        if not dist.is_dir():
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "dist directory not found")
            return

        # Normalize
        clean = pathname.lstrip("/")
        if clean == "":
            clean = "index.html"

        file_path = dist / clean
        # Security: ensure we stay within dist
        try:
            file_path.resolve().relative_to(dist.resolve())
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN, "Access denied")
            return

        if file_path.is_file():
            self._send_file(file_path)
        else:
            # SPA fallback: serve index.html for client-side routing
            index = dist / "index.html"
            if index.is_file():
                self._send_file(index)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

    def _send_file(self, file_path: Path) -> None:
        try:
            data = file_path.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        ct = _content_type(str(file_path))
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(data)))
        # Cache immutable hashed assets aggressively
        if "/assets/" in str(file_path):
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.end_headers()
        self.wfile.write(data)

    # ---------------------------------------------------------------
    # API helpers
    # ---------------------------------------------------------------

    def _resolve_project(self, query: dict) -> tuple[str, str, bool]:
        """Return (project_path, slug, is_global)."""
        project_param = query.get("project", [None])[0]
        is_global = project_param == "__global__"
        if project_param and not is_global:
            if not is_registered_project(project_param):
                return ("", "", False)  # sentinel for invalid
            project_path = os.path.realpath(project_param)
        else:
            project_path = self.project_root
        slug = compute_slug(project_path)
        return (project_path, slug, is_global)

    def _json_response(self, status: int, data: object) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _validate_task_id(self, task_id: str) -> bool:
        if not TASK_ID_PATTERN.match(task_id):
            self._json_response(400, {"error": "Invalid task ID"})
            return False
        return True

    def _handle_fs_error(self, err: Exception) -> None:
        if isinstance(err, FileNotFoundError):
            self._json_response(404, {"error": "Not found"})
        elif isinstance(err, json.JSONDecodeError):
            self._json_response(500, {"error": "Invalid JSON in file"})
        else:
            self._json_response(500, {"error": "Internal server error"})

    # ---------------------------------------------------------------
    # API GET routing
    # ---------------------------------------------------------------

    def _handle_api(self, pathname: str, query: dict) -> None:
        project_path, slug, is_global = self._resolve_project(query)
        project_param = query.get("project", [None])[0]
        if project_param and not is_global and not project_path:
            self._json_response(400, {"error": "Project not in registry"})
            return

        try:
            # ---- Simple top-level endpoints ----
            if pathname == "/api/tasks":
                self._api_tasks(project_path, is_global)
                return

            if pathname == "/api/agents":
                self._api_agents(project_path, slug, is_global)
                return

            if pathname == "/api/findings":
                self._api_findings(project_path, is_global)
                return

            if pathname == "/api/autofix-metrics":
                self._api_autofix_metrics(slug, is_global)
                return

            if pathname == "/api/policy":
                self._api_policy_file(slug, "policy.json")
                return

            if pathname == "/api/model-policy":
                self._api_policy_file(slug, "model-policy.json")
                return

            if pathname == "/api/route-policy":
                self._api_policy_file(slug, "route-policy.json")
                return

            if pathname == "/api/skip-policy":
                self._api_policy_file(slug, "skip-policy.json")
                return

            if pathname == "/api/autofix-policy":
                self._api_policy_file(slug, "autofix-policy.json")
                return

            if pathname == "/api/registry":
                data = read_json_file(Path.home() / ".dynos" / "registry.json")
                self._json_response(200, data)
                return

            if pathname == "/api/projects":
                data = read_json_or_default(Path.home() / ".dynos" / "registry.json", {"projects": []})
                self._json_response(200, data.get("projects", []) if isinstance(data, dict) else [])
                return

            if pathname == "/api/retrospectives":
                self._api_retrospectives(project_path, is_global)
                return

            if pathname == "/api/report":
                self._api_report(project_path, is_global)
                return

            if pathname == "/api/project-stats":
                self._api_project_stats(project_path, is_global)
                return

            if pathname == "/api/cost-summary":
                self._json_response(200, build_cost_summary(project_path))
                return

            if pathname == "/api/maintainer-status":
                data = read_json_or_default(local_dynos_dir(project_path) / "maintenance" / "status.json", {"running": False})
                self._json_response(200, data)
                return

            if pathname == "/api/maintenance-cycles":
                last_param = query.get("last", ["20"])[0]
                try:
                    last_n = int(last_param)
                except (ValueError, TypeError):
                    last_n = 20
                cycles_path = local_dynos_dir(project_path) / "maintenance" / "cycles.jsonl"
                try:
                    raw = read_text_file(cycles_path)
                    lines = [l for l in raw.split("\n") if l.strip()]
                    all_cycles = []
                    for line in lines:
                        try:
                            all_cycles.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
                    self._json_response(200, {"total_cycles": len(all_cycles), "cycles": all_cycles[-last_n:]})
                except (FileNotFoundError, OSError):
                    self._json_response(200, {"total_cycles": 0, "cycles": []})
                return

            if pathname == "/api/control-plane":
                self._json_response(200, build_control_plane(project_path, slug))
                return

            if pathname == "/api/state":
                if is_global:
                    self._json_response(400, {"error": "Repo state is only available for a single project"})
                else:
                    self._json_response(200, {"version": 1, "target": project_path})
                return

            # ---- Task-specific endpoints: /api/tasks/:taskId/... ----
            task_match = re.match(r"^/api/tasks/([^/]+)/(.+)$", pathname)
            if task_match:
                task_id = task_match.group(1)
                sub = task_match.group(2)
                if not self._validate_task_id(task_id):
                    return
                self._handle_task_endpoint(project_path, slug, task_id, sub, query)
                return

            # GET /api/tasks/:taskId/manifest (bare task ID)
            task_bare = re.match(r"^/api/tasks/([^/]+)$", pathname)
            if task_bare:
                task_id = task_bare.group(1)
                if not self._validate_task_id(task_id):
                    return
                task_dir = local_dynos_dir(project_path) / task_id
                data = read_json_file(task_dir / "manifest.json")
                self._json_response(200, data)
                return

            self._json_response(404, {"error": "Unknown API endpoint"})
        except Exception as err:
            self._handle_fs_error(err)

    def _handle_task_endpoint(self, project_path: str, slug: str, task_id: str, sub: str, query: dict) -> None:
        task_dir = local_dynos_dir(project_path) / task_id

        # JSON file endpoints
        if sub == "retrospective":
            data = read_json_file(task_dir / "task-retrospective.json")
            self._json_response(200, data)
            return

        if sub == "execution-graph":
            data = read_json_file(task_dir / "execution-graph.json")
            self._json_response(200, data)
            return

        if sub == "token-usage":
            data = read_json_file(task_dir / "token-usage.json")
            self._json_response(200, data)
            return

        if sub == "manifest":
            data = read_json_file(task_dir / "manifest.json")
            self._json_response(200, data)
            return

        if sub == "completion":
            data = read_json_file(task_dir / "completion.json")
            self._json_response(200, data)
            return

        # Text file endpoints (return as {content: "..."})
        if sub == "execution-log":
            raw = read_text_file(task_dir / "execution-log.md")
            lines = [l for l in raw.split("\n") if l.strip()]
            self._json_response(200, {"lines": lines})
            return

        if sub == "spec":
            content = read_text_file(task_dir / "spec.md")
            self._json_response(200, {"content": content})
            return

        if sub == "plan":
            content = read_text_file(task_dir / "plan.md")
            self._json_response(200, {"content": content})
            return

        if sub == "raw-input":
            content = read_text_file(task_dir / "raw-input.md")
            self._json_response(200, {"content": content})
            return

        if sub == "discovery-notes":
            content = read_text_file(task_dir / "discovery-notes.md")
            self._json_response(200, {"content": content})
            return

        if sub == "design-decisions":
            content = read_text_file(task_dir / "design-decisions.md")
            self._json_response(200, {"content": content})
            return

        # JSONL endpoints
        if sub == "events":
            try:
                raw = read_text_file(task_dir / "events.jsonl")
                events = []
                for line in raw.split("\n"):
                    line = line.strip()
                    if line:
                        try:
                            events.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
                self._json_response(200, {"events": events})
            except FileNotFoundError:
                self._json_response(200, {"events": []})
            return

        # Directory listing endpoints
        if sub == "audit-reports":
            dir_path = task_dir / "audit-reports"
            try:
                entries = sorted(e for e in os.listdir(dir_path) if e.endswith(".json"))
            except OSError:
                self._json_response(200, [])
                return
            reports = []
            for entry in entries:
                try:
                    data = read_json_file(dir_path / entry)
                    reports.append(data)
                except (FileNotFoundError, json.JSONDecodeError, OSError):
                    pass
            self._json_response(200, reports)
            return

        if sub == "receipts":
            dir_path = task_dir / "receipts"
            try:
                entries = sorted(e for e in os.listdir(dir_path) if e.endswith(".json"))
            except OSError:
                self._json_response(200, {"receipts": []})
                return
            receipts = []
            for entry in entries:
                try:
                    data = read_json_file(dir_path / entry)
                    receipts.append({"filename": entry, "data": data})
                except (FileNotFoundError, json.JSONDecodeError, OSError):
                    pass
            self._json_response(200, {"receipts": receipts})
            return

        if sub == "evidence":
            dir_path = task_dir / "evidence"
            try:
                entries = sorted(e for e in os.listdir(dir_path) if e.endswith(".md"))
            except OSError:
                self._json_response(200, {"files": []})
                return
            files = []
            for entry in entries:
                try:
                    content = read_text_file(dir_path / entry)
                    files.append({"name": entry, "content": content})
                except (FileNotFoundError, OSError):
                    pass
            self._json_response(200, {"files": files})
            return

        if sub == "postmortem":
            pdir = persistent_dir(slug)
            pm_dir = pdir / "postmortems"
            result = {}
            try:
                result["json"] = read_json_file(pm_dir / f"{task_id}.json")
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                pass
            try:
                result["markdown"] = read_text_file(pm_dir / f"{task_id}.md")
            except (FileNotFoundError, OSError):
                pass
            if not result:
                self._json_response(404, {"error": "Not found"})
            else:
                self._json_response(200, result)
            return

        if sub == "router-decisions":
            decisions = []
            task_created_at = ""
            task_completed_at = ""
            try:
                manifest = read_json_file(task_dir / "manifest.json")
                if isinstance(manifest, dict):
                    task_created_at = manifest.get("created_at", "")
                    task_completed_at = manifest.get("completed_at", "")
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                pass

            # Per-task events
            try:
                raw = read_text_file(task_dir / "events.jsonl")
                for line in raw.split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                        if isinstance(evt, dict) and isinstance(evt.get("event"), str) and evt["event"].startswith("router_"):
                            decisions.append(evt)
                    except json.JSONDecodeError:
                        pass
            except (FileNotFoundError, OSError):
                pass

            # Global events
            try:
                global_path = local_dynos_dir(project_path) / "events.jsonl"
                raw = read_text_file(global_path)
                for line in raw.split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                        if not isinstance(evt, dict):
                            continue
                        if not isinstance(evt.get("event"), str) or not evt["event"].startswith("router_"):
                            continue
                        if evt.get("task") == task_id:
                            decisions.append(evt)
                            continue
                        if task_created_at and isinstance(evt.get("ts"), str):
                            evt_time = evt["ts"]
                            in_range = evt_time >= task_created_at and (not task_completed_at or evt_time <= task_completed_at)
                            if in_range:
                                decisions.append(evt)
                    except json.JSONDecodeError:
                        pass
            except (FileNotFoundError, OSError):
                pass

            self._json_response(200, {"decisions": decisions})
            return

        self._json_response(404, {"error": "Unknown task endpoint"})

    # ---------------------------------------------------------------
    # Top-level API endpoint handlers
    # ---------------------------------------------------------------

    def _api_tasks(self, project_path: str, is_global: bool) -> None:
        if is_global:
            tasks = collect_from_all_projects(lambda pp, _slug: self._collect_tasks(pp))
        else:
            tasks = self._collect_tasks(project_path)
        self._json_response(200, tasks)

    def _collect_tasks(self, project_path: str) -> list[dict]:
        results = []
        for td in list_task_dirs(project_path):
            try:
                task_path = local_dynos_dir(project_path) / td
                manifest = read_json_file(task_path / "manifest.json")
                if isinstance(manifest, dict):
                    manifest = reconcile_stage(task_path, manifest)
                    manifest["task_dir"] = td
                    manifest["project_path"] = project_path
                    results.append(manifest)
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                pass
        return results

    def _api_agents(self, project_path: str, slug: str, is_global: bool) -> None:
        if is_global:
            agents = collect_from_all_projects(lambda pp, s: self._collect_agents(pp, s))
            self._json_response(200, agents)
        else:
            data = read_json_file(persistent_dir(slug) / "learned-agents" / "registry.json")
            self._json_response(200, data.get("agents", []) if isinstance(data, dict) else [])

    def _collect_agents(self, project_path: str, slug: str) -> list[dict]:
        try:
            data = read_json_file(persistent_dir(slug) / "learned-agents" / "registry.json")
            agents = data.get("agents", []) if isinstance(data, dict) else []
            return [{**a, "project_path": project_path} for a in agents if isinstance(a, dict)]
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []

    def _api_findings(self, project_path: str, is_global: bool) -> None:
        if is_global:
            findings = collect_from_all_projects(lambda pp, _s: self._collect_findings(pp))
            self._json_response(200, findings)
        else:
            data = read_json_file(local_dynos_dir(project_path) / "proactive-findings.json")
            self._json_response(200, data.get("findings", []) if isinstance(data, dict) else [])

    def _collect_findings(self, project_path: str) -> list[dict]:
        try:
            data = read_json_file(local_dynos_dir(project_path) / "proactive-findings.json")
            findings = data.get("findings", []) if isinstance(data, dict) else []
            return [{**f, "project_path": project_path} for f in findings if isinstance(f, dict)]
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []

    def _api_autofix_metrics(self, slug: str, is_global: bool) -> None:
        if is_global:
            all_metrics = collect_from_all_projects(lambda _pp, s: self._collect_autofix_metrics(s))
            if not all_metrics:
                self._json_response(200, {"totals": {}})
            else:
                merged: dict[str, float] = {}
                for m in all_metrics:
                    totals = m.get("totals", {}) if isinstance(m, dict) else {}
                    if isinstance(totals, dict):
                        for key, val in totals.items():
                            if isinstance(val, (int, float)):
                                merged[key] = merged.get(key, 0) + val
                self._json_response(200, {"totals": merged})
        else:
            data = read_json_file(persistent_dir(slug) / "autofix-metrics.json")
            self._json_response(200, data)

    def _collect_autofix_metrics(self, slug: str) -> list[dict]:
        try:
            data = read_json_file(persistent_dir(slug) / "autofix-metrics.json")
            return [data] if isinstance(data, dict) else []
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []

    def _api_policy_file(self, slug: str, filename: str) -> None:
        data = read_json_file(persistent_dir(slug) / filename)
        self._json_response(200, data)

    def _api_retrospectives(self, project_path: str, is_global: bool) -> None:
        if is_global:
            retros = collect_from_all_projects(lambda pp, _s: self._collect_retros(pp))
        else:
            retros = self._collect_retros(project_path)
        self._json_response(200, retros)

    def _collect_retros(self, project_path: str) -> list[dict]:
        results = []
        for td in list_task_dirs(project_path):
            try:
                data = read_json_file(local_dynos_dir(project_path) / td / "task-retrospective.json")
                if isinstance(data, dict):
                    data["task_id"] = td
                    data["project_path"] = project_path
                    results.append(data)
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                pass
        return results

    def _api_report(self, project_path: str, is_global: bool) -> None:
        if is_global:
            reports = collect_from_all_projects(lambda pp, _s: self._collect_report(pp))
            merged = {
                "registry_updated_at": None,
                "summary": {
                    "learned_components": 0, "active_routes": 0, "shadow_components": 0,
                    "demoted_components": 0, "queued_automation_jobs": 0, "benchmark_runs": 0,
                    "tracked_fixtures": 0, "coverage_gaps": 0,
                },
                "active_routes": [],
                "demotions": [],
                "automation_queue": [],
                "coverage_gaps": [],
                "recent_runs": [],
            }
            for report in reports:
                if not isinstance(report, dict):
                    continue
                summary = report.get("summary", {})
                if isinstance(summary, dict):
                    for key, value in summary.items():
                        if isinstance(value, (int, float)):
                            merged["summary"][key] = merged["summary"].get(key, 0) + value
                pp = report.get("project_path", "")
                for field in ("active_routes", "demotions", "automation_queue", "coverage_gaps", "recent_runs"):
                    items = report.get(field, [])
                    if isinstance(items, list):
                        for item in items:
                            if isinstance(item, dict):
                                merged[field].append({**item, "project_path": pp})
            merged["recent_runs"] = merged["recent_runs"][-10:]
            self._json_response(200, merged)
        else:
            self._json_response(200, build_repo_report(project_path))

    def _collect_report(self, project_path: str) -> list[dict]:
        try:
            report = build_repo_report(project_path)
            report["project_path"] = project_path
            return [report]
        except Exception:
            return []

    def _api_project_stats(self, project_path: str, is_global: bool) -> None:
        if is_global:
            stats_list = collect_from_all_projects(lambda pp, _s: self._collect_project_stats(pp))
            task_counts_by_type: dict[str, int] = {}
            executor_reliability_buckets: dict[str, list[float]] = {}
            prevention_rule_frequencies: dict[str, int] = {}
            prevention_rule_executors: dict[str, str] = {}
            total_tasks = 0
            quality_weighted_sum = 0.0

            for stats in stats_list:
                if not isinstance(stats, dict):
                    continue
                st = stats.get("total_tasks", 0)
                if isinstance(st, (int, float)):
                    total_tasks += st
                aq = stats.get("average_quality_score", 0)
                if isinstance(aq, (int, float)) and isinstance(st, (int, float)):
                    quality_weighted_sum += aq * st
                tcbt = stats.get("task_counts_by_type", {})
                if isinstance(tcbt, dict):
                    for tt, count in tcbt.items():
                        if isinstance(count, (int, float)):
                            task_counts_by_type[tt] = task_counts_by_type.get(tt, 0) + count
                er = stats.get("executor_reliability", {})
                if isinstance(er, dict):
                    for role, reliability in er.items():
                        if isinstance(reliability, (int, float)):
                            executor_reliability_buckets.setdefault(role, []).append(reliability)
                prf = stats.get("prevention_rule_frequencies", {})
                if isinstance(prf, dict):
                    for rule, count in prf.items():
                        if isinstance(count, (int, float)):
                            prevention_rule_frequencies[rule] = prevention_rule_frequencies.get(rule, 0) + count
                pre = stats.get("prevention_rule_executors", {})
                if isinstance(pre, dict):
                    for rule, executor in pre.items():
                        if isinstance(executor, str):
                            prevention_rule_executors.setdefault(rule, executor)

            executor_reliability = {
                role: round(sum(values) / max(1, len(values)), 3)
                for role, values in executor_reliability_buckets.items()
            }

            self._json_response(200, {
                "total_tasks": total_tasks,
                "task_counts_by_type": task_counts_by_type,
                "average_quality_score": round(quality_weighted_sum / max(1, total_tasks), 3),
                "executor_reliability": executor_reliability,
                "prevention_rule_frequencies": prevention_rule_frequencies,
                "prevention_rule_executors": prevention_rule_executors,
            })
        else:
            self._json_response(200, build_project_stats(project_path))

    def _collect_project_stats(self, project_path: str) -> list[dict]:
        try:
            return [build_project_stats(project_path)]
        except Exception:
            return []

    # ---------------------------------------------------------------
    # API POST routing
    # ---------------------------------------------------------------

    def _handle_api_post(self, pathname: str, query: dict) -> None:
        project_path, slug, is_global = self._resolve_project(query)
        project_param = query.get("project", [None])[0]
        if project_param and not is_global and not project_path:
            self._json_response(400, {"error": "Project not in registry"})
            return

        if is_global:
            self._json_response(400, {"error": "Global mode not supported for this endpoint"})
            return

        try:
            body = self._read_body()
        except Exception:
            self._json_response(400, {"error": "Invalid JSON body"})
            return

        if pathname == "/api/policy":
            self._write_policy(slug, "policy.json", body)
            return

        if pathname == "/api/autofix-policy":
            self._write_policy(slug, "autofix-policy.json", body)
            return

        self._json_response(404, {"error": "Unknown API endpoint"})

    def _read_body(self) -> object:
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > 1024 * 1024:
            raise ValueError("Request body too large")
        raw = self.rfile.read(content_length) if content_length > 0 else b""
        return json.loads(raw)

    def _write_policy(self, slug: str, filename: str, data: object) -> None:
        file_path = persistent_dir(slug) / filename
        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = file_path.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(data, indent=2))
            tmp_path.rename(file_path)
            self._json_response(200, {"ok": True})
        except OSError as err:
            self._handle_fs_error(err)


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def _local_pid_path(root: Path) -> Path:
    return root / ".dynos" / "dashboard.pid"


def _read_local_pid(root: Path) -> int | None:
    pid_path = _local_pid_path(root)
    if not pid_path.exists():
        return None
    try:
        pid = int(pid_path.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (ValueError, OSError):
        pid_path.unlink(missing_ok=True)
        return None


_REDIRECT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>dynos-work dashboard</title>
  <meta http-equiv="refresh" content="0; url=/">
  <style>body{font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;background:#0e1117;color:#e0e4eb}</style>
</head>
<body>
  <p>Redirecting to SPA dashboard&hellip; <a href="/">Click here</a> if not redirected.</p>
  <div id="stats" style="display:none"></div>
  <div id="routes" style="display:none"></div>
  <div id="queue" style="display:none"></div>
  <div id="gaps" style="display:none"></div>
  <div id="demotions" style="display:none"></div>
  <div id="runs" style="display:none"></div>
  <svg id="sparkline" style="display:none"></svg>
  <span id="updated" style="display:none"></span>
  <span id="lineage" style="display:none"></span>
</body>
</html>
"""


def build_dashboard_payload(root: Path) -> dict:
    """Build the full dashboard data payload. Used by global_dashboard."""
    report = build_report(root)
    lineage = build_lineage(root)
    report["generated_at"] = report.get("registry_updated_at")
    report["lineage"] = {"nodes": len(lineage.get("nodes", [])), "edges": len(lineage.get("edges", []))}
    report["lineage_graph"] = lineage
    return report


def cmd_generate(args: argparse.Namespace) -> int:
    """Generate dashboard-data.json and a minimal dashboard.html for backward compatibility."""
    root = Path(args.root).resolve()
    dynos_dir = root / ".dynos"
    dynos_dir.mkdir(parents=True, exist_ok=True)

    payload = build_dashboard_payload(root)

    data_path = dynos_dir / "dashboard-data.json"
    html_path = dynos_dir / "dashboard.html"
    data_path.write_text(json.dumps(payload, indent=2) + "\n")
    html_path.write_text(_REDIRECT_HTML)

    validation_errors = validate_generated_html(html_path)
    if validation_errors:
        for err in validation_errors:
            print(f"WARNING: {err}", file=_sys.stderr)

    print(json.dumps({
        "html_path": str(html_path),
        "data_path": str(data_path),
        "summary": payload.get("summary", {}),
        "validation_errors": validation_errors,
    }, indent=2))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    existing = _read_local_pid(root)
    if existing:
        print(json.dumps({"ok": False, "error": f"dashboard server already running (PID {existing}). Use 'kill' first."}))
        return 1

    dist_dir = _resolve_dist_dir(root)
    if dist_dir is None:
        print(json.dumps({"ok": False, "error": "Cannot find dashboard-ui/dist/ directory. Build the SPA first."}))
        return 1

    handler_cls = type("H", (DashboardHandler,), {
        "project_root": str(root),
        "dist_dir": str(dist_dir),
    })

    pid_path = _local_pid_path(root)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()))

    class _ReuseServer(ThreadingHTTPServer):
        allow_reuse_address = True
        allow_reuse_port = True

    port = getattr(args, "port", 8765)
    print(json.dumps({"url": f"http://127.0.0.1:{port}/"}))
    server = _ReuseServer(("127.0.0.1", port), handler_cls)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        pid_path.unlink(missing_ok=True)
    return 0


def cmd_kill(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    pid = _read_local_pid(root)
    if pid is None:
        print(json.dumps({"ok": True, "message": "no dashboard server running"}))
        return 0
    import signal
    try:
        os.kill(pid, signal.SIGTERM)
        print(json.dumps({"ok": True, "message": f"killed dashboard server (PID {pid})"}))
    except OSError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    _local_pid_path(root).unlink(missing_ok=True)
    return 0


def _kill_port(port: int) -> None:
    """Kill any process holding a port (fallback when PID file is missing)."""
    import subprocess
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            for pid_str in result.stdout.strip().split("\n"):
                try:
                    os.kill(int(pid_str), 9)
                except (ValueError, OSError):
                    pass
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def cmd_restart(args: argparse.Namespace) -> int:
    import signal, time
    root = Path(args.root).resolve()
    port = getattr(args, "port", 8765)
    pid = _read_local_pid(root)
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
            print(json.dumps({"message": f"killed old server (PID {pid})"}), flush=True)
        except OSError:
            pass
        _local_pid_path(root).unlink(missing_ok=True)
        for _ in range(20):
            try:
                os.kill(pid, 0)
                time.sleep(0.25)
            except OSError:
                break
    else:
        _kill_port(port)
    time.sleep(0.5)
    return cmd_serve(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="Generate dashboard-data.json")
    generate.add_argument("--root", default=".")
    generate.set_defaults(func=cmd_generate)

    serve = subparsers.add_parser("serve", help="Start the SPA dashboard server")
    serve.add_argument("--root", default=".")
    serve.add_argument("--port", type=int, default=8765)
    serve.set_defaults(func=cmd_serve)

    kill = subparsers.add_parser("kill", help="Stop the dashboard server")
    kill.add_argument("--root", default=".")
    kill.set_defaults(func=cmd_kill)

    restart = subparsers.add_parser("restart", help="Restart the dashboard server")
    restart.add_argument("--root", default=".")
    restart.add_argument("--port", type=int, default=8765)
    restart.set_defaults(func=cmd_restart)

    return parser


if __name__ == "__main__":
    from cli_base import cli_main
    raise SystemExit(cli_main(build_parser))
