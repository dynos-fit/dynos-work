#!/usr/bin/env python3
"""Trajectory store management for dynos-work."""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent)); _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent.parent / "hooks"))

import argparse
import json
from pathlib import Path

from lib_trajectory import rebuild_trajectory_store, search_trajectories


def cmd_rebuild(args: argparse.Namespace) -> int:
    store = rebuild_trajectory_store(Path(args.root).resolve())
    print(
        json.dumps(
            {
                "version": store["version"],
                "updated_at": store["updated_at"],
                "trajectory_count": len(store.get("trajectories", [])),
            },
            indent=2,
        )
    )
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    query = json.loads(Path(args.query_json).read_text())
    results = search_trajectories(Path(args.root).resolve(), query, limit=args.limit)
    print(json.dumps(results, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    rebuild_parser = subparsers.add_parser("rebuild", help="Rebuild the trajectory store from retrospectives")
    rebuild_parser.add_argument("--root", default=".")
    rebuild_parser.set_defaults(func=cmd_rebuild)

    search_parser = subparsers.add_parser("search", help="Search trajectories from a query JSON file")
    search_parser.add_argument("query_json")
    search_parser.add_argument("--root", default=".")
    search_parser.add_argument("--limit", type=int, default=3)
    search_parser.set_defaults(func=cmd_search)

    return parser


if __name__ == "__main__":
    from cli_base import cli_main
    raise SystemExit(cli_main(build_parser))
