#!/usr/bin/env python3
"""Deterministic control plane for dynos-work."""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import argparse
import sys
from pathlib import Path

from lib_core import find_active_tasks, load_json, next_command_for_stage, transition_task
from lib_receipts import hash_file, receipt_human_approval
from lib_validate import check_segment_ownership, validate_task_artifacts


_APPROVE_STAGE_MAP: dict[str, tuple[str, str]] = {
    # review_stage -> (relative artifact path, next stage)
    "SPEC_REVIEW": ("spec.md", "PLANNING"),
    "PLAN_REVIEW": ("plan.md", "PLAN_AUDIT"),
    "TDD_REVIEW": ("evidence/tdd-tests.md", "PRE_EXECUTION_SNAPSHOT"),
}


def _rules_corrupt_sentinel(root: Path) -> Path:
    """Return sentinel path co-located with daemon.py's writer.

    Duplicates the path shape from ``daemon.rules_corrupt_sentinel_path``;
    deliberately inlined here so ctl.py need not import daemon.py (and
    pull in its subprocess/signal machinery) just to check one file.
    """
    return root / ".dynos" / ".rules_corrupt"


def _refuse_if_rules_corrupt(root: Path) -> int | None:
    """Block task-creation commands when prevention-rules.json is corrupt.

    Returns an exit code (1) when the sentinel exists so the caller can
    propagate it directly; returns None when there is no sentinel and
    the command may proceed. Error goes to stderr and names the
    persistent rules path so the operator knows which file to fix.

    AC 18 scope: only task-creation entry-points call this. Existing-task
    operations (transition, approve-stage, validate-receipts, etc.) MUST
    NOT be blocked — the sentinel is a *bootstrap* gate, not a runtime
    kill switch.
    """
    sentinel = _rules_corrupt_sentinel(root)
    if not sentinel.exists():
        return None
    try:
        from lib_core import _persistent_project_dir
        persistent = _persistent_project_dir(root) / "prevention-rules.json"
    except Exception:
        persistent = Path("~/.dynos/projects/{slug}/prevention-rules.json")
    print(
        f"ERROR: prevention-rules.json is corrupt; "
        f"fix {persistent} and retry "
        f"(sentinel: {sentinel})",
        file=sys.stderr,
    )
    return 1


def _root_for_task_dir(task_dir: Path) -> Path:
    """Resolve the project root that contains ``.dynos/task-<id>/``.

    ``task_dir`` is expected to be ``<root>/.dynos/task-<id>``; the
    grandparent is the project root. Falls back to the task_dir itself
    if the structure is unexpected (defensive — the sentinel check will
    then look in the wrong place, which is safer than crashing on a
    path with <2 ancestors).
    """
    try:
        return task_dir.parent.parent
    except Exception:
        return task_dir


def cmd_validate_task(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir).resolve()
    # AC 18: validate-task is a task-creation entry-point — refuse when
    # the corrupt-rules sentinel is present. Other ctl.py commands that
    # operate on existing tasks do NOT call this gate.
    root = _root_for_task_dir(task_dir)
    blocked = _refuse_if_rules_corrupt(root)
    if blocked is not None:
        return blocked
    errors = validate_task_artifacts(task_dir, strict=args.strict)
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


def cmd_approve_stage(args: argparse.Namespace) -> int:
    """Record a human approval receipt for a review stage.

    stage must be one of SPEC_REVIEW / PLAN_REVIEW / TDD_REVIEW. Exits 1 on any
    failure that prevents the receipt write (unknown stage, missing artifact,
    receipt-write refusal); exits 0 after the receipt is durably on disk.
    The scheduler (hooks/scheduler.py) observes the receipt write via the
    write_receipt chokepoint and drives any resulting stage advance
    asynchronously in-process. Exit 0 therefore signals "receipt written";
    it does NOT signal "stage advanced" — callers that need the latter must
    re-read manifest.json after the call returns. stderr carries the
    ValueError text; stdout is reserved for a success line.
    """
    stage = args.stage
    mapping = _APPROVE_STAGE_MAP.get(stage)
    if mapping is None:
        allowed = ", ".join(sorted(_APPROVE_STAGE_MAP))
        print(
            f"unknown stage: {stage!r} (expected one of: {allowed})",
            file=sys.stderr,
        )
        return 1
    artifact_rel, _ = mapping

    task_dir = Path(args.task_dir).resolve()
    artifact_path = task_dir / artifact_rel
    if not artifact_path.is_file():
        print(
            f"missing artifact for {stage}: {artifact_path}",
            file=sys.stderr,
        )
        return 1

    try:
        sha256_hex = hash_file(artifact_path)
    except OSError as exc:
        print(f"failed to hash {artifact_path}: {exc}", file=sys.stderr)
        return 1

    try:
        receipt_human_approval(task_dir, stage, sha256_hex, approver="human")
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"{task_dir.name}: approved {stage} ({sha256_hex[:12]}) — receipt written, scheduler will advance")
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
        # Write retrospective receipt. B-002 (task-007): writer self-computes
        # scores via compute_reward(task_dir) internally — caller supplies only
        # task_dir. Legacy score kwargs would now raise TypeError.
        try:
            from lib_receipts import receipt_retrospective
            receipt_retrospective(task_dir)
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
    """Validate the receipt chain for a task.

    AC 25 extensions:
      * Each receipt row carries ``contract_version`` (read from payload
        via ``read_receipt(..., min_version=1)`` which always returns the
        raw receipt if present, bypassing the per-step floor).
      * Floor violations (receipt exists but below
        ``MIN_VERSION_PER_STEP[step]`` via ``_resolve_min_version``) are
        flagged as ``FLOOR_VIOLATION: step=... version=... required=...``.
      * Exit codes:
            0 — all receipts present at or above floor
            1 — chain gap (missing required receipts)
            2 — floor violation on at least one receipt (distinct)
        When BOTH a gap AND a floor violation exist, exit code 2 takes
        precedence — a below-floor receipt is a structural defect the
        operator must fix first before re-evaluating gaps.
    """
    import json as _json
    from lib_receipts import (
        MIN_VERSION_PER_STEP,
        _resolve_min_version,
        read_receipt,
        validate_chain as validate_receipt_chain,
    )

    task_dir = Path(args.task_dir).resolve()
    gaps = validate_receipt_chain(task_dir)

    receipts_dir = task_dir / "receipts"
    receipt_rows: list[dict] = []
    floor_violations: list[dict] = []

    if receipts_dir.exists():
        for rp in sorted(receipts_dir.glob("*.json")):
            step_name = rp.stem
            # Read raw receipt (min_version=1 disables the floor gate so
            # we can inspect contract_version even for below-floor files).
            raw = read_receipt(task_dir, step_name, min_version=1)
            if raw is None:
                # Receipt file exists but is unparseable/invalid=false.
                receipt_rows.append({
                    "step": step_name,
                    "contract_version": None,
                    "present": False,
                    "error": "unparseable or valid=false",
                })
                continue
            actual = raw.get("contract_version", 1)
            try:
                actual_int = int(actual)
            except (TypeError, ValueError):
                actual_int = None

            required = _resolve_min_version(step_name)
            row: dict = {
                "step": step_name,
                "contract_version": actual_int,
                "required_floor": required,
                "present": True,
            }
            if actual_int is None:
                row["floor_violation"] = True
                floor_violations.append({
                    "step": step_name,
                    "version": actual,
                    "required": required,
                })
            elif actual_int < required:
                row["floor_violation"] = True
                floor_violations.append({
                    "step": step_name,
                    "version": actual_int,
                    "required": required,
                })
            else:
                row["floor_violation"] = False
            receipt_rows.append(row)

    result = {
        "valid": len(gaps) == 0 and len(floor_violations) == 0,
        "gaps": gaps,
        "receipts": receipt_rows,
        "floor_violations": floor_violations,
        "task_dir": str(task_dir),
    }
    print(_json.dumps(result, indent=2))

    # Human-readable floor-violation lines to stderr so shell consumers
    # grepping for "FLOOR_VIOLATION:" do not need to JSON-parse stdout.
    for fv in floor_violations:
        print(
            f"FLOOR_VIOLATION: step={fv['step']} "
            f"version={fv['version']} required={fv['required']}",
            file=sys.stderr,
        )

    # Exit-code precedence: floor violation (2) > gap (1) > clean (0).
    if floor_violations:
        return 2
    if gaps:
        return 1
    return 0


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


def cmd_bus(args: argparse.Namespace) -> int:
    from pathlib import Path
    root = Path(args.root).resolve()

    if args.bus_action == "emit":
        from lib_events import emit_event
        import json as _json
        payload = _json.loads(args.payload) if args.payload else {}
        path = emit_event(root, args.event_type, "cli", payload=payload)
        print(f"  Emitted {args.event_type} → {path.name}")
        return 0

    if args.bus_action == "drain":
        from eventbus import drain
        summary = drain(root, max_iterations=args.max_iterations)
        if summary:
            for event_type, results in summary.items():
                for result in results:
                    print(f"  {event_type}: {result}")
        else:
            print("  No events to process")
        return 0

    if args.bus_action == "status":
        from lib_events import _events_dir
        events_dir = _events_dir(root)
        pending = sorted(events_dir.glob("*.json"))
        if not pending:
            print("  No pending events")
            return 0
        import json as _json
        for p in pending:
            try:
                data = _json.loads(p.read_text())
                et = data.get("event_type", "?")
                ts = data.get("emitted_at", "?")
                pb = data.get("processed_by", {})
                consumers = list(pb.keys()) if isinstance(pb, dict) else list(pb) if isinstance(pb, list) else []
                print(f"  {p.name}  type={et}  emitted={ts}  processed_by={consumers or 'none'}")
            except Exception:
                print(f"  {p.name}  (unreadable)")
        return 0

    if args.bus_action == "handlers":
        from eventbus import HANDLERS
        for event_type, entries in sorted(HANDLERS.items()):
            print(f"  {event_type}:")
            for name, _ in entries:
                print(f"    - {name}")
        return 0

    return 1


def cmd_calibration(args: argparse.Namespace) -> int:
    from pathlib import Path
    import json as _json
    root = Path(args.root).resolve()

    if args.cal_action == "status":
        from lib_registry import ensure_learned_registry
        registry = ensure_learned_registry(root)
        agents = registry.get("agents", [])
        if not agents:
            print("  No learned agents registered")
            return 0

        print(f"  Learned Agents: {len(agents)}")
        print(f"  {'Name':<35} {'Mode':<12} {'Status':<25} {'Route':<6} {'Composite':<10} {'Samples'}")
        print(f"  {'-'*35} {'-'*12} {'-'*25} {'-'*6} {'-'*10} {'-'*7}")
        for a in agents:
            name = a.get("agent_name", "?")[:35]
            mode = a.get("mode", "?")
            status = a.get("status", "?")
            route = "yes" if a.get("route_allowed") else "no"
            bs = a.get("benchmark_summary", {})
            composite = f"{bs.get('mean_composite', 0):.3f}" if bs.get("mean_composite") else "-"
            samples = bs.get("sample_count", 0)
            print(f"  {name:<35} {mode:<12} {status:<25} {route:<6} {composite:<10} {samples}")
        return 0

    if args.cal_action == "history":
        from lib_registry import ensure_learned_registry
        registry = ensure_learned_registry(root)
        agents = registry.get("agents", [])
        for a in agents:
            evals = a.get("benchmarks", [])
            if not evals:
                continue
            name = a.get("agent_name", "?")
            print(f"  {name}:")
            for e in evals[-5:]:  # last 5
                rec = e.get("recommendation", "?")
                dq = e.get("delta_quality", 0)
                dc = e.get("delta_composite", 0)
                ts = e.get("evaluated_at", "?")[:19]
                print(f"    {ts}  {rec:<20} Δq={dq:+.3f}  Δc={dc:+.3f}")
        if not any(a.get("benchmarks") for a in agents):
            print("  No benchmark history yet")
        return 0

    if args.cal_action == "json":
        from lib_registry import ensure_learned_registry
        registry = ensure_learned_registry(root)
        print(_json.dumps(registry, indent=2))
        return 0

    return 1


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

    approve_stage_parser = subparsers.add_parser(
        "approve-stage",
        help="Record human approval for a review stage and advance the task",
    )
    approve_stage_parser.add_argument("task_dir")
    approve_stage_parser.add_argument(
        "stage",
        help="Review stage: SPEC_REVIEW, PLAN_REVIEW, or TDD_REVIEW",
    )
    approve_stage_parser.set_defaults(func=cmd_approve_stage)

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

    bus_parser = subparsers.add_parser("bus", help="Event bus: emit, drain, status, handlers")
    bus_sub = bus_parser.add_subparsers(dest="bus_action", required=True)
    bus_emit = bus_sub.add_parser("emit", help="Emit an event")
    bus_emit.add_argument("event_type", help="Event type (e.g. task-completed)")
    bus_emit.add_argument("--payload", default=None, help="JSON payload string")
    bus_emit.add_argument("--root", default=".")
    bus_drain = bus_sub.add_parser("drain", help="Process all pending events")
    bus_drain.add_argument("--root", default=".")
    bus_drain.add_argument("--max-iterations", type=int, default=10)
    bus_status = bus_sub.add_parser("status", help="Show pending events")
    bus_status.add_argument("--root", default=".")
    bus_handlers = bus_sub.add_parser("handlers", help="List registered handlers")
    bus_handlers.add_argument("--root", default=".")
    bus_parser.set_defaults(func=cmd_bus)

    cal_parser = subparsers.add_parser("calibration", help="Learned agent registry and benchmark status")
    cal_sub = cal_parser.add_subparsers(dest="cal_action", required=True)
    cal_status = cal_sub.add_parser("status", help="Show all learned agents with mode/status/scores")
    cal_status.add_argument("--root", default=".")
    cal_history = cal_sub.add_parser("history", help="Show recent benchmark history per agent")
    cal_history.add_argument("--root", default=".")
    cal_json = cal_sub.add_parser("json", help="Dump full registry as JSON")
    cal_json.add_argument("--root", default=".")
    cal_parser.set_defaults(func=cmd_calibration)

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
