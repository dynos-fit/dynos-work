#!/usr/bin/env python3
"""Serve the Vite SPA dashboard with a Python API backend."""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent)); _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "hooks"))

import argparse
import heapq
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer, HTTPStatus
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from lineage import build_lineage
from report import build_report
from lib_core import write_json
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

TERMINAL_STAGES = {"DONE", "FAILED", "CALIBRATED"}

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


def reconcile_stage(task_dir: Path, manifest: dict, cached_log: str | None = None) -> dict:
    """Reconcile manifest stage with execution log.

    cached_log: pre-read content of execution-log.md; avoids a redundant disk
    read when the caller already holds the file text.
    """
    stage = manifest.get("stage", "")
    if stage == "DONE" or (isinstance(stage, str) and "FAIL" in stage):
        return manifest

    try:
        if cached_log is None:
            log_content = (task_dir / "execution-log.md").read_text()
        else:
            log_content = cached_log
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
# Operator-dashboard helpers (AC 4-8)
# ---------------------------------------------------------------------------

# Parse "2026-04-25T03:49:54Z [STAGE] → SPEC_NORMALIZATION"
# or       "2026-04-25T04:26:54Z [ADVANCE] PLAN_AUDIT → PRE_EXECUTION_SNAPSHOT"
# Anchored, possessive-friendly. The arrow is unicode "→" (U+2192).
# Source: telemetry/dashboard.py reconcile_stage() format observed in
#         .dynos/<task>/execution-log.md.
_STAGE_LOG_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s+\[(?:STAGE|ADVANCE)\][^\n→]*→\s*(?P<stage>\S+)"
)

# Stage gates that require a human-approval-<stage>.json receipt per AC 5.
# Source: spec.md AC 45 + assumption #5 (lines 220-225 of spec.md).
GATE_STAGES = ("SPEC_REVIEW", "PLAN_REVIEW", "PLAN_AUDIT", "FINAL_AUDIT")

# Numeric ordering used to determine which gates a task has progressed past.
# Source: STAGE_ORDER above (deduplicated for clarity).
_STAGE_NUMERIC = STAGE_ORDER


def _parse_iso_z(s: str) -> datetime | None:
    """Parse a 'YYYY-MM-DDTHH:MM:SSZ' or ISO 8601 timestamp. Returns None on failure."""
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _stage_last_change(task_dir: Path, manifest: dict, cached_log: str | None = None) -> tuple[str, datetime | None]:
    """Return (current_stage, last_stage_change_dt). Falls back to created_at when log missing.

    Source: .dynos/<task>/execution-log.md  → last [STAGE] or [ADVANCE] line ts;
            falls back to manifest.json field `created_at` when log is unavailable.

    cached_log: pre-read content of execution-log.md; avoids a redundant disk
    read when the caller already holds the file text.
    """
    stage = ""
    if isinstance(manifest, dict):
        v = manifest.get("stage", "")
        if isinstance(v, str):
            stage = v

    last_change: datetime | None = None
    try:
        raw = cached_log if cached_log is not None else (task_dir / "execution-log.md").read_text()
        # Walk lines bottom-up to find the most recent stage-change marker.
        for line in reversed(raw.split("\n")):
            m = _STAGE_LOG_LINE_RE.match(line.strip())
            if m:
                last_change = _parse_iso_z(m.group("ts"))
                # Prefer log stage when manifest is silent.
                if not stage:
                    stage = m.group("stage")
                break
    except (OSError, UnicodeDecodeError):
        pass

    if last_change is None and isinstance(manifest, dict):
        last_change = _parse_iso_z(manifest.get("created_at", ""))

    return (stage, last_change)


def _iter_jsonl_events(path: Path):
    """Yield parsed JSON dicts from a .jsonl file. Silently skips bad lines."""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        yield obj
                except json.JSONDecodeError:
                    continue
    except (FileNotFoundError, OSError):
        return


def _read_global_events(project_path: str):
    """Yield events from <project>/.dynos/events.jsonl."""
    yield from _iter_jsonl_events(local_dynos_dir(project_path) / "events.jsonl")


def _read_task_events(project_path: str, task_id: str):
    """Yield events from <project>/.dynos/<task>/events.jsonl."""
    yield from _iter_jsonl_events(local_dynos_dir(project_path) / task_id / "events.jsonl")


def _expected_gates_for_stage(stage: str) -> list[str]:
    """Return the gate stages a task at `stage` should have passed.

    Source: STAGE_ORDER. A task at stage X should have approval receipts for
    every GATE_STAGES entry whose ordinal <= ordinal(X), unless the task is
    currently AT that gate stage (the gate itself is in-progress).
    """
    if not isinstance(stage, str) or stage not in _STAGE_NUMERIC:
        return []
    cur = _STAGE_NUMERIC[stage]
    out = []
    for g in GATE_STAGES:
        g_ord = _STAGE_NUMERIC.get(g)
        if g_ord is None:
            continue
        # Strictly past: g_ord < cur. AT the gate: g_ord == cur (still pending).
        if g_ord < cur:
            out.append(g)
    return out


# ---------------------------------------------------------------------------
# Computed endpoint builders
# ---------------------------------------------------------------------------

def _is_daemon_running(project_path: str) -> bool:
    """Return True if the maintenance daemon is running for the given project."""
    pid_file = Path(project_path) / ".dynos" / "maintenance" / "daemon.pid"
    if not pid_file.exists():
        return False
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, OSError):
        return False


def _build_project_summary(proj: dict) -> dict:
    """Build a single entry for /api/projects-summary from a registry project dict."""
    project_path = proj.get("path", "")
    slug = compute_slug(project_path)
    name = os.path.basename(project_path) if project_path else ""

    # Count tasks and derive stage/activity metrics
    task_dirs = list_task_dirs(project_path)
    task_count = len(task_dirs)

    quality_scores: list[float] = []
    last_active_at: str | None = None
    active_task_stage: str | None = None
    # Track manifests sorted by created_at for stage and last_active_at
    manifests: list[dict] = []
    for td in task_dirs:
        try:
            task_path = local_dynos_dir(project_path) / td
            manifest = read_json_file(task_path / "manifest.json")
            if isinstance(manifest, dict):
                manifest = reconcile_stage(task_path, manifest)
                manifests.append(manifest)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass

    # Sort by created_at descending (most recent first)
    def _created_at_key(m: dict) -> str:
        v = m.get("created_at", "")
        return v if isinstance(v, str) else ""

    manifests_sorted = sorted(manifests, key=_created_at_key, reverse=True)

    for m in manifests_sorted:
        stage = m.get("stage", "")
        if not isinstance(stage, str):
            stage = ""
        # active_task_stage: stage of most recently created non-terminal task
        if active_task_stage is None and stage not in TERMINAL_STAGES:
            active_task_stage = stage if stage else None
        # last_active_at: completed_at of most recently completed (DONE) task
        if last_active_at is None and stage == "DONE":
            ca = m.get("completed_at")
            if isinstance(ca, str) and ca:
                last_active_at = ca

    # Quality scores from retrospectives
    for td in task_dirs:
        try:
            retro = read_json_file(local_dynos_dir(project_path) / td / "task-retrospective.json")
            if isinstance(retro, dict):
                qs = retro.get("quality_score")
                if isinstance(qs, (int, float)):
                    quality_scores.append(float(qs))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass

    avg_quality_score: float | None = (
        round(sum(quality_scores) / len(quality_scores), 3)
        if quality_scores else None
    )

    # prevention_rule_count: length of prevention-rules.json array
    prevention_rule_count: int | None = None
    try:
        rules_path = persistent_dir(slug) / "prevention-rules.json"
        rules_data = read_json_file(rules_path)
        if isinstance(rules_data, list):
            prevention_rule_count = len(rules_data)
        elif isinstance(rules_data, dict):
            items = rules_data.get("rules", rules_data.get("items", []))
            if isinstance(items, list):
                prevention_rule_count = len(items)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        prevention_rule_count = None

    # learned_routes_count: agents with route_allowed==True in learned-agents/registry.json
    learned_routes_count: int | None = None
    try:
        agents_registry = read_json_file(persistent_dir(slug) / "learned-agents" / "registry.json")
        if isinstance(agents_registry, dict):
            agents = agents_registry.get("agents", [])
            if isinstance(agents, list):
                learned_routes_count = sum(
                    1 for a in agents
                    if isinstance(a, dict) and a.get("route_allowed")
                )
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        learned_routes_count = None

    daemon_running = _is_daemon_running(project_path)

    return {
        "slug": slug,
        "name": name,
        "path": project_path,
        "task_count": task_count,
        "avg_quality_score": avg_quality_score,
        "active_task_stage": active_task_stage,
        "daemon_running": daemon_running,
        "last_active_at": last_active_at,
        "prevention_rule_count": prevention_rule_count,
        "learned_routes_count": learned_routes_count,
    }


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
        # SEC-OPUS-002: reject DNS-rebinding attempts via Host header.
        if not self._check_host():
            self.send_error(HTTPStatus.FORBIDDEN, "Forbidden: invalid Host header")
            return

        parsed = urlparse(self.path)
        pathname = parsed.path
        query = parse_qs(parsed.query)

        if pathname.startswith("/api/"):
            self._handle_api(pathname, query)
            return

        self._serve_static(pathname)

    def do_POST(self) -> None:
        # SEC-OPUS-002: reject DNS-rebinding attempts via Host header.
        if not self._check_host():
            self.send_error(HTTPStatus.FORBIDDEN, "Forbidden: invalid Host header")
            return
        # SEC-OPUS-001: reject cross-origin writes (CSRF / DNS rebinding).
        if not self._check_origin():
            self.send_error(HTTPStatus.FORBIDDEN, "Forbidden: cross-origin request rejected")
            return

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
    # Security: Host / Origin validation (DNS rebinding + CSRF defense)
    # ---------------------------------------------------------------

    def _check_host(self) -> bool:
        """Return True iff the Host header points at a localhost address.

        Browsers always send a Host header. Legitimate operator traffic
        will have Host: localhost:<port> or 127.0.0.1:<port>. Anything
        else (e.g. attacker.com via DNS rebinding) is rejected.
        Missing Host is allowed for non-browser tools (curl --http1.0).
        """
        host_header = self.headers.get("Host", "")
        if not host_header:
            return True
        # Strip optional :port suffix; handle IPv6 bracketed form too.
        host = host_header.strip().lower()
        if host.startswith("["):
            # IPv6 literal: [::1]:port -> ::1
            end = host.find("]")
            if end == -1:
                return False
            hostname = host[1:end]
        else:
            hostname = host.split(":", 1)[0]
        return hostname in ("localhost", "127.0.0.1", "::1")

    def _check_origin(self) -> bool:
        """Return True iff the Origin header is absent or points at localhost.

        Used to defend mutating endpoints from CSRF / DNS-rebinding writes.
        Same-origin XHR/fetch from the dashboard SPA will set Origin to
        http://localhost:<port> or http://127.0.0.1:<port>. Cross-origin
        requests from another website will carry that other origin and
        are rejected.
        """
        origin = self.headers.get("Origin", "")
        if not origin:
            return True  # same-origin form post or non-browser client
        try:
            port = self.server.server_port
        except AttributeError:
            port = None
        allowed = {
            "http://localhost",
            "http://127.0.0.1",
        }
        if port is not None:
            allowed.add(f"http://localhost:{port}")
            allowed.add(f"http://127.0.0.1:{port}")
        return origin in allowed

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

            if pathname == "/api/projects-summary":
                registry_data = read_json_or_default(
                    Path.home() / ".dynos" / "registry.json",
                    {"projects": []},
                )
                projects_list = (
                    registry_data.get("projects", [])
                    if isinstance(registry_data, dict)
                    else []
                )
                if not isinstance(projects_list, list):
                    projects_list = []
                summary: list[dict] = []
                for proj in projects_list:
                    if not isinstance(proj, dict):
                        continue
                    try:
                        summary.append(_build_project_summary(proj))
                    except Exception:
                        pass
                self._json_response(200, summary)
                return

            if pathname == "/api/state":
                if is_global:
                    self._json_response(400, {"error": "Repo state is only available for a single project"})
                else:
                    state_path = local_dynos_dir(project_path) / "repo-state.json"
                    raw_state = read_json_or_default(state_path, {})
                    if not isinstance(raw_state, dict):
                        raw_state = {}
                    self._json_response(200, {
                        "version": raw_state.get("version", 1),
                        "target": raw_state.get("target", project_path),
                        "architecture_complexity_score": raw_state.get("architecture_complexity_score", None),
                        "dependency_flux": raw_state.get("dependency_flux", None),
                        "finding_entropy": raw_state.get("finding_entropy", None),
                        "file_count": raw_state.get("file_count", None),
                        "line_count": raw_state.get("line_count", None),
                        "import_count": raw_state.get("import_count", None),
                        "control_flow_count": raw_state.get("control_flow_count", None),
                        "dominant_languages": raw_state.get("dominant_languages", None),
                        "recent_findings_by_category": raw_state.get("recent_findings_by_category", None),
                        "blocked_reason": raw_state.get("blocked_reason", None),
                    })
                return

            # ---- Operator-dashboard aggregation endpoints (AC 4-8, 13) ----
            # Routed before the task-specific regex per AC 13.
            if pathname == "/api/machine-summary":
                self._handle_machine_summary(query)
                return
            if pathname == "/api/trust-summary":
                self._handle_trust_summary(query)
                return
            if pathname == "/api/events-feed":
                self._handle_events_feed(query)
                return
            if pathname == "/api/cross-repo-timeline":
                self._handle_cross_repo_timeline(query)
                return
            if pathname == "/api/palette-index":
                self._handle_palette_index(query)
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
            if not isinstance(data, dict):
                self._json_response(200, data)
                return
            # AC 37: augment with DORA fields
            lead_time_seconds: int | None = None
            change_failure_rate: float | None = None
            recovery_time_seconds: int | None = None

            # SC-003 fix: lead_time_seconds is computed from the manifest
            # (stage/created_at/completed_at are manifest fields, not retrospective fields).
            manifest_data = read_json_or_default(task_dir / "manifest.json", {})
            if not isinstance(manifest_data, dict):
                manifest_data = {}
            try:
                m_stage = manifest_data.get("stage", "")
                m_created = manifest_data.get("created_at", "")
                m_completed = manifest_data.get("completed_at", "")
                if (
                    m_stage == "DONE"
                    and isinstance(m_created, str) and m_created
                    and isinstance(m_completed, str) and m_completed
                ):
                    from datetime import datetime
                    created = datetime.fromisoformat(m_created.replace("Z", "+00:00"))
                    completed = datetime.fromisoformat(m_completed.replace("Z", "+00:00"))
                    diff = (completed - created).total_seconds()
                    if diff >= 0:
                        lead_time_seconds = int(diff)
                    else:
                        lead_time_seconds = None
                else:
                    lead_time_seconds = None
            except (ValueError, TypeError, AttributeError):
                lead_time_seconds = None

            # change_failure_rate: FAILED / total for this project
            try:
                all_task_dirs = list_task_dirs(project_path)
                total_count = 0
                failed_count = 0
                for td in all_task_dirs:
                    try:
                        t_path = local_dynos_dir(project_path) / td
                        m = read_json_file(t_path / "manifest.json")
                        if isinstance(m, dict):
                            m = reconcile_stage(t_path, m)
                            total_count += 1
                            if isinstance(m.get("stage"), str) and "FAIL" in m["stage"]:
                                failed_count += 1
                    except (FileNotFoundError, json.JSONDecodeError, OSError):
                        pass
                change_failure_rate = (
                    round(failed_count / total_count, 4)
                    if total_count > 0 else None
                )
            except Exception:
                change_failure_rate = None

            self._json_response(200, {
                **data,
                "lead_time_seconds": lead_time_seconds,
                "change_failure_rate": change_failure_rate,
                "recovery_time_seconds": recovery_time_seconds,
            })
            return

        if sub == "audit-summary":
            raw = read_json_or_default(task_dir / "audit-summary.json", None)
            if raw is None:
                self._json_response(200, {"present": False, "data": None})
            else:
                self._json_response(200, {"present": True, "data": raw})
            return

        if sub == "repair-log":
            raw = read_json_or_default(task_dir / "repair-log.json", None)
            if raw is None:
                self._json_response(200, {"present": False, "data": None})
            else:
                self._json_response(200, {"present": True, "data": raw})
            return

        if sub == "handoff":
            raw = read_json_or_default(task_dir / "handoff-execute-audit.json", None)
            if raw is None:
                self._json_response(200, {"present": False, "data": None})
            else:
                self._json_response(200, {"present": True, "data": raw})
            return

        if sub == "audit-plan":
            raw = read_json_or_default(task_dir / "audit-plan.json", None)
            if raw is None:
                self._json_response(200, {"present": False, "data": None})
            else:
                self._json_response(200, {"present": True, "data": raw})
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
            try:
                tail_raw = query.get("tail", ["100"])[0]
                try:
                    tail = int(tail_raw)
                    tail = max(10, min(500, tail))
                except (ValueError, TypeError):
                    tail = 100
                manifest_data = read_json_or_default(task_dir / "manifest.json", {})
                stage = str(manifest_data.get("stage", "UNKNOWN")) if isinstance(manifest_data, dict) else "UNKNOWN"
                try:
                    raw = read_text_file(task_dir / "execution-log.md")
                    lines = [l for l in raw.split("\n") if l.strip()]
                    lines = lines[-tail:]
                except (FileNotFoundError, OSError):
                    lines = []
                self._json_response(200, {"lines": lines, "stage": stage, "task_id": task_id})
            except Exception as err:
                self._handle_fs_error(err)
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
    # Operator-dashboard aggregation handlers (AC 4-8)
    # ---------------------------------------------------------------

    def _get_all_projects(self, query: dict) -> list[tuple[str, str]]:
        """Return [(project_path, slug), ...] either scoped to ?project= or all registered.

        AC 11: when ?project=<absolute-path> is supplied and resolves, scope to
        that single repo. The caller is expected to have already validated the
        project param via _resolve_project; this method just shapes the list.
        """
        project_param = query.get("project", [None])[0]
        if project_param and project_param != "__global__":
            # Already validated by _resolve_project at handler entry.
            pp = os.path.realpath(project_param)
            return [(pp, compute_slug(pp))]
        # Otherwise: every registered project.
        try:
            registry = get_registry()
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []
        out = []
        for proj in registry.get("projects", []) if isinstance(registry, dict) else []:
            if not isinstance(proj, dict):
                continue
            pp = proj.get("path", "")
            if not isinstance(pp, str) or not pp:
                continue
            out.append((pp, compute_slug(pp)))
        return out

    def _handle_machine_summary(self, query: dict) -> None:
        """AC 4: machine-wide aggregation with 2-second budget (AC 10) and per-repo
        try/except (AC 9). Source files per metric:

          - active_repos: registry.json `projects` length
          - active_tasks/active_agents: per-task manifest.json `stage` not in TERMINAL_STAGES
          - token_burn_rate_per_min: per-repo .dynos/events.jsonl `tokens` field within last 60s
          - current_cost_by_model: per-task .dynos/<task>/token-usage.json `by_model`
          - error_rate: per-task manifest.json `stage` containing 'FAIL' over tasks created in last 24h
          - stalled_agents: per-task .dynos/<task>/execution-log.md last [STAGE]/[ADVANCE] ts (>30min)
          - orphan_token_events: per-repo .dynos/events.jsonl `tokens` field with no executor receipt
          - receipt_failures: per-repo .dynos/events.jsonl events containing 'validate_receipts' fail
          - stage_lag: same as stalled_agents but >60min
          - queue_depth: per-task manifest.json `stage` in {PLANNING, SPEC_NORMALIZATION}
          - retry_rate: per-task manifest.json any value of `retry_counts` > 0
          - top_failing_repos: error_rate>0 sorted desc, top 5
          - top_expensive_repos: sum(by_model.estimated_usd) per repo, top 5
          - top_expensive_tasks: per-task token-usage.json total_tokens, top 10
        """
        budget_start = time.monotonic()
        BUDGET_SECONDS = 2.0
        STALLED_THRESHOLD_S = 30 * 60  # 30 minutes per spec
        STAGE_LAG_THRESHOLD_S = 60 * 60  # 1 hour per spec

        now = datetime.now(timezone.utc)
        cutoff_24h = now - timedelta(hours=24)
        cutoff_60s = now - timedelta(seconds=60)

        all_projects = self._get_all_projects(query)
        active_repos = len(all_projects)

        active_tasks = 0
        token_burn_rate_per_min = 0
        by_model_agg: dict[str, dict] = {}
        failed_24h = 0
        created_24h = 0
        stalled_agents: list[dict] = []
        orphan_token_events = 0
        receipt_failures = 0
        stage_lag: list[dict] = []
        queue_depth = 0
        retry_with_count = 0
        total_tasks_count = 0
        repo_error_rates: list[tuple[str, float]] = []
        repo_costs: list[tuple[str, float]] = []
        per_task_costs: list[dict] = []
        repos_failed: list[dict] = []
        repos_skipped: list[str] = []
        degraded = False

        for idx, (pp, slug) in enumerate(all_projects):
            if (time.monotonic() - budget_start) > BUDGET_SECONDS:
                degraded = True
                repos_skipped.extend(s for _p, s in all_projects[idx:])
                break

            try:
                # ---- per-repo events.jsonl scan: token burn + receipt failures ----
                # Source: <repo>/.dynos/events.jsonl
                repo_token_burn = 0
                repo_receipt_failures = 0
                # Track tokens-event task_ids → cross-check executor receipts
                token_event_tasks: list[tuple[str, str]] = []  # (task_id, agent or '')
                for evt in _read_global_events(pp):
                    ev_name = evt.get("event", "")
                    ev_ts = _parse_iso_z(evt.get("ts", ""))
                    tokens = evt.get("tokens")
                    # token burn: any event with a numeric tokens field in last 60s
                    if isinstance(tokens, (int, float)) and ev_ts and ev_ts >= cutoff_60s:
                        repo_token_burn += int(tokens)
                    # orphan-token bookkeeping: any event with tokens field
                    if isinstance(tokens, (int, float)):
                        tid = evt.get("task", "") if isinstance(evt.get("task"), str) else ""
                        agent = evt.get("agent", "") if isinstance(evt.get("agent"), str) else ""
                        if tid:
                            token_event_tasks.append((tid, agent))
                    # receipt_failures: validate_receipts_failed-style events in last 24h
                    if (
                        isinstance(ev_name, str)
                        and ("validate_receipts_failed" in ev_name or ev_name == "receipt_validation_failed")
                        and ev_ts and ev_ts >= cutoff_24h
                    ):
                        repo_receipt_failures += 1
                token_burn_rate_per_min += repo_token_burn
                receipt_failures += repo_receipt_failures

                # ---- per-task scan: tasks, costs, stages, stalled/lag, retries ----
                task_dirs = list_task_dirs(pp)
                # Pre-build set of task_ids that have an executor receipt for orphan check.
                # Source: <repo>/.dynos/<task>/receipts/executor-*.json
                tasks_with_executor_receipt: set[str] = set()
                repo_total_usd = 0.0
                repo_total_tasks = 0
                repo_failed_tasks = 0

                for td in task_dirs:
                    # PERF-002: task-level budget check — bail early before more I/O
                    if (time.monotonic() - budget_start) > BUDGET_SECONDS:
                        degraded = True
                        break

                    task_path = local_dynos_dir(pp) / td

                    # PERF-002: read execution-log.md ONCE per task and reuse for
                    # both reconcile_stage and _stage_last_change to halve file I/O.
                    try:
                        _log_text: str | None = (task_path / "execution-log.md").read_text()
                    except (OSError, UnicodeDecodeError):
                        _log_text = None

                    # manifest read
                    try:
                        manifest = read_json_file(task_path / "manifest.json")
                    except (FileNotFoundError, json.JSONDecodeError, OSError):
                        manifest = {}
                    if not isinstance(manifest, dict):
                        manifest = {}
                    manifest = reconcile_stage(task_path, manifest, cached_log=_log_text)
                    stage = manifest.get("stage", "")
                    if not isinstance(stage, str):
                        stage = ""

                    repo_total_tasks += 1
                    total_tasks_count += 1

                    # active counters
                    if stage and stage not in TERMINAL_STAGES:
                        active_tasks += 1

                    # queue depth
                    if stage in ("PLANNING", "SPEC_NORMALIZATION"):
                        queue_depth += 1

                    # error/created in last 24h
                    created_dt = _parse_iso_z(manifest.get("created_at", ""))
                    if created_dt and created_dt >= cutoff_24h:
                        created_24h += 1
                        if "FAIL" in stage:
                            failed_24h += 1
                    if "FAIL" in stage:
                        repo_failed_tasks += 1

                    # retry rate
                    rc = manifest.get("retry_counts", {})
                    if isinstance(rc, dict) and any(
                        isinstance(v, (int, float)) and v > 0 for v in rc.values()
                    ):
                        retry_with_count += 1

                    # stalled / stage-lag (only meaningful for non-terminal tasks)
                    if stage and stage not in TERMINAL_STAGES:
                        _, last_change = _stage_last_change(task_path, manifest, cached_log=_log_text)
                        if last_change is not None:
                            age_s = int((now - last_change).total_seconds())
                            if age_s > STALLED_THRESHOLD_S:
                                stalled_agents.append({
                                    "task_id": td,
                                    "repo_slug": slug,
                                    "stage": stage,
                                    "stage_age_seconds": age_s,
                                })
                            if age_s > STAGE_LAG_THRESHOLD_S:
                                stage_lag.append({
                                    "task_id": td,
                                    "repo_slug": slug,
                                    "stage": stage,
                                    "last_change_at": last_change.isoformat(),
                                })

                    # cost per task: token-usage.json
                    try:
                        usage = read_json_file(task_path / "token-usage.json")
                    except (FileNotFoundError, json.JSONDecodeError, OSError):
                        usage = None
                    task_total_tokens = 0
                    task_total_usd = 0.0
                    if isinstance(usage, dict):
                        models = usage.get("by_model", {})
                        if isinstance(models, dict):
                            for model, info in models.items():
                                if not isinstance(info, dict):
                                    continue
                                inp = info.get("input_tokens", 0)
                                outp = info.get("output_tokens", 0)
                                if not isinstance(inp, (int, float)):
                                    inp = 0
                                if not isinstance(outp, (int, float)):
                                    outp = 0
                                key = model.lower() if isinstance(model, str) else "unknown"
                                rates = RATES_PER_MILLION.get(key, {"input": 3.00, "output": 15.00})
                                cost = (inp / 1_000_000) * rates["input"] + (outp / 1_000_000) * rates["output"]
                                task_total_usd += cost
                                # roll into machine-wide cost by model
                                bucket = by_model_agg.setdefault(key, {
                                    "input_tokens": 0,
                                    "output_tokens": 0,
                                    "estimated_usd": 0.0,
                                })
                                bucket["input_tokens"] += int(inp)
                                bucket["output_tokens"] += int(outp)
                                bucket["estimated_usd"] += cost
                        tt = usage.get("total_tokens")
                        if not isinstance(tt, (int, float)):
                            tt = 0
                            agents_obj = usage.get("agents", {})
                            if isinstance(agents_obj, dict):
                                for v in agents_obj.values():
                                    if isinstance(v, (int, float)):
                                        tt += int(v)
                        task_total_tokens = int(tt)

                    repo_total_usd += task_total_usd
                    per_task_costs.append({
                        "task_id": td,
                        "repo_slug": slug,
                        "total_tokens": task_total_tokens,
                        "estimated_usd": round(task_total_usd, 4),
                    })

                    # executor-receipt presence for orphan check
                    # Source: <repo>/.dynos/<task>/receipts/executor-*.json
                    try:
                        for entry in os.listdir(task_path / "receipts"):
                            if entry.startswith("executor-") and entry.endswith(".json"):
                                tasks_with_executor_receipt.add(td)
                                break
                    except OSError:
                        pass

                # orphan_token_events: token events whose task has no executor receipt
                # Source: counted from token_event_tasks built from <repo>/.dynos/events.jsonl
                for tid, _agent in token_event_tasks:
                    if tid not in tasks_with_executor_receipt:
                        orphan_token_events += 1

                # repo error rate (over all tasks in repo, not just last 24h)
                if repo_total_tasks > 0:
                    er = repo_failed_tasks / repo_total_tasks
                    if er > 0:
                        repo_error_rates.append((slug, round(er, 4)))
                if repo_total_usd > 0:
                    repo_costs.append((slug, round(repo_total_usd, 4)))
            except Exception as exc:
                # AC 9: never propagate, never 500
                repos_failed.append({"slug": slug, "reason": str(exc)[:200]})
                continue

        # Round per-model usd for stable JSON
        for key in by_model_agg:
            by_model_agg[key]["estimated_usd"] = round(by_model_agg[key]["estimated_usd"], 4)

        # Top-N rankings
        repo_error_rates.sort(key=lambda x: x[1], reverse=True)
        top_failing_repos = [
            {"slug": s, "error_rate": e} for (s, e) in repo_error_rates[:5]
        ]
        repo_costs.sort(key=lambda x: x[1], reverse=True)
        top_expensive_repos = [
            {"slug": s, "total_estimated_usd": c} for (s, c) in repo_costs[:5]
        ]
        per_task_costs.sort(key=lambda x: x["total_tokens"], reverse=True)
        top_expensive_tasks = per_task_costs[:10]

        error_rate = (
            round(failed_24h / created_24h, 4) if created_24h > 0 else None
        )
        retry_rate = (
            round(retry_with_count / total_tasks_count, 4)
            if total_tasks_count > 0 else None
        )

        active_agents = active_tasks  # AC 4: same as active_tasks

        payload = {
            "active_repos": active_repos,
            "active_tasks": active_tasks,
            "active_agents": active_agents,
            "token_burn_rate_per_min": int(token_burn_rate_per_min),
            "current_cost_by_model": by_model_agg,
            "error_rate": error_rate,
            "stalled_agents": stalled_agents,
            "orphan_token_events": orphan_token_events,
            "receipt_failures": receipt_failures,
            "stage_lag": stage_lag,
            "queue_depth": queue_depth,
            "retry_rate": retry_rate,
            "top_failing_repos": top_failing_repos,
            "top_expensive_repos": top_expensive_repos,
            "top_expensive_tasks": top_expensive_tasks,
            "repos_failed": repos_failed,
            "degraded": degraded,
            "repos_skipped": repos_skipped,
            "computed_at": now.isoformat(),
        }
        self._json_response(200, payload)

    def _handle_trust_summary(self, query: dict) -> None:
        """AC 5: trust-substrate aggregation. Per-repo try/except (AC 9), 2-second
        budget (AC 10). Sources:

          - deterministic_ops: stage_transition events with forced!=true from
            <repo>/.dynos/events.jsonl AND <repo>/.dynos/<task>/events.jsonl
          - prompt_owned_ops: events whose name contains 'agent' or 'spawn' with
            no matching <repo>/.dynos/<task>/receipts/executor-*.json receipt
          - missing_receipts: per task that has progressed past a gate stage,
            check <repo>/.dynos/<task>/receipts/human-approval-<gate>.json
          - skipped_gates: same source — when manifest stage > gate but no
            receipt is present
          - unverifiable_transitions: per task, count receipts/ entries vs
            expected gates; gaps reported as reason strings
        """
        budget_start = time.monotonic()
        BUDGET_SECONDS = 2.0
        now = datetime.now(timezone.utc)

        all_projects = self._get_all_projects(query)

        deterministic_ops = 0
        prompt_owned_ops = 0
        missing_receipts: list[dict] = []
        skipped_gates: list[dict] = []
        unverifiable_transitions: list[dict] = []
        repos_skipped: list[str] = []
        degraded = False

        # PERF-007: request-local cache so the same task's manifest isn't
        # re-read if other handlers in the same request touch it. Keyed by
        # absolute task_dir path. Lives only for this handler invocation.
        _manifest_cache: dict[str, dict] = {}

        for idx, (pp, slug) in enumerate(all_projects):
            if (time.monotonic() - budget_start) > BUDGET_SECONDS:
                degraded = True
                repos_skipped.extend(s for _p, s in all_projects[idx:])
                break
            try:
                # Global events for this repo
                # Source: <repo>/.dynos/events.jsonl
                for evt in _read_global_events(pp):
                    ev_name = evt.get("event", "")
                    if not isinstance(ev_name, str):
                        continue
                    if ev_name == "stage_transition":
                        forced = evt.get("forced", False)
                        if not forced:
                            deterministic_ops += 1

                # Per-task scan
                for td in list_task_dirs(pp):
                    # PERF-003/PERF-004: task-level budget check — N+1 file I/O
                    # (manifest.json + receipts dir + per-task events.jsonl)
                    # otherwise blows past the budget on large repos.
                    if (time.monotonic() - budget_start) > BUDGET_SECONDS:
                        degraded = True
                        break

                    task_path = local_dynos_dir(pp) / td
                    cache_key = str(task_path)

                    # PERF-007: manifest cache — avoid re-reading manifest.json
                    # if this task was already touched in this request.
                    cached_manifest = _manifest_cache.get(cache_key)
                    if cached_manifest is not None:
                        manifest = cached_manifest
                    else:
                        try:
                            manifest = read_json_file(task_path / "manifest.json")
                        except (FileNotFoundError, json.JSONDecodeError, OSError):
                            manifest = {}
                        if not isinstance(manifest, dict):
                            manifest = {}

                    # PERF-009: read execution-log.md once and pass it into
                    # reconcile_stage rather than letting reconcile re-read it.
                    try:
                        _log_text: str | None = (task_path / "execution-log.md").read_text()
                    except (OSError, UnicodeDecodeError):
                        _log_text = None
                    manifest = reconcile_stage(task_path, manifest, cached_log=_log_text)
                    _manifest_cache[cache_key] = manifest
                    stage = manifest.get("stage", "")
                    if not isinstance(stage, str):
                        stage = ""

                    # Build set of present receipts for this task
                    # Source: <repo>/.dynos/<task>/receipts/*.json
                    present_receipts: set[str] = set()
                    has_executor_receipt = False
                    try:
                        for entry in os.listdir(task_path / "receipts"):
                            if not entry.endswith(".json"):
                                continue
                            present_receipts.add(entry)
                            if entry.startswith("executor-"):
                                has_executor_receipt = True
                    except OSError:
                        pass

                    # Per-task events
                    # Source: <repo>/.dynos/<task>/events.jsonl
                    for evt in _read_task_events(pp, td):
                        ev_name = evt.get("event", "")
                        if not isinstance(ev_name, str):
                            continue
                        if ev_name == "stage_transition":
                            forced = evt.get("forced", False)
                            if not forced:
                                deterministic_ops += 1
                        # prompt_owned_ops: events implying agent execution
                        # without an executor receipt to anchor them.
                        if ("agent" in ev_name or "spawn" in ev_name):
                            if not has_executor_receipt:
                                prompt_owned_ops += 1

                    # missing_receipts + skipped_gates: per gate the task
                    # has progressed strictly past.
                    expected_gates = _expected_gates_for_stage(stage)
                    for gate in expected_gates:
                        receipt_name = f"human-approval-{gate}.json"
                        if receipt_name not in present_receipts:
                            missing_receipts.append({
                                "task_id": td,
                                "repo_slug": slug,
                                "receipt_name": receipt_name,
                            })
                            skipped_gates.append({
                                "task_id": td,
                                "repo_slug": slug,
                                "stage": gate,
                            })

                    # unverifiable_transitions: detect manifest.stage that is
                    # past at least one gate but the task has zero receipts at
                    # all (chain has no anchor).
                    if expected_gates and not present_receipts:
                        unverifiable_transitions.append({
                            "task_id": td,
                            "repo_slug": slug,
                            "reason": "no receipts present despite stage progression past gate(s)",
                        })
            except Exception:
                # AC 9: per-repo isolation. TrustSummary type has no
                # repos_failed slot; the failure is silent in the schema.
                continue

        payload = {
            "deterministic_ops": deterministic_ops,
            "prompt_owned_ops": prompt_owned_ops,
            "missing_receipts": missing_receipts,
            "skipped_gates": skipped_gates,
            "stale_skill_installs": None,  # AC 5: literal null, untracked
            "unverifiable_transitions": unverifiable_transitions,
            "computed_at": now.isoformat(),
            "data_source_caveats": [
                "stale_skill_installs is not tracked — skill version history is not stored on disk",
            ],
            "degraded": degraded,
            "repos_skipped": repos_skipped,
        }
        self._json_response(200, payload)

    def _handle_events_feed(self, query: dict) -> None:
        """AC 6: cross-repo events feed.

        Source: <repo>/.dynos/events.jsonl  AND  <repo>/.dynos/<task>/events.jsonl

        Uses a min-heap of size `limit` to maintain the top-N events by ts
        descending without accumulating all events into memory first.
        A 2-second wall-clock budget stops repo iteration early so the handler
        always returns within the budget; partial results are returned normally
        (no degraded flag for this endpoint).
        """
        # Limit parsing
        limit_raw = query.get("limit", ["100"])[0]
        try:
            limit = int(limit_raw)
        except (ValueError, TypeError):
            limit = 100
        limit = max(1, min(500, limit))

        all_projects = self._get_all_projects(query)

        # Min-heap: each entry is (ts_str, tie_breaker, event_dict).
        # Keeping only the `limit` largest ts values means we heappop the
        # *smallest* ts when the heap is full — standard top-N pattern.
        heap: list[tuple[str, int, dict]] = []
        counter = 0  # stable tie-breaker so dict comparison is never reached
        budget_start = time.monotonic()

        for pp, slug in all_projects:
            if time.monotonic() - budget_start > 2.0:
                break
            try:
                # Global events
                for evt in _read_global_events(pp):
                    if self._events_feed_match(evt):
                        aug = self._augment_event(evt, slug)
                        ts = aug.get("ts", "")
                        if not isinstance(ts, str):
                            ts = ""
                        heapq.heappush(heap, (ts, counter, aug))
                        counter += 1
                        if len(heap) > limit:
                            heapq.heappop(heap)  # drop smallest ts

                # Per-task events
                for td in list_task_dirs(pp):
                    if time.monotonic() - budget_start > 2.0:
                        break
                    for evt in _read_task_events(pp, td):
                        if self._events_feed_match(evt):
                            aug = self._augment_event(evt, slug)
                            # Prefer task_id from path when event lacks `task`
                            if "task_id" not in aug or not aug["task_id"]:
                                aug["task_id"] = td
                            ts = aug.get("ts", "")
                            if not isinstance(ts, str):
                                ts = ""
                            heapq.heappush(heap, (ts, counter, aug))
                            counter += 1
                            if len(heap) > limit:
                                heapq.heappop(heap)  # drop smallest ts
            except Exception:
                # AC 9: never propagate
                continue

        # Drain heap and sort descending by ts.
        events = [e for _ts, _i, e in sorted(heap, key=lambda x: x[0], reverse=True)]
        self._json_response(200, {"events": events})

    @staticmethod
    def _events_feed_match(evt: dict) -> bool:
        """AC 6 filter."""
        ev = evt.get("event", "")
        if not isinstance(ev, str):
            return False
        if ev == "write_policy_denied":
            return True
        if ev == "stage_transition" and evt.get("forced") is True:
            return True
        if ev.startswith("repair_"):
            return True
        if ev.startswith("postmortem_"):
            return True
        return False

    @staticmethod
    def _augment_event(evt: dict, slug: str) -> dict:
        """Add repo_slug and surface task_id (already on evt['task'] often)."""
        out = dict(evt)
        out["repo_slug"] = slug
        # Promote 'task' → 'task_id' for the typed contract.
        if "task_id" not in out:
            t = evt.get("task", "")
            if isinstance(t, str) and t:
                out["task_id"] = t
        return out

    def _handle_cross_repo_timeline(self, query: dict) -> None:
        """AC 7: non-terminal task timeline.

        Source: <repo>/.dynos/<task>/manifest.json  +  execution-log.md
        """
        # PERF-017/PERF-018: cross-repo timeline previously had no wall-clock
        # budget; on a registry with many repos and per-task execution logs
        # this could pin the dashboard process for tens of seconds.
        budget_start = time.monotonic()
        BUDGET_SECONDS = 2.0
        # PERF-005: read only the tail of execution-log.md to extract the
        # last dated line. The newest stage/advance line is always near EOF;
        # reading the entire file (often megabytes after long runs) wastes IO.
        LOG_TAIL_BYTES = 64 * 1024  # 64 KiB tail is enough for ~500 stage lines

        # PERF-007: request-local manifest cache. Keyed by absolute task_dir
        # path. Scoped to this handler invocation only.
        _manifest_cache: dict[str, dict] = {}

        all_projects = self._get_all_projects(query)
        entries: list[dict] = []

        for pp, slug in all_projects:
            # PERF-017: repo-level budget check.
            if time.monotonic() - budget_start > BUDGET_SECONDS:
                break
            try:
                for td in list_task_dirs(pp):
                    # PERF-005: per-task budget check — bail before more I/O.
                    if time.monotonic() - budget_start > BUDGET_SECONDS:
                        break

                    task_path = local_dynos_dir(pp) / td
                    cache_key = str(task_path)

                    # PERF-007: manifest cache.
                    cached_manifest = _manifest_cache.get(cache_key)
                    if cached_manifest is not None:
                        manifest = cached_manifest
                    else:
                        try:
                            manifest = read_json_file(task_path / "manifest.json")
                        except (FileNotFoundError, json.JSONDecodeError, OSError):
                            continue
                        if not isinstance(manifest, dict):
                            continue

                    # PERF-005/PERF-009: read only the LAST 64 KiB of the
                    # execution log and pass it into reconcile_stage so the
                    # function does not re-read from disk.
                    raw: str | None = None
                    try:
                        log_path = task_path / "execution-log.md"
                        with open(log_path, "rb") as _fh:
                            try:
                                _fh.seek(0, os.SEEK_END)
                                size = _fh.tell()
                                start = max(0, size - LOG_TAIL_BYTES)
                                _fh.seek(start)
                                _data = _fh.read()
                            except OSError:
                                _data = b""
                        try:
                            raw = _data.decode("utf-8", errors="replace")
                        except Exception:
                            raw = None
                    except (OSError, FileNotFoundError):
                        raw = None

                    manifest = reconcile_stage(task_path, manifest, cached_log=raw)
                    _manifest_cache[cache_key] = manifest
                    stage = manifest.get("stage", "")
                    if not isinstance(stage, str) or stage in TERMINAL_STAGES:
                        continue
                    created_at = manifest.get("created_at", "")
                    title = manifest.get("title", "")
                    if not isinstance(created_at, str):
                        created_at = ""
                    if not isinstance(title, str):
                        title = ""

                    # updated_at: most recent [STAGE]/[ADVANCE]/[DONE] line ts
                    # Source: tail of <repo>/.dynos/<task>/execution-log.md
                    updated_at = created_at
                    if raw:
                        # Walk reverse to find most recent dated line
                        for line in reversed(raw.split("\n")):
                            line = line.strip()
                            if not line:
                                continue
                            # Any line matching ISO-Z prefix
                            if len(line) >= 20 and line[4] == "-" and line[10] == "T" and line[19] == "Z":
                                ts_candidate = line[:20]
                                if _parse_iso_z(ts_candidate):
                                    updated_at = ts_candidate
                                    break

                    entries.append({
                        "task_id": td,
                        "repo_slug": slug,
                        "title": title,
                        "stage": stage,
                        "created_at": created_at,
                        "updated_at": updated_at,
                    })
            except Exception:
                continue

        entries.sort(key=lambda e: e.get("created_at", ""))
        self._json_response(200, entries)

    def _handle_palette_index(self, query: dict) -> None:
        """AC 8: command-palette index. AC 12: ignores ?project= entirely.

        Source: registry.json  +  per-task <repo>/.dynos/<task>/manifest.json
        """
        # AC 12: do not honour ?project=. Always machine-wide.
        try:
            registry = get_registry()
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            registry = {"projects": []}

        projects = registry.get("projects", []) if isinstance(registry, dict) else []
        if not isinstance(projects, list):
            projects = []

        repos_out: list[dict] = []
        all_pairs: list[tuple[str, str]] = []
        for proj in projects:
            if not isinstance(proj, dict):
                continue
            pp = proj.get("path", "")
            if not isinstance(pp, str) or not pp:
                continue
            slug = compute_slug(pp)
            name = os.path.basename(pp) if pp else ""
            repos_out.append({"slug": slug, "name": name})
            all_pairs.append((pp, slug))

        # PERF-018: previously unbounded over all repos/tasks. Add a 2-second
        # wall-clock budget so the palette endpoint cannot pin the process.
        budget_start = time.monotonic()
        BUDGET_SECONDS = 2.0
        # PERF-006: cap at 2000 tasks but exit early — do not materialize all
        # tasks into memory only to slice the head off.
        TASK_CAP = 2000

        # PERF-007: request-local manifest cache.
        _manifest_cache: dict[str, dict] = {}

        tasks_with_meta: list[tuple[str, dict]] = []  # (created_at, task entry)
        _hit_cap = False
        for pp, slug in all_pairs:
            # PERF-018: repo-level budget + cap check.
            if _hit_cap or time.monotonic() - budget_start > BUDGET_SECONDS:
                break
            try:
                for td in list_task_dirs(pp):
                    # PERF-006: early exit once we've collected TASK_CAP tasks.
                    if len(tasks_with_meta) >= TASK_CAP:
                        _hit_cap = True
                        break
                    # PERF-018: per-task budget check.
                    if time.monotonic() - budget_start > BUDGET_SECONDS:
                        break

                    task_path = local_dynos_dir(pp) / td
                    cache_key = str(task_path)

                    # PERF-007: manifest cache.
                    cached_manifest = _manifest_cache.get(cache_key)
                    if cached_manifest is not None:
                        manifest = cached_manifest
                    else:
                        try:
                            manifest = read_json_file(task_path / "manifest.json")
                        except (FileNotFoundError, json.JSONDecodeError, OSError):
                            continue
                        if not isinstance(manifest, dict):
                            continue
                        _manifest_cache[cache_key] = manifest

                    title = manifest.get("title", "")
                    stage = manifest.get("stage", "")
                    created_at = manifest.get("created_at", "")
                    if not isinstance(title, str):
                        title = ""
                    if not isinstance(stage, str):
                        stage = ""
                    if not isinstance(created_at, str):
                        created_at = ""
                    tasks_with_meta.append((created_at, {
                        "task_id": td,
                        "title": title,
                        "repo_slug": slug,
                        "stage": stage,
                    }))
            except Exception:
                continue

        # Sort by created_at desc; cap at TASK_CAP (already enforced by early
        # exit, but keep the slice as a defensive guard).
        tasks_with_meta.sort(key=lambda x: x[0], reverse=True)
        tasks_out = [t for (_c, t) in tasks_with_meta[:TASK_CAP]]

        self._json_response(200, {"repos": repos_out, "tasks": tasks_out})

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
    write_json(data_path, payload)
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
    print(f"http://127.0.0.1:{port}")
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
