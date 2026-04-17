#!/usr/bin/env python3
"""One-time backfill script to add model field to existing benchmark history runs.

Idempotent: skips runs that already have a non-null model field.
Resolves model from source task token-usage.json by_agent entries.
"""

from __future__ import annotations

import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent)); _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent.parent / "hooks"))

import argparse
import json
from pathlib import Path

from lib_benchmark import resolve_model_for_benchmark_run
from lib_core import benchmark_history_path, load_json, now_iso, write_json


def backfill_models(root: Path) -> dict:
    """Backfill model field on benchmark history runs.

    For each run without a model field (or with model=None), resolves
    the model from the run's cases[].source_task_id by reading each
    source task's token-usage.json by_agent entries.

    Returns {"backfilled": int, "skipped": int, "failed": int, "total": int}.
    """
    history_path = benchmark_history_path(root)
    if not history_path.exists():
        return {"backfilled": 0, "skipped": 0, "failed": 0, "total": 0}

    try:
        history = load_json(history_path)
    except (json.JSONDecodeError, OSError):
        return {"backfilled": 0, "skipped": 0, "failed": 0, "total": 0}

    runs = history.get("runs", [])
    backfilled = 0
    skipped = 0
    failed = 0

    for run in runs:
        # Skip runs that already have a model
        if run.get("model"):
            skipped += 1
            continue

        role = run.get("role", "")
        cases = run.get("cases", [])
        if not role or not cases:
            failed += 1
            continue

        try:
            model = resolve_model_for_benchmark_run(root, run, role)
        except Exception:
            failed += 1
            continue

        if model:
            run["model"] = model
            backfilled += 1
        else:
            failed += 1

    # Write back
    if backfilled > 0:
        history["updated_at"] = now_iso()
        write_json(history_path, history)

    return {
        "backfilled": backfilled,
        "skipped": skipped,
        "failed": failed,
        "total": len(runs),
    }


def cmd_backfill(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    counts = backfill_models(root)
    print(json.dumps(counts, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="Project root directory")
    args = parser.parse_args()
    return cmd_backfill(args)


if __name__ == "__main__":
    raise SystemExit(main())
