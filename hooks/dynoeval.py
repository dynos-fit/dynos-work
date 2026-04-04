#!/usr/bin/env python3
"""Offline benchmark evaluator for learned-agent promotion."""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import argparse
import json
from pathlib import Path

from dynoslib import apply_evaluation_to_registry, evaluate_candidate


def cmd_evaluate(args: argparse.Namespace) -> int:
    candidate = json.loads(Path(args.candidate_json).read_text())
    baseline = json.loads(Path(args.baseline_json).read_text())
    result = evaluate_candidate(candidate, baseline)
    print(json.dumps(result, indent=2))
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    candidate = json.loads(Path(args.candidate_json).read_text())
    baseline = json.loads(Path(args.baseline_json).read_text())
    evaluation = evaluate_candidate(candidate, baseline)
    registry = apply_evaluation_to_registry(
        Path(args.root).resolve(),
        args.agent_name,
        args.role,
        args.task_type,
        evaluation,
        item_kind=args.item_kind,
    )
    output = {
        "agent_name": args.agent_name,
        "role": args.role,
        "task_type": args.task_type,
        "item_kind": args.item_kind,
        "recommendation": evaluation["recommendation"],
        "target_mode": evaluation["target_mode"],
        "registry_updated_at": registry["updated_at"],
    }
    print(json.dumps(output, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    evaluate_parser = subparsers.add_parser("evaluate", help="Compare candidate and baseline benchmark results")
    evaluate_parser.add_argument("candidate_json")
    evaluate_parser.add_argument("baseline_json")
    evaluate_parser.set_defaults(func=cmd_evaluate)

    promote_parser = subparsers.add_parser("promote", help="Apply offline evaluation result to the learned-agent registry")
    promote_parser.add_argument("agent_name")
    promote_parser.add_argument("role")
    promote_parser.add_argument("task_type")
    promote_parser.add_argument("candidate_json")
    promote_parser.add_argument("baseline_json")
    promote_parser.add_argument("--item-kind", choices=["agent", "skill"], default="agent")
    promote_parser.add_argument("--root", default=".")
    promote_parser.set_defaults(func=cmd_promote)

    return parser


if __name__ == "__main__":
    from dyno_cli_base import cli_main
    raise SystemExit(cli_main(build_parser))
