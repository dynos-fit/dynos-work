#!/usr/bin/env python3
"""Automatic challenger benchmark scheduling and execution."""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent)); _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent.parent / "hooks"))

import argparse
import json
import subprocess
from pathlib import Path

from lib_core import benchmark_policy_config, now_iso, tasks_since
from lib_registry import ensure_learned_registry, entry_is_stale
from lib_benchmark import matching_fixtures_for_registry_entry
from lib_queue import ensure_automation_queue, queue_identity, replace_automation_queue


def _run_python(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(script), *args],
        text=True,
        capture_output=True,
        check=False,
    )


def discover_jobs(root: Path) -> list[dict]:
    registry = ensure_learned_registry(root)
    policy = benchmark_policy_config(root)
    jobs: list[dict] = []
    for entry in registry.get("agents", []):
        if entry.get("status") == "archived":
            continue
        route_allowed = entry.get("route_allowed", entry.get("mode") in {"alongside", "replace"})
        is_stale, stale_offset, _ = entry_is_stale(root, entry)
        shadow_mode = entry.get("mode") == "shadow"
        active_window = int(policy.get("active_rebenchmark_task_window", 3) or 3)
        shadow_window = int(policy.get("shadow_rebenchmark_task_window", 2) or 2)
        last_evaluation = entry.get("last_evaluation", {})
        benchmark_anchor = last_evaluation.get("source_tasks", [None])[-1] if last_evaluation.get("source_tasks") else None
        benchmark_offset = tasks_since(root, benchmark_anchor) if benchmark_anchor else None
        should_run = False
        reason = "shadow"
        if shadow_mode and (not last_evaluation or benchmark_offset is None or benchmark_offset >= shadow_window):
            should_run = True
            reason = "shadow"
        elif route_allowed and is_stale:
            should_run = True
            reason = "stale_active"
        elif route_allowed and benchmark_offset is not None and benchmark_offset >= active_window:
            should_run = True
            reason = "active_refresh"
        if not should_run:
            continue
        for fixture_path in matching_fixtures_for_registry_entry(root, entry):
            jobs.append(
                {
                    "agent_name": entry.get("agent_name"),
                    "role": entry.get("role"),
                    "task_type": entry.get("task_type"),
                    "item_kind": entry.get("item_kind", "agent"),
                    "fixture_path": str(fixture_path),
                    "priority": priority_for_entry(entry),
                    "reason": reason,
                    "task_offset": benchmark_offset,
                    "stale_offset": stale_offset,
                    "status": "queued",
                    "queued_at": now_iso(),
                }
            )
    return jobs


def synthesize_missing_fixtures(root: Path) -> None:
    hooks_dir = Path(__file__).resolve().parent
    registry = ensure_learned_registry(root)
    for entry in registry.get("agents", []):
        if matching_fixtures_for_registry_entry(root, entry):
            continue
        _run_python(
            hooks_dir / "fixture.py",
            "synthesize",
            str(entry.get("agent_name")),
            str(entry.get("role")),
            str(entry.get("task_type")),
            "--item-kind",
            str(entry.get("item_kind", "agent")),
            "--root",
            str(root),
        )


def priority_for_entry(entry: dict) -> int:
    sample_count = int(entry.get("benchmark_summary", {}).get("sample_count", 0) or 0)
    status = entry.get("status", "")
    priority = 100 - min(sample_count, 50)
    if status == "demoted_on_regression":
        priority += 40
    if status == "active_shadow":
        priority += 20
    return priority


def sync_queue(root: Path) -> dict:
    synthesize_missing_fixtures(root)
    queue = ensure_automation_queue(root)
    existing = {
        queue_identity(item): item
        for item in queue.get("items", [])
        if item.get("status") in {"queued", "running"}
    }
    for item in discover_jobs(root):
        existing.setdefault(queue_identity(item), item)
    items = sorted(
        existing.values(),
        key=lambda item: (-int(item.get("priority", 0)), item.get("agent_name", ""), item.get("fixture_path", "")),
    )
    return replace_automation_queue(root, items)


def _select_runner(fixture_path: Path) -> str:
    fixture = json.loads(fixture_path.read_text())
    if fixture.get("execution_harness") == "rollout":
        return "rollout.py"
    for case in fixture.get("cases", []):
        sandbox = case.get("sandbox", {})
        if sandbox.get("copy_repo_paths"):
            return "rollout.py"
    return "bench.py"


def run_queue(root: Path, *, limit: int | None = None) -> dict:
    queue = sync_queue(root)
    items = list(queue.get("items", []))
    completed: list[dict] = []
    pending: list[dict] = []
    run_count = 0
    hooks_dir = Path(__file__).resolve().parent

    for item in items:
        if item.get("status") not in {"queued", "running"}:
            pending.append(item)
            continue
        if limit is not None and run_count >= limit:
            pending.append(item)
            continue
        fixture_path = Path(item["fixture_path"]).resolve()
        runner = _select_runner(fixture_path)
        command = [str(fixture_path), "--root", str(root), "--update-registry"]
        if runner == "bench.py":
            command = ["run", *command]
        result = _run_python(hooks_dir / runner, *command)
        payload = None
        if result.stdout.strip():
            try:
                payload = json.loads(result.stdout)
            except json.JSONDecodeError:
                payload = None
        item_result = {
            **item,
            "status": "completed" if result.returncode == 0 else "failed",
            "completed_at": now_iso(),
            "runner": runner,
            "returncode": result.returncode,
        }
        if payload is not None:
            item_result["result"] = payload.get("evaluation", payload)
        if result.stderr.strip():
            item_result["stderr"] = result.stderr.strip()
        completed.append(item_result)
        run_count += 1

    replace_automation_queue(root, pending)
    result = {
        "queued_before": len(items),
        "executed": len(completed),
        "pending_after": len(pending),
        "completed": completed,
    }
    status_path = root / ".dynos" / "automation" / "status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps({"updated_at": now_iso(), **result}, indent=2) + "\n")
    return result


def cmd_sync(args: argparse.Namespace) -> int:
    queue = sync_queue(Path(args.root).resolve())
    print(json.dumps(queue, indent=2))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    result = run_queue(Path(args.root).resolve(), limit=args.limit)
    print(json.dumps(result, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync_parser = subparsers.add_parser("sync", help="Discover shadow challengers and enqueue matching fixtures")
    sync_parser.add_argument("--root", default=".")
    sync_parser.set_defaults(func=cmd_sync)

    run_parser = subparsers.add_parser("run", help="Sync and execute queued challenger benchmarks")
    run_parser.add_argument("--root", default=".")
    run_parser.add_argument("--limit", type=int)
    run_parser.set_defaults(func=cmd_run)
    return parser


if __name__ == "__main__":
    from cli_base import cli_main
    raise SystemExit(cli_main(build_parser))
