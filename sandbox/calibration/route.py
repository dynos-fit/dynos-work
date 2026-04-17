#!/usr/bin/env python3
"""Resolve live learned-agent or learned-skill routing from the registry."""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent)); _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent.parent / "hooks"))

import argparse
import json
from pathlib import Path

from lib_registry import resolve_registry_route


def cmd_resolve(args: argparse.Namespace) -> int:
    result = resolve_registry_route(
        Path(args.root).resolve(),
        args.role,
        args.task_type,
        item_kind=args.item_kind,
    )
    print(json.dumps(result, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role")
    parser.add_argument("task_type")
    parser.add_argument("--item-kind", choices=["agent", "skill"], default="agent")
    parser.add_argument("--root", default=".")
    parser.set_defaults(func=cmd_resolve)
    return parser


if __name__ == "__main__":
    from cli_base import cli_main
    raise SystemExit(cli_main(build_parser))
