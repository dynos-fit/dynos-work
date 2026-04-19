#!/usr/bin/env python3
"""Validation functions for dynos-work artifacts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable

from lib_core import (
    ALLOWED_STAGE_TRANSITIONS,
    VALID_CLASSIFICATION_TYPES,
    VALID_DOMAINS,
    VALID_EXECUTORS,
    VALID_RISK_LEVELS,
    load_json,
    require,
    write_json,
)

REQUIRED_SPEC_HEADINGS: list[str] = [
    "Task Summary",
    "User Context",
    "Acceptance Criteria",
    "Implicit Requirements Surfaced",
    "Out of Scope",
    "Assumptions",
    "Risk Notes",
]

REQUIRED_PLAN_HEADINGS: list[str] = [
    "Technical Approach",
    "Reference Code",
    "Components / Modules",
    "Data Flow",
    "Error Handling Strategy",
    "Test Strategy",
    "Dependency Graph",
    "Open Questions",
]

# Domain sets that trigger conditional plan sections.
_API_CONTRACT_DOMAINS: set[str] = {"backend", "ui", "security"}
_DATA_MODEL_DOMAINS: set[str] = {"db"}


_HIGH_RISK_LEVELS: set[str] = {"high", "critical"}


def conditional_plan_headings(domains: Iterable[str], risk_level: str = "medium") -> list[str]:
    """Return additional required plan headings based on classification.

    - ``API Contracts`` is required when domains include backend, ui, or security.
    - ``Data Model`` is required when domains include db.
    - ``Architecture Decisions`` is required when risk_level is high or critical.
    """
    extra: list[str] = []
    domain_set = set(domains) if not isinstance(domains, set) else domains
    if domain_set & _API_CONTRACT_DOMAINS:
        extra.append("API Contracts")
    if domain_set & _DATA_MODEL_DOMAINS:
        extra.append("Data Model")
    if risk_level in _HIGH_RISK_LEVELS:
        extra.append("Architecture Decisions")
    return extra


def validate_generated_html(html_path: Path) -> list[str]:
    """Validate generated HTML for common template rendering bugs.

    Returns a list of error strings. Empty list means valid.
    """
    errors: list[str] = []
    try:
        content = html_path.read_text()
    except (FileNotFoundError, OSError) as exc:
        return [f"cannot read {html_path}: {exc}"]

    style_match = re.search(r"<style>(.*?)</style>", content, re.DOTALL)
    if style_match:
        style = style_match.group(1)
        double_open = len(re.findall(r"\{\{", style))
        double_close = len(re.findall(r"\}\}", style))
        if double_open > 0:
            errors.append(f"CSS contains {double_open} doubled '{{{{' sequences (template escaping bug)")
        if double_close > 0:
            errors.append(f"CSS contains {double_close} doubled '}}}}' sequences (template escaping bug)")

    script_match = re.search(r"<script>(.*?)</script>", content, re.DOTALL)
    if script_match:
        script = script_match.group(1)
        template_bugs = len(re.findall(r"\$\{\{", script))
        if template_bugs > 0:
            errors.append(f"JS contains {template_bugs} '${{{{' sequences (should be '${{' in template literals)")

    required_ids = {"stats", "updated", "lineage", "routes", "queue", "sparkline", "gaps", "demotions", "runs"}
    for eid in required_ids:
        if f'id="{eid}"' not in content:
            errors.append(f"missing required element id='{eid}'")

    return errors


def collect_headings(markdown: str) -> set[str]:
    """Extract all level-2 headings from markdown text."""
    return {
        match.group(1).strip()
        for match in re.finditer(r"^##\s+(.+?)\s*$", markdown, flags=re.MULTILINE)
    }


def parse_acceptance_criteria(spec_text: str) -> list[int]:
    """Parse numbered acceptance criteria from a spec document."""
    numbers: list[int] = []
    in_section = False
    for line in spec_text.splitlines():
        if line.startswith("## "):
            in_section = line.strip() == "## Acceptance Criteria"
            continue
        if not in_section:
            continue
        match = re.match(r"^(\d+)\.\s+\S+", line)
        if match:
            numbers.append(int(match.group(1)))
    return numbers


def detect_cycle(graph: dict) -> bool:
    """Detect cycles in a dependency graph."""
    visiting: set[str] = set()
    visited: set[str] = set()
    by_id = {segment["id"]: segment for segment in graph.get("segments", []) if isinstance(segment, dict) and "id" in segment}

    def walk(node_id: str) -> bool:
        if node_id in visited:
            return False
        if node_id in visiting:
            return True
        visiting.add(node_id)
        for dep in by_id.get(node_id, {}).get("depends_on", []):
            if isinstance(dep, str) and walk(dep):
                return True
        visiting.remove(node_id)
        visited.add(node_id)
        return False

    return any(walk(node_id) for node_id in by_id)


def validate_manifest(manifest: dict) -> list[str]:
    """Validate a task manifest for required fields and valid values."""
    errors: list[str] = []
    for key in ("task_id", "created_at", "raw_input", "stage"):
        if key not in manifest:
            errors.append(f"manifest missing key: {key}")
    stage = manifest.get("stage")
    if stage not in ALLOWED_STAGE_TRANSITIONS:
        errors.append(f"manifest has invalid stage: {stage!r}")
    classification = manifest.get("classification")
    if classification is not None:
        ctype = classification.get("type")
        if ctype not in VALID_CLASSIFICATION_TYPES:
            errors.append(f"classification.type invalid: {ctype!r}")
        risk = classification.get("risk_level")
        if risk not in VALID_RISK_LEVELS:
            errors.append(f"classification.risk_level invalid: {risk!r}")
        domains = classification.get("domains")
        if not isinstance(domains, list) or not domains:
            errors.append("classification.domains must be a non-empty array")
        else:
            for domain in domains:
                if domain not in VALID_DOMAINS:
                    errors.append(f"classification domain invalid: {domain!r}")
    return errors


def compute_fast_track(manifest: dict) -> bool:
    """Determine if a task qualifies for fast-track execution."""
    classification = manifest.get("classification")
    if not isinstance(classification, dict):
        return False
    if classification.get("risk_level") != "low":
        return False
    domains = classification.get("domains", [])
    if not isinstance(domains, list) or len(domains) != 1:
        return False
    return True


def apply_fast_track(task_dir: Path) -> bool:
    """Compute fast-track eligibility AND persist the result to ``manifest.json``.

    This is NOT a pure computation. As a side-effect, this function writes the
    boolean result back to ``task_dir/manifest.json`` under the ``fast_track``
    key (overwriting any prior value). Callers that only want the eligibility
    decision without persisting it should use ``compute_fast_track`` directly.

    Eligibility rule (delegated to ``compute_fast_track``): a task is
    fast-track-eligible when its ``classification.risk_level`` is ``"low"`` AND
    its ``classification.domains`` list contains exactly one element.

    Returns the same boolean that was written to the manifest: ``True`` if
    fast-track-eligible, ``False`` otherwise.
    """
    manifest_path = task_dir / "manifest.json"
    manifest = load_json(manifest_path)
    fast = compute_fast_track(manifest)
    manifest["fast_track"] = fast
    write_json(manifest_path, manifest)
    return fast


# Stages at which each artifact becomes required
_SPEC_REQUIRED_AFTER = {"SPEC_NORMALIZATION", "SPEC_REVIEW", "PLANNING", "PLAN_REVIEW",
                         "PLAN_AUDIT", "EXECUTION_GRAPH_BUILD", "PRE_EXECUTION_SNAPSHOT",
                         "EXECUTION", "TEST_EXECUTION", "CHECKPOINT_AUDIT", "FINAL_AUDIT",
                         "REPAIR_PLANNING", "REPAIR_EXECUTION", "DONE"}
_PLAN_REQUIRED_AFTER = {"PLANNING", "PLAN_REVIEW", "PLAN_AUDIT", "EXECUTION_GRAPH_BUILD",
                         "PRE_EXECUTION_SNAPSHOT", "EXECUTION", "TEST_EXECUTION",
                         "CHECKPOINT_AUDIT", "FINAL_AUDIT", "REPAIR_PLANNING",
                         "REPAIR_EXECUTION", "DONE"}


def validate_task_artifacts(
    task_dir: Path,
    strict: bool = False,
    *,
    run_gap: bool = True,
) -> list[str]:
    """Validate all task artifacts in a task directory.

    Stage-aware: only checks artifacts that should exist at the current stage.
    At FOUNDRY_INITIALIZED, only manifest is required.

    The plan gap analysis (`run_gap_analysis`) is the heaviest part of this
    function — it walks up to 2000 source files in the repo. Callers that
    have already validated the same plan (e.g., execute preflight after
    planning ran gap analysis successfully) can pass `run_gap=False` to skip
    it. The default stays True so existing callers see no behavior change.
    """
    errors: list[str] = []
    manifest_path = task_dir / "manifest.json"
    spec_path = task_dir / "spec.md"
    plan_path = task_dir / "plan.md"
    graph_path = task_dir / "execution-graph.json"

    try:
        manifest = load_json(manifest_path)
    except FileNotFoundError:
        return [f"missing required file: {manifest_path}"]
    except json.JSONDecodeError as exc:
        return [f"invalid JSON in {manifest_path}: {exc}"]

    errors.extend(validate_manifest(manifest))
    stage = manifest.get("stage", "")

    # Spec is only required after SPEC_NORMALIZATION
    if stage not in _SPEC_REQUIRED_AFTER and not spec_path.exists():
        # Early stage — spec doesn't exist yet, that's fine
        return errors

    try:
        spec_text = require(spec_path)
    except FileNotFoundError:
        if stage in _SPEC_REQUIRED_AFTER or strict:
            return errors + [f"missing required file: {spec_path}"]
        return errors

    spec_headings = collect_headings(spec_text)
    for heading in REQUIRED_SPEC_HEADINGS:
        if heading not in spec_headings:
            errors.append(f"spec missing heading: {heading}")

    criteria_numbers = parse_acceptance_criteria(spec_text)
    if not criteria_numbers:
        errors.append("spec has no acceptance criteria")
    else:
        expected = list(range(1, len(criteria_numbers) + 1))
        if criteria_numbers != expected:
            errors.append(f"acceptance criteria numbering must be contiguous from 1: got {criteria_numbers}")

    # AC 15: if a spec-validated receipt exists, its recorded spec_sha256 must
    # match the current sha256(spec.md). Hash-drift is a validation error.
    try:
        from lib_receipts import hash_file, read_receipt  # local import to avoid cycles
        receipt = read_receipt(task_dir, "spec-validated")
        if receipt is not None:
            recorded = receipt.get("spec_sha256")
            current = hash_file(spec_path)
            if recorded and recorded != current:
                errors.append(
                    f"spec.md content drifted from approved spec-validated receipt "
                    f"(expected {recorded[:12]}, got {current[:12]})"
                )
    except ImportError:
        pass

    if plan_path.exists():
        plan_text = require(plan_path)
        classification = manifest.get("classification") or {}
        domains = classification.get("domains", [])
        risk_level = classification.get("risk_level", "medium")
        all_required = REQUIRED_PLAN_HEADINGS + conditional_plan_headings(domains, risk_level)
        # Hoisted out of the inner loop — collect_headings is a regex scan
        # over plan_text and was being re-executed on every required heading.
        plan_headings = collect_headings(plan_text)
        for heading in all_required:
            if heading not in plan_headings:
                errors.append(f"plan missing heading: {heading}")
        in_ref_section = False
        for line in plan_text.splitlines():
            if line.startswith("## "):
                in_ref_section = line.strip() == "## Reference Code"
                continue
            if not in_ref_section:
                continue
            for match in re.finditer(r"`([^`]+\.[a-zA-Z]{1,5})`", line):
                ref_path = match.group(1)
                if "to-be-created" in line.lower():
                    continue
                full = task_dir.parent.parent / ref_path
                if not full.exists():
                    errors.append(f"plan Reference Code path does not exist: {ref_path}")

        # Gap analysis: verify API Contracts / Data Model claims against code.
        # Skipped when caller passes run_gap=False (e.g., execute preflight
        # after planning has already validated the same plan).
        if run_gap:
            from plan_gap_analysis import findings_from_report, run_gap_analysis
            project_root = task_dir.parent.parent
            gap_report = run_gap_analysis(project_root, task_dir)
            errors.extend(findings_from_report(gap_report))
    elif strict or stage in _PLAN_REQUIRED_AFTER:
        if not plan_path.exists():
            errors.append(f"missing required file: {plan_path}")

    if graph_path.exists():
        try:
            graph = load_json(graph_path)
        except json.JSONDecodeError as exc:
            errors.append(f"invalid JSON in {graph_path}: {exc}")
            graph = {}
        segments = graph.get("segments")
        if not isinstance(segments, list) or not segments:
            errors.append("execution graph must contain a non-empty segments array")
        else:
            seen_files: dict[str, str] = {}
            segment_ids: list[str] = []
            criteria_set = set(criteria_numbers)
            for segment in segments:
                if not isinstance(segment, dict):
                    errors.append("every segment must be an object")
                    continue
                segment_id = segment.get("id")
                if not segment_id or not isinstance(segment_id, str):
                    errors.append("every segment must have a string id")
                    continue
                segment_ids.append(segment_id)
                executor = segment.get("executor")
                if executor not in VALID_EXECUTORS:
                    errors.append(f"{segment_id}: invalid executor {executor!r}")
                files_expected = segment.get("files_expected")
                if not isinstance(files_expected, list) or not files_expected:
                    errors.append(f"{segment_id}: files_expected must be a non-empty array")
                else:
                    for file_path in files_expected:
                        if not isinstance(file_path, str):
                            errors.append(f"{segment_id}: file path must be a string")
                            continue
                        if Path(file_path).is_absolute() or ".." in Path(file_path).parts:
                            errors.append(f"{segment_id}: file path must stay inside repo: {file_path}")
                        owner = seen_files.get(file_path)
                        if owner and owner != segment_id:
                            errors.append(f"file {file_path} appears in multiple segments: {owner}, {segment_id}")
                        else:
                            seen_files[file_path] = segment_id
                depends_on = segment.get("depends_on", [])
                if not isinstance(depends_on, list):
                    errors.append(f"{segment_id}: depends_on must be an array")
                criteria_ids = segment.get("criteria_ids")
                if not isinstance(criteria_ids, list) or not criteria_ids:
                    errors.append(f"{segment_id}: criteria_ids must be a non-empty array")
                else:
                    for criterion_id in criteria_ids:
                        if criterion_id not in criteria_set:
                            errors.append(f"{segment_id}: criteria_id {criterion_id!r} does not exist in spec")
            if len(segment_ids) != len(set(segment_ids)):
                errors.append("execution graph segment ids must be unique")
            by_id = {segment.get("id"): segment for segment in segments if isinstance(segment, dict) and segment.get("id")}
            for segment_id, segment in by_id.items():
                for dep in segment.get("depends_on", []):
                    if dep not in by_id:
                        errors.append(f"{segment_id}: depends_on references missing segment {dep}")
            if detect_cycle(graph):
                errors.append("execution graph must be acyclic")
            covered = {
                criterion_id
                for segment in segments
                if isinstance(segment, dict)
                for criterion_id in segment.get("criteria_ids", [])
                if isinstance(criterion_id, int)
            }
            missing = sorted(set(criteria_numbers) - covered)
            if missing:
                errors.append(f"uncovered acceptance criteria: {missing}")
    elif strict:
        errors.append(f"missing required file: {graph_path}")

    errors.extend(validate_repair_log(task_dir))
    errors.extend(validate_retrospective(task_dir))
    return errors


def validate_repair_log(task_dir: Path) -> list[str]:
    """Validate repair-log.json if present."""
    path = task_dir / "repair-log.json"
    if not path.exists():
        return []
    try:
        data = load_json(path)
    except json.JSONDecodeError as exc:
        return [f"invalid JSON in {path}: {exc}"]
    errors: list[str] = []
    batches = data.get("batches")
    if not isinstance(batches, list):
        return ["repair-log batches must be an array"]
    seen_batch_ids: set[str] = set()
    for batch in batches:
        if not isinstance(batch, dict):
            errors.append("repair-log batch must be an object")
            continue
        batch_id = batch.get("batch_id")
        if not isinstance(batch_id, str):
            errors.append("repair-log batch missing string batch_id")
        elif batch_id in seen_batch_ids:
            errors.append(f"duplicate repair batch id: {batch_id}")
        else:
            seen_batch_ids.add(batch_id)
        tasks = batch.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            errors.append(f"{batch_id}: tasks must be a non-empty array")
            continue
        for task in tasks:
            if not isinstance(task, dict):
                errors.append(f"{batch_id}: task must be an object")
                continue
            executor = task.get("assigned_executor")
            if executor not in VALID_EXECUTORS:
                errors.append(f"{batch_id}: invalid assigned_executor {executor!r}")
            files = task.get("files_to_modify")
            if not isinstance(files, list) or not files:
                errors.append(f"{batch_id}: files_to_modify must be a non-empty array")
            retry_count = task.get("retry_count", 0)
            if not isinstance(retry_count, int) or retry_count < 0:
                errors.append(f"{batch_id}: retry_count must be a non-negative integer")
    return errors


def validate_retrospective(task_dir: Path) -> list[str]:
    """Validate task-retrospective.json if present."""
    path = task_dir / "task-retrospective.json"
    if not path.exists():
        return []
    try:
        data = load_json(path)
    except json.JSONDecodeError as exc:
        return [f"invalid JSON in {path}: {exc}"]
    errors: list[str] = []
    required: dict[str, type] = {
        "task_id": str,
        "task_outcome": str,
        "task_type": str,
        "task_domains": str,
        "task_risk_level": str,
        "findings_by_auditor": dict,
        "findings_by_category": dict,
        "executor_repair_frequency": dict,
        "spec_review_iterations": int,
        "repair_cycle_count": int,
        "subagent_spawn_count": int,
        "wasted_spawns": int,
        "auditor_zero_finding_streaks": dict,
        "executor_zero_repair_streak": int,
    }
    for key, expected_type in required.items():
        value = data.get(key)
        if not isinstance(value, expected_type):
            errors.append(f"retrospective field {key!r} must be {expected_type.__name__}")
    for key in ("quality_score", "cost_score", "efficiency_score"):
        if key in data:
            value = data[key]
            if not isinstance(value, (int, float)) or not 0 <= value <= 1:
                errors.append(f"retrospective field {key!r} must be a number in [0, 1]")
    # DORA fields — optional for backwards compat with old retrospectives
    if "lead_time_seconds" in data and data["lead_time_seconds"] is not None:
        if not isinstance(data["lead_time_seconds"], (int, float)) or data["lead_time_seconds"] < 0:
            errors.append("retrospective field 'lead_time_seconds' must be a non-negative number")
    if "change_failure" in data:
        if not isinstance(data["change_failure"], bool):
            errors.append("retrospective field 'change_failure' must be a boolean")
    if "recovery_time_seconds" in data and data["recovery_time_seconds"] is not None:
        if not isinstance(data["recovery_time_seconds"], (int, float)) or data["recovery_time_seconds"] < 0:
            errors.append("retrospective field 'recovery_time_seconds' must be a non-negative number")
    return errors


RISK_BUDGETS: dict[str, int] = {"low": 8000, "medium": 12000, "high": 18000, "critical": 25000}


def compute_reward(task_dir: Path) -> dict:
    """Deterministically compute reward scores from task artifacts.

    Reads audit reports, repair-log, token-usage, execution-log, and manifest.
    Returns the full task-retrospective dict ready to be written to disk.
    """
    task_dir = Path(task_dir)
    task_id = task_dir.name

    # --- 1. Scan audit reports ---
    findings_by_auditor: dict[str, int] = {}
    findings_by_category: dict[str, int] = {}
    total_findings = 0
    total_blocking = 0
    reports_dir = task_dir / "audit-reports"
    if reports_dir.exists():
        for report_path in sorted(reports_dir.glob("*.json")):
            try:
                report = load_json(report_path)
            except (json.JSONDecodeError, OSError):
                continue
            findings = report.get("findings", [])
            # Sanitize hallucinated findings: if recommendation/description
            # says "no action required" or "confirmed" but blocking=True,
            # downgrade to non-blocking minor.
            _confirm_signals = ("no action required", "no action needed",
                                "correctly implemented", "properly implemented",
                                "no changes needed", "no fix needed")
            for finding in findings:
                if not finding.get("blocking"):
                    continue
                rec = str(finding.get("recommendation", "")).lower()
                desc = str(finding.get("description", "")).lower()
                title = str(finding.get("title", "")).lower()
                if any(s in rec or s in desc for s in _confirm_signals):
                    finding["blocking"] = False
                    finding["severity"] = "minor"
                elif "confirmed" in title and "not" not in title:
                    finding["blocking"] = False
                    finding["severity"] = "minor"
            auditor = report.get("auditor_name", report_path.stem)
            count = len(findings)
            findings_by_auditor[auditor] = findings_by_auditor.get(auditor, 0) + count
            total_findings += count
            total_blocking += sum(1 for f in findings if f.get("blocking"))
            for finding in findings:
                fid = finding.get("id", "")
                category = fid.split("-")[0] if "-" in fid else fid
                if category:
                    findings_by_category[category] = findings_by_category.get(category, 0) + 1

    # --- 2. Repair log ---
    executor_repair_frequency: dict[str, int] = {}
    repair_cycle_count = 0
    repair_log_path = task_dir / "repair-log.json"
    if repair_log_path.exists():
        try:
            repair_log = load_json(repair_log_path)
            repair_cycle_count = int(repair_log.get("repair_cycle", 0))
            for batch in repair_log.get("batches", []):
                for task in batch.get("tasks", []):
                    executor = task.get("assigned_executor", "")
                    if executor:
                        executor_repair_frequency[executor] = executor_repair_frequency.get(executor, 0) + 1
        except (json.JSONDecodeError, OSError):
            pass

    # --- 3. Spec review iterations ---
    # Count approval receipts (not log lines): matches the current filename
    # `human-approval-SPEC_REVIEW.json` and any future rotation suffix
    # (e.g. `human-approval-SPEC_REVIEW-002.json`). No fallback to
    # `execution-log.md` scanning — the log-line scanner was deprecated
    # because log lines are not a hash-bound audit record.
    #
    # SEC-005 hardening: plain `touch human-approval-SPEC_REVIEW-fake.json`
    # (empty file, no content) no longer inflates the count. Each
    # candidate must parse as a JSON object AND carry the expected
    # `step == "human-approval-SPEC_REVIEW"` field (and non-empty
    # `artifact_sha256`). Receipts written by `receipt_human_approval`
    # satisfy this automatically; planted files do not.
    receipts_dir = task_dir / "receipts"
    spec_review_iterations = 0
    if receipts_dir.is_dir():
        for candidate in receipts_dir.glob("human-approval-SPEC_REVIEW*.json"):
            try:
                payload = json.loads(candidate.read_text("utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            step = payload.get("step")
            if not isinstance(step, str) or not step.startswith("human-approval-SPEC_REVIEW"):
                continue
            art_hash = payload.get("artifact_sha256")
            if not isinstance(art_hash, str) or not art_hash:
                continue
            spec_review_iterations += 1

    # execution-log.md path is still needed below for DORA recovery-time
    # tracking and subagent-spawn counting (sections 4b and 6).
    log_path = task_dir / "execution-log.md"

    # --- 4. Classification from manifest ---
    manifest = load_json(task_dir / "manifest.json")
    classification = manifest.get("classification", {})
    task_type = classification.get("type", "feature")
    task_domains = ",".join(classification.get("domains", []))
    task_risk_level = classification.get("risk_level", "medium")

    # --- 4b. DORA metrics ---
    created_at = manifest.get("created_at")
    completed_at = manifest.get("completed_at")
    lead_time_seconds: int | None = None
    if created_at and completed_at:
        try:
            from datetime import datetime, timezone
            t0 = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
            lead_time_seconds = max(0, int((t1 - t0).total_seconds()))
        except (ValueError, TypeError):
            pass

    stage = manifest.get("stage", "")
    change_failure = stage == "REPAIR_FAILED"

    recovery_time_seconds: int | None = None
    if change_failure and log_path.exists():
        try:
            from datetime import datetime, timezone
            fail_time = None
            done_time = None
            for line in log_path.read_text().splitlines():
                if "REPAIR_FAILED" in line and fail_time is None:
                    ts = line.split("]")[0].lstrip("[").strip() if "]" in line else ""
                    if ts:
                        try:
                            fail_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        except ValueError:
                            pass
                if "[ADVANCE]" in line and "DONE" in line and done_time is None:
                    ts = line.split("]")[0].lstrip("[").strip() if "]" in line else ""
                    if ts:
                        try:
                            done_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        except ValueError:
                            pass
            if fail_time and done_time and done_time > fail_time:
                recovery_time_seconds = int((done_time - fail_time).total_seconds())
        except (OSError, ValueError):
            pass

    # --- 5. Token and model tracking ---
    from lib_tokens import get_summary as _get_token_summary
    token_data = _get_token_summary(task_dir)
    token_usage_by_agent: dict[str, int] = token_data.get("agents", {})
    total_token_usage: int = token_data.get("total", 0)
    total_input_tokens: int = token_data.get("total_input_tokens", 0)
    total_output_tokens: int = token_data.get("total_output_tokens", 0)
    token_usage_by_model: dict[str, dict] = token_data.get("by_model", {})
    input_tokens_by_agent: dict[str, int] = {}
    output_tokens_by_agent: dict[str, int] = {}
    model_used_by_agent: dict[str, str] = {}
    for agent, info in token_data.get("by_agent", {}).items():
        if isinstance(info, dict):
            input_tokens_by_agent[agent] = info.get("input_tokens", 0)
            output_tokens_by_agent[agent] = info.get("output_tokens", 0)
            m = info.get("model")
            if m and m not in ("none", "n/a", "", "unknown"):
                model_used_by_agent[agent] = m

    # --- 6. Spawn/waste tracking ---
    subagent_spawn_count = 0
    if log_path.exists():
        try:
            for line in log_path.read_text().splitlines():
                if "[SPAWN]" in line:
                    subagent_spawn_count += 1
        except OSError:
            pass

    wasted_spawns = 0
    if reports_dir.exists():
        for report_path in sorted(reports_dir.glob("*.json")):
            try:
                report = load_json(report_path)
                if len(report.get("findings", [])) == 0:
                    wasted_spawns += 1
            except (json.JSONDecodeError, OSError):
                continue

    # --- 7. Reward vector ---
    # quality_score — only blocking findings affect quality.
    # Non-blocking findings are informational and don't penalize.
    if total_blocking == 0:
        quality_score = 0.9 if total_findings > 0 else 0.9
    else:
        surviving_blocking = total_blocking - repair_cycle_count
        quality_score = 1.0 - (max(0, surviving_blocking) / total_blocking)

    # cost_score
    budget = RISK_BUDGETS.get(task_risk_level, 12000)
    if subagent_spawn_count == 0 or total_token_usage == 0:
        cost_score = 1.0
    else:
        avg_tokens = total_token_usage / subagent_spawn_count
        cost_score = 1.0 / (1.0 + (avg_tokens / budget))

    # efficiency_score
    efficiency_score = 1.0 - (repair_cycle_count / 3.0) - (max(0, spec_review_iterations - 1) * 0.1)

    # Clamp all to [0, 1]
    quality_score = max(0.0, min(1.0, quality_score))
    cost_score = max(0.0, min(1.0, cost_score))
    efficiency_score = max(0.0, min(1.0, efficiency_score))

    return {
        "task_id": task_id,
        "task_outcome": "DONE",
        "task_type": task_type,
        "task_domains": task_domains,
        "task_risk_level": task_risk_level,
        "findings_by_auditor": findings_by_auditor,
        "findings_by_category": findings_by_category,
        "executor_repair_frequency": executor_repair_frequency,
        "spec_review_iterations": spec_review_iterations,
        "repair_cycle_count": repair_cycle_count,
        "subagent_spawn_count": subagent_spawn_count,
        "wasted_spawns": wasted_spawns,
        "token_usage_by_agent": token_usage_by_agent,
        "total_token_usage": total_token_usage,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "input_tokens_by_agent": input_tokens_by_agent,
        "output_tokens_by_agent": output_tokens_by_agent,
        "token_usage_by_model": token_usage_by_model,
        "model_used_by_agent": model_used_by_agent,
        "quality_score": round(quality_score, 4),
        "cost_score": round(cost_score, 4),
        "efficiency_score": round(efficiency_score, 4),
        "lead_time_seconds": lead_time_seconds,
        "change_failure": change_failure,
        "recovery_time_seconds": recovery_time_seconds,
    }


def check_segment_ownership(task_dir: Path, segment_id: str, files: Iterable[str]) -> list[str]:
    """Check that files are owned by the specified segment."""
    graph = load_json(task_dir / "execution-graph.json")
    for segment in graph.get("segments", []):
        if segment.get("id") == segment_id:
            allowed = set(segment.get("files_expected", []))
            return [file_path for file_path in files if file_path not in allowed]
    raise ValueError(f"Unknown segment id: {segment_id}")
