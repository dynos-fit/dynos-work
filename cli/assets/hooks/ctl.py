#!/usr/bin/env python3
"""Deterministic control plane for dynos-work."""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import argparse
import sys
from pathlib import Path

from lib_core import find_active_tasks, load_json, next_command_for_stage, transition_task
from lib_validate import check_segment_ownership, validate_task_artifacts


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
    from lib_qlearn import build_repair_plan
    findings = _json.load(_sys.stdin)
    if isinstance(findings, dict):
        findings = findings.get("findings", [])
    result = build_repair_plan(Path(args.root).resolve(), findings, args.task_type)
    print(_json.dumps(result, indent=2))
    return 0


def cmd_repair_update(args: argparse.Namespace) -> int:
    import json as _json
    import sys as _sys
    from lib_qlearn import update_from_outcomes
    data = _json.load(_sys.stdin)
    outcomes = data.get("outcomes", []) if isinstance(data, dict) else data
    result = update_from_outcomes(Path(args.root).resolve(), outcomes, args.task_type)
    print(_json.dumps(result, indent=2))
    return 0


def cmd_compute_reward(args: argparse.Namespace) -> int:
    import json
    from lib_validate import compute_reward
    task_dir = Path(args.task_dir).resolve()
    result = compute_reward(task_dir)
    if args.write:
        from lib_core import write_json
        write_json(task_dir / "task-retrospective.json", result)
        print(f"Written to {task_dir / 'task-retrospective.json'}")
        # Write retrospective receipt
        try:
            from lib_receipts import receipt_retrospective
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
    from lib_contracts import validate_inputs, validate_outputs
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
    from lib_receipts import validate_chain as validate_receipt_chain
    task_dir = Path(args.task_dir).resolve()
    gaps = validate_receipt_chain(task_dir)
    result = {"valid": len(gaps) == 0, "gaps": gaps, "task_dir": str(task_dir)}
    print(json.dumps(result, indent=2))
    return 1 if gaps else 0


def cmd_validate_chain(args: argparse.Namespace) -> int:
    import json
    from lib_contracts import validate_chain
    errors = validate_chain()
    result = {"valid": len(errors) == 0, "errors": errors}
    print(json.dumps(result, indent=2))
    return 1 if errors else 0


def cmd_list_pending(args: argparse.Namespace) -> int:
    from postmortem import cmd_list_pending as _list_pending
    return _list_pending(args)


def cmd_approve(args: argparse.Namespace) -> int:
    from postmortem import cmd_approve as _approve
    return _approve(args)



def cmd_stats_usage(args: argparse.Namespace) -> int:
    """Show module usage telemetry for dormancy detection."""
    import json as _json
    from lib_usage_telemetry import read_telemetry, summarize_telemetry

    monitored = ["dream", "postmortem_improve", "lib_qlearn", "cli_base"]

    if args.json:
        counts = summarize_telemetry()
        result = {mod: counts.get(mod, 0) for mod in monitored}
        result["_other"] = {k: v for k, v in counts.items() if k not in monitored}
        print(_json.dumps(result, indent=2))
    else:
        counts = summarize_telemetry()
        print("Module Usage Telemetry (dormancy detection)")
        print(f"{'Module':<25} {'Invocations':>12}  Status")
        print("-" * 55)
        for mod in monitored:
            count = counts.get(mod, 0)
            status = "ACTIVE" if count > 0 else "DORMANT"
            print(f"{mod:<25} {count:>12}  {status}")
        other = {k: v for k, v in counts.items() if k not in monitored}
        if other:
            print(f"\nOther modules recorded: {len(other)}")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    """Get or set project policy values."""
    import json as _json
    from lib_core import _persistent_project_dir, ensure_persistent_project_dir, load_json, write_json

    root = Path(args.root).resolve()

    if args.action == "get":
        policy_path = _persistent_project_dir(root) / "policy.json"
        try:
            data = load_json(policy_path)
        except (FileNotFoundError, _json.JSONDecodeError):
            data = {}
        if args.key:
            val = data.get(args.key)
            if val is None:
                print(f"{args.key}: <not set>")
            else:
                print(f"{args.key}: {_json.dumps(val)}")
        else:
            print(_json.dumps(data, indent=2))
        return 0

    elif args.action == "set":
        if not args.key or args.value is None:
            print("Usage: config set <key> <value>", file=sys.stderr)
            return 1
        policy_dir = ensure_persistent_project_dir(root)
        policy_path = policy_dir / "policy.json"
        try:
            data = load_json(policy_path)
        except (FileNotFoundError, _json.JSONDecodeError):
            data = {}
        # Parse value: try JSON first (for booleans, numbers), fall back to string
        try:
            parsed = _json.loads(args.value)
        except _json.JSONDecodeError:
            parsed = args.value
        data[args.key] = parsed
        write_json(policy_path, data)
        print(f"{args.key}: {_json.dumps(parsed)}")
        return 0

    return 1


def cmd_stats_dora(args: argparse.Namespace) -> int:
    """Compute DORA metrics from all retrospectives."""
    import json as _json
    from lib_core import collect_retrospectives

    root = Path(args.root).resolve()
    retros = collect_retrospectives(root)

    if not retros:
        print("No retrospectives found.")
        return 0

    # Deployment frequency: tasks completed per day
    lead_times: list[int] = []
    failures = 0
    recovery_times: list[int] = []
    total = len(retros)

    for r in retros:
        lt = r.get("lead_time_seconds")
        if lt is not None and isinstance(lt, (int, float)) and lt >= 0:
            lead_times.append(int(lt))
        if r.get("change_failure") is True:
            failures += 1
            rt = r.get("recovery_time_seconds")
            if rt is not None and isinstance(rt, (int, float)) and rt >= 0:
                recovery_times.append(int(rt))

    # Compute DORA-aligned metrics
    avg_lead_time = sum(lead_times) / len(lead_times) if lead_times else None
    change_failure_rate = failures / total if total > 0 else 0.0
    avg_recovery = sum(recovery_times) / len(recovery_times) if recovery_times else None

    result = {
        "total_tasks": total,
        "tasks_with_lead_time": len(lead_times),
        "avg_lead_time_seconds": round(avg_lead_time, 1) if avg_lead_time is not None else None,
        "avg_lead_time_minutes": round(avg_lead_time / 60, 1) if avg_lead_time is not None else None,
        "change_failure_rate": round(change_failure_rate, 4),
        "change_failures": failures,
        "avg_recovery_time_seconds": round(avg_recovery, 1) if avg_recovery is not None else None,
        "avg_recovery_time_minutes": round(avg_recovery / 60, 1) if avg_recovery is not None else None,
    }

    if args.json:
        print(_json.dumps(result, indent=2))
    else:
        print(f"DORA Metrics ({total} tasks)")
        print(f"  Lead time (avg):         {result['avg_lead_time_minutes']}m" if result["avg_lead_time_minutes"] else "  Lead time:               n/a")
        print(f"  Change failure rate:     {result['change_failure_rate']:.1%}")
        print(f"  Recovery time (avg):     {result['avg_recovery_time_minutes']}m" if result["avg_recovery_time_minutes"] else "  Recovery time:           n/a")
        print(f"  Tasks with lead time:    {result['tasks_with_lead_time']}/{total}")
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

    dora_parser = subparsers.add_parser("stats-dora", help="Compute DORA metrics from retrospectives")
    dora_parser.add_argument("--root", default=".", help="Project root")
    dora_parser.add_argument("--json", action="store_true", help="Output as JSON")
    dora_parser.set_defaults(func=cmd_stats_dora)

    usage_parser = subparsers.add_parser("stats-usage", help="Module usage telemetry for dormancy detection")
    usage_parser.add_argument("--json", action="store_true", help="Output as JSON")
    usage_parser.set_defaults(func=cmd_stats_usage)

    config_parser = subparsers.add_parser("config", help="Get or set project policy values")
    config_parser.add_argument("action", choices=["get", "set"], help="Action: get or set")
    config_parser.add_argument("key", nargs="?", default=None, help="Policy key (e.g. learning_enabled)")
    config_parser.add_argument("value", nargs="?", default=None, help="Value to set (JSON: true, false, 123, \"string\")")
    config_parser.add_argument("--root", default=".", help="Project root")
    config_parser.set_defaults(func=cmd_config)

    return parser


if __name__ == "__main__":
    from cli_base import cli_main
    raise SystemExit(cli_main(build_parser))
