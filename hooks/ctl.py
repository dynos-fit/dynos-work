#!/usr/bin/env python3
"""Deterministic control plane for dynos-work."""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import argparse
import json
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from lib_core import (
    VALID_CLASSIFICATION_TYPES,
    VALID_DOMAINS,
    VALID_RISK_LEVELS,
    find_active_tasks,
    load_json,
    next_command_for_stage,
    transition_task,
    write_json,
)
from lib_receipts import hash_file, receipt_audit_done, receipt_human_approval
from lib_validate import check_segment_ownership, validate_task_artifacts
from write_policy import WriteAttempt, require_write_allowed


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


_EXTERNAL_TRIGGER_TERMS: tuple[str, ...] = (
    "sdk",
    "oauth",
    "sso",
    "webhook",
    "stripe",
    "docker",
    "terraform",
    "github actions",
    "gitlab ci",
    "retry",
    "queue",
    "cache",
    "pagination",
    "rate limit",
    "rate limiting",
    "metrics",
    "tracing",
    "protocol",
    "migration",
    "migrate",
    "setup",
    "configure",
    "integration",
    "event bus",
    "eventbus",
    "cloud",
    "aws",
    "gcp",
    "azure",
)

_LOCAL_BUG_TERMS: tuple[str, ...] = (
    "bug",
    "fix",
    "failing test",
    "regression",
    "traceback",
    "exception",
    "stack trace",
    "null",
    "none",
)


def _task_raw_input(task_dir: Path, manifest: dict) -> str:
    raw = manifest.get("raw_input")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    raw_input_path = task_dir / "raw-input.md"
    if raw_input_path.exists():
        return raw_input_path.read_text(encoding="utf-8").strip()
    return ""


def _match_terms(text: str, terms: tuple[str, ...]) -> list[str]:
    lowered = text.lower()
    return [term for term in terms if term in lowered]


def _looks_like_local_file_scoped_task(text: str) -> bool:
    return bool(
        re.search(r"\b[\w./-]+\.(py|ts|tsx|js|jsx|go|rb|java|rs|yml|yaml|json|md|sql)\b", text.lower())
    )


def _compute_external_solution_gate(task_dir: Path) -> dict:
    manifest = load_json(task_dir / "manifest.json")
    classification = manifest.get("classification")
    if not isinstance(classification, dict):
        classification = {}
    task_type = str(classification.get("type", "feature") or "feature")
    risk_level = str(classification.get("risk_level", "medium") or "medium")
    domains = classification.get("domains")
    if not isinstance(domains, list):
        domains = []
    domains = [str(d) for d in domains if str(d).strip()]

    raw_input = _task_raw_input(task_dir, manifest)
    trigger_matches = _match_terms(raw_input, _EXTERNAL_TRIGGER_TERMS)
    local_bug_matches = _match_terms(raw_input, _LOCAL_BUG_TERMS)
    file_scoped = _looks_like_local_file_scoped_task(raw_input)
    migration_like = task_type in {"migration", "full-stack"} or "migration" in domains or "infra" in domains

    search_recommended = False
    reasons: list[str] = []
    if migration_like:
        search_recommended = True
        reasons.append(f"task_type={task_type}")
    if trigger_matches:
        search_recommended = True
        reasons.append("matched external-solution terms")
    if file_scoped and local_bug_matches and not trigger_matches and not migration_like:
        search_recommended = False
        reasons = ["file-scoped local bugfix task"]
    elif not search_recommended:
        reasons = ["local repo evidence likely sufficient"]

    query_reason = (
        "external search recommended: " + ", ".join(reasons)
        if search_recommended
        else "local repo evidence is sufficient"
    )
    if len(query_reason) > 200:
        query_reason = query_reason[:197] + "..."

    return {
        "search_recommended": search_recommended,
        "search_used": False,
        "query_reason": query_reason,
        "candidates": [],
        "recommended_choice": None,
        "decision_basis": {
            "task_type": task_type,
            "risk_level": risk_level,
            "domains": domains,
            "trigger_matches": trigger_matches[:8],
            "local_bug_matches": local_bug_matches[:8],
            "file_scoped": file_scoped,
        },
    }


def _write_ctl_json(task_dir: Path, path: Path, payload: dict) -> None:
    require_write_allowed(
        WriteAttempt(
            role="ctl",
            task_dir=task_dir,
            path=path,
            operation="modify" if path.exists() else "create",
            source="ctl",
        )
    )
    write_json(path, payload)


def _normalize_repo_relative_path(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("path entries must be non-empty strings")
    p = Path(raw.strip())
    if p.is_absolute() or ".." in p.parts:
        raise ValueError(f"path must stay inside repo: {raw}")
    return p.as_posix()


def _dedupe_preserve(values: list[object]) -> list[object]:
    seen: set[object] = set()
    out: list[object] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _compute_fast_track_from_classification(classification: dict[str, object]) -> bool:
    return (
        classification.get("risk_level") == "low"
        and isinstance(classification.get("domains"), list)
        and len(classification["domains"]) == 1
    )


def _normalize_classification_payload(task_dir: Path, raw: object) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("classification payload must be a JSON object")

    ctype = str(raw.get("type", "")).strip()
    if ctype not in VALID_CLASSIFICATION_TYPES:
        raise ValueError(f"classification.type invalid: {ctype!r}")

    risk_level = str(raw.get("risk_level", "")).strip()
    if risk_level not in VALID_RISK_LEVELS:
        raise ValueError(f"classification.risk_level invalid: {risk_level!r}")

    domains_raw = raw.get("domains")
    if not isinstance(domains_raw, list):
        raise ValueError("classification.domains must be a non-empty array")
    domains: list[str] = []
    seen_domains: set[str] = set()
    for domain_raw in domains_raw:
        domain = str(domain_raw).strip()
        if not domain:
            continue
        if domain not in VALID_DOMAINS:
            raise ValueError(f"classification domain invalid: {domain!r}")
        if domain in seen_domains:
            continue
        seen_domains.add(domain)
        domains.append(domain)
    if not domains:
        raise ValueError("classification.domains must contain at least one known domain")

    notes_raw = raw.get("notes", "")
    if notes_raw is None:
        notes = ""
    elif isinstance(notes_raw, str):
        notes = notes_raw.strip()
    else:
        raise ValueError("classification.notes must be a string")

    normalized: dict[str, object] = {
        "type": ctype,
        "domains": domains,
        "risk_level": risk_level,
        "notes": notes,
    }
    tdd_required = raw.get("tdd_required")
    if tdd_required is not None:
        if not isinstance(tdd_required, bool):
            raise ValueError("classification.tdd_required must be a boolean when present")
        normalized["tdd_required"] = tdd_required

    normalized["fast_track"] = _compute_fast_track_from_classification(normalized)
    return normalized


def _normalize_execution_graph_payload(task_dir: Path, raw: object) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("execution graph payload must be a JSON object")
    manifest = load_json(task_dir / "manifest.json")
    task_id = str(manifest.get("task_id") or task_dir.name)
    segments_raw = raw.get("segments")
    if not isinstance(segments_raw, list):
        raise ValueError("execution graph segments must be an array")
    normalized_segments: list[dict] = []
    seen_ids: set[str] = set()
    for seg in segments_raw:
        if not isinstance(seg, dict):
            raise ValueError("execution graph segment must be an object")
        seg_id = str(seg.get("id", "")).strip()
        if not seg_id:
            raise ValueError("execution graph segment missing non-empty id")
        if seg_id in seen_ids:
            raise ValueError(f"duplicate execution graph segment id: {seg_id}")
        seen_ids.add(seg_id)
        files_expected = seg.get("files_expected", [])
        if not isinstance(files_expected, list):
            raise ValueError(f"{seg_id}: files_expected must be an array")
        criteria_ids = seg.get("criteria_ids", [])
        if not isinstance(criteria_ids, list):
            raise ValueError(f"{seg_id}: criteria_ids must be an array")
        depends_on = seg.get("depends_on", [])
        if not isinstance(depends_on, list):
            raise ValueError(f"{seg_id}: depends_on must be an array")
        normalized_segments.append({
            **seg,
            "id": seg_id,
            "files_expected": _dedupe_preserve([_normalize_repo_relative_path(v) for v in files_expected]),
            "criteria_ids": _dedupe_preserve([int(v) for v in criteria_ids]),
            "depends_on": _dedupe_preserve([str(v).strip() for v in depends_on if str(v).strip()]),
        })
    return {
        **raw,
        "task_id": task_id,
        "segments": normalized_segments,
    }


def _normalize_repair_log_payload(task_dir: Path, raw: object) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("repair log payload must be a JSON object")
    manifest = load_json(task_dir / "manifest.json")
    task_id = str(manifest.get("task_id") or task_dir.name)
    batches_raw = raw.get("batches")
    if not isinstance(batches_raw, list):
        raise ValueError("repair-log batches must be an array")
    normalized_batches: list[dict] = []
    seen_batch_ids: set[str] = set()
    seen_finding_ids: set[str] = set()
    for index, batch in enumerate(batches_raw, start=1):
        if not isinstance(batch, dict):
            raise ValueError("repair-log batch must be an object")
        batch_id = str(batch.get("batch_id", "")).strip()
        if not batch_id:
            raise ValueError("repair-log batch missing string batch_id")
        if batch_id in seen_batch_ids:
            raise ValueError(f"duplicate repair batch id: {batch_id}")
        seen_batch_ids.add(batch_id)
        tasks_raw = batch.get("tasks")
        if not isinstance(tasks_raw, list) or not tasks_raw:
            raise ValueError(f"{batch_id}: tasks must be a non-empty array")
        normalized_tasks: list[dict] = []
        for task in tasks_raw:
            if not isinstance(task, dict):
                raise ValueError(f"{batch_id}: task must be an object")
            finding_id = str(task.get("finding_id", "")).strip()
            if not finding_id:
                raise ValueError(f"{batch_id}: finding_id must be a non-empty string")
            if finding_id in seen_finding_ids:
                raise ValueError(f"duplicate finding_id across repair-log batches: {finding_id}")
            seen_finding_ids.add(finding_id)
            files = task.get("affected_files")
            if files is None:
                files = task.get("files_to_modify")
            if not isinstance(files, list) or not files:
                raise ValueError(f"{batch_id}: affected_files must be a non-empty array")
            normalized_task = dict(task)
            normalized_task["finding_id"] = finding_id
            normalized_task["affected_files"] = _dedupe_preserve([_normalize_repo_relative_path(v) for v in files])
            normalized_task.pop("files_to_modify", None)
            normalized_task["retry_count"] = int(task.get("retry_count", 0) or 0)
            normalized_task["max_retries"] = int(task.get("max_retries", 3) or 3)
            normalized_task["status"] = str(task.get("status", "pending") or "pending")
            normalized_tasks.append(normalized_task)
        normalized_batches.append({
            **batch,
            "batch_id": batch_id,
            "tasks": normalized_tasks,
            "parallel": bool(batch.get("parallel", False)),
            "_order": index,
        })
    normalized_batches.sort(key=lambda batch: (batch["batch_id"], batch["_order"]))
    for batch in normalized_batches:
        batch.pop("_order", None)
    return {
        **raw,
        "task_id": task_id,
        "repair_cycle": int(raw.get("repair_cycle", 0) or 0),
        "batches": normalized_batches,
    }


def _shadow_task_dir(task_dir: Path) -> Path:
    shadow_root = Path(tempfile.mkdtemp(prefix="dynos-write-boundary-"))
    shadow_task = shadow_root / task_dir.name
    shadow_task.mkdir(parents=True)
    for name in ("manifest.json", "spec.md", "plan.md"):
        src = task_dir / name
        if src.exists():
            shutil.copy2(src, shadow_task / name)
    src_reports = task_dir / "audit-reports"
    if src_reports.is_dir():
        dst_reports = shadow_task / "audit-reports"
        shutil.copytree(src_reports, dst_reports)
    return shadow_task


def _validate_execution_graph_payload(task_dir: Path, payload: dict) -> None:
    shadow = _shadow_task_dir(task_dir)
    try:
        write_json(shadow / "execution-graph.json", payload)
        errors = validate_task_artifacts(shadow, strict=False)
        if errors:
            raise ValueError("; ".join(errors))
    finally:
        shutil.rmtree(shadow.parent, ignore_errors=True)


def _validate_repair_log_payload(task_dir: Path, payload: dict) -> None:
    from lib_validate import validate_repair_log

    shadow = _shadow_task_dir(task_dir)
    try:
        write_json(shadow / "repair-log.json", payload)
        errors = validate_repair_log(shadow)
        if errors:
            raise ValueError("; ".join(errors))
    finally:
        shutil.rmtree(shadow.parent, ignore_errors=True)


def _persist_classification(task_dir: Path, payload: dict) -> None:
    manifest_path = task_dir / "manifest.json"
    manifest = load_json(manifest_path)
    manifest["classification"] = payload
    manifest["fast_track"] = bool(payload.get("fast_track"))
    _write_ctl_json(task_dir, task_dir / "classification.json", payload)
    _write_ctl_json(task_dir, manifest_path, manifest)


def _read_json_input(path_arg: str) -> object:
    return json.loads(Path(path_arg).read_text(encoding="utf-8"))


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

    # F1: --force requires both --reason and --approver. Validate at the
    # CLI boundary BEFORE invoking transition_task so the caller gets a
    # named-flag error message rather than a generic ValueError from
    # the library. Exit code 2 mirrors argparse's usage-error convention.
    force_reason: str | None = None
    force_approver: str | None = None
    if args.force:
        reason_val = getattr(args, "reason", None)
        approver_val = getattr(args, "approver", None)
        if not isinstance(reason_val, str) or not reason_val.strip():
            print(
                "--force requires --reason STR (non-empty; whitespace-only values are rejected)",
                file=sys.stderr,
            )
            return 2
        if not isinstance(approver_val, str) or not approver_val.strip():
            print(
                "--force requires --approver STR (non-empty; whitespace-only values are rejected)",
                file=sys.stderr,
            )
            return 2
        force_reason = reason_val
        force_approver = approver_val

    try:
        previous, manifest = transition_task(
            task_dir,
            args.next_stage,
            force=args.force,
            force_reason=force_reason,
            force_approver=force_approver,
        )
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


def cmd_run_external_solution_gate(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir).resolve()
    try:
        gate = _compute_external_solution_gate(task_dir)
        gate_path = task_dir / "external-solution-gate.json"
        _write_ctl_json(task_dir, gate_path, gate)
        print(json.dumps({
            "status": "external_solution_gate_ready",
            "task_dir": str(task_dir),
            "gate_path": str(gate_path),
            **gate,
        }, indent=2))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


def cmd_write_execute_handoff(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir).resolve()
    try:
        manifest = load_json(task_dir / "manifest.json")
        handoff = {
            "from_skill": "execute",
            "to_skill": "audit",
            "handoff_at": datetime.now(timezone.utc).isoformat(),
            "contract_version": "1.0.0",
            "manifest_stage": str(manifest.get("stage", "unknown")),
        }
        handoff_path = task_dir / "handoff-execute-audit.json"
        _write_ctl_json(task_dir, handoff_path, handoff)
        print(json.dumps({
            "status": "execute_handoff_ready",
            "task_dir": str(task_dir),
            "handoff_path": str(handoff_path),
            **handoff,
        }, indent=2))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


def cmd_write_execution_graph(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir).resolve()
    try:
        payload = _normalize_execution_graph_payload(task_dir, _read_json_input(args.from_path))
        _validate_execution_graph_payload(task_dir, payload)
        out_path = task_dir / "execution-graph.json"
        _write_ctl_json(task_dir, out_path, payload)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps({"status": "execution_graph_written", "path": str(out_path)}, indent=2))
    return 0


def cmd_write_repair_log(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir).resolve()
    try:
        payload = _normalize_repair_log_payload(task_dir, _read_json_input(args.from_path))
        _validate_repair_log_payload(task_dir, payload)
        out_path = task_dir / "repair-log.json"
        _write_ctl_json(task_dir, out_path, payload)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps({"status": "repair_log_written", "path": str(out_path)}, indent=2))
    return 0


def cmd_write_classification(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir).resolve()
    try:
        payload = _normalize_classification_payload(task_dir, _read_json_input(args.from_path))
        _persist_classification(task_dir, payload)
        out_path = task_dir / "classification.json"
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps({"status": "classification_written", "path": str(out_path)}, indent=2))
    return 0


def cmd_audit_receipt(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir).resolve()
    try:
        out = receipt_audit_done(
            task_dir,
            args.auditor_name,
            args.model,
            report_path=args.report_path,
            tokens_used=args.tokens_used,
            route_mode=args.route_mode,
            agent_path=args.agent_path,
            injected_agent_sha256=args.injected_agent_sha256,
            ensemble_context=args.ensemble_context,
        )
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(str(out))
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
    transition_parser.add_argument(
        "--reason",
        default=None,
        help="Break-glass rationale — required when --force is used.",
    )
    transition_parser.add_argument(
        "--approver",
        default=None,
        help="Operator identity authorising the force — required with --force.",
    )
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

    external_gate_parser = subparsers.add_parser(
        "run-external-solution-gate",
        help="Write external-solution-gate.json from deterministic task heuristics",
    )
    external_gate_parser.add_argument("task_dir")
    external_gate_parser.set_defaults(func=cmd_run_external_solution_gate)

    execute_handoff_parser = subparsers.add_parser(
        "write-execute-handoff",
        help="Write handoff-execute-audit.json deterministically",
    )
    execute_handoff_parser.add_argument("task_dir")
    execute_handoff_parser.set_defaults(func=cmd_write_execute_handoff)

    graph_write_parser = subparsers.add_parser(
        "write-execution-graph",
        help="Validate, normalize, and atomically write execution-graph.json",
    )
    graph_write_parser.add_argument("task_dir")
    graph_write_parser.add_argument("--from", dest="from_path", required=True)
    graph_write_parser.set_defaults(func=cmd_write_execution_graph)

    repair_write_parser = subparsers.add_parser(
        "write-repair-log",
        help="Validate, normalize, and atomically write repair-log.json",
    )
    repair_write_parser.add_argument("task_dir")
    repair_write_parser.add_argument("--from", dest="from_path", required=True)
    repair_write_parser.set_defaults(func=cmd_write_repair_log)

    classification_write_parser = subparsers.add_parser(
        "write-classification",
        help="Validate, normalize, and atomically write classification.json plus synced manifest state",
    )
    classification_write_parser.add_argument("task_dir")
    classification_write_parser.add_argument("--from", dest="from_path", required=True)
    classification_write_parser.set_defaults(func=cmd_write_classification)

    audit_receipt_parser = subparsers.add_parser(
        "audit-receipt",
        help="Write an audit receipt deterministically from the on-disk report",
    )
    audit_receipt_parser.add_argument("task_dir")
    audit_receipt_parser.add_argument("auditor_name")
    audit_receipt_parser.add_argument(
        "--model",
        default=None,
        help="Model identifier used by the auditor (optional).",
    )
    audit_receipt_parser.add_argument(
        "--report-path",
        default=None,
        help="Path to the audit report JSON. When present, counts are derived from it.",
    )
    audit_receipt_parser.add_argument(
        "--tokens-used",
        type=int,
        default=0,
        help="Tokens consumed by the auditor run.",
    )
    audit_receipt_parser.add_argument(
        "--route-mode",
        required=True,
        help="Routing mode from audit-routing (generic, learned, replace, alongside).",
    )
    audit_receipt_parser.add_argument(
        "--agent-path",
        default=None,
        help="Learned agent path from audit-routing, or null/omitted for generic.",
    )
    audit_receipt_parser.add_argument(
        "--injected-agent-sha256",
        default=None,
        help="Injected auditor prompt sidecar digest when non-generic.",
    )
    audit_receipt_parser.add_argument(
        "--ensemble-context",
        action="store_true",
        help="Mark this receipt as part of ensemble voting/escalation.",
    )
    audit_receipt_parser.set_defaults(func=cmd_audit_receipt)

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
