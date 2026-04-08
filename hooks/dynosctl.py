#!/usr/bin/env python3
"""Deterministic control plane for dynos-work."""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import argparse
import sys
from pathlib import Path

from dynoslib_core import find_active_tasks, load_json, next_command_for_stage, transition_task
from dynoslib_validate import check_segment_ownership, validate_task_artifacts


def cmd_validate_task(args: argparse.Namespace) -> int:
    errors = validate_task_artifacts(Path(args.task_dir).resolve(), strict=args.strict)
    if errors:
        print("Validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("Validation passed.")
    return 0


def cmd_transition(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir).resolve()
    try:
        previous, manifest = transition_task(task_dir, args.next_stage, force=args.force)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"{manifest['task_id']}: {previous} -> {manifest['stage']}")
    return 0


def cmd_next_command(args: argparse.Namespace) -> int:
    manifest = load_json(Path(args.task_dir).resolve() / "manifest.json")
    stage = manifest.get("stage")
    print(next_command_for_stage(stage))
    return 0


def cmd_active_task(args: argparse.Namespace) -> int:
    tasks = find_active_tasks(Path(args.root).resolve())
    if not tasks:
        print("No active task.")
        return 1
    for task in tasks:
        manifest = load_json(task / "manifest.json")
        print(f"{task} {manifest.get('stage')}")
    return 0


def cmd_check_ownership(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir).resolve()
    try:
        unauthorized = check_segment_ownership(task_dir, args.segment_id, args.files)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if unauthorized:
        print("Unauthorized file edits:")
        for file_path in unauthorized:
            print(f"- {file_path}")
        return 1
    print("Ownership check passed.")
    return 0


def cmd_repair_plan(args: argparse.Namespace) -> int:
    import json as _json
    import sys as _sys
    from dynoslib_qlearn import build_repair_plan
    findings = _json.load(_sys.stdin)
    if isinstance(findings, dict):
        findings = findings.get("findings", [])
    result = build_repair_plan(Path(args.root).resolve(), findings, args.task_type)
    print(_json.dumps(result, indent=2))
    return 0


def cmd_repair_update(args: argparse.Namespace) -> int:
    import json as _json
    import sys as _sys
    from dynoslib_qlearn import update_from_outcomes
    data = _json.load(_sys.stdin)
    outcomes = data.get("outcomes", []) if isinstance(data, dict) else data
    result = update_from_outcomes(Path(args.root).resolve(), outcomes, args.task_type)
    print(_json.dumps(result, indent=2))
    return 0


def cmd_check_eager_repair(args: argparse.Namespace) -> int:
    import json as _json
    import sys as _sys
    from dynoslib_validate import check_eager_repair
    report = _json.load(_sys.stdin)
    result = check_eager_repair(report)
    print(_json.dumps(result, indent=2))
    return 0 if result["verdict"] == "CONTINUE_WAITING" else 2


def cmd_compute_reward(args: argparse.Namespace) -> int:
    import json
    from dynoslib_validate import compute_reward
    task_dir = Path(args.task_dir).resolve()
    result = compute_reward(task_dir)
    if args.write:
        from dynoslib_core import write_json
        write_json(task_dir / "task-retrospective.json", result)
        print(f"Written to {task_dir / 'task-retrospective.json'}")
        # Write retrospective receipt
        try:
            from dynoslib_receipts import receipt_retrospective
            receipt_retrospective(
                task_dir,
                quality_score=result.get("quality_score", 0),
                cost_score=result.get("cost_score", 0),
                efficiency_score=result.get("efficiency_score", 0),
                total_tokens=result.get("total_token_usage", 0),
            )
        except Exception as exc:
            print(f"[warn] retrospective receipt failed: {exc}", file=sys.stderr)
    else:
        print(json.dumps(result, indent=2))
    return 0


def cmd_validate_contract(args: argparse.Namespace) -> int:
    import json
    from dynoslib_contracts import validate_inputs, validate_outputs
    task_dir = Path(args.task_dir).resolve()
    project_root = Path(args.root).resolve() if args.root else task_dir.parent.parent

    if args.direction == "input":
        errors = validate_inputs(args.skill, task_dir, project_root, strict=args.strict)
    elif args.direction == "output":
        errors = validate_outputs(args.skill, task_dir)
    else:
        errors = validate_inputs(args.skill, task_dir, project_root, strict=args.strict)
        errors.extend(validate_outputs(args.skill, task_dir))

    result = {"skill": args.skill, "valid": len(errors) == 0, "errors": errors}
    print(json.dumps(result, indent=2))
    return 1 if errors else 0


def cmd_validate_receipts(args: argparse.Namespace) -> int:
    import json
    from dynoslib_receipts import validate_chain as validate_receipt_chain
    task_dir = Path(args.task_dir).resolve()
    gaps = validate_receipt_chain(task_dir)
    result = {"valid": len(gaps) == 0, "gaps": gaps, "task_dir": str(task_dir)}
    print(json.dumps(result, indent=2))
    return 1 if gaps else 0


def cmd_validate_chain(args: argparse.Namespace) -> int:
    import json
    from dynoslib_contracts import validate_chain
    errors = validate_chain()
    result = {"valid": len(errors) == 0, "errors": errors}
    print(json.dumps(result, indent=2))
    return 1 if errors else 0


def cmd_list_pending(args: argparse.Namespace) -> int:
    from dynopostmortem import cmd_list_pending as _list_pending
    return _list_pending(args)


def cmd_approve(args: argparse.Namespace) -> int:
    from dynopostmortem import cmd_approve as _approve
    return _approve(args)


def cmd_crawl_graph(args: argparse.Namespace) -> int:
    import json as _json
    from dynoslib_crawler import build_import_graph
    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"Error: {root} is not a directory", file=sys.stderr)
        return 1
    result = build_import_graph(root)
    print(_json.dumps(result, indent=2, default=str))
    return 0


def cmd_crawl_targets(args: argparse.Namespace) -> int:
    from dynoslib_crawler import compute_scan_targets
    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"Error: {root} is not a directory", file=sys.stderr)
        return 1
    max_files = int(args.max)
    if max_files < 1:
        print("Error: --max must be at least 1", file=sys.stderr)
        return 1
    targets = compute_scan_targets(root, max_files=max_files)
    for filepath, score in targets:
        print(f"{score:.2f}  {filepath}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate-task", help="Validate a task directory")
    validate_parser.add_argument("task_dir")
    validate_parser.add_argument("--strict", action="store_true")
    validate_parser.set_defaults(func=cmd_validate_task)

    transition_parser = subparsers.add_parser("transition", help="Advance task stage with guardrails")
    transition_parser.add_argument("task_dir")
    transition_parser.add_argument("next_stage")
    transition_parser.add_argument("--force", action="store_true")
    transition_parser.set_defaults(func=cmd_transition)

    next_parser = subparsers.add_parser("next-command", help="Resolve next command for current stage")
    next_parser.add_argument("task_dir")
    next_parser.set_defaults(func=cmd_next_command)

    active_parser = subparsers.add_parser("active-task", help="List active tasks under .dynos")
    active_parser.add_argument("--root", default=".")
    active_parser.set_defaults(func=cmd_active_task)

    ownership_parser = subparsers.add_parser("check-ownership", help="Check that files belong to a segment")
    ownership_parser.add_argument("task_dir")
    ownership_parser.add_argument("segment_id")
    ownership_parser.add_argument("files", nargs="+")
    ownership_parser.set_defaults(func=cmd_check_ownership)

    rp_parser = subparsers.add_parser("repair-plan", help="Q-learning repair plan (reads findings from stdin)")
    rp_parser.add_argument("--root", default=".")
    rp_parser.add_argument("--task-type", dest="task_type", required=True)
    rp_parser.set_defaults(func=cmd_repair_plan)

    ru_parser = subparsers.add_parser("repair-update", help="Update Q-tables from repair outcomes (reads from stdin)")
    ru_parser.add_argument("--root", default=".")
    ru_parser.add_argument("--task-type", dest="task_type", required=True)
    ru_parser.set_defaults(func=cmd_repair_update)

    eager_parser = subparsers.add_parser("check-eager-repair", help="Check audit report for blocking findings (reads from stdin, exit 2 = REPAIR_NOW)")
    eager_parser.set_defaults(func=cmd_check_eager_repair)

    reward_parser = subparsers.add_parser("compute-reward", help="Deterministically compute reward scores from task artifacts")
    reward_parser.add_argument("task_dir")
    reward_parser.add_argument("--write", action="store_true", help="Write task-retrospective.json")
    reward_parser.set_defaults(func=cmd_compute_reward)

    contract_parser = subparsers.add_parser("validate-contract", help="Validate skill contract inputs/outputs")
    contract_parser.add_argument("--skill", required=True, help="Skill name (e.g. execute, audit)")
    contract_parser.add_argument("--task-dir", dest="task_dir", required=True, help="Task directory")
    contract_parser.add_argument("--root", default=None, help="Project root (default: inferred from task dir)")
    contract_parser.add_argument("--direction", choices=["input", "output", "both"], default="input")
    contract_parser.add_argument("--strict", action="store_true")
    contract_parser.set_defaults(func=cmd_validate_contract)

    receipt_parser = subparsers.add_parser("validate-receipts", help="Validate receipt chain for a task")
    receipt_parser.add_argument("task_dir")
    receipt_parser.set_defaults(func=cmd_validate_receipts)

    chain_parser = subparsers.add_parser("validate-chain", help="Validate contract chain across the pipeline")
    chain_parser.set_defaults(func=cmd_validate_chain)

    pending_parser = subparsers.add_parser("list-pending", help="List unapplied improvement proposals")
    pending_parser.add_argument("--root", default=".")
    pending_parser.set_defaults(func=cmd_list_pending)

    approve_parser = subparsers.add_parser("approve", help="Approve and apply an improvement by ID")
    approve_parser.add_argument("improvement_id", help="Proposal ID (e.g. imp-prevent-cq)")
    approve_parser.add_argument("--root", default=".")
    approve_parser.set_defaults(func=cmd_approve)

    crawl_parser = subparsers.add_parser("crawl", help="Crawl and analyze project import structure")
    crawl_subs = crawl_parser.add_subparsers(dest="crawl_command", required=True)

    crawl_graph_parser = crawl_subs.add_parser("graph", help="Print import graph as JSON")
    crawl_graph_parser.add_argument("--root", required=True, help="Project root directory")
    crawl_graph_parser.set_defaults(func=cmd_crawl_graph)

    crawl_targets_parser = crawl_subs.add_parser("targets", help="Print top N scan targets with scores")
    crawl_targets_parser.add_argument("--root", required=True, help="Project root directory")
    crawl_targets_parser.add_argument("--max", default="10", help="Maximum number of targets (default: 10)")
    crawl_targets_parser.set_defaults(func=cmd_crawl_targets)

    return parser


if __name__ == "__main__":
    from dyno_cli_base import cli_main
    raise SystemExit(cli_main(build_parser))
