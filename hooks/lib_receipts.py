"""Receipt-based contract validation chain for dynos-work.

Every pipeline step writes a structured JSON receipt to
.dynos/task-{id}/receipts/{step-name}.json proving what it did.
The next step refuses to proceed unless the prior receipt exists.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from lib_core import now_iso, append_execution_log
from lib_log import log_event


# Receipt contract version. Receipts written by this module embed
# `contract_version: 2`. Readers MUST treat receipts without this field
# as v1 and accept them when `valid=true`.
RECEIPT_CONTRACT_VERSION = 2


# Files that compose the calibration policy snapshot used by the
# retrospective/calibration pipeline. Receipt writers and downstream
# consumers may use this list when computing aggregate policy hashes.
CALIBRATION_POLICY_FILES = [
    "effectiveness-scores.json",
    "model-policy.json",
    "skip-policy.json",
    "route-policy.json",
    "prevention-rules.json",
    "project_rules.md",
    "policy.json",
    "trajectories.json",
    "learned-agents/registry.json",
    "benchmarks/history.json",
]


# Allowed reasons for receipt_postmortem_skipped. Enum-validated at write
# time so callers cannot silently drift the skip taxonomy.
_POSTMORTEM_SKIP_REASONS = frozenset({
    "clean-task",
    "no-findings",
    "quality-above-threshold",
})


# Sidecar directory names. These names are the filename schema for the
# per-spawn injected-prompt sidecars and MUST be used by both the writer
# (router.py CLI subcommands) and the reader/asserter (receipt_* functions
# in this module). Defining them here makes the contract unforgeable from
# a single side — renaming requires updating this constant and every
# importer.
INJECTED_PROMPTS_DIR = "_injected-prompts"
INJECTED_AUDITOR_PROMPTS_DIR = "_injected-auditor-prompts"
INJECTED_PLANNER_PROMPTS_DIR = "_injected-planner-prompts"


# PERF-001: per-process memo for hash_file keyed by (abs_path, mtime_ns,
# size). Repeated hashes of the same unchanged file in one Python process
# (e.g. receipt_plan_audit write + plan_audit_matches gate check) now
# share one disk read. Invalidated automatically when mtime or size
# change, so writes between hashes are picked up. Bounded to 128 entries
# to keep the cache small.
_HASH_CACHE: dict[tuple[str, int, int], str] = {}
_HASH_CACHE_MAX = 128


def hash_file(path: Path) -> str:
    """Return sha256 hex digest of a file's contents.

    Raises FileNotFoundError if path does not exist.

    Cached per (absolute-path, mtime_ns, size) within the Python process.
    Any mutation changes mtime_ns or size and invalidates the entry on
    the next call — no stale-hash risk.
    """
    try:
        st = path.stat()
    except OSError:
        # File missing → let open() raise the canonical error below.
        st = None
    if st is not None:
        key = (str(path.resolve()), st.st_mtime_ns, st.st_size)
        cached = _HASH_CACHE.get(key)
        if cached is not None:
            return cached

    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    digest = h.hexdigest()

    if st is not None:
        # Evict oldest if over cap (FIFO is fine; hot keys churn little).
        if len(_HASH_CACHE) >= _HASH_CACHE_MAX:
            try:
                _HASH_CACHE.pop(next(iter(_HASH_CACHE)))
            except StopIteration:
                pass
        _HASH_CACHE[(str(path.resolve()), st.st_mtime_ns, st.st_size)] = digest
    return digest


__all__ = [
    "write_receipt",
    "read_receipt",
    "require_receipt",
    "validate_chain",
    "hash_file",
    "plan_validated_receipt_matches",
    "plan_audit_matches",
    "receipt_plan_routing",
    "receipt_spec_validated",
    "receipt_plan_validated",
    "receipt_executor_routing",
    "receipt_executor_done",
    "receipt_audit_routing",
    "receipt_audit_done",
    "receipt_retrospective",
    "receipt_post_completion",
    "receipt_planner_spawn",
    "receipt_plan_audit",
    "receipt_tdd_tests",
    "receipt_human_approval",
    "receipt_postmortem_generated",
    "receipt_postmortem_analysis",
    "receipt_postmortem_skipped",
    "receipt_calibration_applied",
    "receipt_rules_check_passed",
    "receipt_force_override",
    "RECEIPT_CONTRACT_VERSION",
    "CALIBRATION_POLICY_FILES",
    "INJECTED_PROMPTS_DIR",
    "INJECTED_AUDITOR_PROMPTS_DIR",
    "INJECTED_PLANNER_PROMPTS_DIR",
]

# Map receipt steps to human-readable execution-log entries
_LOG_MESSAGES: dict[str, str] = {
    "plan-routing": "[ROUTE] plan-skill → {route_mode} agent={agent_name}",
    "spec-validated": "[DONE] spec validated — {criteria_count} acceptance criteria",
    "plan-validated": "[DONE] plan validated — {segment_count} segments, criteria {criteria_coverage}",
    "executor-routing": "[ROUTE] executor plan — {n_segments} segments routed",
    "audit-routing": "[ROUTE] audit plan — {n_auditors} auditors routed",
    "retrospective": "[DONE] retrospective — quality={quality_score} cost={cost_score} efficiency={efficiency_score}",
    "post-completion": "[DONE] post-completion — {n_handlers} handlers",
    "planner-discovery": "[DONE] planner discovery — tokens={tokens_used}",
    "planner-spec": "[DONE] planner spec — tokens={tokens_used}",
    "planner-plan": "[DONE] planner plan — tokens={tokens_used}",
    "plan-audit-check": "[DONE] plan audit — tokens={tokens_used}",
    "tdd-tests": "[DONE] TDD tests — tokens={tokens_used}",
    "human-approval-SPEC_REVIEW": "[DONE] human-approval SPEC_REVIEW — approver={approver}",
    "human-approval-PLAN_REVIEW": "[DONE] human-approval PLAN_REVIEW — approver={approver}",
    "human-approval-TDD_REVIEW": "[DONE] human-approval TDD_REVIEW — approver={approver}",
    "postmortem-generated": "[DONE] postmortem generated — anomalies={anomaly_count} patterns={pattern_count}",
    "postmortem-analysis": "[DONE] postmortem analysis — rules_added={rules_added}",
    "postmortem-skipped": "[DONE] postmortem skipped — reason={reason}",
    "calibration-applied": "[DONE] calibration applied — retros={retros_consumed} scores={scores_updated}",
    "rules-check-passed": "[DONE] rules check — {rules_evaluated} rules evaluated, {violations_count} violations",
    # Prefix pattern: force-override-{from_stage}-{to_stage} — handled in
    # write_receipt() via the prefix branch below. The entry here documents
    # the format string for reviewers; the {N} placeholder is filled in by
    # the writer using len(bypassed_gates) at write time.
    "force-override-*": "[FORCE] {from_stage} → {to_stage} — bypassed {N} gate(s)",
}


def _record_tokens(task_dir: Path, agent: str, model: str, tokens: int) -> None:
    """Record token usage to token-usage.json. Called by receipt writers."""
    try:
        from lib_tokens import record_tokens
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
        # Fallback: write directly if lib_tokens isn't available
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


def _atomic_write_text(path: Path, content: str) -> None:
    """Atomically write text to `path` via tempfile + os.replace.

    Avoids partial-write torn-state if the process is killed mid-write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def write_receipt(task_dir: Path, step_name: str, **payload: Any) -> Path:
    """Write a receipt proving a pipeline step completed.

    Returns the path to the written receipt file.

    Every receipt embeds `contract_version: RECEIPT_CONTRACT_VERSION`.
    Readers MUST tolerate older receipts without this field.
    """
    receipts = _receipts_dir(task_dir)
    receipts.mkdir(parents=True, exist_ok=True)

    receipt = {
        "step": step_name,
        "ts": now_iso(),
        "valid": True,
        "contract_version": RECEIPT_CONTRACT_VERSION,
        **payload,
    }

    receipt_path = receipts / f"{step_name}.json"
    _atomic_write_text(receipt_path, json.dumps(receipt, indent=2, default=str))

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
        agent = payload.get("agent_name", "")
        # Distinguish learned vs generic by agent_name presence (post-v2 contract).
        injected = bool(agent)
        append_execution_log(task_dir, f"[DONE] {step_name} — executor={payload.get('executor_type','')} agent={'learned:' + agent if injected else 'generic'} tokens={payload.get('tokens_used','?')}")
    elif step_name.startswith("audit-") and step_name != "audit-routing":
        append_execution_log(task_dir, f"[DONE] {step_name} — findings={payload.get('finding_count',0)} blocking={payload.get('blocking_count',0)}")
    elif step_name.startswith("force-override-"):
        # Dedicated log line for forced transitions. Format pinned by
        # _LOG_MESSAGES["force-override-*"].
        bypassed = payload.get("bypassed_gates", [])
        n_bypassed = len(bypassed) if isinstance(bypassed, list) else 0
        from_stage = payload.get("from_stage", "?")
        to_stage = payload.get("to_stage", "?")
        append_execution_log(
            task_dir,
            f"[FORCE] {from_stage} → {to_stage} — bypassed {n_bypassed} gate(s)",
        )

    return receipt_path


def read_receipt(task_dir: Path, step_name: str) -> dict | None:
    """Read a receipt. Returns None if missing or invalid.

    Backwards-compat: receipts without `contract_version` are treated as v1
    and accepted as long as `valid=true`. Never crash on missing field.
    """
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



def validate_chain(task_dir: Path) -> list[str]:
    """Validate the entire receipt chain for a task. Returns list of gaps.

    This is a diagnostic tool — it checks all possible receipts and reports
    which ones are missing, without raising exceptions.

    Backwards-compat: receipts without `contract_version` are treated as v1
    and remain valid (no field is required for legacy reads).
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

    # What receipts should exist based on stage progression.
    # `audit-routing` is required at DONE — without it we cannot enumerate
    # the dynamic audit receipts that should follow it.
    stage_requires: dict[str, list[str]] = {
        "EXECUTION": ["plan-validated"],
        "TEST_EXECUTION": ["plan-validated", "executor-routing"],
        "CHECKPOINT_AUDIT": ["plan-validated", "executor-routing"],
        "REPAIR_PLANNING": ["plan-validated", "executor-routing"],
        "REPAIR_EXECUTION": ["plan-validated", "executor-routing"],
        "DONE": [
            "plan-validated",
            "executor-routing",
            "audit-routing",
            "retrospective",
            "post-completion",
        ],
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

    # Check audit receipts if we're at DONE.
    # audit-routing absence is already reported via stage_requires above;
    # we still enumerate dynamic audit receipts when routing IS present.
    if stage == "DONE":
        audit_routing = read_receipt(task_dir, "audit-routing")
        if audit_routing:
            for auditor in audit_routing.get("auditors", []):
                if auditor.get("action") == "spawn":
                    name = auditor.get("name", "")
                    if name and read_receipt(task_dir, f"audit-{name}") is None:
                        gaps.append(f"audit-{name}")

    return gaps



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


def _hash_artifact(path: Path) -> str | None:
    """Return sha256 hex of file content, or None if the file is missing."""
    try:
        return hash_file(path)
    except (FileNotFoundError, OSError):
        return None


def receipt_spec_validated(
    task_dir: Path,
    criteria_count: int,
    spec_sha256: str,
) -> Path:
    """Write receipt proving spec.md passed validation.

    Payload includes {criteria_count, spec_sha256, valid: true}.
    """
    if not isinstance(criteria_count, int) or criteria_count < 0:
        raise ValueError("criteria_count must be a non-negative int")
    if not isinstance(spec_sha256, str) or not spec_sha256:
        raise ValueError("spec_sha256 must be a non-empty string")
    return write_receipt(
        task_dir,
        "spec-validated",
        criteria_count=criteria_count,
        spec_sha256=spec_sha256,
    )


def receipt_plan_validated(
    task_dir: Path,
    segment_count: int,
    criteria_coverage: list[int],
    validation_passed: bool = True,
) -> Path:
    """Write receipt proving plan + execution graph passed validation.

    Captures content hashes of spec.md, plan.md, and execution-graph.json
    so downstream consumers (e.g. execute preflight) can short-circuit
    re-validation when none of the artifacts have changed since the
    receipt was written.
    """
    artifact_hashes = {
        "spec.md": _hash_artifact(task_dir / "spec.md"),
        "plan.md": _hash_artifact(task_dir / "plan.md"),
        "execution-graph.json": _hash_artifact(task_dir / "execution-graph.json"),
    }
    return write_receipt(
        task_dir,
        "plan-validated",
        segment_count=segment_count,
        criteria_coverage=criteria_coverage,
        validation_passed=validation_passed,
        artifact_hashes=artifact_hashes,
    )


def plan_validated_receipt_matches(task_dir: Path) -> "bool | str":
    """Return True if a plan-validated receipt exists AND its captured
    artifact hashes match the current spec.md, plan.md, and
    execution-graph.json content.

    Returns ``False`` when the receipt is missing or malformed (e.g. an
    older receipt without an ``artifact_hashes`` payload). Returns a
    descriptive string naming the drifted artifact (e.g.
    ``"plan.md hash drift"``) when the receipt is present but one of the
    tracked artifacts has changed on disk. Callers distinguish these
    three outcomes to surface drift-vs-missing distinctly.
    """
    receipt = read_receipt(task_dir, "plan-validated")
    if receipt is None or not receipt.get("validation_passed", False):
        return False
    captured = receipt.get("artifact_hashes")
    if not isinstance(captured, dict):
        # Old receipts written before hashes existed — treat as drift,
        # forcing re-validation. Safer than assuming they match.
        return False
    for name in ("spec.md", "plan.md", "execution-graph.json"):
        current = _hash_artifact(task_dir / name)
        if current != captured.get(name):
            return f"{name} hash drift"
    return True


def plan_audit_matches(task_dir: Path) -> "bool | str":
    """Return ``True`` if a ``plan-audit-check`` receipt exists AND its
    captured artifact hashes match the current spec.md, plan.md, and
    execution-graph.json content.

    Returns ``False`` when the receipt is missing entirely. Returns a
    descriptive string naming the drifted artifact (e.g.
    ``"plan.md hash drift"``) when the receipt is present but one of the
    tracked artifacts has changed on disk since the audit ran. Callers
    distinguish the three outcomes to surface drift-vs-missing distinctly
    at the PLAN_AUDIT exit gate.

    Receipts written before the hash-binding landed (pre-F2) do not carry
    ``spec_sha256``/``plan_sha256``/``graph_sha256`` fields. Such receipts
    are treated as ``False`` (missing) so the gate forces a fresh audit
    rather than silently trusting a legacy payload.
    """
    receipt = read_receipt(task_dir, "plan-audit-check")
    if receipt is None:
        return False
    # Legacy receipts (pre-F2) lacked the three hash fields. Without
    # hashes we cannot verify freshness — behave like missing.
    expected_spec = receipt.get("spec_sha256")
    expected_plan = receipt.get("plan_sha256")
    expected_graph = receipt.get("graph_sha256")
    if not (
        isinstance(expected_spec, str) and expected_spec
        and isinstance(expected_plan, str) and expected_plan
        and isinstance(expected_graph, str) and expected_graph
    ):
        return False
    # Hash each artifact on disk. Missing files count as drift with a
    # descriptive string (the audit was computed over a file that is now
    # absent — clearly not fresh).
    current_spec = _hash_artifact(task_dir / "spec.md")
    if current_spec != expected_spec:
        return "spec.md hash drift"
    current_plan = _hash_artifact(task_dir / "plan.md")
    if current_plan != expected_plan:
        return "plan.md hash drift"
    current_graph = _hash_artifact(task_dir / "execution-graph.json")
    if current_graph != expected_graph:
        return "execution-graph.json hash drift"
    return True


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
    injected_prompt_sha256: str,
    agent_name: str | None,
    evidence_path: str | None,
    tokens_used: int | None,
) -> Path:
    """Write receipt proving an executor segment completed.

    Asserts the per-segment injected prompt sidecar at
    ``task_dir / "receipts" / "_injected-prompts" / f"{segment_id}.sha256"``
    exists and matches `injected_prompt_sha256`. Raises:

    - ``ValueError("... injected_prompt_sha256 sidecar missing ...")`` if
      the sidecar file does not exist.
    - ``ValueError("... injected_prompt_sha256 mismatch ...")`` if the
      sidecar contents do not match the supplied digest.

    Also records token usage to token-usage.json — the only reliable path
    for token recording since receipts are gated.
    """
    if not isinstance(injected_prompt_sha256, str) or not injected_prompt_sha256:
        raise ValueError("injected_prompt_sha256 must be a non-empty string")

    sidecar_dir = task_dir / "receipts" / INJECTED_PROMPTS_DIR
    sidecar_file = sidecar_dir / f"{segment_id}.sha256"

    if not sidecar_file.exists():
        raise ValueError(
            f"executor-{segment_id}: injected_prompt_sha256 sidecar missing "
            f"at {sidecar_file}"
        )
    try:
        on_disk = sidecar_file.read_text().strip()
    except OSError as e:
        raise ValueError(
            f"executor-{segment_id}: injected_prompt_sha256 sidecar missing "
            f"(unreadable {sidecar_file}: {e})"
        ) from e
    if on_disk != injected_prompt_sha256:
        raise ValueError(
            f"executor-{segment_id}: injected_prompt_sha256 mismatch "
            f"(sidecar={on_disk!r}, payload={injected_prompt_sha256!r})"
        )

    # Record tokens deterministically
    if tokens_used and tokens_used > 0:
        _record_tokens(task_dir, f"{executor_type}-{segment_id}", model_used or "default", tokens_used)

    return write_receipt(
        task_dir,
        f"executor-{segment_id}",
        segment_id=segment_id,
        executor_type=executor_type,
        model_used=model_used,
        injected_prompt_sha256=injected_prompt_sha256,
        agent_name=agent_name,
        evidence_path=evidence_path,
        tokens_used=tokens_used,
    )


def receipt_audit_routing(
    task_dir: Path,
    auditors: list[dict],
) -> Path:
    """Write receipt proving all auditor routing decisions were made.

    Each entry in `auditors` MUST include the keys:
      - injected_agent_sha256: str | None
          (None only when route_mode == "generic"; otherwise required)
      - agent_path: str | None
    Callers are responsible for populating these; this writer enforces
    presence so downstream consumers can rely on the schema.
    """
    if not isinstance(auditors, list):
        raise ValueError("auditors must be a list")
    for idx, entry in enumerate(auditors):
        if not isinstance(entry, dict):
            raise ValueError(f"auditors[{idx}] must be a dict")
        if "injected_agent_sha256" not in entry:
            raise ValueError(
                f"auditors[{idx}] missing required key 'injected_agent_sha256' "
                f"(must be str or None)"
            )
        if "agent_path" not in entry:
            raise ValueError(
                f"auditors[{idx}] missing required key 'agent_path' "
                f"(must be str or None)"
            )
        route_mode = entry.get("route_mode")
        injected = entry.get("injected_agent_sha256")
        # injected_agent_sha256 may be None only when route_mode is generic.
        if injected is None and route_mode != "generic":
            raise ValueError(
                f"auditors[{idx}] injected_agent_sha256 may be None only "
                f"when route_mode=='generic' (got route_mode={route_mode!r})"
            )
        if injected is not None and not isinstance(injected, str):
            raise ValueError(
                f"auditors[{idx}] injected_agent_sha256 must be str or None"
            )
        agent_path = entry.get("agent_path")
        if agent_path is not None and not isinstance(agent_path, str):
            raise ValueError(
                f"auditors[{idx}] agent_path must be str or None"
            )

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
    *,
    route_mode: str,
    agent_path: str | None,
    injected_agent_sha256: str | None,
) -> Path:
    """Write receipt proving an auditor completed.

    Asserts the per-(auditor, model) sidecar at
    ``task_dir / "receipts" / "_injected-auditor-prompts"
    / f"{auditor_name}-{model_used}.sha256"`` matches
    `injected_agent_sha256` when non-null. Per-model disambiguation lets
    ensemble voting compare distinct injected prompts per model.

    Raises ValueError on sidecar mismatch or missing. There is no env
    bypass — sidecar enforcement is unconditional.

    Also records token usage — same enforcement path as executor receipts.
    """
    if not isinstance(route_mode, str) or not route_mode:
        raise ValueError("route_mode must be a non-empty string")
    if injected_agent_sha256 is None and route_mode != "generic":
        raise ValueError(
            f"injected_agent_sha256 may be None only when route_mode=='generic' "
            f"(got route_mode={route_mode!r})"
        )
    if injected_agent_sha256 is not None and not isinstance(injected_agent_sha256, str):
        raise ValueError("injected_agent_sha256 must be str or None")
    if agent_path is not None and not isinstance(agent_path, str):
        raise ValueError("agent_path must be str or None")

    if injected_agent_sha256 is not None:
        sidecar_file = (
            task_dir / "receipts" / INJECTED_AUDITOR_PROMPTS_DIR
            / f"{auditor_name}-{model_used}.sha256"
        )
        if not sidecar_file.exists():
            raise ValueError(
                f"audit-{auditor_name}: injected auditor prompt sidecar "
                f"missing at {sidecar_file}"
            )
        try:
            on_disk = sidecar_file.read_text().strip()
        except OSError as e:
            raise ValueError(
                f"audit-{auditor_name}: injected auditor prompt sidecar "
                f"unreadable at {sidecar_file}: {e}"
            ) from e
        if on_disk != injected_agent_sha256:
            raise ValueError(
                f"audit-{auditor_name}: injected_agent_sha256 mismatch "
                f"(sidecar={on_disk!r}, payload={injected_agent_sha256!r})"
            )

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
        route_mode=route_mode,
        agent_path=agent_path,
        injected_agent_sha256=injected_agent_sha256,
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
) -> Path:
    """Write receipt proving post-completion pipeline ran.

    Per the v2 contract, post-completion no longer carries postmortem or
    pattern-update flags — those concerns belong to dedicated postmortem
    receipts (see `receipt_postmortem_*`).
    """
    return write_receipt(
        task_dir,
        "post-completion",
        handlers_run=handlers_run,
    )


# ---------------------------------------------------------------------------
# Start skill spawn receipts (planner, plan-audit, TDD)
# ---------------------------------------------------------------------------


_INJECTED_PROMPT_SHA256_MISSING = object()


def receipt_planner_spawn(  # called dynamically from skills/start/SKILL.md
    task_dir: Path,
    phase: str,  # "discovery", "spec", or "plan"
    tokens_used: int | None,
    model_used: str | None = None,
    agent_name: str | None = None,
    injected_prompt_sha256: str = _INJECTED_PROMPT_SHA256_MISSING,  # type: ignore[assignment]
) -> Path:
    """Write receipt proving a planner subagent completed. Also records tokens.

    SEC-004 hardening: ``injected_prompt_sha256`` is REQUIRED at the call
    site. Omitting the kwarg entirely raises ``TypeError`` (the sentinel
    default is a deliberate forced-kwarg pattern so a forgotten sidecar
    assertion cannot silently ship). Passing ``injected_prompt_sha256=None``
    explicitly is ALSO rejected — ``None`` is no longer a legal value and
    raises ``ValueError`` with a message containing the substring
    ``legacy None path removed``. The only valid value is a non-empty
    sha256 hex digest captured from
    ``hooks/router.py planner-inject-prompt --task-id <id> --phase <phase>``.

    The writer asserts that the per-phase planner injected-prompt sidecar
    at ``task_dir / "receipts" / INJECTED_PLANNER_PROMPTS_DIR /
    f"{phase}.sha256"`` exists AND its contents (after stripping trailing
    whitespace) match the supplied digest. On missing file or mismatch
    this function raises ``ValueError`` naming the phase. The mismatch
    message contains the literal substring ``hash mismatch`` so
    downstream tests can pin it.

    The sidecar path is
    ``task_dir/receipts/_injected-planner-prompts/{phase}.sha256`` — both
    writer and reader import the directory name from
    ``INJECTED_PLANNER_PROMPTS_DIR`` so the schema is defined in exactly
    one place. The sidecar itself is written by the
    ``planner-inject-prompt`` CLI subcommand in ``hooks/router.py``.
    """
    if injected_prompt_sha256 is _INJECTED_PROMPT_SHA256_MISSING:
        raise TypeError(
            "receipt_planner_spawn: injected_prompt_sha256 is required. "
            "Pass a non-empty sha256 hex digest obtained from "
            "`hooks/router.py planner-inject-prompt --task-id <id> "
            "--phase <phase>`. The None (no-sidecar) path has been removed."
        )
    if injected_prompt_sha256 is None:
        raise ValueError(
            "injected_prompt_sha256 must be a non-empty sha256 hex string; "
            "legacy None path removed"
        )
    step_name = f"planner-{phase}"

    # Sidecar assertion — unconditional now. Every caller must first run
    # `hooks/router.py planner-inject-prompt` and pass the captured digest.
    if not isinstance(injected_prompt_sha256, str) or not injected_prompt_sha256:
        raise ValueError(
            "receipt_planner_spawn: injected_prompt_sha256 must be a "
            "non-empty string"
        )
    sidecar_file = (
        task_dir / "receipts" / INJECTED_PLANNER_PROMPTS_DIR
        / f"{phase}.sha256"
    )
    if not sidecar_file.exists():
        raise ValueError(
            f"receipt_planner_spawn: planner sidecar missing for phase "
            f"{phase!r} at {sidecar_file}. Run `hooks/router.py "
            f"planner-inject-prompt --task-id <id> --phase {phase}` first."
        )
    try:
        on_disk = sidecar_file.read_text().strip()
    except OSError as e:
        raise ValueError(
            f"receipt_planner_spawn: planner sidecar unreadable for "
            f"phase {phase!r} at {sidecar_file}: {e}"
        ) from e
    if on_disk != injected_prompt_sha256:
        raise ValueError(
            f"receipt_planner_spawn: hash mismatch for phase {phase!r} "
            f"— sidecar={on_disk!r}, payload={injected_prompt_sha256!r}."
        )

    if tokens_used and tokens_used > 0:
        _record_tokens(task_dir, f"planner-{phase}", model_used or "default", tokens_used)
    return write_receipt(
        task_dir,
        step_name,
        phase=phase,
        tokens_used=tokens_used,
        model_used=model_used,
        agent_name=agent_name,
        injected_prompt_sha256=injected_prompt_sha256,
    )


def receipt_plan_audit(
    task_dir: Path,
    tokens_used: int | None,
    finding_count: int = 0,
    model_used: str | None = None,
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

    Also records token usage to ``token-usage.json`` when ``tokens_used``
    is positive.
    """
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

    if tokens_used and tokens_used > 0:
        _record_tokens(task_dir, "plan-audit-check", model_used or "default", tokens_used)
    return write_receipt(
        task_dir,
        "plan-audit-check",
        tokens_used=tokens_used,
        finding_count=finding_count,
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
    if not isinstance(tests_evidence_sha256, str) or not tests_evidence_sha256:
        raise ValueError("tests_evidence_sha256 must be a non-empty string")
    if not isinstance(tokens_used, int) or tokens_used < 0:
        raise ValueError("tokens_used must be a non-negative int")
    if not isinstance(model_used, str) or not model_used:
        raise ValueError("model_used must be a non-empty string")

    if tokens_used > 0:
        _record_tokens(task_dir, "tdd-tests", model_used, tokens_used)

    return write_receipt(
        task_dir,
        "tdd-tests",
        test_file_paths=test_file_paths,
        tests_evidence_sha256=tests_evidence_sha256,
        tokens_used=tokens_used,
        model_used=model_used,
    )


# ---------------------------------------------------------------------------
# Human approval, postmortem, and calibration receipts
# ---------------------------------------------------------------------------


def receipt_human_approval(
    task_dir: Path,
    stage: str,
    artifact_sha256: str,
    approver: str = "human",
) -> Path:
    """Write receipt proving a human approved an artifact at `stage`.

    Writes ``receipts/human-approval-{stage}.json``.

    Validates inputs: `stage` must be a non-empty identifier-safe string
    (no path separators), `artifact_sha256` must be a non-empty string,
    `approver` defaults to "human".
    """
    if not isinstance(stage, str) or not stage:
        raise ValueError("stage must be a non-empty string")
    if "/" in stage or "\\" in stage or stage.startswith("."):
        raise ValueError(f"stage must not contain path separators: {stage!r}")
    if not isinstance(artifact_sha256, str) or not artifact_sha256:
        raise ValueError("artifact_sha256 must be a non-empty string")
    if not isinstance(approver, str) or not approver:
        raise ValueError("approver must be a non-empty string")

    return write_receipt(
        task_dir,
        f"human-approval-{stage}",
        stage=stage,
        artifact_sha256=artifact_sha256,
        approver=approver,
    )


def receipt_postmortem_generated(
    task_dir: Path,
    json_sha256: str,
    md_sha256: str,
    anomaly_count: int,
    pattern_count: int,
) -> Path:
    """Write receipt proving a postmortem (json+md pair) was generated."""
    if not isinstance(json_sha256, str) or not json_sha256:
        raise ValueError("json_sha256 must be a non-empty string")
    if not isinstance(md_sha256, str) or not md_sha256:
        raise ValueError("md_sha256 must be a non-empty string")
    if not isinstance(anomaly_count, int) or anomaly_count < 0:
        raise ValueError("anomaly_count must be a non-negative int")
    if not isinstance(pattern_count, int) or pattern_count < 0:
        raise ValueError("pattern_count must be a non-negative int")
    return write_receipt(
        task_dir,
        "postmortem-generated",
        json_sha256=json_sha256,
        md_sha256=md_sha256,
        anomaly_count=anomaly_count,
        pattern_count=pattern_count,
    )


def receipt_postmortem_analysis(
    task_dir: Path,
    analysis_sha256: str,
    rules_added: int,
    rules_sha256_after: str,
) -> Path:
    """Write receipt proving postmortem analysis ran and rules updated."""
    if not isinstance(analysis_sha256, str) or not analysis_sha256:
        raise ValueError("analysis_sha256 must be a non-empty string")
    if not isinstance(rules_added, int) or rules_added < 0:
        raise ValueError("rules_added must be a non-negative int")
    if not isinstance(rules_sha256_after, str) or not rules_sha256_after:
        raise ValueError("rules_sha256_after must be a non-empty string")
    return write_receipt(
        task_dir,
        "postmortem-analysis",
        analysis_sha256=analysis_sha256,
        rules_added=rules_added,
        rules_sha256_after=rules_sha256_after,
    )


def receipt_postmortem_skipped(
    task_dir: Path,
    reason: str,
    retrospective_sha256: str,
) -> Path:
    """Write receipt proving postmortem was deliberately skipped.

    `reason` is enum-validated against {"clean-task", "no-findings",
    "quality-above-threshold"}.
    """
    if reason not in _POSTMORTEM_SKIP_REASONS:
        raise ValueError(
            f"invalid postmortem skip reason: {reason!r} "
            f"(allowed: {sorted(_POSTMORTEM_SKIP_REASONS)})"
        )
    if not isinstance(retrospective_sha256, str) or not retrospective_sha256:
        raise ValueError("retrospective_sha256 must be a non-empty string")
    return write_receipt(
        task_dir,
        "postmortem-skipped",
        reason=reason,
        retrospective_sha256=retrospective_sha256,
    )


def receipt_calibration_applied(
    task_dir: Path,
    retros_consumed: int,
    scores_updated: int,
    policy_sha256_before: str,
    policy_sha256_after: str,
) -> Path:
    """Write receipt proving calibration policy update was applied.

    Calibration is deterministic — this writer does NOT call
    ``_record_tokens``; no model invocation is involved.
    """
    if not isinstance(retros_consumed, int) or retros_consumed < 0:
        raise ValueError("retros_consumed must be a non-negative int")
    if not isinstance(scores_updated, int) or scores_updated < 0:
        raise ValueError("scores_updated must be a non-negative int")
    if not isinstance(policy_sha256_before, str) or not policy_sha256_before:
        raise ValueError("policy_sha256_before must be a non-empty string")
    if not isinstance(policy_sha256_after, str) or not policy_sha256_after:
        raise ValueError("policy_sha256_after must be a non-empty string")
    return write_receipt(
        task_dir,
        "calibration-applied",
        retros_consumed=retros_consumed,
        scores_updated=scores_updated,
        policy_sha256_before=policy_sha256_before,
        policy_sha256_after=policy_sha256_after,
    )


def receipt_rules_check_passed(
    task_dir: Path,
    rules_evaluated: int,
    violations_count: int,
    error_violations: int,
    mode: str,
    advisory_violations: int = 0,
    rules_file_sha256: str = "none",
) -> Path:
    """Write receipt proving a rules-check pass (no error-severity violations).

    This writer is a *passed-receipt by construction*: it REFUSES to write if
    ``error_violations != 0``. The rules-check pipeline must take a different
    path (failure path) when errors are present — this receipt proves the
    clean outcome only.

    Validates:
      - All four counts are non-negative ints.
      - ``error_violations <= violations_count``.
      - ``error_violations == 0`` (else raises ValueError).
      - ``mode`` is one of {"staged", "all"}.

    ``engine_version`` is hardcoded to ``"1"`` to avoid importing rules_engine
    (which may not exist yet during early bootstrap of this feature).
    ``checked_at`` is stamped via ``now_iso()``.
    """
    if not isinstance(rules_evaluated, int) or isinstance(rules_evaluated, bool) or rules_evaluated < 0:
        raise ValueError("rules_evaluated must be a non-negative int")
    if not isinstance(violations_count, int) or isinstance(violations_count, bool) or violations_count < 0:
        raise ValueError("violations_count must be a non-negative int")
    if not isinstance(error_violations, int) or isinstance(error_violations, bool) or error_violations < 0:
        raise ValueError("error_violations must be a non-negative int")
    if not isinstance(advisory_violations, int) or isinstance(advisory_violations, bool) or advisory_violations < 0:
        raise ValueError("advisory_violations must be a non-negative int")
    if error_violations > violations_count:
        raise ValueError(
            f"error_violations ({error_violations}) must be <= violations_count "
            f"({violations_count})"
        )
    if error_violations != 0:
        raise ValueError(
            f"receipt_rules_check_passed REFUSES to write: error_violations="
            f"{error_violations} (must be 0 — this receipt is a passed-receipt "
            f"by construction; use a failure-path receipt when errors exist)"
        )
    if mode not in ("staged", "all"):
        raise ValueError(
            f"mode must be 'staged' or 'all' (got mode={mode!r})"
        )
    if not isinstance(rules_file_sha256, str) or not rules_file_sha256:
        raise ValueError("rules_file_sha256 must be a non-empty string")

    return write_receipt(
        task_dir,
        "rules-check-passed",
        rules_evaluated=rules_evaluated,
        violations_count=violations_count,
        error_violations=error_violations,
        advisory_violations=advisory_violations,
        engine_version="1",
        rules_file_sha256=rules_file_sha256,
        checked_at=now_iso(),
        mode=mode,
    )


def receipt_force_override(
    task_dir: Path,
    from_stage: str,
    to_stage: str,
    bypassed_gates: list[str],
) -> Path:
    """Write receipt proving a forced stage transition occurred.

    Emitted by ``transition_task`` when invoked with ``force=True``. The
    payload enumerates the gate errors that would have been raised if
    ``force`` were ``False`` (``bypassed_gates``) so the audit chain
    records not just that force was used but which guardrails it
    bypassed.

    Writes ``receipts/force-override-{from_stage}-{to_stage}.json``. One
    force per edge — subsequent forced transitions over the same edge
    overwrite via the atomic write path.

    Validation:
      - ``from_stage`` and ``to_stage`` MUST be non-empty strings; empty
        or non-string values raise ``ValueError`` naming the arg.
      - ``bypassed_gates`` MUST be a list (possibly empty). Every entry
        MUST be a string. Any other container type or non-string entry
        raises ``ValueError``.
    """
    if not isinstance(from_stage, str) or not from_stage:
        raise ValueError("from_stage must be a non-empty string")
    if not isinstance(to_stage, str) or not to_stage:
        raise ValueError("to_stage must be a non-empty string")
    # SEC-002 hardening: stage names MUST be strict uppercase identifier
    # slugs. Prevents path traversal via crafted manifest["stage"] values
    # like "../../etc/x" reaching the receipt filename.
    import re as _re_stage
    _STAGE_RE = r"^[A-Z][A-Z0-9_]*$"
    if not _re_stage.match(_STAGE_RE, from_stage):
        raise ValueError(
            f"from_stage must match {_STAGE_RE} (got {from_stage!r})"
        )
    if not _re_stage.match(_STAGE_RE, to_stage):
        raise ValueError(
            f"to_stage must match {_STAGE_RE} (got {to_stage!r})"
        )
    if not isinstance(bypassed_gates, list):
        raise ValueError("bypassed_gates must be a list of strings")
    for idx, entry in enumerate(bypassed_gates):
        if not isinstance(entry, str):
            raise ValueError(
                f"bypassed_gates[{idx}] must be a string (got {type(entry).__name__})"
            )

    step_name = f"force-override-{from_stage}-{to_stage}"
    return write_receipt(
        task_dir,
        step_name,
        from_stage=from_stage,
        to_stage=to_stage,
        bypassed_gates=list(bypassed_gates),
        bypassed_count=len(bypassed_gates),
        forced_at=now_iso(),
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
