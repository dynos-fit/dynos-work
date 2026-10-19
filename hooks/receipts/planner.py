"""Planner / plan-audit / TDD spawn-receipt writers.

Each function in this module writes a structured JSON receipt for a
planner-related skill spawn (planner discovery/spec/plan, plan-audit
check, TDD test generation). Function bodies are copied byte-for-byte
from ``hooks/lib_receipts.py`` during the receipts package split — see
``hooks/receipts/__init__.py`` for the public re-export surface.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from lib_validate import require_nonblank_str

from .core import (
    read_receipt,
    hash_file,
    write_receipt,
)


def receipt_planner_spawn(  # called dynamically from skills/start/SKILL.md
    task_dir: Path,
    phase: str,  # "discovery", "spec", or "plan"
    tokens_used: int,
    model_used: str | None = None,
    agent_name: str | None = None,
    injected_prompt_sha256: str | None = None,
) -> Path:
    """Write a planner receipt bound to the latest hook-observed spawn.

    ``injected_prompt_sha256`` is accepted for compatibility but ignored;
    the PreToolUse Agent hook's prompt/result hashes are authoritative.
    Token counts remain in the receipt for older consumers; token ledger
    events are recorded only by SubagentStop.
    """
    # tokens_used remains a compatibility payload field. Zero no longer
    # signals an anomaly because hook-based attribution may be unavailable.
    if not isinstance(tokens_used, int) or isinstance(tokens_used, bool) or tokens_used < 0:
        raise ValueError(
            f"receipt_planner_spawn: tokens_used must be non-negative int "
            f"(got {tokens_used!r})"
        )
    step_name = f"planner-{phase}"

    spawn_log = task_dir / "spawn-log.jsonl"
    spawn_evidence: dict[str, Any] = {"spawn_evidence": "degraded"}
    if spawn_log.is_file():
        previous = ""
        for receipt_path in (task_dir / "receipts").glob("planner-*.json"):
            receipt = read_receipt(task_dir, receipt_path.stem)
            if isinstance(receipt, dict):
                previous = max(previous, str(receipt.get("written_at") or receipt.get("ts") or ""))
        rows = []
        for line in spawn_log.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except (ValueError, TypeError):
                continue
            rows.append(row)
        candidates = []
        for row in rows:
            stamp = str(row.get("timestamp", ""))
            if (row.get("phase") == "post"
                    and str(row.get("subagent_type", "")) in {"dynos-work:planning", "planning"}
                    and stamp > previous):
                candidates.append(row)
        if not candidates:
            raise ValueError(f"receipt_planner_spawn: no planner spawn recorded since {previous or 'task creation'}")
        spawn = candidates[-1]
        start_entry = next((row for row in reversed(rows)
                            if row.get("phase") == "pre"
                            and row.get("prompt_sha256") == spawn.get("prompt_sha256")), {})
        spawn_evidence = {
            "spawn_evidence": "hook",
            "spawn_prompt_sha256": spawn.get("prompt_sha256"),
            "spawn_result_sha256": spawn.get("result_sha256"),
            "spawn_started_at": start_entry.get("timestamp"),
            "spawn_finished_at": spawn.get("timestamp"),
        }
    else:
        try:
            from lib_log import log_event
            log_event(task_dir.parent.parent, "planner_spawn_evidence_degraded",
                      task=task_dir.name, phase=phase)
        except Exception:
            pass
    # Token usage is recorded by SubagentStop; token count remains in receipt.
    return write_receipt(
        task_dir,
        step_name,
        phase=phase,
        tokens_used=tokens_used,
        model_used=model_used,
        agent_name=agent_name,
        **spawn_evidence,
    )


def receipt_plan_audit(
    task_dir: Path,
    tokens_used: int,
    model_used: str | None = None,
    **_legacy: Any,
) -> Path:
    """Write receipt proving plan audit (spec-completion check) ran.

    Hash-binding (SEC-004 + F2): the writer re-hashes ``spec.md``,
    ``plan.md``, and ``execution-graph.json`` from the task directory at
    write time. Those sha256 hex digests are embedded in the receipt
    payload so the PLAN_AUDIT exit gate can detect artifact drift via
    ``plan_audit_matches(task_dir)`` and refuse to advance when the audit
    was computed over a stale version of the artifacts.

    Callers no longer supply the three hashes (breaking change vs. the
    initial F2 signature). Closes the TOCTOU between a caller's
    hash-read and the receipt write — the writer's own read is the
    authoritative source. Missing artifact files land the literal
    string ``missing`` in the corresponding payload slot, which always
    fails ``plan_audit_matches`` downstream with a distinctive drift
    reason.

    v4 AC 14 (task-007): ``finding_count`` has been removed from the
    signature and the receipt payload. Callers that still pass it raise
    ``TypeError`` — the plan-audit result is surfaced via dedicated audit
    receipts, not embedded in this spawn receipt. ``tokens_used`` must
    be a non-negative int.

    Also records token usage to ``token-usage.json`` when ``tokens_used``
    is positive.
    """
    if _legacy:
        raise TypeError(
            "receipt_plan_audit no longer accepts caller-supplied "
            f"{sorted(_legacy)} — finding_count was removed from the v4 "
            "plan-audit receipt payload"
        )
    if not isinstance(tokens_used, int) or isinstance(tokens_used, bool) or tokens_used < 0:
        raise ValueError(
            f"receipt_plan_audit: tokens_used must be non-negative int "
            f"(got {tokens_used!r})"
        )

    def _hash_or_missing(rel: str) -> str:
        p = task_dir / rel
        if not p.exists():
            return "missing"
        try:
            return hash_file(p)
        except OSError:
            return "missing"

    spec_sha256 = _hash_or_missing("spec.md")
    plan_sha256 = _hash_or_missing("plan.md")
    graph_sha256 = _hash_or_missing("execution-graph.json")

    return write_receipt(
        task_dir,
        "plan-audit-check",
        tokens_used=tokens_used,
        model_used=model_used,
        spec_sha256=spec_sha256,
        plan_sha256=plan_sha256,
        graph_sha256=graph_sha256,
    )


def receipt_tdd_tests(
    task_dir: Path,
    test_file_paths: list[str],
    tests_evidence_sha256: str,
    tokens_used: int,
    model_used: str,
) -> Path:
    """Write receipt proving TDD tests were generated.

    Also records token usage. Validates inputs strictly: paths must be a
    list of strings, the evidence digest must be non-empty.
    """
    if not isinstance(test_file_paths, list) or not all(
        isinstance(p, str) for p in test_file_paths
    ):
        raise ValueError("test_file_paths must be a list[str]")
    require_nonblank_str(tests_evidence_sha256, field_name="tests_evidence_sha256")
    if not isinstance(tokens_used, int) or tokens_used < 0:
        raise ValueError("tokens_used must be a non-negative int")
    require_nonblank_str(model_used, field_name="model_used")


    return write_receipt(
        task_dir,
        "tdd-tests",
        test_file_paths=test_file_paths,
        tests_evidence_sha256=tests_evidence_sha256,
        tokens_used=tokens_used,
        model_used=model_used,
    )
