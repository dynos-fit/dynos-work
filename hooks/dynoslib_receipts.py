"""Receipt-based contract validation chain for dynos-work.

Every pipeline step writes a structured JSON receipt to
.dynos/task-{id}/receipts/{step-name}.json proving what it did.
The next step refuses to proceed unless the prior receipt exists.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from dynoslib_core import now_iso, append_execution_log
from dynoslib_log import log_event

# Map receipt steps to human-readable execution-log entries
_LOG_MESSAGES: dict[str, str] = {
    "plan-routing": "[ROUTE] plan-skill → {route_mode} agent={agent_name}",
    "spec-validated": "[DONE] spec validated — {criteria_count} acceptance criteria",
    "plan-validated": "[DONE] plan validated — {segment_count} segments, criteria {criteria_coverage}",
    "executor-routing": "[ROUTE] executor plan — {n_segments} segments routed",
    "audit-routing": "[ROUTE] audit plan — {n_auditors} auditors routed",
    "retrospective": "[DONE] retrospective — quality={quality_score} cost={cost_score} efficiency={efficiency_score}",
    "post-completion": "[DONE] post-completion — {n_handlers} handlers, postmortem={postmortem_written}",
    "planner-discovery": "[DONE] planner discovery — tokens={tokens_used}",
    "planner-spec": "[DONE] planner spec — tokens={tokens_used}",
    "planner-plan": "[DONE] planner plan — tokens={tokens_used}",
    "plan-audit-check": "[DONE] plan audit — tokens={tokens_used}",
    "tdd-tests": "[DONE] TDD tests — tokens={tokens_used}",
}


def _record_tokens(task_dir: Path, agent: str, model: str, tokens: int) -> None:
    """Record token usage to token-usage.json. Called by receipt writers."""
    try:
        from dynoslib_tokens import record_tokens
        record_tokens(
            task_dir=task_dir,
            agent=agent,
            model=model,
            input_tokens=tokens // 2,  # rough split since we only have total
            output_tokens=tokens - tokens // 2,
            phase="receipt",
            stage="receipt",
            event_type="receipt-record",
            detail=f"auto-recorded via receipt for {agent}",
        )
    except Exception:
        # Fallback: write directly if dynoslib_tokens isn't available
        try:
            token_path = task_dir / "token-usage.json"
            data = json.loads(token_path.read_text()) if token_path.exists() else {"agents": {}, "total": 0}
            data["agents"][agent] = data.get("agents", {}).get(agent, 0) + tokens
            data["total"] = sum(v for v in data["agents"].values() if isinstance(v, (int, float)))
            token_path.write_text(json.dumps(data, indent=2))
        except Exception:
            pass


def _receipts_dir(task_dir: Path) -> Path:
    return task_dir / "receipts"


def write_receipt(task_dir: Path, step_name: str, **payload: Any) -> Path:
    """Write a receipt proving a pipeline step completed.

    Returns the path to the written receipt file.
    """
    receipts = _receipts_dir(task_dir)
    receipts.mkdir(parents=True, exist_ok=True)

    receipt = {
        "step": step_name,
        "ts": now_iso(),
        "valid": True,
        **payload,
    }

    receipt_path = receipts / f"{step_name}.json"
    receipt_path.write_text(json.dumps(receipt, indent=2, default=str))

    # Log the receipt write to events.jsonl
    root = task_dir.parent.parent
    task_id = task_dir.name
    log_event(root, "receipt_written", task=task_id, step=step_name)

    # Auto-append to execution-log.md
    template = _LOG_MESSAGES.get(step_name)
    if template:
        try:
            fmt_data = {**payload}
            # Add computed fields for formatting
            if "segments" in payload:
                fmt_data["n_segments"] = len(payload["segments"])
            if "auditors" in payload:
                fmt_data["n_auditors"] = len(payload["auditors"])
            if "handlers_run" in payload:
                fmt_data["n_handlers"] = len(payload["handlers_run"])
            msg = template.format(**{k: v for k, v in fmt_data.items() if isinstance(v, (str, int, float, bool, list))})
            append_execution_log(task_dir, msg)
        except (KeyError, TypeError):
            append_execution_log(task_dir, f"[RECEIPT] {step_name}")
    elif step_name.startswith("executor-seg"):
        agent = payload.get("agent_name", "generic")
        injected = payload.get("learned_agent_injected", False)
        append_execution_log(task_dir, f"[DONE] {step_name} — executor={payload.get('executor_type','')} agent={'learned:' + agent if injected else 'generic'} tokens={payload.get('tokens_used','?')}")
    elif step_name.startswith("audit-") and step_name != "audit-routing":
        append_execution_log(task_dir, f"[DONE] {step_name} — findings={payload.get('finding_count',0)} blocking={payload.get('blocking_count',0)}")

    return receipt_path


def read_receipt(task_dir: Path, step_name: str) -> dict | None:
    """Read a receipt. Returns None if missing or invalid."""
    receipt_path = _receipts_dir(task_dir) / f"{step_name}.json"
    if not receipt_path.exists():
        return None
    try:
        data = json.loads(receipt_path.read_text())
        if not isinstance(data, dict) or not data.get("valid"):
            return None
        return data
    except (json.JSONDecodeError, OSError):
        return None


def require_receipt(task_dir: Path, step_name: str) -> dict:
    """Read a receipt, raising ValueError if missing or invalid.

    Use this as a gate before proceeding to the next step.
    """
    receipt = read_receipt(task_dir, step_name)
    if receipt is None:
        root = task_dir.parent.parent
        task_id = task_dir.name
        log_event(root, "receipt_missing", task=task_id, step=step_name)
        raise ValueError(
            f"Required receipt missing: {step_name}\n"
            f"  Expected at: {_receipts_dir(task_dir) / f'{step_name}.json'}\n"
            f"  This means the prior pipeline step did not complete correctly."
        )
    return receipt


def require_receipts(task_dir: Path, step_names: list[str]) -> dict[str, dict]:
    """Validate multiple receipts exist. Returns all receipts or raises on first missing."""
    results = {}
    for name in step_names:
        results[name] = require_receipt(task_dir, name)
    return results


def validate_chain(task_dir: Path) -> list[str]:
    """Validate the entire receipt chain for a task. Returns list of gaps.

    This is a diagnostic tool — it checks all possible receipts and reports
    which ones are missing, without raising exceptions.
    """
    # Define the expected receipt chain based on task stage
    manifest_path = task_dir / "manifest.json"
    if not manifest_path.exists():
        return ["manifest.json missing"]

    manifest = json.loads(manifest_path.read_text())
    stage = manifest.get("stage", "")

    # All possible receipts in order
    all_receipts = [
        "plan-routing",
        "spec-validated",
        "plan-validated",
        "executor-routing",
        # executor-{seg-id} receipts are dynamic
        "audit-routing",
        # audit-{auditor} receipts are dynamic
        "retrospective",
        "post-completion",
    ]

    # What receipts should exist based on stage progression
    stage_requires: dict[str, list[str]] = {
        "EXECUTION": ["plan-validated"],
        "TEST_EXECUTION": ["plan-validated", "executor-routing"],
        "CHECKPOINT_AUDIT": ["plan-validated", "executor-routing"],
        "REPAIR_PLANNING": ["plan-validated", "executor-routing"],
        "REPAIR_EXECUTION": ["plan-validated", "executor-routing"],
        "DONE": ["plan-validated", "executor-routing", "retrospective", "post-completion"],
    }

    required = stage_requires.get(stage, [])
    gaps = []

    for receipt_name in required:
        if read_receipt(task_dir, receipt_name) is None:
            gaps.append(receipt_name)

    # Check dynamic executor receipts if we're past EXECUTION
    if stage in ("TEST_EXECUTION", "CHECKPOINT_AUDIT", "REPAIR_PLANNING",
                 "REPAIR_EXECUTION", "DONE"):
        exec_routing = read_receipt(task_dir, "executor-routing")
        if exec_routing:
            for seg in exec_routing.get("segments", []):
                seg_id = seg.get("segment_id", "")
                if seg_id and read_receipt(task_dir, f"executor-{seg_id}") is None:
                    gaps.append(f"executor-{seg_id}")

    # Check audit receipts if we're at DONE
    if stage == "DONE":
        audit_routing = read_receipt(task_dir, "audit-routing")
        if audit_routing:
            for auditor in audit_routing.get("auditors", []):
                if auditor.get("action") == "spawn":
                    name = auditor.get("name", "")
                    if name and read_receipt(task_dir, f"audit-{name}") is None:
                        gaps.append(f"audit-{name}")

    return gaps


def hash_file(path: Path) -> str | None:
    """SHA256 hash of a file's contents. Returns None if file doesn't exist."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except (OSError, FileNotFoundError):
        return None


# ---------------------------------------------------------------------------
# Convenience receipt writers for common steps
# ---------------------------------------------------------------------------


def receipt_plan_routing(
    task_dir: Path,
    agent_name: str | None,
    agent_path: str | None,
    route_mode: str,
    agent_file_hash: str | None = None,
) -> Path:
    """Write receipt proving plan-skill routing was resolved."""
    return write_receipt(
        task_dir,
        "plan-routing",
        agent_name=agent_name,
        agent_path=agent_path,
        route_mode=route_mode,
        agent_content_hash=agent_file_hash,
    )


def receipt_spec_validated(
    task_dir: Path,
    criteria_count: int,
    validation_passed: bool = True,
) -> Path:
    """Write receipt proving spec passed deterministic validation."""
    return write_receipt(
        task_dir,
        "spec-validated",
        criteria_count=criteria_count,
        validation_passed=validation_passed,
    )


def receipt_plan_validated(
    task_dir: Path,
    segment_count: int,
    criteria_coverage: list[int],
    validation_passed: bool = True,
) -> Path:
    """Write receipt proving plan + execution graph passed validation."""
    return write_receipt(
        task_dir,
        "plan-validated",
        segment_count=segment_count,
        criteria_coverage=criteria_coverage,
        validation_passed=validation_passed,
    )


def receipt_executor_routing(
    task_dir: Path,
    segments: list[dict],
) -> Path:
    """Write receipt proving all executor routing decisions were made."""
    return write_receipt(
        task_dir,
        "executor-routing",
        segments=segments,
    )


def receipt_executor_done(
    task_dir: Path,
    segment_id: str,
    executor_type: str,
    model_used: str | None,
    learned_agent_injected: bool,
    agent_name: str | None,
    evidence_path: str | None,
    tokens_used: int | None,
) -> Path:
    """Write receipt proving an executor segment completed with learned agent injection.

    Also records token usage to token-usage.json — this is the ONLY reliable
    path for token recording since receipts are enforced by gates.
    """
    # Record tokens deterministically
    if tokens_used and tokens_used > 0:
        _record_tokens(task_dir, f"{executor_type}-{segment_id}", model_used or "default", tokens_used)

    return write_receipt(
        task_dir,
        f"executor-{segment_id}",
        segment_id=segment_id,
        executor_type=executor_type,
        model_used=model_used,
        learned_agent_injected=learned_agent_injected,
        agent_name=agent_name,
        evidence_path=evidence_path,
        tokens_used=tokens_used,
    )


def receipt_audit_routing(
    task_dir: Path,
    auditors: list[dict],
) -> Path:
    """Write receipt proving all auditor routing decisions were made."""
    return write_receipt(
        task_dir,
        "audit-routing",
        auditors=auditors,
    )


def receipt_audit_done(
    task_dir: Path,
    auditor_name: str,
    model_used: str | None,
    finding_count: int,
    blocking_count: int,
    report_path: str | None,
    tokens_used: int | None,
) -> Path:
    """Write receipt proving an auditor completed.

    Also records token usage — same enforcement path as executor receipts.
    """
    if tokens_used and tokens_used > 0:
        _record_tokens(task_dir, auditor_name, model_used or "default", tokens_used)
    return write_receipt(
        task_dir,
        f"audit-{auditor_name}",
        auditor_name=auditor_name,
        model_used=model_used,
        finding_count=finding_count,
        blocking_count=blocking_count,
        report_path=report_path,
        tokens_used=tokens_used,
    )


def receipt_retrospective(
    task_dir: Path,
    quality_score: float,
    cost_score: float,
    efficiency_score: float,
    total_tokens: int,
) -> Path:
    """Write receipt proving retrospective was computed."""
    return write_receipt(
        task_dir,
        "retrospective",
        quality_score=quality_score,
        cost_score=cost_score,
        efficiency_score=efficiency_score,
        total_tokens=total_tokens,
        retrospective_path=str(task_dir / "task-retrospective.json"),
    )


def receipt_post_completion(
    task_dir: Path,
    handlers_run: list[dict],
    postmortem_written: bool,
    patterns_updated: bool,
) -> Path:
    """Write receipt proving post-completion pipeline ran."""
    return write_receipt(
        task_dir,
        "post-completion",
        handlers_run=handlers_run,
        postmortem_written=postmortem_written,
        patterns_updated=patterns_updated,
    )


# ---------------------------------------------------------------------------
# Start skill spawn receipts (planner, plan-audit, TDD)
# ---------------------------------------------------------------------------


def receipt_planner_spawn(
    task_dir: Path,
    phase: str,  # "discovery", "spec", or "plan"
    tokens_used: int | None,
    model_used: str | None = None,
    learned_agent_injected: bool = False,
    agent_name: str | None = None,
) -> Path:
    """Write receipt proving a planner subagent completed. Also records tokens."""
    step_name = f"planner-{phase}"
    if tokens_used and tokens_used > 0:
        _record_tokens(task_dir, f"planner-{phase}", model_used or "default", tokens_used)
    return write_receipt(
        task_dir,
        step_name,
        phase=phase,
        tokens_used=tokens_used,
        model_used=model_used,
        learned_agent_injected=learned_agent_injected,
        agent_name=agent_name,
    )


def receipt_plan_audit(
    task_dir: Path,
    tokens_used: int | None,
    finding_count: int = 0,
    model_used: str | None = None,
) -> Path:
    """Write receipt proving plan audit (spec-completion check) ran. Also records tokens."""
    if tokens_used and tokens_used > 0:
        _record_tokens(task_dir, "plan-audit-check", model_used or "default", tokens_used)
    return write_receipt(
        task_dir,
        "plan-audit-check",
        tokens_used=tokens_used,
        finding_count=finding_count,
        model_used=model_used,
    )



# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_validate_chain(args: Any) -> int:
    """CLI: validate the receipt chain for a task."""
    task_dir = Path(args.task_dir).resolve()
    gaps = validate_chain(task_dir)
    if gaps:
        print(f"Receipt chain gaps ({len(gaps)}):")
        for gap in gaps:
            print(f"  ✗ {gap}")
        return 1
    print("Receipt chain complete — all required receipts present.")
    return 0


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Receipt chain validation")
    parser.add_argument("task_dir", help="Path to task directory")
    args = parser.parse_args()
    raise SystemExit(cmd_validate_chain(args))
