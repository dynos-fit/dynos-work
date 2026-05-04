#!/usr/bin/env python3
"""Deterministic control plane for dynos-work."""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from lib_core import (
    VALID_CLASSIFICATION_TYPES,
    VALID_DOMAINS,
    VALID_RISK_LEVELS,
    find_active_tasks,
    get_tdd_required,
    load_json,
    next_command_for_stage,
    now_iso,
    transition_task,
    write_json,
)
from lib_receipts import (
    hash_file,
    plan_validated_receipt_matches,
    read_receipt,
    receipt_audit_done,
    receipt_executor_done,
    receipt_human_approval,
    receipt_plan_audit,
    receipt_plan_validated,
    receipt_planner_spawn,
    receipt_search_conducted,
    receipt_spec_validated,
    receipt_executor_routing,
)
from lib_validate import apply_fast_track, check_segment_ownership, require_nonblank, validate_task_artifacts
from write_policy import WriteAttempt, get_capability_key, require_write_allowed


_APPROVE_STAGE_MAP: dict[str, tuple[str, str]] = {
    # review_stage -> (relative artifact path, next stage)
    "SPEC_NORMALIZATION": ("spec.md", "SPEC_REVIEW"),
    "SPEC_REVIEW": ("spec.md", "PLANNING"),
    "PLAN_REVIEW": ("plan.md", "PLAN_AUDIT"),
    "TDD_REVIEW": ("evidence/tdd-tests.md", "PRE_EXECUTION_SNAPSHOT"),
}

# AC 8: single source of truth for the ctl write role used at all write sites.
_WRITE_ROLE = "ctl"


def _rules_corrupt_sentinel(root: Path) -> Path:
    """Return sentinel path co-located with daemon.py's writer.

    Duplicates the path shape from ``daemon.rules_corrupt_sentinel_path``;
    deliberately inlined here so ctl.py need not import daemon.py (and
    pull in its subprocess/signal machinery) just to check one file.
    """
    return root / ".dynos" / ".rules_corrupt"


def _refuse_if_rules_corrupt(root: Path) -> int | None:
    """Block stage-advancing ctl commands when prevention-rules.json is corrupt.

    Returns an exit code (1) when the sentinel exists so the caller can
    propagate it directly; returns None when there is no sentinel and
    the command may proceed. Error goes to stderr and names the
    persistent rules path so the operator knows which file to fix.

    This sentinel blocks every stage-advancing ctl command, including
    cmd_transition (both normal and --force paths), cmd_approve_stage, and
    each cmd_run_* function that invokes transition_task. The sentinel is a
    kill switch for a corrupt prevention-rules.json, not a bootstrap-only gate.
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
    manifest = _load_manifest(task_dir)
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

    gate = {
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
    return gate


def _write_ctl_json(task_dir: Path, path: Path, payload: dict) -> None:
    """Persist ctl-owned JSON artifacts through the write boundary."""
    require_write_allowed(
        WriteAttempt(
            role=_WRITE_ROLE,
            task_dir=task_dir,
            path=path,
            operation="modify" if path.exists() else "create",
            source=_WRITE_ROLE,
        ),
        capability_key=get_capability_key(_WRITE_ROLE),
    )
    write_json(path, payload)


def _read_json_input(path: str) -> dict:
    payload = load_json(Path(path).resolve())
    if not isinstance(payload, dict):
        raise ValueError("input payload must be a JSON object")
    return payload


def _normalize_execution_graph_payload(task_dir: Path, payload: dict) -> dict:
    segments_in = payload.get("segments", [])
    if not isinstance(segments_in, list):
        raise ValueError("segments must be an array")
    normalized_segments: list[dict] = []
    for raw in segments_in:
        if not isinstance(raw, dict):
            raise ValueError("each segment must be an object")
        files_expected: list[str] = []
        seen_files: set[str] = set()
        for entry in raw.get("files_expected", []) or []:
            if not isinstance(entry, str):
                continue
            try:
                item = require_nonblank(entry, field_name="files_expected entry")
            except ValueError:
                continue
            if item in seen_files:
                continue
            seen_files.add(item)
            files_expected.append(item)
        depends_on: list[str] = []
        seen_deps: set[str] = set()
        for entry in raw.get("depends_on", []) or []:
            if not isinstance(entry, str):
                continue
            try:
                item = require_nonblank(entry, field_name="depends_on entry")
            except ValueError:
                continue
            if item in seen_deps:
                continue
            seen_deps.add(item)
            depends_on.append(item)
        criteria_ids: list[int] = []
        seen_criteria: set[int] = set()
        for entry in raw.get("criteria_ids", []) or []:
            try:
                item = int(entry)
            except (TypeError, ValueError):
                continue
            if item in seen_criteria:
                continue
            seen_criteria.add(item)
            criteria_ids.append(item)
        normalized_segments.append(
            {
                "id": str(raw.get("id", "")).strip(),
                "executor": str(raw.get("executor", "")).strip(),
                "description": str(raw.get("description", "")).strip(),
                "files_expected": files_expected,
                "depends_on": depends_on,
                "parallelizable": bool(raw.get("parallelizable", False)),
                "criteria_ids": criteria_ids,
            }
        )
    return {"task_id": task_dir.name, "segments": normalized_segments}


def _validate_execution_graph_payload(task_dir: Path, payload: dict) -> None:
    segments = payload.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ValueError("execution graph must contain at least one segment")
    seen_ids: set[str] = set()
    for idx, segment in enumerate(segments):
        if not isinstance(segment, dict):
            raise ValueError(f"segment[{idx}] must be an object")
        try:
            seg_id = require_nonblank(str(segment.get("id", "")), field_name=f"segment[{idx}].id")
        except ValueError:
            raise ValueError(f"segment[{idx}] missing id")
        if seg_id in seen_ids:
            raise ValueError(f"duplicate segment id: {seg_id}")
        seen_ids.add(seg_id)
        try:
            require_nonblank(str(segment.get("executor", "")), field_name=f"{seg_id}.executor")
        except ValueError:
            raise ValueError(f"{seg_id}: executor is required")
        files_expected = segment.get("files_expected")
        if not isinstance(files_expected, list) or not files_expected:
            raise ValueError(f"{seg_id}: files_expected must be a non-empty array")
        criteria_ids = segment.get("criteria_ids")
        if not isinstance(criteria_ids, list) or not criteria_ids:
            raise ValueError(f"{seg_id}: criteria_ids must be a non-empty array")


def _normalize_repair_log_payload(task_dir: Path, payload: dict) -> dict:
    batches_in = payload.get("batches", [])
    if not isinstance(batches_in, list):
        raise ValueError("batches must be an array")
    normalized_batches: list[dict] = []
    for raw_batch in batches_in:
        if not isinstance(raw_batch, dict):
            raise ValueError("each batch must be an object")
        tasks_in = raw_batch.get("tasks", [])
        if not isinstance(tasks_in, list):
            raise ValueError("batch tasks must be an array")
        normalized_tasks: list[dict] = []
        for raw_task in tasks_in:
            if not isinstance(raw_task, dict):
                raise ValueError("repair task must be an object")
            files_source = raw_task.get("affected_files")
            if files_source is None:
                files_source = raw_task.get("files_to_modify", [])
            affected_files: list[str] = []
            seen_files: set[str] = set()
            for entry in files_source or []:
                if not isinstance(entry, str):
                    continue
                try:
                    item = require_nonblank(entry, field_name="affected_files entry")
                except ValueError:
                    continue
                if item in seen_files:
                    continue
                seen_files.add(item)
                affected_files.append(item)
            normalized_tasks.append(
                {
                    "finding_id": str(raw_task.get("finding_id", "")).strip(),
                    "auditor": str(raw_task.get("auditor", "")).strip(),
                    "severity": str(raw_task.get("severity", "")).strip(),
                    "instruction": str(raw_task.get("instruction", "")).strip(),
                    "assigned_executor": str(raw_task.get("assigned_executor", "")).strip(),
                    "affected_files": affected_files,
                    "retry_count": int(raw_task.get("retry_count", 0) or 0),
                    "max_retries": int(raw_task.get("max_retries", 3) or 3),
                    "status": str(raw_task.get("status", "pending") or "pending"),
                }
            )
        normalized_batches.append(
            {
                "batch_id": str(raw_batch.get("batch_id", "")).strip(),
                "parallel": bool(raw_batch.get("parallel", False)),
                "tasks": normalized_tasks,
            }
        )
    return {
        "task_id": task_dir.name,
        "repair_cycle": int(payload.get("repair_cycle", 1) or 1),
        "batches": normalized_batches,
    }


def _validate_repair_log_payload(task_dir: Path, payload: dict) -> None:
    batches = payload.get("batches")
    if not isinstance(batches, list) or not batches:
        raise ValueError("repair log must contain at least one batch")
    for batch in batches:
        if not isinstance(batch, dict):
            raise ValueError("repair batch must be an object")
        batch_id = str(batch.get("batch_id", "")).strip() or "<unknown-batch>"
        tasks = batch.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            raise ValueError(f"{batch_id}: tasks must be a non-empty array")
        for task in tasks:
            if not isinstance(task, dict):
                raise ValueError(f"{batch_id}: task must be an object")
            try:
                require_nonblank(str(task.get("finding_id", "")), field_name=f"{batch_id}.finding_id")
            except ValueError:
                raise ValueError(f"{batch_id}: finding_id is required")
            try:
                require_nonblank(str(task.get("assigned_executor", "")), field_name=f"{batch_id}.assigned_executor")
            except ValueError:
                raise ValueError(f"{batch_id}: assigned_executor is required")
            affected_files = task.get("affected_files")
            if not isinstance(affected_files, list) or not affected_files:
                raise ValueError(f"{batch_id}: affected_files must be a non-empty array")


_RISK_LEVEL_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2, "critical": 3}

# Hardcoded keyword list — no external config, no ReDoS risk (anchored word-boundary
# alternation over a fixed set of short literals; all branches are O(n) in input length).
_RISK_KEYWORD_PATTERN: re.Pattern[str] = re.compile(
    r"\b(auth|migration|payment|delete|drop|irreversible|hmac|signing|encryption)\b",
    re.IGNORECASE,
)

_HIGH_RISK_DOMAINS: frozenset[str] = frozenset({"security", "db", "migration"})


def _files_expected_from_graph(task_dir: Path) -> list[str] | None:
    """Return the flat list of all files_expected from execution-graph.json, or None if absent."""
    graph_path = task_dir / "execution-graph.json"
    if not graph_path.exists():
        return None
    try:
        graph = load_json(graph_path)
    except Exception:
        return None
    if not isinstance(graph, dict):
        return None
    segments = graph.get("segments", [])
    if not isinstance(segments, list):
        return None
    files: list[str] = []
    seen: set[str] = set()
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        for f in seg.get("files_expected", []) or []:
            if not isinstance(f, str):
                continue
            try:
                item = require_nonblank(f, field_name="files_expected entry")
            except ValueError:
                continue
            if item not in seen:
                seen.add(item)
                files.append(item)
    return files


def _compute_risk_floor(
    task_dir: Path,
    domains: list[str],
    payload: dict,
) -> tuple[str, list[str], dict]:
    """Compute observed_floor from the three composite signals.

    Returns (floor_level, triggering_signals, raw_matches).
    """
    triggering_signals: list[str] = []
    raw_matches: dict = {}

    # --- Signal (a): file/domain heuristic ---
    files_from_graph = _files_expected_from_graph(task_dir)
    if files_from_graph is not None:
        files_expected = files_from_graph
    else:
        fe = payload.get("files_expected", [])
        if not isinstance(fe, list):
            fe = []
        files_expected = [str(f).strip() for f in fe if isinstance(f, str) and str(f).strip()]

    high_risk_domain_hits = [d for d in domains if d in _HIGH_RISK_DOMAINS]
    file_domain_floor: str | None = None
    if len(files_expected) >= 10 or len(domains) >= 3 or high_risk_domain_hits:
        file_domain_floor = "high"
        triggering_signals.append("file_domain")
        raw_matches["file_domain"] = {
            "files_expected_count": len(files_expected),
            "domains": list(domains),
            "high_risk_domains_matched": high_risk_domain_hits,
        }

    # --- Signal (b): keyword scan on raw-input.md + spec.md ---
    keyword_floor: str | None = None
    raw_input_text = ""
    spec_text = ""
    raw_input_path = task_dir / "raw-input.md"
    spec_path = task_dir / "spec.md"
    if raw_input_path.exists():
        try:
            raw_input_text = raw_input_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            raw_input_text = ""
    if spec_path.exists():
        try:
            spec_text = spec_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            spec_text = ""
    combined_text = raw_input_text + "\n" + spec_text
    keyword_matches = _RISK_KEYWORD_PATTERN.findall(combined_text)
    if keyword_matches:
        keyword_floor = "high"
        triggering_signals.append("keyword_scan")
        raw_matches["keyword_scan"] = {
            "matched_keywords": sorted(set(k.lower() for k in keyword_matches)),
        }

    # --- Signal (c): token count on spec.md ---
    token_count_floor: str | None = None
    spec_tokens = len(spec_text.split())
    if spec_tokens > 1500:
        token_count_floor = "medium"
        triggering_signals.append("token_count")
        raw_matches["token_count"] = {"spec_token_count": spec_tokens}

    # observed_floor = maximum of the three
    floor_levels = [
        lvl
        for lvl in (file_domain_floor, keyword_floor, token_count_floor)
        if lvl is not None
    ]
    if not floor_levels:
        return "low", [], {}

    observed_floor = max(floor_levels, key=lambda lvl: _RISK_LEVEL_ORDER.get(lvl, 0))
    return observed_floor, triggering_signals, raw_matches


def _normalize_classification_payload(task_dir: Path, payload: dict) -> dict:
    domains = payload.get("domains", [])
    if not isinstance(domains, list):
        domains = []
    normalized_domains: list[str] = []
    seen_domains: set[str] = set()
    for entry in domains:
        try:
            item = require_nonblank(str(entry), field_name="domain")
        except ValueError:
            continue
        if item in seen_domains:
            continue
        seen_domains.add(item)
        normalized_domains.append(item)
    out = {
        "type": str(payload.get("type", "")).strip(),
        "domains": normalized_domains,
        "risk_level": str(payload.get("risk_level", "")).strip(),
    }
    notes = payload.get("notes")
    if isinstance(notes, str):
        out["notes"] = notes.strip()
    if "tdd_required" in payload:
        out["tdd_required"] = bool(payload.get("tdd_required"))

    # Compute observed_floor and override upward if needed (AC8/AC9).
    planner_risk = out["risk_level"]
    observed_floor, triggering_signals, raw_matches = _compute_risk_floor(
        task_dir, normalized_domains, payload
    )
    planner_order = _RISK_LEVEL_ORDER.get(planner_risk, 0)
    floor_order = _RISK_LEVEL_ORDER.get(observed_floor, 0)
    if floor_order > planner_order:
        out["risk_level"] = observed_floor
        # Emit exactly one event per call when an upgrade happens.
        try:
            from lib_log import log_event  # noqa: PLC0415
            task_id = task_dir.name
            root = _root_for_task_dir(task_dir)
            manifest = _load_manifest(task_dir)
            log_event(
                root,
                "risk_level_upgrade_blocked",
                task=task_id,
                task_id=manifest.get("task_id", task_id),
                planner_risk=planner_risk,
                observed_floor=observed_floor,
                triggering_signals=triggering_signals,
                raw_matches=raw_matches,
            )
        except Exception:
            pass

    return out


def _persist_classification(task_dir: Path, payload: dict) -> None:
    ctype = payload.get("type")
    risk = payload.get("risk_level")
    domains = payload.get("domains")
    if ctype not in VALID_CLASSIFICATION_TYPES:
        raise ValueError(f"invalid classification.type: {ctype!r}")
    if risk not in VALID_RISK_LEVELS:
        raise ValueError(f"invalid classification.risk_level: {risk!r}")
    if not isinstance(domains, list) or not domains:
        raise ValueError("classification.domains must be a non-empty array")
    invalid_domains = [domain for domain in domains if domain not in VALID_DOMAINS]
    if invalid_domains:
        raise ValueError(f"classification domain invalid: {invalid_domains}")
    manifest = _load_manifest(task_dir)
    manifest["classification"] = payload
    from lib_validate import compute_fast_track  # noqa: PLC0415

    fast_track = compute_fast_track(manifest)
    payload["fast_track"] = fast_track
    manifest["classification"] = payload
    manifest["fast_track"] = fast_track
    _write_ctl_json(task_dir, task_dir / "classification.json", payload)
    _write_ctl_json(task_dir, task_dir / "manifest.json", manifest)


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
    root = _root_for_task_dir(task_dir)
    blocked = _refuse_if_rules_corrupt(root)
    if blocked is not None:
        return blocked

    # F1: --force requires both --reason and --approver. Validate at the
    # CLI boundary BEFORE invoking transition_task so the caller gets a
    # named-flag error message rather than a generic ValueError from
    # the library. Exit code 2 mirrors argparse's usage-error convention.
    force_reason: str | None = None
    force_approver: str | None = None
    if args.force:
        reason_val = getattr(args, "reason", None)
        approver_val = getattr(args, "approver", None)
        try:
            force_reason = require_nonblank(reason_val if isinstance(reason_val, str) else "", field_name="--reason")
        except (TypeError, ValueError):
            print(
                "--force requires --reason STR (non-empty; whitespace-only values are rejected)",
                file=sys.stderr,
            )
            return 2
        try:
            force_approver = require_nonblank(approver_val if isinstance(approver_val, str) else "", field_name="--approver")
        except (TypeError, ValueError):
            print(
                "--force requires --approver STR (non-empty; whitespace-only values are rejected)",
                file=sys.stderr,
            )
            return 2

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
    """Record a human approval receipt and atomically advance the manifest stage.

    stage must be one of SPEC_NORMALIZATION / SPEC_REVIEW / PLAN_REVIEW /
    TDD_REVIEW. Exits 1 on any failure that prevents the receipt write or the
    stage transition (unknown stage, missing artifact, receipt-write refusal,
    illegal transition). Exits 0 only after both the receipt is durably on disk
    AND manifest.json reflects the new stage.

    The manifest stage is guaranteed to be advanced before this function
    returns; callers do not depend on the daemon to observe the receipt and
    issue the transition. The daemon may still observe receipts for telemetry
    purposes but is not required for correctness. stderr carries error text;
    stdout is reserved for a success line.
    """
    task_dir = Path(args.task_dir).resolve()
    root = _root_for_task_dir(task_dir)
    blocked = _refuse_if_rules_corrupt(root)
    if blocked is not None:
        return blocked

    stage = args.stage
    mapping = _APPROVE_STAGE_MAP.get(stage)
    if mapping is None:
        allowed = ", ".join(sorted(_APPROVE_STAGE_MAP))
        print(
            f"unknown stage: {stage!r} (expected one of: {allowed})",
            file=sys.stderr,
        )
        return 1
    artifact_rel, next_stage = mapping

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

    # The scheduler (scheduler.py) fires synchronously inside write_receipt
    # and may have already advanced the stage. Re-read the manifest to check;
    # if the stage is already at next_stage, the receipt-write path did the
    # work and we do not call transition_task a second time. If it is still
    # at the current review stage, we call it explicitly so the guarantee
    # holds regardless of scheduler availability.
    try:
        current_manifest = load_json(task_dir / "manifest.json")
        already_advanced = current_manifest.get("stage") == next_stage
    except Exception:
        already_advanced = False

    if not already_advanced:
        try:
            transition_task(task_dir, next_stage)
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            return 1

    print(f"{task_dir.name}: approved {stage} ({sha256_hex[:12]}) — stage advanced to {next_stage}")
    return 0


# Artifact name → (relative file path within task_dir, canonical receipt stem)
_AMEND_ARTIFACT_MAP: dict[str, tuple[str, str]] = {
    "spec": ("spec.md", "spec-validated"),
    "plan": ("plan.md", "plan-validated"),
    "tdd": ("evidence/tdd-tests.md", "tdd_review-approved"),
}


def cmd_amend_artifact(args: argparse.Namespace) -> int:
    """Amend an artifact after it was approved, recording a receipt trail.

    Resolves the artifact file from the artifact name (spec/plan/tdd),
    requires a non-empty --reason, re-hashes the file, writes an amendment
    receipt to receipts/amend-<name>-<ts>.json, and updates the canonical
    receipt in-place by setting artifact_sha256 and appending to amendments.

    Exits 1 on missing/blank --reason or missing artifact file.
    """
    task_dir = Path(args.task_dir).resolve()

    # Validate --reason is non-empty.
    reason_raw: str | None = getattr(args, "reason", None)
    if not reason_raw or not reason_raw.strip():
        print("amend-artifact: --reason must be non-empty", file=sys.stderr)
        return 1

    try:
        reason = require_nonblank(reason_raw, field_name="--reason")
    except (TypeError, ValueError):
        print("amend-artifact: --reason must be non-empty", file=sys.stderr)
        return 1

    artifact_name: str = args.artifact_name
    mapping = _AMEND_ARTIFACT_MAP.get(artifact_name)
    if mapping is None:
        allowed = ", ".join(sorted(_AMEND_ARTIFACT_MAP))
        print(
            f"amend-artifact: unknown artifact {artifact_name!r} (expected one of: {allowed})",
            file=sys.stderr,
        )
        return 1

    artifact_rel, canonical_stem = mapping
    artifact_path = task_dir / artifact_rel
    if not artifact_path.is_file():
        print(
            f"amend-artifact: artifact file not found: {artifact_path}",
            file=sys.stderr,
        )
        return 1

    # Re-hash artifact self-computedly — never accept caller-supplied hash.
    try:
        sha256_after = hash_file(artifact_path)
    except OSError as exc:
        print(f"amend-artifact: failed to hash {artifact_path}: {exc}", file=sys.stderr)
        return 1

    # Read canonical receipt to obtain before-hash.
    receipts_dir = task_dir / "receipts"
    canonical_path = receipts_dir / f"{canonical_stem}.json"
    sha256_before: str = "none"
    canonical_data: dict = {}
    if canonical_path.is_file():
        try:
            canonical_data = json.loads(canonical_path.read_text(encoding="utf-8"))
            # Look for the existing artifact hash under the canonical field name
            # (spec-validated uses spec_sha256; we also check artifact_sha256 for
            # receipts already amended at least once).
            sha256_before = (
                canonical_data.get("artifact_sha256")
                or canonical_data.get("spec_sha256")
                or canonical_data.get("plan_sha256")
                or "none"
            )
        except (OSError, json.JSONDecodeError) as exc:
            print(
                f"amend-artifact: could not read canonical receipt {canonical_path}: {exc}",
                file=sys.stderr,
            )
            return 1

    amended_at = now_iso()

    # Build amendment record (stored both in the amendment receipt and
    # appended to the canonical receipt's amendments list).
    amendment_record = {
        "artifact_name": artifact_name,
        "artifact_sha256_before": sha256_before,
        "artifact_sha256_after": sha256_after,
        "reason": reason,
        "amended_at": amended_at,
        "amended_by": "human",
    }

    # Write amendment receipt: receipts/amend-<name>-<ts>.json
    # Compact timestamp: drop colons/dashes for filesystem friendliness.
    ts_compact = amended_at.replace(":", "").replace("-", "").replace("Z", "Z")
    amend_receipt_name = f"amend-{artifact_name}-{ts_compact}"
    amend_receipt_path = receipts_dir / f"{amend_receipt_name}.json"
    receipts_dir.mkdir(parents=True, exist_ok=True)

    amend_receipt_payload = {
        "step": amend_receipt_name,
        "ts": amended_at,
        "valid": True,
        **amendment_record,
    }
    try:
        require_write_allowed(
            WriteAttempt(
                role="receipt-writer",
                task_dir=task_dir,
                path=amend_receipt_path,
                operation="create",
                source="amend-artifact",
            ),
            capability_key=get_capability_key("receipt-writer"),
        )
    except Exception as exc:
        print(f"amend-artifact: write denied for amendment receipt: {exc}", file=sys.stderr)
        return 1

    try:
        fd, tmp = tempfile.mkstemp(
            prefix=f".{amend_receipt_path.name}.",
            suffix=".tmp",
            dir=str(receipts_dir),
        )
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(json.dumps(amend_receipt_payload, indent=2))
            os.replace(tmp, amend_receipt_path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as exc:
        print(f"amend-artifact: failed to write amendment receipt: {exc}", file=sys.stderr)
        return 1

    # Update canonical receipt in-place: set artifact_sha256 and append amendment.
    if canonical_path.is_file():
        try:
            require_write_allowed(
                WriteAttempt(
                    role="receipt-writer",
                    task_dir=task_dir,
                    path=canonical_path,
                    operation="modify",
                    source="amend-artifact",
                ),
                capability_key=get_capability_key("receipt-writer"),
            )
        except Exception as exc:
            print(f"amend-artifact: write denied for canonical receipt update: {exc}", file=sys.stderr)
            return 1

        try:
            canonical_data["artifact_sha256"] = sha256_after
            existing_amendments = canonical_data.get("amendments", [])
            if not isinstance(existing_amendments, list):
                existing_amendments = []
            existing_amendments.append(amendment_record)
            canonical_data["amendments"] = existing_amendments

            fd2, tmp2 = tempfile.mkstemp(
                prefix=f".{canonical_path.name}.",
                suffix=".tmp",
                dir=str(receipts_dir),
            )
            try:
                with os.fdopen(fd2, "w") as fh:
                    fh.write(json.dumps(canonical_data, indent=2))
                os.replace(tmp2, canonical_path)
            except Exception:
                try:
                    os.unlink(tmp2)
                except OSError:
                    pass
                raise
        except Exception as exc:
            print(f"amend-artifact: failed to update canonical receipt: {exc}", file=sys.stderr)
            return 1

    print(f"amended {artifact_name}: sha256={sha256_after}")
    print(f"amendment receipt: {amend_receipt_path}")
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



def cmd_check_retro_integrity(args: argparse.Namespace) -> int:
    """Report persistent retrospectives with no matching flush event in events.jsonl.

    Exits non-zero if any persistent-unverified entries exist. Use as a pre-calibration
    gate. Closes the weaker attack variant (no flush event). The stronger variant
    (forged flush event) requires tamper-evident events.jsonl and is out of scope.
    """
    root = Path(args.root).resolve()
    from lib_core import collect_retrospectives
    try:
        all_entries = collect_retrospectives(root, include_unverified=True)
        unverified = [e for e in all_entries if e.get("_source") == "persistent-unverified"]
        if unverified:
            print(json.dumps({
                "status": "unverified_entries_found",
                "root": str(root),
                "count": len(unverified),
                "task_ids": [e.get("task_id") for e in unverified],
                "paths": [e.get("_path") for e in unverified],
                "recommendation": (
                    "For each path: hash the file with sha256sum and append a "
                    "'retrospective_flushed' event to .dynos/events.jsonl, "
                    "or delete the entry if it is spurious."
                ),
            }, indent=2))
            return 1
        print(json.dumps({
            "status": "ok",
            "root": str(root),
            "total_retros": len(all_entries),
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


# Roles that may be stamped to active-segment-role through this wrapper.
# Mirrors pre_tool_use._EXECUTOR_ROLE_ALLOWLIST so the role file can hold
# any role pre_tool_use will accept. Forgery defense for audit-* roles
# does NOT live here — stamping the role file does not by itself produce
# a fake audit. The defense lives in receipt_audit_done's spawn-log
# cross-check: a receipt that claims an audit-* spawn must point at a
# matching agent_spawn_pre/post pair in spawn-log.jsonl, which is hook-owned
# and unforgeable from the orchestrator side. Stamping audit-spec-completion
# without a matching spawn produces a role file that does nothing useful —
# the auditor that the role would unblock never ran, so its report file
# never appears, and the receipt is rejected.
_STAMP_ROLE_ALLOWLIST: frozenset[str] = frozenset({
    "backend-executor", "ui-executor", "testing-executor", "integration-executor",
    "ml-executor", "db-executor", "refactor-executor", "docs-executor",
    "planning", "execute-inline", "repair-coordinator",
    "audit-spec-completion", "audit-security", "audit-code-quality",
    "audit-performance", "audit-dead-code", "audit-db-schema", "audit-ui",
    "audit-claude-md",
})


def cmd_stamp_role(args: argparse.Namespace) -> int:
    """Write the executor role to active-segment-role under role=ctl.

    Replaces the legacy `printf '%s' "{role}" > active-segment-role` pattern.
    Validates the role string against the allowlist. Forgery defense for
    audit-* claims is enforced downstream at receipt_audit_done, which
    cross-checks against spawn-log.jsonl — see task-20260430-005.
    """
    task_dir = Path(args.task_dir).resolve()
    if not task_dir.is_dir():
        print(f"stamp-role: task_dir does not exist: {task_dir}", file=sys.stderr)
        return 1

    role = (args.role or "").strip()
    if not role:
        print("stamp-role: --role is required and must be non-empty", file=sys.stderr)
        return 1

    if role not in _STAMP_ROLE_ALLOWLIST:
        print(
            f"stamp-role: refused — role {role!r} is not in the role "
            f"allowlist; permitted values: {sorted(_STAMP_ROLE_ALLOWLIST)}",
            file=sys.stderr,
        )
        return 1

    role_file = task_dir / "active-segment-role"
    try:
        role_file.write_text(role, encoding="utf-8")
    except OSError as exc:
        print(f"stamp-role: failed to write {role_file}: {exc}", file=sys.stderr)
        return 1

    print(json.dumps({"status": "role_stamped", "path": str(role_file), "role": role}, indent=2))
    return 0


def cmd_audit_receipt(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir).resolve()
    root = _root_for_task_dir(task_dir)
    blocked = _refuse_if_rules_corrupt(root)
    if blocked is not None:
        return blocked
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


def cmd_planner_receipt(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir).resolve()
    try:
        out = receipt_planner_spawn(
            task_dir,
            args.phase,
            tokens_used=args.tokens_used,
            model_used=args.model,
            agent_name=args.agent_name,
            injected_prompt_sha256=args.injected_prompt_sha256,
        )
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(str(out))
    return 0


def cmd_plan_validated_receipt(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir).resolve()
    try:
        out = receipt_plan_validated(task_dir, run_gap=args.run_gap)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(str(out))
    return 0


def cmd_plan_audit_receipt(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir).resolve()
    try:
        out = receipt_plan_audit(
            task_dir,
            tokens_used=args.tokens_used,
            model_used=args.model,
        )
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(str(out))
    return 0


def _load_manifest(task_dir: Path) -> dict:
    manifest = load_json(task_dir / "manifest.json")
    return manifest if isinstance(manifest, dict) else {}


def _risk_level_for_task(task_dir: Path) -> str:
    manifest = _load_manifest(task_dir)
    classification = manifest.get("classification")
    if not isinstance(classification, dict):
        return "medium"
    risk = classification.get("risk_level")
    return risk if isinstance(risk, str) and risk else "medium"


def _parse_audit_report(report_path: Path, root: Path | None = None) -> tuple[int, int]:
    data = load_json(report_path)
    findings = data.get("findings", []) if isinstance(data, dict) else []
    if not isinstance(findings, list):
        return (0, 0)
    total = len(findings)
    blocking = 0
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        if bool(finding.get("blocking")):
            blocking += 1

    # Evidence gate: a report that claims "passed" (findings empty) must
    # provide non-empty evidence.files_inspected and evidence.patterns_checked.
    # Reports with non-empty findings are unaffected by this check.
    if total == 0:
        evidence = data.get("evidence") if isinstance(data, dict) else None
        evidence = evidence if isinstance(evidence, dict) else {}
        files_inspected = evidence.get("files_inspected")
        patterns_checked = evidence.get("patterns_checked")
        missing_fields: list[str] = []
        if not (isinstance(files_inspected, list) and len(files_inspected) > 0):
            missing_fields.append("evidence.files_inspected")
        if not (isinstance(patterns_checked, list) and len(patterns_checked) > 0):
            missing_fields.append("evidence.patterns_checked")
        if missing_fields:
            raise ValueError(
                f"audit report {report_path} asserts passed without evidence: "
                f"missing {missing_fields}"
            )

    # AC 10: Verify that every file listed in evidence.files_inspected exists on disk.
    # Runs on both empty-findings and non-empty-findings branches.
    # Only enforced when root is supplied; callers that do not pass root skip this gate.
    if root is not None:
        evidence = data.get("evidence") if isinstance(data, dict) else None
        evidence = evidence if isinstance(evidence, dict) else {}
        files_inspected = evidence.get("files_inspected")
        if isinstance(files_inspected, list):
            missing: list[str] = []
            for p in files_inspected:
                if not isinstance(p, str):
                    continue
                # Glob pattern check
                if any(c in p for c in ("*", "?", "[")):
                    if not list(root.glob(p)):
                        missing.append(p)
                else:
                    if not (root / p).exists():
                        missing.append(p)
            if missing:
                raise ValueError(
                    f"audit report {report_path} lists files_inspected that do not exist: {missing}"
                )

    return (total, blocking)


def _load_graph_segments(task_dir: Path) -> list[dict]:
    graph = load_json(task_dir / "execution-graph.json")
    segments = graph.get("segments", []) if isinstance(graph, dict) else []
    return [seg for seg in segments if isinstance(seg, dict)]


def _load_routing_segments(task_dir: Path) -> list[dict]:
    payload = read_receipt(task_dir, "executor-routing")
    if not isinstance(payload, dict):
        return []
    segments = payload.get("segments", [])
    return [seg for seg in segments if isinstance(seg, dict)]


def _dependency_depths(segments: list[dict]) -> dict[str, int]:
    by_id = {
        str(seg.get("id")): seg
        for seg in segments
        if isinstance(seg.get("id"), str) and seg.get("id")
    }
    children: dict[str, list[str]] = {seg_id: [] for seg_id in by_id}
    for seg_id, seg in by_id.items():
        for dep in seg.get("depends_on", []) or []:
            if isinstance(dep, str) and dep in children:
                children[dep].append(seg_id)

    memo: dict[str, int] = {}

    def walk(seg_id: str) -> int:
        cached = memo.get(seg_id)
        if cached is not None:
            return cached
        downstream = children.get(seg_id, [])
        if not downstream:
            memo[seg_id] = 0
            return 0
        depth = 1 + max(walk(child) for child in downstream)
        memo[seg_id] = depth
        return depth

    for seg_id in by_id:
        walk(seg_id)
    return memo


def _git_dirty_files(root: Path, files_expected: list[str]) -> set[str]:
    if not files_expected:
        return set()
    import subprocess as _subprocess

    cmd = ["git", "status", "--porcelain", "--", *files_expected]
    result = _subprocess.run(
        cmd,
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return set()
    dirty: set[str] = set()
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if path:
            dirty.add(path)
    return dirty


def _verify_git_diff_covers_files(
    root: Path,
    snapshot_sha: str,
    files_expected: list[str],
) -> list[str]:
    """Return files_expected entries NOT present in git diff since snapshot.

    Checks both 'git diff --name-only --diff-filter=AMRD <snapshot_sha>' and
    'git ls-files --others --exclude-standard' (untracked new files). Any entry
    from files_expected that appears in either set is considered covered.

    Fails closed:
    - snapshot_sha empty/None/whitespace  -> ValueError('snapshot_sha required')
    - git binary not found (FileNotFoundError) -> ValueError('git binary not available')
    - non-zero git returncode -> ValueError('git command failed: <returncode>')
    """
    import subprocess as _subprocess

    if not snapshot_sha or not isinstance(snapshot_sha, str) or not snapshot_sha.strip():
        raise ValueError("snapshot_sha required")

    diff_cmd = ["git", "-C", str(root), "diff", "--name-only", "--diff-filter=AMRD", snapshot_sha]
    try:
        diff_result = _subprocess.run(
            diff_cmd,
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
    except _subprocess.TimeoutExpired:
        raise ValueError(f"git command timed out (cmd: {diff_cmd!r})")
    except FileNotFoundError:
        raise ValueError("git binary not available")

    if diff_result.returncode != 0:
        raise ValueError(f"git command failed: {diff_result.returncode} (cmd: {diff_cmd!r})")

    untracked_cmd = ["git", "-C", str(root), "ls-files", "--others", "--exclude-standard"]
    try:
        untracked_result = _subprocess.run(
            untracked_cmd,
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
    except _subprocess.TimeoutExpired:
        raise ValueError(f"git command timed out (cmd: {untracked_cmd!r})")
    except FileNotFoundError:
        raise ValueError("git binary not available")

    if untracked_result.returncode != 0:
        raise ValueError(f"git command failed: {untracked_result.returncode} (cmd: {untracked_cmd!r})")

    covered: set[str] = set()
    for line in diff_result.stdout.splitlines():
        stripped = line.strip()
        if stripped:
            covered.add(stripped)
    for line in untracked_result.stdout.splitlines():
        stripped = line.strip()
        if stripped:
            covered.add(stripped)

    return [f for f in files_expected if f not in covered]


def _segment_runtime_state(
    task_dir: Path,
    root: Path,
    segment: dict,
    routing_entry: dict,
    specs_fresh: bool,
    dirty_files: set[str],
) -> dict:
    seg_id = str(segment.get("id", ""))
    evidence_path = task_dir / "evidence" / f"{seg_id}.md"
    receipt = read_receipt(task_dir, f"executor-{seg_id}")
    evidence_exists = evidence_path.exists()
    files_expected = [
        str(path)
        for path in (segment.get("files_expected", []) or [])
        if isinstance(path, str) and path
    ]
    if isinstance(receipt, dict):
        return {
            "segment_id": seg_id,
            "status": "completed",
            "cache_eligible": False,
            "cache_reason": "executor receipt already present",
            "receipt_present": True,
            "evidence_exists": evidence_exists,
            "evidence_path": str(evidence_path),
            "files_expected": files_expected,
            "dirty_files": sorted(dirty_files),
            "route_mode": routing_entry.get("route_mode"),
            "agent_path": routing_entry.get("agent_path"),
            "model": routing_entry.get("model"),
        }

    if not evidence_exists:
        return {
            "segment_id": seg_id,
            "status": "pending",
            "cache_eligible": False,
            "cache_reason": "no evidence file",
            "receipt_present": False,
            "evidence_exists": False,
            "evidence_path": str(evidence_path),
            "files_expected": files_expected,
            "dirty_files": sorted(dirty_files),
            "route_mode": routing_entry.get("route_mode"),
            "agent_path": routing_entry.get("agent_path"),
            "model": routing_entry.get("model"),
        }

    if specs_fresh is not True:
        reason = "plan/spec drift" if isinstance(specs_fresh, str) else "missing fresh plan-validated receipt"
        return {
            "segment_id": seg_id,
            "status": "pending",
            "cache_eligible": False,
            "cache_reason": reason,
            "receipt_present": False,
            "evidence_exists": True,
            "evidence_path": str(evidence_path),
            "files_expected": files_expected,
            "dirty_files": sorted(dirty_files),
            "route_mode": routing_entry.get("route_mode"),
            "agent_path": routing_entry.get("agent_path"),
            "model": routing_entry.get("model"),
        }

    if dirty_files:
        return {
            "segment_id": seg_id,
            "status": "pending",
            "cache_eligible": False,
            "cache_reason": "files_expected have uncommitted changes",
            "receipt_present": False,
            "evidence_exists": True,
            "evidence_path": str(evidence_path),
            "files_expected": files_expected,
            "dirty_files": sorted(dirty_files),
            "route_mode": routing_entry.get("route_mode"),
            "agent_path": routing_entry.get("agent_path"),
            "model": routing_entry.get("model"),
        }

    try:
        evidence_mtime = evidence_path.stat().st_mtime_ns
    except OSError:
        evidence_mtime = None

    newer_files: list[str] = []
    if evidence_mtime is not None:
        for rel_path in files_expected:
            path = root / rel_path
            if not path.exists():
                continue
            try:
                if path.stat().st_mtime_ns > evidence_mtime:
                    newer_files.append(rel_path)
            except OSError:
                newer_files.append(rel_path)
    if newer_files:
        return {
            "segment_id": seg_id,
            "status": "pending",
            "cache_eligible": False,
            "cache_reason": "files newer than evidence",
            "receipt_present": False,
            "evidence_exists": True,
            "evidence_path": str(evidence_path),
            "files_expected": files_expected,
            "newer_files": newer_files,
            "dirty_files": [],
            "route_mode": routing_entry.get("route_mode"),
            "agent_path": routing_entry.get("agent_path"),
            "model": routing_entry.get("model"),
        }

    return {
        "segment_id": seg_id,
        "status": "cached",
        "cache_eligible": True,
        "cache_reason": "evidence present and inputs unchanged",
        "receipt_present": False,
        "evidence_exists": True,
        "evidence_path": str(evidence_path),
        "files_expected": files_expected,
        "dirty_files": [],
        "route_mode": routing_entry.get("route_mode"),
        "agent_path": routing_entry.get("agent_path"),
        "model": routing_entry.get("model"),
    }


def _compute_execution_batch_payload(task_dir: Path) -> dict:
    manifest = _load_manifest(task_dir)
    stage = manifest.get("stage")
    if stage != "EXECUTION":
        raise ValueError(f"unexpected stage for execution batch planning: {stage}")

    root = _root_for_task_dir(task_dir)
    graph_segments = _load_graph_segments(task_dir)
    routing_segments = _load_routing_segments(task_dir)
    if not graph_segments:
        raise ValueError("execution graph missing or empty")
    if not routing_segments:
        raise ValueError("executor-routing receipt missing or empty")

    specs_fresh = plan_validated_receipt_matches(task_dir)
    depths = _dependency_depths(graph_segments)
    routing_by_id = {
        str(entry.get("segment_id")): entry
        for entry in routing_segments
        if isinstance(entry.get("segment_id"), str)
    }

    states: dict[str, dict] = {}
    for segment in graph_segments:
        seg_id = str(segment.get("id", ""))
        routing_entry = routing_by_id.get(seg_id, {})
        files_expected = [
            str(path)
            for path in (segment.get("files_expected", []) or [])
            if isinstance(path, str) and path
        ]
        dirty_files = _git_dirty_files(root, files_expected)
        state = _segment_runtime_state(
            task_dir,
            root,
            segment,
            routing_entry,
            specs_fresh,
            dirty_files,
        )
        state["depends_on"] = [
            str(dep)
            for dep in (segment.get("depends_on", []) or [])
            if isinstance(dep, str)
        ]
        state["depth"] = depths.get(seg_id, 0)
        state["parallelizable"] = bool(segment.get("parallelizable", False))
        state["executor"] = segment.get("executor")
        states[seg_id] = state

    satisfied = {
        seg_id
        for seg_id, state in states.items()
        if state.get("status") in {"completed", "cached"}
    }
    pending = {
        seg_id
        for seg_id, state in states.items()
        if state.get("status") == "pending"
    }

    batches: list[list[dict]] = []
    simulated_done = set(satisfied)
    remaining = set(pending)
    while remaining:
        runnable = [
            seg_id
            for seg_id in remaining
            if all(dep in simulated_done for dep in states[seg_id].get("depends_on", []))
        ]
        if not runnable:
            break
        runnable.sort(key=lambda seg_id: (-int(states[seg_id].get("depth", 0)), seg_id))
        batch = [states[seg_id] for seg_id in runnable]
        batches.append(batch)
        simulated_done.update(runnable)
        remaining.difference_update(runnable)

    critical_depth = max((int(state.get("depth", 0)) for state in states.values()), default=0)
    critical_path_segments = sorted(
        [seg_id for seg_id, state in states.items() if int(state.get("depth", 0)) == critical_depth]
    )
    return {
        "status": "execution_batch_plan_ready",
        "task_dir": str(task_dir),
        "specs_fresh": specs_fresh is True,
        "specs_fresh_detail": None if specs_fresh is True else specs_fresh,
        "critical_depth": critical_depth,
        "critical_path_segments": critical_path_segments,
        "completed_segments": sorted(seg_id for seg_id, state in states.items() if state["status"] == "completed"),
        "cached_segments": sorted(seg_id for seg_id, state in states.items() if state["status"] == "cached"),
        "pending_segments": sorted(seg_id for seg_id, state in states.items() if state["status"] == "pending"),
        "next_batch": batches[0] if batches else [],
        "remaining_batches": batches,
        "segments": [states[str(seg.get("id"))] for seg in graph_segments if str(seg.get("id")) in states],
    }


def cmd_run_planning(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir).resolve()
    try:
        errors = validate_task_artifacts(task_dir, strict=True, run_gap=True)
        if errors:
            print(json.dumps({
                "status": "replan_required",
                "task_dir": str(task_dir),
                "errors": errors,
            }, indent=2))
            return 1

        receipt_path = receipt_plan_validated(task_dir, run_gap=True)
        transitioned_to = None
        stage = _load_manifest(task_dir).get("stage")
        if stage == "PLANNING":
            _, manifest = transition_task(task_dir, "PLAN_REVIEW")
            transitioned_to = manifest.get("stage")

        print(json.dumps({
            "status": "plan_review_ready",
            "task_dir": str(task_dir),
            "receipt_path": str(receipt_path),
            "transitioned_to": transitioned_to,
            "next_action": "human_plan_review",
        }, indent=2))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


def cmd_run_plan_audit(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir).resolve()
    root = _root_for_task_dir(task_dir)
    blocked = _refuse_if_rules_corrupt(root)
    if blocked is not None:
        return blocked
    try:
        from plan_gap_analysis import run_gap_analysis

        gap_report = run_gap_analysis(root, task_dir)
        gap_errors: list[str] = []
        if gap_report.get("error"):
            gap_errors.append(str(gap_report["error"]))
        api = gap_report.get("api_contracts")
        if isinstance(api, dict):
            for item in api.get("unverified", []) or []:
                if isinstance(item, dict):
                    gap_errors.append(f"unverified API contract: {item.get('method', '').strip()} {item.get('endpoint', '').strip()}".strip())
        data_model = gap_report.get("data_model")
        if isinstance(data_model, dict):
            for item in data_model.get("unverified", []) or []:
                gap_errors.append(f"unverified data model entry: {item}")

        if gap_errors:
            print(json.dumps({
                "status": "replan_required",
                "task_dir": str(task_dir),
                "mode": "deterministic_gap_analysis",
                "errors": gap_errors,
                "gap_report": gap_report,
            }, indent=2))
            return 1

        # Signature-check (task-20260501-001): verify spec AC signature claims match plan.
        _sig_script = Path(__file__).parent / "plan_signature_check.py"
        if _sig_script.exists():
            sig_result = subprocess.run(
                [sys.executable, str(_sig_script), "--root", str(root), "--task-dir", str(task_dir)],
                capture_output=True, text=True,
            )
            if sig_result.returncode != 0:
                print(json.dumps({
                    "status": "plan_audit_failed",
                    "task_dir": str(task_dir),
                    "error": "signature-check script non-zero exit",
                    "check_output": sig_result.stderr or sig_result.stdout,
                }, indent=2))
                return 1
            try:
                sig_payload = json.loads(sig_result.stdout)
            except json.JSONDecodeError:
                print(json.dumps({
                    "status": "ctl_internal_error",
                    "task_dir": str(task_dir),
                    "error": "plan_signature_check.py stdout is not valid JSON",
                }, indent=2))
                return 1
            if sig_payload.get("findings"):
                print(json.dumps({
                    "status": "plan_audit_failed",
                    "task_dir": str(task_dir),
                    "error": "signature mismatch found",
                    "findings": sig_payload["findings"],
                }, indent=2))
                return 1

        # Run intermediate-state pipeline check (task-003 PRO-006 capture).
        # Bootstrap: tolerate the script not existing yet; when it exists, failures block.
        _check_script = Path(__file__).parent / "plan_intermediate_state_check.py"
        if _check_script.exists():
            check_result = subprocess.run(
                [sys.executable, str(_check_script), "--root", str(root), "--task-dir", str(task_dir)],
                capture_output=True, text=True,
            )
            if check_result.returncode != 0:
                try:
                    check_payload = json.loads(check_result.stdout)
                except json.JSONDecodeError:
                    check_payload = {"status": "blocked", "error": check_result.stderr or check_result.stdout}
                print(json.dumps({
                    "status": "plan_audit_failed",
                    "task_dir": str(task_dir),
                    "error": "intermediate-state check blocked",
                    "check_output": check_payload,
                }, indent=2))
                return 1

        risk = _risk_level_for_task(task_dir)
        if risk not in {"high", "critical"}:
            # Auto-advance to TDD_REVIEW when tdd_required=true. This fires
            # deterministically — the executor cannot skip it by reading prose.
            manifest = _load_manifest(task_dir)
            transitioned_to = None
            if get_tdd_required(manifest) and manifest.get("stage") == "PLAN_AUDIT":
                _, updated = transition_task(task_dir, "TDD_REVIEW")
                transitioned_to = updated.get("stage")
            print(json.dumps({
                "status": "passed",
                "task_dir": str(task_dir),
                "mode": "deterministic_only",
                "risk_level": risk,
                "llm_audit_required": False,
                "tdd_required": get_tdd_required(manifest),
                "transitioned_to": transitioned_to,
                "gap_report": gap_report,
            }, indent=2))
            return 0

        if not args.report_path:
            print(json.dumps({
                "status": "llm_audit_required",
                "task_dir": str(task_dir),
                "mode": "llm_required",
                "risk_level": risk,
                "llm_audit_required": True,
                "gap_report": gap_report,
            }, indent=2))
            return 0

        report_path = Path(args.report_path).resolve()
        receipt_path = receipt_plan_audit(
            task_dir,
            tokens_used=args.tokens_used,
            model_used=args.model,
        )
        finding_count, blocking_count = _parse_audit_report(report_path, root=_root_for_task_dir(task_dir))
        # Auto-advance to TDD_REVIEW when tdd_required=true and audit passed.
        transitioned_to = None
        manifest = _load_manifest(task_dir)
        if finding_count == 0 and get_tdd_required(manifest) and manifest.get("stage") == "PLAN_AUDIT":
            _, updated = transition_task(task_dir, "TDD_REVIEW")
            transitioned_to = updated.get("stage")
        status = "passed" if finding_count == 0 else "replan_required"
        payload = {
            "status": status,
            "task_dir": str(task_dir),
            "mode": "llm_required",
            "risk_level": risk,
            "llm_audit_required": True,
            "report_path": str(report_path),
            "receipt_path": str(receipt_path),
            "finding_count": finding_count,
            "blocking_count": blocking_count,
            "tdd_required": get_tdd_required(manifest),
            "transitioned_to": transitioned_to,
            "gap_report": gap_report,
        }
        print(json.dumps(payload, indent=2))
        return 0 if finding_count == 0 else 1
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


def cmd_run_start_classification(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir).resolve()
    root = _root_for_task_dir(task_dir)
    blocked = _refuse_if_rules_corrupt(root)
    if blocked is not None:
        return blocked
    try:
        manifest = _load_manifest(task_dir)
        errors = []
        classification = manifest.get("classification")
        if not isinstance(classification, dict):
            errors.append("classification missing or not an object")
        else:
            ctype = classification.get("type")
            if ctype not in {"feature", "bugfix", "refactor", "migration", "ml", "full-stack"}:
                errors.append(f"classification.type invalid: {ctype!r}")
            risk = classification.get("risk_level")
            if risk not in {"low", "medium", "high", "critical"}:
                errors.append(f"classification.risk_level invalid: {risk!r}")
            domains = classification.get("domains")
            if not isinstance(domains, list) or not domains:
                errors.append("classification.domains must be a non-empty array")

        if errors:
            print(json.dumps({
                "status": "reclassify_required",
                "task_dir": str(task_dir),
                "errors": errors,
            }, indent=2))
            return 1

        fast_track = apply_fast_track(task_dir)
        manifest = _load_manifest(task_dir)
        current_stage = manifest.get("stage")
        transitioned_to = None
        if current_stage in {"FOUNDRY_INITIALIZED", "CLASSIFY_AND_SPEC"}:
            _, updated = transition_task(task_dir, "SPEC_NORMALIZATION")
            transitioned_to = updated.get("stage")
            manifest = updated
        elif current_stage != "SPEC_NORMALIZATION":
            print(json.dumps({
                "status": "blocked",
                "task_dir": str(task_dir),
                "error": f"unexpected stage for classification finalization: {current_stage}",
            }, indent=2))
            return 1

        classification = manifest.get("classification") if isinstance(manifest, dict) else {}
        tdd_required = None
        if isinstance(classification, dict):
            tdd_required = classification.get("tdd_required")

        print(json.dumps({
            "status": "spec_normalization_ready",
            "task_dir": str(task_dir),
            "fast_track": bool(fast_track),
            "tdd_required": tdd_required,
            "transitioned_to": transitioned_to,
            "next_action": "spec_normalization",
        }, indent=2))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


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


def cmd_write_search_receipt(args: argparse.Namespace) -> int:
    """Write a search-conducted receipt after the executor performs research.

    Called by the executor after conducting external research in response to
    ``external-solution-gate.json`` recommending a search.  ``run-spec-ready``
    asserts this receipt before advancing to SPEC_REVIEW when the gate wrote
    ``search_recommended: true``.
    """
    task_dir = Path(args.task_dir).resolve()
    query = (args.query or "").strip()
    if not query:
        print(json.dumps({
            "status": "error",
            "error": "--query must be a non-empty string describing the search conducted",
        }, indent=2), file=sys.stderr)
        return 1
    try:
        receipt_path = receipt_search_conducted(task_dir, query=query)
        print(json.dumps({
            "status": "search_receipt_written",
            "task_dir": str(task_dir),
            "receipt_path": str(receipt_path),
            "query": query,
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


def _try_extend_chain_for_artifact(task_dir: Path, file_path: Path) -> None:
    """Best-effort task-receipt-chain extension for an artifact write.
    Failures log task_receipt_chain_extension_failed and never propagate.
    """
    try:
        from lib_chain import extend_chain_for_artifact
        extend_chain_for_artifact(task_dir, file_path)
    except Exception as exc:
        try:
            from lib_log import log_event
            log_event(
                task_dir.parent.parent,
                "task_receipt_chain_extension_failed",
                task=task_dir.name, file=str(file_path), error=str(exc),
            )
        except Exception:
            pass


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
    _try_extend_chain_for_artifact(task_dir, out_path)
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
    _try_extend_chain_for_artifact(task_dir, out_path)
    print(json.dumps({"status": "classification_written", "path": str(out_path)}, indent=2))
    return 0


def cmd_run_spec_ready(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir).resolve()
    root = _root_for_task_dir(task_dir)
    blocked = _refuse_if_rules_corrupt(root)
    if blocked is not None:
        return blocked
    try:
        manifest = _load_manifest(task_dir)
        if bool(manifest.get("fast_track", False)):
            print(json.dumps({
                "status": "skipped",
                "task_dir": str(task_dir),
                "reason": "fast_track defers spec finalization to combined spec+plan path",
            }, indent=2))
            return 0

        # External-solution gate enforcement: if the gate wrote
        # search_recommended=true, a search-conducted receipt must exist before
        # we advance. This converts the advisory gate into a hard block —
        # the executor cannot reach SPEC_REVIEW by rationalizing around the
        # recommendation. Missing gate file is treated as not recommended
        # (cold-start / pre-gate tasks are not penalised).
        gate_path = task_dir / "external-solution-gate.json"
        if gate_path.exists():
            try:
                gate = load_json(gate_path)
            except Exception:
                gate = {}
            if gate.get("search_recommended") is True:
                receipts_dir = task_dir / "receipts"
                search_receipt = receipts_dir / "search-conducted.json"
                if not search_receipt.exists():
                    print(json.dumps({
                        "status": "search_required",
                        "task_dir": str(task_dir),
                        "error": (
                            "external-solution-gate.json has search_recommended=true "
                            "but no search-conducted receipt exists. "
                            "Conduct the search and run: "
                            "python3 hooks/ctl.py write-search-receipt "
                            f".dynos/task-{{id}} --query '<your search query>'"
                        ),
                        "gate_path": str(gate_path),
                        "missing_receipt": str(search_receipt),
                    }, indent=2), file=sys.stderr)
                    return 1

        errors = validate_task_artifacts(task_dir, strict=False, run_gap=False)
        errors = [e for e in errors if not e.startswith("plan ") and "execution-graph" not in e]
        if errors:
            print(json.dumps({
                "status": "respec_required",
                "task_dir": str(task_dir),
                "errors": errors,
            }, indent=2))
            return 1

        # Spec-lint gate (task-20260501-001): anti-pattern detection for measurement+structural co-location.
        _lint_script = Path(__file__).parent / "spec_lint.py"
        lint_result = subprocess.run(
            [sys.executable, str(_lint_script), "--spec", str(task_dir / "spec.md")],
            capture_output=True, text=True,
        )
        try:
            lint_payload = json.loads(lint_result.stdout)
        except json.JSONDecodeError:
            print(json.dumps({
                "status": "ctl_internal_error",
                "task_dir": str(task_dir),
                "error": "spec_lint.py stdout is not valid JSON",
            }, indent=2))
            return 1
        unacked_findings = [f for f in lint_payload.get("findings", []) if f not in lint_payload.get("acked", [])]
        if unacked_findings:
            print(json.dumps({
                "status": "respec_required",
                "task_dir": str(task_dir),
                "errors": [f.get("message", str(f)) for f in unacked_findings],
            }, indent=2))
            return 1

        receipt_path = receipt_spec_validated(task_dir)
        # Task-receipt-chain (task-20260503-001 AC 8): chain spec.md after
        # validation. Best-effort.
        spec_path = task_dir / "spec.md"
        if spec_path.exists():
            _try_extend_chain_for_artifact(task_dir, spec_path)

        transitioned_to = None
        stage = _load_manifest(task_dir).get("stage")
        if stage == "SPEC_NORMALIZATION":
            _, updated = transition_task(task_dir, "SPEC_REVIEW")
            transitioned_to = updated.get("stage")

        print(json.dumps({
            "status": "spec_review_ready",
            "task_dir": str(task_dir),
            "receipt_path": str(receipt_path),
            "transitioned_to": transitioned_to,
            "next_action": "human_spec_review",
        }, indent=2))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


def cmd_run_planning_mode(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir).resolve()
    try:
        manifest = _load_manifest(task_dir)
        if bool(manifest.get("fast_track", False)):
            print(json.dumps({
                "status": "planning_mode_ready",
                "task_dir": str(task_dir),
                "planning_mode": "fast_track_combined",
                "reason": "manifest.fast_track is true",
            }, indent=2))
            return 0

        classification = manifest.get("classification")
        risk_level = classification.get("risk_level") if isinstance(classification, dict) else "medium"
        from lib_validate import parse_acceptance_criteria  # noqa: PLC0415

        spec_text = (task_dir / "spec.md").read_text(encoding="utf-8")
        criteria_count = len(parse_acceptance_criteria(spec_text))
        planning_mode = "hierarchical" if risk_level in {"high", "critical"} or criteria_count > 10 else "standard"
        reasons: list[str] = []
        if risk_level in {"high", "critical"}:
            reasons.append(f"risk_level={risk_level}")
        if criteria_count > 10:
            reasons.append(f"criteria_count={criteria_count}")
        if not reasons:
            reasons.append(f"criteria_count={criteria_count}")

        print(json.dumps({
            "status": "planning_mode_ready",
            "task_dir": str(task_dir),
            "planning_mode": planning_mode,
            "risk_level": risk_level,
            "criteria_count": criteria_count,
            "reason": ", ".join(reasons),
        }, indent=2))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


def cmd_run_audit_setup(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir).resolve()
    try:
        manifest = _load_manifest(task_dir)
        stage = manifest.get("stage")
        if stage != "CHECKPOINT_AUDIT":
            print(json.dumps({
                "status": "blocked",
                "task_dir": str(task_dir),
                "error": f"unexpected stage for audit setup: {stage}",
            }, indent=2))
            return 1

        classification = manifest.get("classification")
        if not isinstance(classification, dict):
            print(json.dumps({
                "status": "blocked",
                "task_dir": str(task_dir),
                "error": "classification missing or not an object",
            }, indent=2))
            return 1

        task_type = classification.get("type", "feature")
        domains = classification.get("domains", [])
        if not isinstance(domains, list):
            domains = []
        fast_track = bool(manifest.get("fast_track", False))
        risk_level = str(classification.get("risk_level", "medium"))
        derived_task_id = task_dir.name if task_dir.name.startswith("task-") else ""

        from router import build_audit_plan  # noqa: PLC0415

        root = _root_for_task_dir(task_dir)

        # Compute diff scope BEFORE building the audit plan so the
        # claude-md-auditor risk gate can decide based on actual diff
        # contents (task-20260430-007). When diff_base is unavailable or
        # git diff fails, diff_files stays empty and we pass None to
        # build_audit_plan to fail-open: the auditor still runs.
        snapshot = manifest.get("snapshot")
        head_sha = snapshot.get("head_sha") if isinstance(snapshot, dict) else None

        diff_base = None
        if isinstance(head_sha, str) and head_sha.strip():
            diff_base = head_sha.strip()
        elif args.allow_head_fallback:
            diff_base = "HEAD"

        diff_files: list[str] = []
        diff_error = None
        if diff_base is not None:
            import subprocess as _subprocess

            result = _subprocess.run(
                ["git", "diff", "--name-only", diff_base],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode == 0:
                diff_files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            else:
                diff_error = (result.stderr or result.stdout or f"git diff failed with {result.returncode}").strip()

        plan = build_audit_plan(
            root,
            str(task_type),
            [str(d) for d in domains],
            fast_track=fast_track,
            risk_level=risk_level,
            task_id=derived_task_id,
            diff_files=diff_files if (diff_base is not None and diff_error is None) else None,
        )
        audit_plan_path = task_dir / "audit-plan.json"
        audit_plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")

        print(json.dumps({
            "status": "audit_setup_ready",
            "task_dir": str(task_dir),
            "audit_plan_path": str(audit_plan_path),
            "task_type": task_type,
            "domains": domains,
            "fast_track": fast_track,
            "diff_base": diff_base,
            "diff_files": diff_files,
            "diff_error": diff_error,
            "spawn_auditors": [a for a in plan.get("auditors", []) if isinstance(a, dict) and a.get("action") == "spawn"],
            "skip_auditors": [a for a in plan.get("auditors", []) if isinstance(a, dict) and a.get("action") == "skip"],
        }, indent=2))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


def cmd_run_audit_findings_gate(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir).resolve()
    try:
        reports: list[dict] = []
        blocking_findings: list[dict] = []
        critical_spec_findings: list[dict] = []
        findings_by_auditor: dict[str, dict[str, int]] = {}

        audit_dir = task_dir / "audit-reports"
        for report_path in sorted(audit_dir.glob("*.json")):
            data = load_json(report_path)
            findings = data.get("findings", []) if isinstance(data, dict) else []
            if not isinstance(findings, list):
                findings = []
            auditor_name = str(data.get("auditor_name") or data.get("auditor") or report_path.stem)
            total = 0
            blocking = 0
            for finding in findings:
                if not isinstance(finding, dict):
                    continue
                total += 1
                if bool(finding.get("blocking")):
                    blocking += 1
                    entry = dict(finding)
                    entry["_auditor"] = auditor_name
                    blocking_findings.append(entry)
                    if auditor_name == "spec-completion-auditor" and str(entry.get("severity", "")).lower() == "critical":
                        critical_spec_findings.append(entry)
            findings_by_auditor[auditor_name] = {"finding_count": total, "blocking_count": blocking}
            reports.append({
                "auditor_name": auditor_name,
                "report_path": str(report_path),
                "finding_count": total,
                "blocking_count": blocking,
            })

        next_action = "repair_phase_1" if blocking_findings else "reflect"
        status = "repair_required" if blocking_findings else "clear"
        print(json.dumps({
            "status": status,
            "task_dir": str(task_dir),
            "reports_seen": reports,
            "findings_by_auditor": findings_by_auditor,
            "blocking_findings": blocking_findings,
            "blocking_finding_ids": [str(f.get("id", "")) for f in blocking_findings if f.get("id")],
            "critical_spec_failure": bool(critical_spec_findings),
            "critical_spec_finding_ids": [str(f.get("id", "")) for f in critical_spec_findings if f.get("id")],
            "next_action": next_action,
        }, indent=2))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


def cmd_run_audit_repair_cycle_plan(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir).resolve()
    try:
        manifest = _load_manifest(task_dir)
        stage = str(manifest.get("stage", ""))
        if stage not in {"CHECKPOINT_AUDIT", "FINAL_AUDIT", "REPAIR_PLANNING"}:
            print(json.dumps({
                "status": "blocked",
                "task_dir": str(task_dir),
                "error": f"unexpected stage for repair cycle planning: {stage}",
            }, indent=2))
            return 1

        reports: list[dict] = []
        blocking_findings: list[dict] = []
        critical_spec_finding_ids: set[str] = set()
        audit_dir = task_dir / "audit-reports"
        for report_path in sorted(audit_dir.glob("*.json")):
            data = load_json(report_path)
            findings = data.get("findings", []) if isinstance(data, dict) else []
            if not isinstance(findings, list):
                findings = []
            auditor_name = str(data.get("auditor_name") or data.get("auditor") or report_path.stem)
            reports.append({
                "auditor_name": auditor_name,
                "report_path": str(report_path),
                "finding_count": len([f for f in findings if isinstance(f, dict)]),
                "blocking_count": len([f for f in findings if isinstance(f, dict) and bool(f.get("blocking"))]),
            })
            for finding in findings:
                if not isinstance(finding, dict) or not bool(finding.get("blocking")):
                    continue
                entry = dict(finding)
                entry["_auditor"] = auditor_name
                fid = str(entry.get("id", "") or "")
                if auditor_name == "spec-completion-auditor" and str(entry.get("severity", "")).lower() == "critical" and fid:
                    critical_spec_finding_ids.add(fid)
                blocking_findings.append(entry)

        if not blocking_findings:
            payload = {
                "status": "clear",
                "task_dir": str(task_dir),
                "stage": stage,
                "reports_seen": reports,
                "repair_cycle": 0,
                "phase": None,
                "blocking_findings": [],
                "blocking_finding_ids": [],
                "next_action": "reflect",
            }
            write_json(task_dir / "repair-cycle-plan.json", payload)
            print(json.dumps(payload, indent=2))
            return 0

        prior_cycle = 0
        prior_retry_counts: dict[str, int] = {}
        repair_log_path = task_dir / "repair-log.json"
        if repair_log_path.exists():
            repair_log = load_json(repair_log_path)
            if isinstance(repair_log, dict):
                try:
                    prior_cycle = int(repair_log.get("repair_cycle", 0) or 0)
                except (TypeError, ValueError):
                    prior_cycle = 0
                for batch in repair_log.get("batches", []) or []:
                    if not isinstance(batch, dict):
                        continue
                    for task in batch.get("tasks", []) or []:
                        if not isinstance(task, dict):
                            continue
                        fid = task.get("finding_id")
                        if not isinstance(fid, str) or not fid:
                            continue
                        try:
                            retry_count = int(task.get("retry_count", 0) or 0)
                        except (TypeError, ValueError):
                            retry_count = 0
                        prior_retry_counts[fid] = max(prior_retry_counts.get(fid, retry_count), retry_count)

        next_cycle = max(1, prior_cycle + 1)
        if next_cycle == 1:
            phase = "phase_1"
        elif next_cycle == 2:
            phase = "phase_2"
        else:
            phase = f"repair_cycle_{next_cycle}"

        ordered_findings = sorted(
            blocking_findings,
            key=lambda finding: (
                0 if str(finding.get("id", "") or "") in critical_spec_finding_ids else 1,
                str(finding.get("_auditor", "")),
                str(finding.get("id", "")),
            ),
        )

        planned_findings: list[dict] = []
        model_overrides: dict[str, str] = {}
        for finding in ordered_findings:
            entry = dict(finding)
            fid = str(entry.get("id", "") or "")
            previous_retry = prior_retry_counts.get(fid)
            retry_count = previous_retry + 1 if previous_retry is not None else 0
            entry["retry_count"] = retry_count
            entry["repair_cycle"] = next_cycle
            entry["phase"] = phase
            entry["priority"] = "critical_spec" if fid in critical_spec_finding_ids else "normal"
            if retry_count >= 2:
                entry["model_override"] = "opus"
                model_overrides[fid] = "opus"
            planned_findings.append(entry)

        updated_manifest = manifest
        transitioned = False
        if stage in {"CHECKPOINT_AUDIT", "FINAL_AUDIT"}:
            _, updated_manifest = transition_task(task_dir, "REPAIR_PLANNING")
            transitioned = True

        payload = {
            "status": "repair_cycle_ready",
            "task_dir": str(task_dir),
            "stage": updated_manifest.get("stage"),
            "transitioned_to_repair_planning": transitioned,
            "reports_seen": reports,
            "prior_repair_cycle": prior_cycle,
            "repair_cycle": next_cycle,
            "phase": phase,
            "blocking_findings": planned_findings,
            "blocking_finding_ids": [str(f.get("id", "")) for f in planned_findings if f.get("id")],
            "critical_spec_finding_ids": sorted(critical_spec_finding_ids),
            "model_overrides": model_overrides,
            "next_action": "spawn_repair_coordinator",
        }
        write_json(task_dir / "repair-cycle-plan.json", payload)
        print(json.dumps(payload, indent=2))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


def _repair_files_for_finding(
    task_dir: Path,
    finding: dict,
    assigned_executor: str | None,
) -> list[str]:
    paths: list[str] = []
    direct = finding.get("file")
    if isinstance(direct, str) and direct:
        paths.append(direct)
    evidence = finding.get("evidence")
    if isinstance(evidence, dict):
        evidence_file = evidence.get("file")
        if isinstance(evidence_file, str) and evidence_file:
            paths.append(evidence_file)
    if paths:
        deduped: list[str] = []
        seen: set[str] = set()
        for path in paths:
            if path not in seen:
                seen.add(path)
                deduped.append(path)
        return deduped

    graph_segments = _load_graph_segments(task_dir)
    if assigned_executor:
        executor_paths: list[str] = []
        for segment in graph_segments:
            if segment.get("executor") != assigned_executor:
                continue
            for path in segment.get("files_expected", []) or []:
                if isinstance(path, str) and path:
                    executor_paths.append(path)
        if executor_paths:
            deduped: list[str] = []
            seen: set[str] = set()
            for path in executor_paths:
                if path not in seen:
                    seen.add(path)
                    deduped.append(path)
            return deduped

    fallback_paths: list[str] = []
    for segment in graph_segments:
        for path in segment.get("files_expected", []) or []:
            if isinstance(path, str) and path:
                fallback_paths.append(path)
    deduped: list[str] = []
    seen: set[str] = set()
    for path in fallback_paths:
        if path not in seen:
            seen.add(path)
            deduped.append(path)
    return deduped


def cmd_run_repair_log_build(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir).resolve()
    root = _root_for_task_dir(task_dir)
    blocked = _refuse_if_rules_corrupt(root)
    if blocked is not None:
        return blocked
    try:
        manifest = _load_manifest(task_dir)
        stage = str(manifest.get("stage", ""))
        if stage != "REPAIR_PLANNING":
            print(json.dumps({
                "status": "blocked",
                "task_dir": str(task_dir),
                "error": f"unexpected stage for repair-log build: {stage}",
            }, indent=2))
            return 1

        cycle_plan_path = task_dir / "repair-cycle-plan.json"
        if not cycle_plan_path.exists():
            print(json.dumps({
                "status": "blocked",
                "task_dir": str(task_dir),
                "error": f"missing repair-cycle-plan: {cycle_plan_path}",
            }, indent=2))
            return 1

        cycle_plan = load_json(cycle_plan_path)
        findings = cycle_plan.get("blocking_findings", []) if isinstance(cycle_plan, dict) else []
        if not isinstance(findings, list) or not findings:
            print(json.dumps({
                "status": "blocked",
                "task_dir": str(task_dir),
                "error": "repair-cycle-plan has no blocking findings",
            }, indent=2))
            return 1

        task_type = str((manifest.get("classification") or {}).get("type", "feature"))
        root = _root_for_task_dir(task_dir)
        from lib_qlearn import build_repair_plan  # noqa: PLC0415

        q_plan = build_repair_plan(root, findings, task_type)
        assignments = q_plan.get("assignments", []) if isinstance(q_plan, dict) else []
        by_finding_id = {
            str(entry.get("finding_id")): entry
            for entry in assignments
            if isinstance(entry, dict) and isinstance(entry.get("finding_id"), str)
        }
        live_findings: dict[str, dict[str, str]] = {}
        audit_dir = task_dir / "audit-reports"
        if audit_dir.is_dir():
            for report_path in sorted(audit_dir.glob("*.json")):
                try:
                    data = load_json(report_path)
                except Exception:
                    continue
                auditor_name = str(data.get("auditor_name") or data.get("auditor") or report_path.stem)
                findings_list = data.get("findings", []) if isinstance(data, dict) else []
                if not isinstance(findings_list, list):
                    continue
                for entry in findings_list:
                    if not isinstance(entry, dict):
                        continue
                    finding_id = str(entry.get("id", "") or "")
                    if not finding_id:
                        continue
                    live_findings[finding_id] = {
                        "auditor": auditor_name,
                        "severity": str(entry.get("severity", "") or ""),
                    }

        normalized_tasks: list[dict] = []
        unresolved_files: list[str] = []
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            finding_id = str(finding.get("id", "") or "")
            if not finding_id:
                continue
            assignment = by_finding_id.get(finding_id, {})
            assigned_executor = assignment.get("assigned_executor") or finding.get("assigned_executor")
            if not isinstance(assigned_executor, str) or not assigned_executor:
                category = finding_id.split("-")[0].lower() if "-" in finding_id else ""
                if category == "db":
                    assigned_executor = "db-executor"
                elif category == "dc":
                    assigned_executor = "refactor-executor"
                elif category == "ui":
                    assigned_executor = "ui-executor"
                elif category == "sec":
                    assigned_executor = "backend-executor"
                else:
                    assigned_executor = "backend-executor"
            files_to_modify = _repair_files_for_finding(task_dir, finding, assigned_executor)
            if not files_to_modify:
                unresolved_files.append(finding_id)
                continue
            live = live_findings.get(finding_id, {})
            auditor = (
                finding.get("auditor")
                or finding.get("_auditor")
                or finding.get("auditor_name")
                or live.get("auditor")
            )
            description = str(finding.get("description") or finding.get("title") or finding_id)
            task = {
                "finding_id": finding_id,
                "auditor": str(auditor or "unknown"),
                "assigned_executor": assigned_executor,
                "instruction": f"Resolve finding {finding_id}: {description}",
                "files_to_modify": files_to_modify,
                "retry_count": int(finding.get("retry_count", 0) or 0),
                "severity": str(finding.get("severity") or live.get("severity") or "medium"),
                "state": assignment.get("state"),
            }
            for key in ("route_mode", "route_source", "agent_path", "agent_name", "model_override", "model_source"):
                value = assignment.get(key)
                if value is not None:
                    task[key] = value
            normalized_tasks.append(task)

        if unresolved_files:
            print(json.dumps({
                "status": "blocked",
                "task_dir": str(task_dir),
                "error": "unable to derive files_to_modify for all findings",
                "finding_ids": unresolved_files,
            }, indent=2))
            return 1

        groups: list[dict] = []
        for task in normalized_tasks:
            task_files = set(task["files_to_modify"])
            placed = False
            for group in groups:
                if task_files & group["files"]:
                    continue
                group["tasks"].append(task)
                group["files"].update(task_files)
                placed = True
                break
            if not placed:
                groups.append({
                    "tasks": [task],
                    "files": set(task_files),
                })

        batches: list[dict] = []
        for idx, group in enumerate(groups, start=1):
            batches.append({
                "batch_id": f"batch-{idx}",
                "parallel": len(group["tasks"]) > 1,
                "tasks": group["tasks"],
            })

        repair_log = {
            "repair_cycle": int(cycle_plan.get("repair_cycle", 0) or 0),
            "phase": cycle_plan.get("phase"),
            "source": "deterministic_ctl",
            "q_learning_source": q_plan.get("source", "default") if isinstance(q_plan, dict) else "default",
            "batches": batches,
        }
        repair_log_path = task_dir / "repair-log.json"
        write_json(repair_log_path, repair_log)

        from lib_validate import validate_repair_log  # noqa: PLC0415
        errors = validate_repair_log(task_dir)
        if errors:
            print(json.dumps({
                "status": "repair_log_invalid",
                "task_dir": str(task_dir),
                "errors": errors,
            }, indent=2))
            return 1

        print(json.dumps({
            "status": "repair_log_built",
            "task_dir": str(task_dir),
            "repair_log_path": str(repair_log_path),
            "repair_cycle": repair_log["repair_cycle"],
            "phase": repair_log["phase"],
            "batch_count": len(batches),
            "task_count": len(normalized_tasks),
            "q_learning_source": repair_log["q_learning_source"],
        }, indent=2))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


def cmd_run_audit_reaudit_plan(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir).resolve()
    try:
        from lib_validate import validate_repair_log  # noqa: PLC0415

        errors = validate_repair_log(task_dir)
        if errors:
            print(json.dumps({
                "status": "repair_log_invalid",
                "task_dir": str(task_dir),
                "errors": errors,
            }, indent=2))
            return 1

        repair_log = load_json(task_dir / "repair-log.json")
        batches = repair_log.get("batches", []) if isinstance(repair_log, dict) else []
        modified_files: set[str] = set()
        repaired_finding_ids: set[str] = set()
        for batch in batches if isinstance(batches, list) else []:
            if not isinstance(batch, dict):
                continue
            for task in batch.get("tasks", []) or []:
                if not isinstance(task, dict):
                    continue
                fid = task.get("finding_id")
                if isinstance(fid, str) and fid:
                    repaired_finding_ids.add(fid)
                for path in task.get("files_to_modify", []) or []:
                    if isinstance(path, str) and path:
                        modified_files.add(path)

        root = _root_for_task_dir(task_dir)
        manifest = _load_manifest(task_dir)
        snapshot = manifest.get("snapshot")
        head_sha = snapshot.get("head_sha") if isinstance(snapshot, dict) else None
        diff_base = head_sha.strip() if isinstance(head_sha, str) and head_sha.strip() else "HEAD"
        import subprocess as _subprocess

        result = _subprocess.run(
            ["git", "diff", "--name-only", diff_base],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
        diff_files: list[str] = []
        diff_error = None
        if result.returncode == 0:
            diff_files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            modified_files.update(diff_files)
        else:
            diff_error = (result.stderr or result.stdout or f"git diff failed with {result.returncode}").strip()

        auditors_to_spawn: list[str] = ["spec-completion-auditor", "security-auditor"]
        matched_auditors: set[str] = set()
        audit_dir = task_dir / "audit-reports"
        if audit_dir.exists():
            for report_path in sorted(audit_dir.glob("*.json")):
                data = load_json(report_path)
                findings = data.get("findings", []) if isinstance(data, dict) else []
                if not isinstance(findings, list):
                    continue
                auditor_name = str(data.get("auditor_name") or data.get("auditor") or report_path.stem)
                for finding in findings:
                    if not isinstance(finding, dict):
                        continue
                    fid = finding.get("id")
                    if isinstance(fid, str) and fid in repaired_finding_ids:
                        matched_auditors.add(auditor_name)
                        break
        for auditor_name in sorted(matched_auditors):
            if auditor_name not in auditors_to_spawn:
                auditors_to_spawn.append(auditor_name)

        print(json.dumps({
            "status": "reaudit_plan_ready",
            "task_dir": str(task_dir),
            "repair_cycle": repair_log.get("repair_cycle", 0) if isinstance(repair_log, dict) else 0,
            "repaired_finding_ids": sorted(repaired_finding_ids),
            "modified_files": sorted(modified_files),
            "diff_base": diff_base,
            "diff_files": diff_files,
            "diff_error": diff_error,
            "auditors_to_spawn": auditors_to_spawn,
            "full_scope_auditors": ["spec-completion-auditor"],
            "scoped_auditors": [a for a in auditors_to_spawn if a != "spec-completion-auditor"],
        }, indent=2))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


def cmd_run_audit_summary(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir).resolve()
    try:
        reports_dir = task_dir / "audit-reports"
        reports: list[dict] = []
        findings_by_auditor: dict[str, int] = {}
        findings_by_category: dict[str, int] = {}
        total_findings = 0
        total_blocking = 0

        if reports_dir.exists():
            for report_path in sorted(reports_dir.glob("*.json")):
                data = load_json(report_path)
                findings = data.get("findings", []) if isinstance(data, dict) else []
                if not isinstance(findings, list):
                    findings = []
                auditor_name = str(data.get("auditor_name") or data.get("auditor") or report_path.stem)
                finding_count = 0
                blocking_count = 0
                for finding in findings:
                    if not isinstance(finding, dict):
                        continue
                    finding_count += 1
                    if bool(finding.get("blocking")):
                        blocking_count += 1
                    fid = str(finding.get("id", "") or "")
                    category = fid.split("-")[0] if "-" in fid else fid
                    if category:
                        findings_by_category[category] = findings_by_category.get(category, 0) + 1
                findings_by_auditor[auditor_name] = findings_by_auditor.get(auditor_name, 0) + finding_count
                total_findings += finding_count
                total_blocking += blocking_count
                reports.append({
                    "auditor_name": auditor_name,
                    "report_path": str(report_path),
                    "finding_count": finding_count,
                    "blocking_count": blocking_count,
                })

        summary = {
            "task_id": _load_manifest(task_dir).get("task_id", task_dir.name),
            "reports": reports,
            "findings_by_auditor": findings_by_auditor,
            "findings_by_category": findings_by_category,
            "total_findings": total_findings,
            "total_blocking": total_blocking,
            "audit_result": "pass" if total_blocking == 0 else "fail",
        }
        # task-20260503-002: derive user_summary BEFORE persisting audit-summary.json
        # so the chain entry's sha256 captures it. cmd_run_audit_finish reads this
        # field (via load_json of audit-summary.json) and copies it verbatim.
        from lib_validate import derive_user_summary
        summary["user_summary"] = derive_user_summary(summary)
        summary_path = task_dir / "audit-summary.json"
        write_json(summary_path, summary)
        print(json.dumps({
            "status": "audit_summary_ready",
            "task_dir": str(task_dir),
            "summary_path": str(summary_path),
            **summary,
        }, indent=2))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


def cmd_run_execute_setup(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir).resolve()
    root = _root_for_task_dir(task_dir)
    blocked = _refuse_if_rules_corrupt(root)
    if blocked is not None:
        return blocked
    try:
        manifest = _load_manifest(task_dir)
        stage = manifest.get("stage")
        if stage != "PRE_EXECUTION_SNAPSHOT":
            print(json.dumps({
                "status": "blocked",
                "task_dir": str(task_dir),
                "error": f"unexpected stage for execute setup: {stage}",
            }, indent=2))
            return 1

        classification = manifest.get("classification")
        if not isinstance(classification, dict):
            print(json.dumps({
                "status": "blocked",
                "task_dir": str(task_dir),
                "error": "classification missing or not an object",
            }, indent=2))
            return 1

        task_type = str(classification.get("type", "feature"))
        graph_path = task_dir / "execution-graph.json"
        if not graph_path.exists():
            print(json.dumps({
                "status": "blocked",
                "task_dir": str(task_dir),
                "error": f"missing execution graph: {graph_path}",
            }, indent=2))
            return 1

        from router import build_executor_plan  # noqa: PLC0415

        root = _root_for_task_dir(task_dir)
        graph = load_json(graph_path)
        segments = graph.get("segments", []) if isinstance(graph, dict) else []
        validation_errors = validate_task_artifacts(task_dir, strict=True, run_gap=True)
        if validation_errors:
            print(json.dumps({
                "status": "replan_required",
                "task_dir": str(task_dir),
                "errors": validation_errors,
            }, indent=2))
            return 1

        _, updated = transition_task(task_dir, "EXECUTION")
        plan = build_executor_plan(root, task_type, segments)
        receipt_path = receipt_executor_routing(task_dir, plan.get("segments", []))
        manifest = updated if isinstance(updated, dict) else _load_manifest(task_dir)
        fast_track = bool(manifest.get("fast_track", False))

        route_segments = plan.get("segments", []) if isinstance(plan, dict) else []
        inline_allowed = (
            fast_track
            and isinstance(route_segments, list)
            and len(route_segments) == 1
            and route_segments[0].get("route_mode") == "generic"
            and route_segments[0].get("agent_path") is None
        )

        print(json.dumps({
            "status": "execution_ready",
            "task_dir": str(task_dir),
            "stage": manifest.get("stage"),
            "task_type": task_type,
            "fast_track": fast_track,
            "receipt_path": str(receipt_path),
            "segment_count": len(route_segments) if isinstance(route_segments, list) else 0,
            "inline_allowed": inline_allowed,
            "segments": route_segments,
        }, indent=2))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


def cmd_run_execution_batch_plan(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir).resolve()
    try:
        print(json.dumps(_compute_execution_batch_payload(task_dir), indent=2))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


def cmd_run_execution_segment_done(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir).resolve()
    root = _root_for_task_dir(task_dir)
    blocked = _refuse_if_rules_corrupt(root)
    if blocked is not None:
        return blocked
    try:
        manifest = _load_manifest(task_dir)
        if manifest.get("stage") != "EXECUTION":
            print(json.dumps({
                "status": "blocked",
                "task_dir": str(task_dir),
                "error": f"unexpected stage for segment finalization: {manifest.get('stage')}",
            }, indent=2))
            return 1

        graph_segments = {
            str(seg.get("id")): seg
            for seg in _load_graph_segments(task_dir)
            if isinstance(seg.get("id"), str)
        }
        segment = graph_segments.get(args.segment_id)
        if not isinstance(segment, dict):
            print(json.dumps({
                "status": "blocked",
                "task_dir": str(task_dir),
                "error": f"unknown segment_id: {args.segment_id}",
            }, indent=2))
            return 1

        evidence_path = Path(args.evidence_path).resolve() if args.evidence_path else (task_dir / "evidence" / f"{args.segment_id}.md")
        if not evidence_path.exists():
            print(json.dumps({
                "status": "segment_invalid",
                "task_dir": str(task_dir),
                "segment_id": args.segment_id,
                "error": f"missing evidence file: {evidence_path}",
            }, indent=2))
            return 1

        files_to_check = args.files or [
            str(path)
            for path in (segment.get("files_expected", []) or [])
            if isinstance(path, str) and path
        ]
        unauthorized = check_segment_ownership(task_dir, args.segment_id, files_to_check)
        if unauthorized:
            print(json.dumps({
                "status": "segment_invalid",
                "task_dir": str(task_dir),
                "segment_id": args.segment_id,
                "error": "ownership violation",
                "unauthorized_files": unauthorized,
            }, indent=2))
            return 1

        # AC 4: bypass iff BOTH no_op_justified is True (strict identity) AND
        # no_op_reason is a non-empty string of at least 20 stripped characters.
        no_op_justified_flag = segment.get("no_op_justified")
        no_op_reason = segment.get("no_op_reason")
        bypass = (
            no_op_justified_flag is True
            and isinstance(no_op_reason, str)
            and len(no_op_reason.strip()) >= 20
        )

        if bypass:
            # AC 5 bypass path: diff_verified_files=[], no_op_justified=True
            executor_type = args.executor_type or str(segment.get("executor", ""))
            receipt_path = receipt_executor_done(
                task_dir=task_dir,
                segment_id=args.segment_id,
                executor_type=executor_type,
                model_used=args.model,
                injected_prompt_sha256=args.injected_prompt_sha256,
                agent_name=args.agent_name,
                evidence_path=str(evidence_path),
                tokens_used=args.tokens_used,
                diff_verified_files=[],
                no_op_justified=True,
            )
        else:
            # AC 3: call _verify_git_diff_covers_files AFTER check_segment_ownership
            # BEFORE receipt_executor_done. snapshot_sha from manifest['snapshot']['head_sha'].
            snapshot = manifest.get("snapshot") if isinstance(manifest, dict) else None
            snapshot_sha = (
                snapshot.get("head_sha")
                if isinstance(snapshot, dict)
                else None
            )
            # AC 3: fail-closed when snapshot SHA absent — no silent bypass.
            if not snapshot_sha or not isinstance(snapshot_sha, str) or not snapshot_sha.strip():
                print(json.dumps({
                    "status": "segment_invalid",
                    "task_dir": str(task_dir),
                    "segment_id": args.segment_id,
                    "error": "snapshot_sha missing from manifest — cannot verify git diff",
                    "missing_files": [],
                }, indent=2))
                return 1
            try:
                missing_files = _verify_git_diff_covers_files(
                    root, snapshot_sha, files_to_check
                )
            except ValueError as exc:
                print(json.dumps({
                    "status": "segment_invalid",
                    "task_dir": str(task_dir),
                    "segment_id": args.segment_id,
                    "error": str(exc),
                }, indent=2))
                return 1

            if missing_files:
                print(json.dumps({
                    "status": "segment_invalid",
                    "task_dir": str(task_dir),
                    "segment_id": args.segment_id,
                    "error": "files_expected not present in git diff since snapshot",
                    "missing_files": missing_files,
                }, indent=2))
                return 1

            # AC 5 happy path: diff_verified_files=files_to_check, no_op_justified=False
            executor_type = args.executor_type or str(segment.get("executor", ""))
            receipt_path = receipt_executor_done(
                task_dir=task_dir,
                segment_id=args.segment_id,
                executor_type=executor_type,
                model_used=args.model,
                injected_prompt_sha256=args.injected_prompt_sha256,
                agent_name=args.agent_name,
                evidence_path=str(evidence_path),
                tokens_used=args.tokens_used,
                diff_verified_files=files_to_check,
                no_op_justified=bool(segment.get("no_op_justified")),
            )

        batch_payload = _compute_execution_batch_payload(task_dir)
        manifest = _load_manifest(task_dir)
        manifest["execution_progress"] = {
            "completed_segments": batch_payload["completed_segments"],
            "cached_segments": batch_payload["cached_segments"],
            "pending_segments": batch_payload["pending_segments"],
            "next_batch": [entry.get("segment_id") for entry in batch_payload["next_batch"] if isinstance(entry, dict)],
            "updated_at": now_iso(),
        }
        _write_ctl_json(task_dir, task_dir / "manifest.json", manifest)

        print(json.dumps({
            "status": "segment_finalized",
            "task_dir": str(task_dir),
            "segment_id": args.segment_id,
            "receipt_path": str(receipt_path),
            "execution_progress": manifest["execution_progress"],
        }, indent=2))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


def cmd_run_rules_check(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir).resolve()
    mode = args.mode
    try:
        from lib_receipts import receipt_rules_check_passed
        receipt_path = receipt_rules_check_passed(task_dir, mode)
        print(json.dumps({
            "status": "rules_check_passed",
            "task_dir": str(task_dir),
            "receipt_path": str(receipt_path),
            "mode": mode,
        }, indent=2))
        return 0
    except ValueError as exc:
        print(json.dumps({
            "status": "rules_check_failed",
            "task_dir": str(task_dir),
            "error": str(exc),
        }, indent=2), file=sys.stderr)
        return 1
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


def cmd_run_execution_finish(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir).resolve()
    root = _root_for_task_dir(task_dir)
    blocked = _refuse_if_rules_corrupt(root)
    if blocked is not None:
        return blocked
    try:
        payload = _compute_execution_batch_payload(task_dir)
        if payload["pending_segments"]:
            print(json.dumps({
                "status": "blocked",
                "task_dir": str(task_dir),
                "error": "execution still has pending segments",
                "pending_segments": payload["pending_segments"],
                "next_batch": [entry.get("segment_id") for entry in payload["next_batch"] if isinstance(entry, dict)],
            }, indent=2))
            return 1

        _, manifest = transition_task(task_dir, "TEST_EXECUTION")
        print(json.dumps({
            "status": "test_execution_ready",
            "task_dir": str(task_dir),
            "stage": manifest.get("stage"),
            "completed_segments": payload["completed_segments"],
            "cached_segments": payload["cached_segments"],
        }, indent=2))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


def cmd_run_execution_verify_evidence(args: argparse.Namespace) -> int:
    """Deterministically verify evidence files and files_expected before Step 5 completion.

    Checks:
    1. Each non-cached segment has a non-empty evidence file at evidence/{seg-id}.md.
    2. Each segment's files_expected entries exist on disk.

    Exits non-zero and prints a JSON error payload if any check fails.
    This replaces the model's narrative completion judgment in execute Step 5.
    """
    task_dir = Path(args.task_dir).resolve()
    root = _root_for_task_dir(task_dir)
    try:
        graph_path = task_dir / "execution-graph.json"
        if not graph_path.exists():
            print(json.dumps({
                "status": "blocked",
                "task_dir": str(task_dir),
                "error": "execution-graph.json not found",
            }, indent=2))
            return 1

        graph = load_json(graph_path)
        segments = graph.get("segments", []) if isinstance(graph, dict) else []
        if not isinstance(segments, list):
            segments = []

        batch_payload = _compute_execution_batch_payload(task_dir)
        cached_ids = set(batch_payload.get("cached_segments", []))

        errors: list[str] = []
        verified: list[str] = []

        for seg in segments:
            if not isinstance(seg, dict):
                continue
            seg_id = str(seg.get("id", "") or "")
            if not seg_id:
                continue
            if seg_id in cached_ids:
                verified.append(f"{seg_id}: cached (evidence reused)")
                continue

            # Check evidence file.
            evidence_path = task_dir / "evidence" / f"{seg_id}.md"
            if not evidence_path.exists():
                errors.append(f"{seg_id}: evidence file missing at {evidence_path}")
                continue
            content = evidence_path.read_text(errors="ignore").strip()
            if not content:
                errors.append(f"{seg_id}: evidence file is empty at {evidence_path}")
                continue

            # Check files_expected exist on disk.
            for expected_file in (seg.get("files_expected") or []):
                if not isinstance(expected_file, str) or not expected_file.strip():
                    continue
                fpath = root / expected_file.strip()
                if not fpath.exists():
                    errors.append(f"{seg_id}: files_expected entry missing on disk: {expected_file}")

            verified.append(f"{seg_id}: ok")

        if errors:
            print(json.dumps({
                "status": "blocked",
                "task_dir": str(task_dir),
                "error": f"{len(errors)} verification failure(s)",
                "failures": errors,
                "verified": verified,
            }, indent=2))
            return 1

        print(json.dumps({
            "status": "verified",
            "task_dir": str(task_dir),
            "segments_verified": len(verified),
            "verified": verified,
        }, indent=2))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


def cmd_run_repair_execution_ready(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir).resolve()
    root = _root_for_task_dir(task_dir)
    blocked = _refuse_if_rules_corrupt(root)
    if blocked is not None:
        return blocked
    try:
        manifest = _load_manifest(task_dir)
        stage = manifest.get("stage")
        if stage != "REPAIR_PLANNING":
            print(json.dumps({
                "status": "blocked",
                "task_dir": str(task_dir),
                "error": f"unexpected stage for repair execution readiness: {stage}",
            }, indent=2))
            return 1

        from lib_validate import validate_repair_log  # noqa: PLC0415

        errors = validate_repair_log(task_dir)
        if errors:
            print(json.dumps({
                "status": "repair_log_invalid",
                "task_dir": str(task_dir),
                "errors": errors,
            }, indent=2))
            return 1

        repair_log = load_json(task_dir / "repair-log.json")
        batches = repair_log.get("batches", []) if isinstance(repair_log, dict) else []
        _, updated = transition_task(task_dir, "REPAIR_EXECUTION")
        print(json.dumps({
            "status": "repair_execution_ready",
            "task_dir": str(task_dir),
            "stage": updated.get("stage"),
            "repair_cycle": repair_log.get("repair_cycle", 0) if isinstance(repair_log, dict) else 0,
            "batch_count": len(batches) if isinstance(batches, list) else 0,
            "batches": batches if isinstance(batches, list) else [],
        }, indent=2))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


def cmd_run_repair_q_update(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir).resolve()
    try:
        manifest = _load_manifest(task_dir)
        task_type = str((manifest.get("classification") or {}).get("type", "feature"))
        repair_log_path = task_dir / "repair-log.json"
        if not repair_log_path.exists():
            print(json.dumps({
                "status": "blocked",
                "task_dir": str(task_dir),
                "error": f"missing repair-log: {repair_log_path}",
            }, indent=2))
            return 1

        repair_log = load_json(repair_log_path)
        batches = repair_log.get("batches", []) if isinstance(repair_log, dict) else []
        repaired_tasks: list[dict] = []
        repaired_finding_ids: set[str] = set()
        for batch in batches if isinstance(batches, list) else []:
            if not isinstance(batch, dict):
                continue
            for task in batch.get("tasks", []) or []:
                if not isinstance(task, dict):
                    continue
                fid = task.get("finding_id")
                if isinstance(fid, str) and fid:
                    repaired_finding_ids.add(fid)
                    repaired_tasks.append(task)

        post_repair_blocking: dict[str, dict] = {}
        reports_dir = task_dir / "audit-reports"
        if reports_dir.exists():
            for report_path in sorted(reports_dir.glob("*.json")):
                report = load_json(report_path)
                findings = report.get("findings", []) if isinstance(report, dict) else []
                if not isinstance(findings, list):
                    continue
                for finding in findings:
                    if not isinstance(finding, dict) or not bool(finding.get("blocking")):
                        continue
                    fid = finding.get("id")
                    if isinstance(fid, str) and fid:
                        post_repair_blocking[fid] = finding

        new_blocking_ids = sorted(set(post_repair_blocking) - repaired_finding_ids)
        from lib_qlearn import encode_repair_state, update_from_outcomes  # noqa: PLC0415
        root = _root_for_task_dir(task_dir)
        outcomes: list[dict] = []
        for task in repaired_tasks:
            finding_id = str(task.get("finding_id", "") or "")
            if not finding_id:
                continue
            retry_count = int(task.get("retry_count", 0) or 0)
            severity = str(task.get("severity", "medium") or "medium")
            category = finding_id.split("-")[0] if "-" in finding_id else finding_id
            resolved = finding_id not in post_repair_blocking
            next_state = None
            if not resolved:
                next_state = encode_repair_state(category, severity, task_type, retry_count + 1)
            outcomes.append({
                "finding_id": finding_id,
                "state": task.get("state") or encode_repair_state(category, severity, task_type, retry_count),
                "executor": task.get("assigned_executor"),
                "route_mode": task.get("route_mode", "generic"),
                "model": task.get("model_override") or "default",
                "resolved": resolved,
                "new_findings": len(new_blocking_ids),
                "tokens_used": 0,
                "next_state": next_state,
            })

        result = update_from_outcomes(root, outcomes, task_type)
        print(json.dumps({
            "status": "repair_q_updated",
            "task_dir": str(task_dir),
            "outcome_count": len(outcomes),
            "new_blocking_ids": new_blocking_ids,
            "update_result": result,
        }, indent=2))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


def cmd_run_repair_batch_plan(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir).resolve()
    try:
        manifest = _load_manifest(task_dir)
        stage = manifest.get("stage")
        if stage != "REPAIR_EXECUTION":
            print(json.dumps({
                "status": "blocked",
                "task_dir": str(task_dir),
                "error": f"unexpected stage for repair batch planning: {stage}",
            }, indent=2))
            return 1

        from lib_validate import validate_repair_log  # noqa: PLC0415

        errors = validate_repair_log(task_dir)
        if errors:
            print(json.dumps({
                "status": "repair_log_invalid",
                "task_dir": str(task_dir),
                "errors": errors,
            }, indent=2))
            return 1

        repair_log = load_json(task_dir / "repair-log.json")
        batches = repair_log.get("batches", []) if isinstance(repair_log, dict) else []
        normalized_batches: list[dict] = []
        for batch in batches if isinstance(batches, list) else []:
            if not isinstance(batch, dict):
                continue
            batch_id = str(batch.get("batch_id", "") or "")
            tasks = batch.get("tasks", [])
            if not batch_id or not isinstance(tasks, list):
                continue
            files = sorted({
                str(path)
                for task in tasks
                if isinstance(task, dict)
                for path in (task.get("files_to_modify", []) or [])
                if isinstance(path, str) and path
            })
            executors = sorted({
                str(task.get("assigned_executor"))
                for task in tasks
                if isinstance(task, dict) and isinstance(task.get("assigned_executor"), str)
            })
            finding_ids = [
                str(task.get("finding_id"))
                for task in tasks
                if isinstance(task, dict) and isinstance(task.get("finding_id"), str)
            ]
            model_overrides = {
                str(task.get("finding_id")): str(task.get("model_override"))
                for task in tasks
                if isinstance(task, dict)
                and isinstance(task.get("finding_id"), str)
                and isinstance(task.get("model_override"), str)
                and task.get("model_override")
            }
            normalized_batches.append({
                "batch_id": batch_id,
                "parallel_hint": bool(batch.get("parallel", False)),
                "files_to_modify": files,
                "executors": executors,
                "finding_ids": finding_ids,
                "task_count": len([task for task in tasks if isinstance(task, dict)]),
                "model_overrides": model_overrides,
            })

        groups: list[dict] = []
        current_group: dict | None = None
        for batch in normalized_batches:
            batch_files = set(batch["files_to_modify"])
            can_share_group = (
                current_group is not None
                and batch["parallel_hint"]
                and current_group["parallel"]
                and not (batch_files & current_group["files"])
            )
            if not can_share_group:
                current_group = {
                    "group_id": f"group-{len(groups) + 1}",
                    "parallel": bool(batch["parallel_hint"]),
                    "batches": [],
                    "files": set(),
                }
                groups.append(current_group)
            current_group["batches"].append(batch)
            current_group["files"].update(batch_files)
            if batch_files & (current_group["files"] - batch_files):
                current_group["parallel"] = False

        plan_groups: list[dict] = []
        for group in groups:
            plan_groups.append({
                "group_id": group["group_id"],
                "parallel": group["parallel"],
                "batch_ids": [batch["batch_id"] for batch in group["batches"]],
                "shared_files": sorted(group["files"]),
                "batches": group["batches"],
            })

        print(json.dumps({
            "status": "repair_batch_plan_ready",
            "task_dir": str(task_dir),
            "repair_cycle": repair_log.get("repair_cycle", 0) if isinstance(repair_log, dict) else 0,
            "batch_count": len(normalized_batches),
            "execution_groups": plan_groups,
            "next_group": plan_groups[0]["batch_ids"] if plan_groups else [],
        }, indent=2))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


def cmd_run_repair_retry(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir).resolve()
    root = _root_for_task_dir(task_dir)
    blocked = _refuse_if_rules_corrupt(root)
    if blocked is not None:
        return blocked
    try:
        manifest = _load_manifest(task_dir)
        stage = manifest.get("stage")
        if stage != "REPAIR_EXECUTION":
            print(json.dumps({
                "status": "blocked",
                "task_dir": str(task_dir),
                "error": f"unexpected stage for repair retry: {stage}",
            }, indent=2))
            return 1

        try:
            _, updated = transition_task(task_dir, "REPAIR_PLANNING")
            print(json.dumps({
                "status": "repair_retry_ready",
                "task_dir": str(task_dir),
                "stage": updated.get("stage"),
                "next_action": "another_repair_cycle",
            }, indent=2))
            return 0
        except Exception as exc:
            print(json.dumps({
                "status": "escalation_required",
                "task_dir": str(task_dir),
                "error": str(exc),
                "next_action": "write_escalation_and_fail",
            }, indent=2))
            return 1
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


def cmd_run_audit_reflect(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir).resolve()
    try:
        from lib_validate import compute_reward  # noqa: PLC0415
        from lib_receipts import receipt_retrospective  # noqa: PLC0415

        result = compute_reward(task_dir)
        from lib_core import write_json  # noqa: PLC0415
        retro_path = task_dir / "task-retrospective.json"
        write_json(retro_path, result)
        receipt_path = receipt_retrospective(task_dir)

        print(json.dumps({
            "status": "reflect_ready",
            "task_dir": str(task_dir),
            "retrospective_path": str(retro_path),
            "receipt_path": str(receipt_path),
            "quality_score": result.get("quality_score"),
            "cost_score": result.get("cost_score"),
            "efficiency_score": result.get("efficiency_score"),
        }, indent=2))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


def cmd_run_audit_finish(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir).resolve()
    root = _root_for_task_dir(task_dir)
    blocked = _refuse_if_rules_corrupt(root)
    if blocked is not None:
        return blocked
    try:
        manifest = _load_manifest(task_dir)
        stage = manifest.get("stage")
        if stage not in {"CHECKPOINT_AUDIT", "FINAL_AUDIT"}:
            print(json.dumps({
                "status": "blocked",
                "task_dir": str(task_dir),
                "error": f"unexpected stage for audit finish: {stage}",
            }, indent=2))
            return 1

        summary_path = task_dir / "audit-summary.json"
        retro_path = task_dir / "task-retrospective.json"
        if not summary_path.exists() or not retro_path.exists():
            print(json.dumps({
                "status": "blocked",
                "task_dir": str(task_dir),
                "error": "audit-summary.json and task-retrospective.json must exist before DONE",
            }, indent=2))
            return 1

        completion = load_json(summary_path)
        completion_path = task_dir / "completion.json"
        write_json(completion_path, completion)

        # Task-receipt-chain (task-20260503-001): extend chain for the
        # final completion artifact. If the chain file is absent, this
        # is a legacy task and we set chain_unverified=true. Best-effort.
        chain_path = task_dir / "task-receipt-chain.jsonl"
        if not chain_path.exists():
            try:
                manifest["chain_unverified"] = True
                from lib_core import write_ctl_json as _wcj
                _wcj(task_dir, task_dir / "manifest.json", manifest)
            except Exception:
                pass
        else:
            _try_extend_chain_for_artifact(task_dir, completion_path)

        _, updated = transition_task(task_dir, "DONE")
        # task-20260503-002: emit user_summary in stdout JSON (read verbatim from
        # the just-copied completion.json — no re-derivation).
        print(json.dumps({
            "status": "done",
            "task_dir": str(task_dir),
            "stage": updated.get("stage"),
            "completion_path": str(completion_path),
            "completed_at": updated.get("completed_at"),
            "user_summary": completion.get("user_summary"),
        }, indent=2))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


_FIXED_CHAIN_ARTIFACTS = (
    "manifest.json",
    "spec.md",
    "plan.md",
    "execution-graph.json",
    "repair-log.json",
    "task-retrospective.json",
    "audit-plan.json",
    "audit-summary.json",
    "audit-context.md",
    "external-solution-gate.json",
    "completion.json",
)


def cmd_run_task_receipt_chain(args: argparse.Namespace) -> int:
    """Walk receipts/ recursively + fixed artifacts, append entries for files
    not yet chained. Idempotent under sequential AND concurrent invocation.

    sec-001 fix: holds fcntl.LOCK_EX on the chain file across the entire
    'already' read + per-file append sequence so two concurrent invocations
    cannot both decide to append the same (kind, file_path).
    """
    task_dir = Path(args.task_dir).resolve()
    try:
        import fcntl as _fcntl
        from lib_chain import _CHAIN_FILENAME, _append_entry_unlocked
    except Exception as exc:
        print(f"lib_chain import failed: {exc}", file=sys.stderr)
        return 1

    chain_path = task_dir / _CHAIN_FILENAME
    chain_path.parent.mkdir(parents=True, exist_ok=True)
    if not chain_path.exists():
        chain_path.touch()

    # Hold LOCK_EX across the entire read+append sequence so concurrent
    # invocations serialize. _append_entry's own LOCK_EX is on the same
    # file (BSD flock is per-open-file-description on POSIX), so the inner
    # acquire is reentrant-safe within the same process.
    pending: list[tuple[str, str, Path]] = []  # (kind, step, file_path)
    with chain_path.open("r+", encoding="utf-8") as lock_f:
        _fcntl.flock(lock_f.fileno(), _fcntl.LOCK_EX)
        try:
            already: set = set()
            try:
                for ln in lock_f.read().splitlines():
                    if not ln.strip():
                        continue
                    try:
                        e = json.loads(ln)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(e, dict):
                        already.add((e.get("kind"), e.get("file_path")))
            except OSError:
                pass

            # Walk receipts/
            receipts_dir = task_dir / "receipts"
            if receipts_dir.is_dir():
                for rp in sorted(receipts_dir.rglob("*")):
                    if not rp.is_file():
                        continue
                    try:
                        rel = rp.relative_to(task_dir).as_posix()
                    except ValueError:
                        continue
                    if ("receipt", rel) in already:
                        continue
                    pending.append(("receipt", rp.stem, rp))

            # Fixed artifact list
            for name in _FIXED_CHAIN_ARTIFACTS:
                ap = task_dir / name
                if not ap.is_file():
                    continue
                if ("artifact", name) in already:
                    continue
                pending.append(("artifact", name, ap))

            # Append all pending entries while still holding the outer
            # lock. Use the unlocked variant — taking the inner LOCK_EX
            # again on a separate FD would deadlock under BSD flock
            # (per-FD lock semantics). The outer lock_f's LOCK_EX is
            # sufficient to serialize concurrent CLI invocations.
            for kind, step, fp in pending:
                try:
                    _append_entry_unlocked(task_dir, step, kind, fp)
                except Exception:
                    pass
        finally:
            _fcntl.flock(lock_f.fileno(), _fcntl.LOCK_UN)

    print(json.dumps({
        "status": "task_receipt_chain_ready",
        "task_dir": str(task_dir),
        "chain_path": str(chain_path),
        "appended": len(pending),
    }, indent=2))
    return 0


def cmd_verify_audit_summary_text(args: argparse.Namespace) -> int:
    """task-20260503-002: verify completion.json::user_summary matches a fresh
    re-derivation from audit-summary.json. Byte-exact compare.

    Exit codes:
      0 — match
      1 — mismatch (stderr JSON: stored_sha, derived_sha, diff_first_line)
      2 — completion.json or audit-summary.json missing
      3 — completion.json has no user_summary key (legacy task)
    """
    task_dir = Path(args.task_dir).resolve()
    completion_path = task_dir / "completion.json"
    summary_path = task_dir / "audit-summary.json"

    if not completion_path.exists() or not summary_path.exists():
        print(json.dumps({
            "error": "missing_file",
            "completion_exists": completion_path.exists(),
            "audit_summary_exists": summary_path.exists(),
        }), file=sys.stderr)
        return 2

    try:
        completion = load_json(completion_path)
        audit_summary = load_json(summary_path)
    except Exception as exc:
        print(json.dumps({"error": "unparseable_json", "detail": str(exc)}), file=sys.stderr)
        return 2

    stored = completion.get("user_summary") if isinstance(completion, dict) else None
    if stored is None:
        print(json.dumps({"error": "legacy_completion_missing_user_summary"}), file=sys.stderr)
        return 3

    from lib_validate import derive_user_summary
    derived = derive_user_summary(audit_summary if isinstance(audit_summary, dict) else {})

    if stored == derived:
        print(json.dumps({"status": "match"}))
        return 0

    import hashlib as _hashlib
    stored_sha = _hashlib.sha256(stored.encode("utf-8")).hexdigest()
    derived_sha = _hashlib.sha256(derived.encode("utf-8")).hexdigest()
    stored_lines = stored.splitlines()
    derived_lines = derived.splitlines()
    diff_first_line = None
    for i, (s, d) in enumerate(zip(stored_lines, derived_lines)):
        if s != d:
            diff_first_line = stored_lines[i] if i < len(stored_lines) else None
            break
    else:
        # All shared lines match; the longer one diverges at its tail.
        if len(stored_lines) != len(derived_lines):
            idx = min(len(stored_lines), len(derived_lines))
            diff_first_line = stored_lines[idx] if idx < len(stored_lines) else None

    print(json.dumps({
        "error": "mismatch",
        "stored_sha": stored_sha,
        "derived_sha": derived_sha,
        "diff_first_line": diff_first_line,
    }), file=sys.stderr)
    return 1


def cmd_validate_task_receipt_chain(args: argparse.Namespace) -> int:
    """Validate the chain. Exit codes: 0 valid, 1 content_mismatch,
    2 chain_corrupt, 3 chain_missing.
    """
    task_dir = Path(args.task_dir).resolve()
    try:
        from lib_chain import validate_chain
    except Exception as exc:
        print(f"lib_chain import failed: {exc}", file=sys.stderr)
        return 1

    result = validate_chain(task_dir)
    code_map = {
        "valid": 0,
        "content_mismatch": 1,
        "chain_corrupt": 2,
        "chain_missing": 3,
        "chain_truncated": 2,
    }
    code = code_map.get(result.status, 1)
    if code == 0:
        print(json.dumps({"status": "valid"}, indent=2))
    else:
        err = {
            "error": result.status,
            "index": result.first_failed_index,
            "file_path": result.first_failed_file_path,
            "field": result.first_failed_field,
            "reason": result.error_reason,
        }
        print(json.dumps(err, indent=2), file=sys.stderr)
    return code


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


def register_task_lifecycle_parsers(subparsers: argparse._SubParsersAction) -> None:
    """Register task-lifecycle subcommands: validate-task, transition, approve-stage,
    amend-artifact, next-command, active-task."""
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

    amend_artifact_parser = subparsers.add_parser(
        "amend-artifact",
        help="Amend an artifact post-approval and record an amendment receipt",
    )
    amend_artifact_parser.add_argument("task_dir")
    amend_artifact_parser.add_argument(
        "artifact_name",
        help="Artifact to amend: spec, plan, or tdd",
    )
    amend_artifact_parser.add_argument(
        "--reason",
        default=None,
        help="Human-readable rationale for the amendment (required, must be non-empty).",
    )
    amend_artifact_parser.set_defaults(func=cmd_amend_artifact)

    next_parser = subparsers.add_parser("next-command", help="Resolve next command for current stage")
    next_parser.add_argument("task_dir")
    next_parser.set_defaults(func=cmd_next_command)

    active_parser = subparsers.add_parser("active-task", help="List active tasks under .dynos")
    active_parser.add_argument("--root", default=".")
    active_parser.set_defaults(func=cmd_active_task)


def register_planning_parsers(subparsers: argparse._SubParsersAction) -> None:
    """Register planning-stage subcommands: run-start-classification, run-spec-ready,
    run-planning-mode, run-planning, run-plan-audit, plan-validated-receipt,
    plan-audit-receipt, planner-receipt."""
    run_start_classification_parser = subparsers.add_parser(
        "run-start-classification",
        help="Validate classification, apply fast-track, and advance to SPEC_NORMALIZATION when ready",
    )
    run_start_classification_parser.add_argument("task_dir")
    run_start_classification_parser.set_defaults(func=cmd_run_start_classification)

    run_spec_ready_parser = subparsers.add_parser(
        "run-spec-ready",
        help="Validate spec.md, write spec-validated receipt, and advance to SPEC_REVIEW when ready",
    )
    run_spec_ready_parser.add_argument("task_dir")
    run_spec_ready_parser.set_defaults(func=cmd_run_spec_ready)

    run_planning_mode_parser = subparsers.add_parser(
        "run-planning-mode",
        help="Choose planning mode deterministically from manifest.fast_track, risk, and AC count",
    )
    run_planning_mode_parser.add_argument("task_dir")
    run_planning_mode_parser.set_defaults(func=cmd_run_planning_mode)

    run_planning_parser = subparsers.add_parser(
        "run-planning",
        help="Validate planning artifacts, write receipt, and advance to PLAN_REVIEW when ready",
    )
    run_planning_parser.add_argument("task_dir")
    run_planning_parser.set_defaults(func=cmd_run_planning)

    run_plan_audit_parser = subparsers.add_parser(
        "run-plan-audit",
        help="Run deterministic plan-audit logic and optional high-risk LLM audit finalization",
    )
    run_plan_audit_parser.add_argument("task_dir")
    run_plan_audit_parser.add_argument(
        "--report-path",
        default=None,
        help="Path to spec-completion auditor report for high/critical-risk tasks.",
    )
    run_plan_audit_parser.add_argument(
        "--tokens-used",
        type=int,
        default=0,
        help="Tokens consumed by the spec-completion auditor run.",
    )
    run_plan_audit_parser.add_argument(
        "--model",
        default=None,
        help="Model used by the spec-completion auditor.",
    )
    run_plan_audit_parser.set_defaults(func=cmd_run_plan_audit)

    plan_validated_receipt_parser = subparsers.add_parser(
        "plan-validated-receipt",
        help="Write the plan-validated receipt deterministically",
    )
    plan_validated_receipt_parser.add_argument("task_dir")
    plan_validated_receipt_parser.add_argument(
        "--no-gap",
        dest="run_gap",
        action="store_false",
        help="Skip plan_gap_analysis during validation before writing the receipt.",
    )
    plan_validated_receipt_parser.set_defaults(run_gap=True)
    plan_validated_receipt_parser.set_defaults(func=cmd_plan_validated_receipt)

    plan_audit_receipt_parser = subparsers.add_parser(
        "plan-audit-receipt",
        help="Write the plan-audit spawn receipt deterministically",
    )
    plan_audit_receipt_parser.add_argument("task_dir")
    plan_audit_receipt_parser.add_argument("--tokens-used", type=int, required=True)
    plan_audit_receipt_parser.add_argument("--model", default=None)
    plan_audit_receipt_parser.set_defaults(func=cmd_plan_audit_receipt)

    planner_receipt_parser = subparsers.add_parser(
        "planner-receipt",
        help="Write a planner spawn receipt deterministically from sidecar proof",
    )
    planner_receipt_parser.add_argument("task_dir")
    planner_receipt_parser.add_argument("phase", choices=("discovery", "spec", "plan"))
    planner_receipt_parser.add_argument("--tokens-used", type=int, required=True)
    planner_receipt_parser.add_argument("--model", default=None)
    planner_receipt_parser.add_argument("--agent-name", default=None)
    planner_receipt_parser.add_argument("--injected-prompt-sha256", required=True)
    planner_receipt_parser.set_defaults(func=cmd_planner_receipt)


def register_execution_parsers(subparsers: argparse._SubParsersAction) -> None:
    """Register execution-stage subcommands: run-execute-setup, run-execution-batch-plan,
    run-execution-segment-done, run-execution-finish, run-execution-verify-evidence,
    check-ownership, write-execute-handoff."""
    run_execute_setup_parser = subparsers.add_parser(
        "run-execute-setup",
        help="Validate execution preflight, advance to EXECUTION, and write executor-routing receipt",
    )
    run_execute_setup_parser.add_argument("task_dir")
    run_execute_setup_parser.set_defaults(func=cmd_run_execute_setup)

    run_execution_batch_plan_parser = subparsers.add_parser(
        "run-execution-batch-plan",
        help="Compute deterministic execution segment state, cache eligibility, and runnable batches",
    )
    run_execution_batch_plan_parser.add_argument("task_dir")
    run_execution_batch_plan_parser.set_defaults(func=cmd_run_execution_batch_plan)

    run_execution_segment_done_parser = subparsers.add_parser(
        "run-execution-segment-done",
        help="Verify ownership/evidence, write executor receipt, and update manifest execution_progress",
    )
    run_execution_segment_done_parser.add_argument("task_dir")
    run_execution_segment_done_parser.add_argument("segment_id")
    run_execution_segment_done_parser.add_argument(
        "--injected-prompt-sha256",
        required=True,
        help="Digest captured from receipts/_injected-prompts/{segment}.sha256",
    )
    run_execution_segment_done_parser.add_argument("--model", default=None)
    run_execution_segment_done_parser.add_argument("--agent-name", default=None)
    run_execution_segment_done_parser.add_argument("--executor-type", default=None)
    run_execution_segment_done_parser.add_argument("--evidence-path", default=None)
    run_execution_segment_done_parser.add_argument("--tokens-used", type=int, default=None)
    run_execution_segment_done_parser.add_argument(
        "--files",
        nargs="*",
        default=None,
        help="Files to ownership-check; defaults to execution-graph files_expected for the segment.",
    )
    run_execution_segment_done_parser.set_defaults(func=cmd_run_execution_segment_done)

    run_execution_finish_parser = subparsers.add_parser(
        "run-execution-finish",
        help="Advance EXECUTION -> TEST_EXECUTION only when no pending segments remain",
    )
    run_execution_finish_parser.add_argument("task_dir")
    run_execution_finish_parser.set_defaults(func=cmd_run_execution_finish)

    run_execution_verify_evidence_parser = subparsers.add_parser(
        "run-execution-verify-evidence",
        help="Verify evidence files and files_expected exist for all non-cached segments (replaces model narrative in Step 5)",
    )
    run_execution_verify_evidence_parser.add_argument("task_dir")
    run_execution_verify_evidence_parser.set_defaults(func=cmd_run_execution_verify_evidence)

    ownership_parser = subparsers.add_parser("check-ownership", help="Check that files belong to a segment")
    ownership_parser.add_argument("task_dir")
    ownership_parser.add_argument("segment_id")
    ownership_parser.add_argument("files", nargs="+")
    ownership_parser.set_defaults(func=cmd_check_ownership)

    execute_handoff_parser = subparsers.add_parser(
        "write-execute-handoff",
        help="Write handoff-execute-audit.json deterministically",
    )
    execute_handoff_parser.add_argument("task_dir")
    execute_handoff_parser.set_defaults(func=cmd_write_execute_handoff)


def register_audit_parsers(subparsers: argparse._SubParsersAction) -> None:
    """Register audit-stage subcommands: run-audit-setup, run-audit-findings-gate,
    run-audit-repair-cycle-plan, run-audit-reaudit-plan, run-audit-summary,
    audit-receipt, run-rules-check."""
    run_audit_setup_parser = subparsers.add_parser(
        "run-audit-setup",
        help="Build audit-plan.json and diff scope deterministically for CHECKPOINT_AUDIT",
    )
    run_audit_setup_parser.add_argument("task_dir")
    run_audit_setup_parser.add_argument(
        "--no-head-fallback",
        dest="allow_head_fallback",
        action="store_false",
        help="Do not fall back to git diff HEAD when snapshot.head_sha is missing.",
    )
    run_audit_setup_parser.set_defaults(allow_head_fallback=True)
    run_audit_setup_parser.set_defaults(func=cmd_run_audit_setup)

    run_audit_findings_gate_parser = subparsers.add_parser(
        "run-audit-findings-gate",
        help="Summarize current audit reports and decide deterministically whether repair is required",
    )
    run_audit_findings_gate_parser.add_argument("task_dir")
    run_audit_findings_gate_parser.set_defaults(func=cmd_run_audit_findings_gate)

    run_audit_repair_cycle_plan_parser = subparsers.add_parser(
        "run-audit-repair-cycle-plan",
        help="Build the deterministic repair queue, retry counts, and phase label from current audit reports",
    )
    run_audit_repair_cycle_plan_parser.add_argument("task_dir")
    run_audit_repair_cycle_plan_parser.set_defaults(func=cmd_run_audit_repair_cycle_plan)

    run_audit_reaudit_plan_parser = subparsers.add_parser(
        "run-audit-reaudit-plan",
        help="Build deterministic re-audit file scope and auditor set from repair-log plus existing reports",
    )
    run_audit_reaudit_plan_parser.add_argument("task_dir")
    run_audit_reaudit_plan_parser.set_defaults(func=cmd_run_audit_reaudit_plan)

    run_audit_summary_parser = subparsers.add_parser(
        "run-audit-summary",
        help="Aggregate audit reports and write audit-summary.json deterministically",
    )
    run_audit_summary_parser.add_argument("task_dir")
    run_audit_summary_parser.set_defaults(func=cmd_run_audit_summary)

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

    run_rules_check_parser = subparsers.add_parser(
        "run-rules-check",
        help="Run prevention-rules engine and write rules-check-passed receipt (required before run-execution-finish)",
    )
    run_rules_check_parser.add_argument("task_dir")
    run_rules_check_parser.add_argument(
        "--mode", choices=["staged", "all"], default="staged",
        help="Check staged files only (default) or all files",
    )
    run_rules_check_parser.set_defaults(func=cmd_run_rules_check)


def register_repair_parsers(subparsers: argparse._SubParsersAction) -> None:
    """Register repair-stage subcommands: run-repair-execution-ready, run-repair-log-build,
    run-repair-batch-plan, run-repair-q-update, run-repair-retry, write-repair-log."""
    run_repair_execution_ready_parser = subparsers.add_parser(
        "run-repair-execution-ready",
        help="Validate repair-log.json and advance REPAIR_PLANNING -> REPAIR_EXECUTION",
    )
    run_repair_execution_ready_parser.add_argument("task_dir")
    run_repair_execution_ready_parser.set_defaults(func=cmd_run_repair_execution_ready)

    run_repair_log_build_parser = subparsers.add_parser(
        "run-repair-log-build",
        help="Build repair-log.json deterministically from repair-cycle-plan plus Q-learning assignments",
    )
    run_repair_log_build_parser.add_argument("task_dir")
    run_repair_log_build_parser.set_defaults(func=cmd_run_repair_log_build)

    run_repair_batch_plan_parser = subparsers.add_parser(
        "run-repair-batch-plan",
        help="Build deterministic repair batch execution groups from repair-log.json",
    )
    run_repair_batch_plan_parser.add_argument("task_dir")
    run_repair_batch_plan_parser.set_defaults(func=cmd_run_repair_batch_plan)

    run_repair_q_update_parser = subparsers.add_parser(
        "run-repair-q-update",
        help="Build repair outcomes deterministically from repair-log plus current audit reports and update Q-tables",
    )
    run_repair_q_update_parser.add_argument("task_dir")
    run_repair_q_update_parser.set_defaults(func=cmd_run_repair_q_update)

    run_repair_retry_parser = subparsers.add_parser(
        "run-repair-retry",
        help="Attempt REPAIR_EXECUTION -> REPAIR_PLANNING and surface retry-cap escalation as JSON",
    )
    run_repair_retry_parser.add_argument("task_dir")
    run_repair_retry_parser.set_defaults(func=cmd_run_repair_retry)

    repair_write_parser = subparsers.add_parser(
        "write-repair-log",
        help="Validate, normalize, and atomically write repair-log.json",
    )
    repair_write_parser.add_argument("task_dir")
    repair_write_parser.add_argument("--from", dest="from_path", required=True)
    repair_write_parser.set_defaults(func=cmd_write_repair_log)


def register_artifact_parsers(subparsers: argparse._SubParsersAction) -> None:
    """Register artifact-write subcommands: write-execution-graph, write-classification,
    write-search-receipt, run-external-solution-gate, check-retro-integrity."""
    graph_write_parser = subparsers.add_parser(
        "write-execution-graph",
        help="Validate, normalize, and atomically write execution-graph.json",
    )
    graph_write_parser.add_argument("task_dir")
    graph_write_parser.add_argument("--from", dest="from_path", required=True)
    graph_write_parser.set_defaults(func=cmd_write_execution_graph)

    classification_write_parser = subparsers.add_parser(
        "write-classification",
        help="Validate, normalize, and atomically write classification.json plus synced manifest state",
    )
    classification_write_parser.add_argument("task_dir")
    classification_write_parser.add_argument("--from", dest="from_path", required=True)
    classification_write_parser.set_defaults(func=cmd_write_classification)

    write_search_receipt_parser = subparsers.add_parser(
        "write-search-receipt",
        help="Write search-conducted receipt after executor performs gate-recommended research",
    )
    write_search_receipt_parser.add_argument("task_dir")
    write_search_receipt_parser.add_argument(
        "--query",
        required=True,
        help="The search query string actually used",
    )
    write_search_receipt_parser.set_defaults(func=cmd_write_search_receipt)

    run_external_solution_gate_parser = subparsers.add_parser(
        "run-external-solution-gate",
        help="Write external-solution-gate.json from deterministic task heuristics",
    )
    run_external_solution_gate_parser.add_argument("task_dir")
    run_external_solution_gate_parser.set_defaults(func=cmd_run_external_solution_gate)

    retro_integrity_parser = subparsers.add_parser(
        "check-retro-integrity",
        help="Report persistent retros with no matching flush event; exits non-zero if any found",
    )
    retro_integrity_parser.add_argument("--root", required=True, help="Project root directory")
    retro_integrity_parser.set_defaults(func=cmd_check_retro_integrity)


def register_meta_parsers(subparsers: argparse._SubParsersAction) -> None:
    """Register meta/utility subcommands: stamp-role, run-audit-reflect, run-audit-finish,
    repair-plan, repair-update, compute-reward, validate-contract, validate-receipts,
    validate-chain, list-pending, approve, stats-dora, stats-usage, bus, calibration, config."""
    stamp_role_parser = subparsers.add_parser(
        "stamp-role",
        help="Stamp the active-segment-role file under role=ctl (refuses audit-* roles)",
    )
    stamp_role_parser.add_argument("task_dir")
    stamp_role_parser.add_argument("--role", required=True, help="Executor role to stamp (allowlist enforced)")
    stamp_role_parser.set_defaults(func=cmd_stamp_role)

    run_audit_reflect_parser = subparsers.add_parser(
        "run-audit-reflect",
        help="Compute task-retrospective.json and write the retrospective receipt deterministically",
    )
    run_audit_reflect_parser.add_argument("task_dir")
    run_audit_reflect_parser.set_defaults(func=cmd_run_audit_reflect)

    run_audit_finish_parser = subparsers.add_parser(
        "run-audit-finish",
        help="Write completion.json and advance CHECKPOINT_AUDIT/FINAL_AUDIT -> DONE deterministically",
    )
    run_audit_finish_parser.add_argument("task_dir")
    run_audit_finish_parser.set_defaults(func=cmd_run_audit_finish)

    chain_run_parser = subparsers.add_parser(
        "run-task-receipt-chain",
        help="Walk receipts/ + fixed artifacts and append unchained entries",
    )
    chain_run_parser.add_argument("task_dir")
    chain_run_parser.set_defaults(func=cmd_run_task_receipt_chain)

    chain_validate_parser = subparsers.add_parser(
        "validate-task-receipt-chain",
        help="Validate task receipt chain (exit 0 valid, 1 content_mismatch, 2 chain_corrupt, 3 chain_missing)",
    )
    chain_validate_parser.add_argument("task_dir")
    chain_validate_parser.set_defaults(func=cmd_validate_task_receipt_chain)

    verify_summary_parser = subparsers.add_parser(
        "verify-audit-summary-text",
        help="Verify completion.json::user_summary matches a fresh derivation from audit-summary.json (exit 0/1/2/3)",
    )
    verify_summary_parser.add_argument("task_dir")
    verify_summary_parser.set_defaults(func=cmd_verify_audit_summary_text)

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    register_task_lifecycle_parsers(subparsers)
    register_planning_parsers(subparsers)
    register_execution_parsers(subparsers)
    register_audit_parsers(subparsers)
    register_repair_parsers(subparsers)
    register_artifact_parsers(subparsers)
    register_meta_parsers(subparsers)
    return parser


if __name__ == "__main__":
    from cli_base import cli_main
    raise SystemExit(cli_main(build_parser))
