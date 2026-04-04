#!/usr/bin/env python3
"""Repo-snapshot rollout harness for candidate vs baseline task fixtures."""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import argparse
import json
from pathlib import Path

from dynobench import load_fixture, run_fixture
from dynoslib import append_benchmark_run, apply_evaluation_to_registry, now_iso, upsert_fixture_trace


def validate_rollout_fixture(fixture: dict) -> None:
    if not isinstance(fixture.get("cases"), list) or not fixture["cases"]:
        raise SystemExit("rollout fixture must contain a non-empty cases array")
    for case in fixture["cases"]:
        sandbox = case.get("sandbox", {})
        if not isinstance(sandbox.get("copy_repo_paths", []), list):
            raise SystemExit("rollout case sandbox.copy_repo_paths must be an array")
        for variant_name in ("baseline", "candidate"):
            variant = case.get(variant_name, {})
            if not isinstance(variant, dict):
                raise SystemExit(f"rollout case {variant_name} must be an object")
            if not variant.get("command") and not variant.get("commands"):
                raise SystemExit(f"rollout case {variant_name} must declare command(s)")


def cmd_run(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    fixture_path = Path(args.fixture_json).resolve()
    fixture = load_fixture(fixture_path)
    fixture["_repo_root"] = str(root)
    validate_rollout_fixture(fixture)
    result = run_fixture(fixture)
    run_record = {
        "run_id": f"{result['fixture_id']}:{now_iso()}",
        "executed_at": now_iso(),
        "fixture_path": str(fixture_path),
        "execution_harness": "dynorollout",
        **result,
    }
    append_benchmark_run(root, run_record)
    upsert_fixture_trace(
        root,
        {
            "fixture_id": result["fixture_id"],
            "fixture_path": str(fixture_path),
            "item_kind": result["item_kind"],
            "target_name": result["target_name"],
            "role": result["role"],
            "task_type": result["task_type"],
            "source_tasks": fixture.get("source_tasks", []),
            "baseline_tasks": fixture.get("baseline_tasks", []),
            "last_run_id": run_record["run_id"],
            "last_run_at": run_record["executed_at"],
        },
    )
    if args.update_registry:
        apply_evaluation_to_registry(
            root,
            fixture["target_name"],
            fixture["role"],
            fixture["task_type"],
            result["evaluation"],
            item_kind=fixture.get("item_kind", "agent"),
            context={
                "fixture_id": result["fixture_id"],
                "fixture_path": str(fixture_path),
                "run_id": run_record["run_id"],
                "source_tasks": fixture.get("source_tasks", []),
            },
        )
    print(json.dumps(run_record, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture_json")
    parser.add_argument("--root", default=".")
    parser.add_argument("--update-registry", action="store_true")
    parser.set_defaults(func=cmd_run)
    return parser


if __name__ == "__main__":
    from dyno_cli_base import cli_main
    raise SystemExit(cli_main(build_parser))
