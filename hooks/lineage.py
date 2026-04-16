#!/usr/bin/env python3
"""Build lineage graph for learned components, fixtures, runs, and source tasks."""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import argparse
import json
from pathlib import Path

from lib_registry import ensure_learned_registry
from lib_benchmark import ensure_benchmark_history, ensure_benchmark_index


def build_lineage(root: Path) -> dict:
    registry = ensure_learned_registry(root)
    index = ensure_benchmark_index(root)
    history = ensure_benchmark_history(root)
    nodes: list[dict] = []
    edges: list[dict] = []

    for item in registry.get("agents", []):
        comp_id = f"component:{item.get('item_kind', 'agent')}:{item.get('agent_name')}"
        nodes.append(
            {
                "id": comp_id,
                "kind": "component",
                "item_kind": item.get("item_kind", "agent"),
                "name": item.get("agent_name"),
                "role": item.get("role"),
                "task_type": item.get("task_type"),
                "mode": item.get("mode"),
                "status": item.get("status"),
            }
        )
        if item.get("generated_from"):
            task_id = str(item["generated_from"])
            nodes.append({"id": f"task:{task_id}", "kind": "task", "task_id": task_id})
            edges.append({"from": f"task:{task_id}", "to": comp_id, "kind": "generated"})

    for fixture in index.get("fixtures", []):
        fixture_id = str(fixture.get("fixture_id"))
        fixture_node = f"fixture:{fixture_id}"
        nodes.append({"id": fixture_node, "kind": "fixture", "fixture_id": fixture_id, "path": fixture.get("fixture_path")})
        comp_id = f"component:{fixture.get('item_kind', 'agent')}:{fixture.get('target_name')}"
        edges.append({"from": comp_id, "to": fixture_node, "kind": "benchmarked_by"})
        for task_id in fixture.get("source_tasks", []):
            nodes.append({"id": f"task:{task_id}", "kind": "task", "task_id": task_id})
            edges.append({"from": f"task:{task_id}", "to": fixture_node, "kind": "source_task"})

    for run in history.get("runs", []):
        run_id = str(run.get("run_id"))
        nodes.append({"id": f"run:{run_id}", "kind": "run", "run_id": run_id, "fixture_id": run.get("fixture_id")})
        if run.get("fixture_id"):
            edges.append({"from": f"fixture:{run.get('fixture_id')}", "to": f"run:{run_id}", "kind": "executed"})
        if run.get("target_name"):
            edges.append(
                {
                    "from": f"component:{run.get('item_kind', 'agent')}:{run.get('target_name')}",
                    "to": f"run:{run_id}",
                    "kind": "evaluated",
                }
            )

    dedup_nodes = {node["id"]: node for node in nodes}
    dedup_edges = {(edge["from"], edge["to"], edge["kind"]): edge for edge in edges}
    return {
        "version": 1,
        "nodes": list(dedup_nodes.values()),
        "edges": list(dedup_edges.values()),
    }


def cmd_lineage(args: argparse.Namespace) -> int:
    print(json.dumps(build_lineage(Path(args.root).resolve()), indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.set_defaults(func=cmd_lineage)
    return parser


if __name__ == "__main__":
    from cli_base import cli_main
    raise SystemExit(cli_main(build_parser))
