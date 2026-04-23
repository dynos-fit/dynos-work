#!/usr/bin/env python3
"""Validation functions for dynos-work artifacts."""

from __future__ import annotations

import json
import os
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
    write_ctl_json,
    write_json,
)
from write_policy import find_write_violations

def require_nonblank(value: str, *, field_name: str) -> str:
    """Validate that *value* is a non-empty string and return its stripped form.

    Raises:
        TypeError: if *value* is not a ``str``.
        ValueError: if the stripped *value* is empty.

    Returns the stripped value when valid.
    """
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string, got {type(value).__name__}")
    stripped = value.strip()
    if stripped == "":
        raise ValueError(f"{field_name} must be non-empty")
    return stripped


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


def extract_markdown_section(markdown: str, heading: str) -> str:
    """Return the body of a level-2 markdown section, or empty string."""
    pattern = rf"^##\s+{re.escape(heading)}\s*$"
    match = re.search(pattern, markdown, flags=re.MULTILINE)
    if not match:
        return ""
    start = match.end()
    rest = markdown[start:]
    next_heading = re.search(r"^##\s+", rest, flags=re.MULTILINE)
    if next_heading:
        return rest[:next_heading.start()]
    return rest


def _contains_file_reference(text: str) -> bool:
    return bool(re.search(r"`?[\w./-]+\.[A-Za-z0-9]{1,8}`?", text))


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


def _collect_audit_findings(task_dir: Path) -> dict[str, dict]:
    """Return audit findings keyed by finding id from live audit reports."""
    findings_by_id: dict[str, dict] = {}
    audit_dir = task_dir / "audit-reports"
    if not audit_dir.is_dir():
        return findings_by_id
    for report_path in sorted(audit_dir.glob("*.json")):
        try:
            payload = load_json(report_path)
        except (json.JSONDecodeError, OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        findings = payload.get("findings")
        if not isinstance(findings, list):
            continue
        auditor_name = payload.get("auditor_name") or payload.get("auditor") or report_path.stem
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            finding_id = finding.get("id")
            if not isinstance(finding_id, str) or not finding_id:
                continue
            if finding_id in findings_by_id:
                continue
            row = dict(finding)
            row["_auditor_name"] = auditor_name
            findings_by_id[finding_id] = row
    return findings_by_id


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
        if not isinstance(classification, dict):
            errors.append("classification must be an object")
            return errors
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
            seen_domains: set[str] = set()
            for domain in domains:
                if not isinstance(domain, str) or not domain.strip():
                    errors.append("classification.domains entries must be non-empty strings")
                    continue
                if domain not in VALID_DOMAINS:
                    errors.append(f"classification domain invalid: {domain!r}")
                if domain in seen_domains:
                    errors.append(f"classification.domains contains duplicate entry: {domain!r}")
                else:
                    seen_domains.add(domain)
        if "notes" in classification and not isinstance(classification.get("notes"), str):
            errors.append("classification.notes must be a string")
        if "tdd_required" in classification and not isinstance(classification.get("tdd_required"), bool):
            errors.append("classification.tdd_required must be a boolean")
        if "fast_track" in classification and not isinstance(classification.get("fast_track"), bool):
            errors.append("classification.fast_track must be a boolean")
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

    Side-effect — TDD auto-derivation (AC 12):
      When ``manifest.classification.tdd_required`` is absent (key not present
      in the dict), derive it as ``risk_level in {"high", "critical"}`` and
      persist it alongside ``fast_track`` in the same atomic write. Explicit
      values (``True`` or ``False``) are preserved and never overwritten.

    Returns the same boolean that was written to the manifest: ``True`` if
    fast-track-eligible, ``False`` otherwise.
    """
    manifest_path = task_dir / "manifest.json"
    manifest = load_json(manifest_path)
    fast = compute_fast_track(manifest)
    manifest["fast_track"] = fast

    # AC 12: auto-derive tdd_required only when the key is absent from
    # classification. Any explicit True/False value set upstream is preserved.
    classification = manifest.get("classification")
    if isinstance(classification, dict) and "tdd_required" not in classification:
        risk_level = classification.get("risk_level")
        classification["tdd_required"] = risk_level in _HIGH_RISK_LEVELS

    write_ctl_json(task_dir, manifest_path, manifest)
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

        components_section = extract_markdown_section(plan_text, "Components / Modules")
        if components_section.strip():
            component_chunks = [
                chunk for chunk in re.split(r"(?=^###\s+)", components_section, flags=re.MULTILINE)
                if chunk.strip()
            ]
            has_component_subsections = any(
                chunk.lstrip().startswith("### ") for chunk in component_chunks
            )
            if has_component_subsections:
                for chunk in component_chunks:
                    stripped = chunk.strip()
                    if not stripped or not stripped.startswith("### "):
                        continue
                    heading_line = stripped.splitlines()[0].strip()
                    if not _contains_file_reference(chunk):
                        errors.append(
                            f"plan component section missing exact files: {heading_line}"
                        )

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
        graph_task_id = graph.get("task_id")
        manifest_task_id = manifest.get("task_id")
        if graph_task_id is not None and manifest_task_id is not None and graph_task_id != manifest_task_id:
            errors.append(
                f"execution graph task_id mismatch: graph={graph_task_id!r} manifest={manifest_task_id!r}"
            )
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
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", segment_id):
                    errors.append(f"{segment_id}: segment id must match [A-Za-z0-9][A-Za-z0-9_.-]*")
                segment_ids.append(segment_id)
                description = segment.get("description")
                if not isinstance(description, str) or not description.strip():
                    errors.append(f"{segment_id}: description must be a non-empty string")
                executor = segment.get("executor")
                if executor not in VALID_EXECUTORS:
                    errors.append(f"{segment_id}: invalid executor {executor!r}")
                files_expected = segment.get("files_expected")
                if not isinstance(files_expected, list) or not files_expected:
                    errors.append(f"{segment_id}: files_expected must be a non-empty array")
                else:
                    segment_seen_files: set[str] = set()
                    for file_path in files_expected:
                        if not isinstance(file_path, str) or not file_path.strip():
                            errors.append(f"{segment_id}: file path must be a string")
                            continue
                        if Path(file_path).is_absolute() or ".." in Path(file_path).parts:
                            errors.append(f"{segment_id}: file path must stay inside repo: {file_path}")
                        if file_path in segment_seen_files:
                            errors.append(f"{segment_id}: duplicate file in files_expected: {file_path}")
                        else:
                            segment_seen_files.add(file_path)
                        owner = seen_files.get(file_path)
                        if owner and owner != segment_id:
                            errors.append(f"file {file_path} appears in multiple segments: {owner}, {segment_id}")
                        else:
                            seen_files[file_path] = segment_id
                depends_on = segment.get("depends_on", [])
                if not isinstance(depends_on, list):
                    errors.append(f"{segment_id}: depends_on must be an array")
                else:
                    seen_deps: set[str] = set()
                    for dep in depends_on:
                        if not isinstance(dep, str) or not dep.strip():
                            errors.append(f"{segment_id}: depends_on entries must be non-empty strings")
                            continue
                        if dep == segment_id:
                            errors.append(f"{segment_id}: depends_on cannot reference itself")
                        if dep in seen_deps:
                            errors.append(f"{segment_id}: duplicate depends_on entry: {dep}")
                        else:
                            seen_deps.add(dep)
                criteria_ids = segment.get("criteria_ids")
                if not isinstance(criteria_ids, list) or not criteria_ids:
                    errors.append(f"{segment_id}: criteria_ids must be a non-empty array")
                else:
                    seen_criteria_ids: set[int] = set()
                    for criterion_id in criteria_ids:
                        if not isinstance(criterion_id, int):
                            errors.append(f"{segment_id}: criteria_id must be an integer")
                            continue
                        if criterion_id in seen_criteria_ids:
                            errors.append(f"{segment_id}: duplicate criteria_id: {criterion_id}")
                        else:
                            seen_criteria_ids.add(criterion_id)
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
    try:
        manifest = load_json(task_dir / "manifest.json")
    except (json.JSONDecodeError, OSError, ValueError, FileNotFoundError):
        manifest = {}
    manifest_task_id = manifest.get("task_id") if isinstance(manifest, dict) else None
    task_id = data.get("task_id")
    if task_id is not None and not isinstance(task_id, str):
        errors.append("repair-log task_id must be a string")
    elif (
        isinstance(task_id, str)
        and isinstance(manifest_task_id, str)
        and task_id != manifest_task_id
    ):
        errors.append(
            f"repair-log task_id mismatch: repair-log={task_id!r} manifest={manifest_task_id!r}"
        )
    repair_cycle = data.get("repair_cycle")
    if repair_cycle is not None and (not isinstance(repair_cycle, int) or repair_cycle < 0):
        errors.append("repair-log repair_cycle must be a non-negative integer")
    live_findings = _collect_audit_findings(task_dir)
    batches = data.get("batches")
    if not isinstance(batches, list):
        return ["repair-log batches must be an array"]
    seen_batch_ids: set[str] = set()
    seen_finding_ids: set[str] = set()
    for batch in batches:
        if not isinstance(batch, dict):
            errors.append("repair-log batch must be an object")
            continue
        batch_id = batch.get("batch_id")
        if not isinstance(batch_id, str) or not batch_id.strip():
            errors.append("repair-log batch missing string batch_id")
        elif batch_id in seen_batch_ids:
            errors.append(f"duplicate repair batch id: {batch_id}")
        else:
            seen_batch_ids.add(batch_id)
        parallel = batch.get("parallel")
        if parallel is not None and not isinstance(parallel, bool):
            errors.append(f"{batch_id}: parallel must be a boolean")
        tasks = batch.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            errors.append(f"{batch_id}: tasks must be a non-empty array")
            continue
        for task in tasks:
            if not isinstance(task, dict):
                errors.append(f"{batch_id}: task must be an object")
                continue
            finding_id = task.get("finding_id")
            if not isinstance(finding_id, str) or not finding_id.strip():
                errors.append(f"{batch_id}: finding_id must be a non-empty string")
            elif finding_id in seen_finding_ids:
                errors.append(f"duplicate finding_id across repair-log batches: {finding_id}")
            else:
                seen_finding_ids.add(finding_id)
            live_finding = live_findings.get(finding_id) if isinstance(finding_id, str) else None
            if finding_id and not live_finding:
                errors.append(f"{batch_id}: finding_id not found in live audit reports: {finding_id}")
            auditor = task.get("auditor")
            if auditor is not None and (not isinstance(auditor, str) or not auditor.strip()):
                errors.append(f"{batch_id}: auditor must be a non-empty string when present")
            elif (
                isinstance(auditor, str)
                and live_finding is not None
                and auditor != live_finding.get("_auditor_name")
            ):
                errors.append(
                    f"{batch_id}: auditor mismatch for {finding_id}: "
                    f"repair-log={auditor!r} audit-report={live_finding.get('_auditor_name')!r}"
                )
            severity = task.get("severity")
            if severity is not None and severity not in {"critical", "high", "medium", "low"}:
                errors.append(f"{batch_id}: invalid severity {severity!r}")
            elif (
                severity is not None
                and live_finding is not None
                and isinstance(live_finding.get("severity"), str)
                and severity != live_finding.get("severity")
            ):
                errors.append(
                    f"{batch_id}: severity mismatch for {finding_id}: "
                    f"repair-log={severity!r} audit-report={live_finding.get('severity')!r}"
                )
            instruction = task.get("instruction")
            if not isinstance(instruction, str) or not instruction.strip():
                errors.append(f"{batch_id}: instruction must be a non-empty string")
            executor = task.get("assigned_executor")
            if executor not in VALID_EXECUTORS:
                errors.append(f"{batch_id}: invalid assigned_executor {executor!r}")
            files = task.get("affected_files")
            if files is None:
                files = task.get("files_to_modify")
            if not isinstance(files, list) or not files:
                errors.append(f"{batch_id}: affected_files must be a non-empty array")
            else:
                seen_files: set[str] = set()
                for file_path in files:
                    if not isinstance(file_path, str) or not file_path.strip():
                        errors.append(f"{batch_id}: affected_files entries must be non-empty strings")
                        continue
                    if Path(file_path).is_absolute() or ".." in Path(file_path).parts:
                        errors.append(f"{batch_id}: affected_files must stay inside repo: {file_path}")
                    if file_path in seen_files:
                        errors.append(f"{batch_id}: duplicate affected_files entry: {file_path}")
                    else:
                        seen_files.add(file_path)
            retry_count = task.get("retry_count", 0)
            if not isinstance(retry_count, int) or retry_count < 0:
                errors.append(f"{batch_id}: retry_count must be a non-negative integer")
            max_retries = task.get("max_retries")
            if max_retries is not None and (not isinstance(max_retries, int) or max_retries <= 0):
                errors.append(f"{batch_id}: max_retries must be a positive integer when present")
            status = task.get("status")
            if status is not None and status not in {"pending", "in_progress", "done", "failed", "blocked"}:
                errors.append(f"{batch_id}: invalid status {status!r}")
            model_override = task.get("model_override")
            if model_override is not None and model_override not in {"haiku", "sonnet", "opus"}:
                errors.append(f"{batch_id}: invalid model_override {model_override!r}")
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

    Idempotence contract (consumed by B-002 self-compute callers):
        Two calls with the same on-disk task artifacts produce an identical
        return value. No module-level caching; every call re-reads the
        underlying files fresh.

    Side effects (telemetry-only — do not feed back into the return value):
        * Appends structured events to ``.dynos/{task}/events.jsonl`` for
          anomalies (missing audit-routing receipt, auditor-not-in-routing
          reports, finding contradictions). These are append-only telemetry
          writes; repeated calls produce duplicate events in the log but do
          NOT change the returned dict.
        * ``lib_tokens.get_summary`` rewrites ``token-usage.json`` with
          resolved model names. The rewrite is content-idempotent once
          converged (legacy ``'default'`` entries get upgraded on first call;
          subsequent calls write the same bytes).

    Returned dict always contains at least: ``quality_score``, ``cost_score``,
    ``efficiency_score``, ``total_tokens`` (alias of ``total_token_usage``),
    plus the full retrospective shape validated by ``validate_retrospective``.
    """
    task_dir = Path(task_dir)
    task_id = task_dir.name
    root = task_dir.parent.parent

    # Lazy imports kept inside the function to preserve the existing module
    # boundary (compute_reward already imports several helpers lazily).
    from lib_log import log_event
    from lib_receipts import read_receipt
    from lib_core import collect_retrospectives

    # --- 0. AC 16: build the auditor cross-check allowlist ---
    # Load audit-routing receipt and derive the set of auditor names that
    # were actually spawned. Reports whose auditor is NOT in this set are
    # dropped (their findings don't count toward totals) and an event is
    # emitted so the anomaly surfaces in telemetry.
    routing_receipt = read_receipt(task_dir, "audit-routing")
    # SEC-005: missing audit-routing is a trust gap (attacker could suppress
    # the receipt to bypass cross-check). Full fail-closed causes unit-test
    # boilerplate churn; deferred to a future spec-level decision. For now:
    # log the gap and fall through to the permissive path (prior behavior).
    # See task-20260419-006 audit-summary.json for the accepted-risk note.
    valid_auditor_names: set[str] | None
    if routing_receipt is None:
        valid_auditor_names = None
        log_event(
            root,
            "auditor_cross_check_skipped",
            task=task_id,
            reason="audit-routing receipt missing",
        )
        _auditor_check_disabled = True
    else:
        valid_auditor_names = {
            a["name"]
            for a in routing_receipt.get("auditors", [])
            if isinstance(a, dict)
            and a.get("action") == "spawn"
            and isinstance(a.get("name"), str)
            and a.get("name")
        }
        _auditor_check_disabled = False

    # --- 1. Scan audit reports ---
    findings_by_auditor: dict[str, int] = {}
    findings_by_category: dict[str, int] = {}
    total_findings = 0
    total_blocking = 0
    # AC 15: track ids of findings that are still blocking post-repair.
    post_repair_blocking_ids: set[str] = set()
    reports_dir = task_dir / "audit-reports"
    if reports_dir.exists():
        for report_path in sorted(reports_dir.glob("*.json")):
            try:
                report = load_json(report_path)
            except (json.JSONDecodeError, OSError):
                continue

            # Resolve the auditor name. Prefer explicit fields; fall back to
            # the report filename stem so legacy reports still have an id.
            auditor = (
                report.get("auditor_name")
                or report.get("auditor")
                or report_path.stem
            )
            if not isinstance(auditor, str) or not auditor:
                auditor = report_path.stem

            # AC 16: drop the report if its auditor was not in audit-routing.
            # (When _auditor_check_disabled is True, the routing receipt was
            # missing — we log once above and accept all reports here.)
            if not _auditor_check_disabled and valid_auditor_names is not None and auditor not in valid_auditor_names:
                log_event(
                    root,
                    "auditor_not_in_routing",
                    task=task_id,
                    auditor=auditor,
                    report_path=str(report_path),
                )
                continue

            findings = report.get("findings", [])
            if not isinstance(findings, list):
                findings = []

            # AC 14: the old sanitizer silently downgraded blocking=True
            # findings whose recommendation/description/title matched an
            # "exemption phrase" (e.g. "no action required", "correctly
            # implemented"). That hid contradictions. We now EMIT an event
            # and leave the finding blocking -- the contradiction surfaces
            # as telemetry instead of being swallowed.
            _confirm_signals = (
                "no action required",
                "no action needed",
                "correctly implemented",
                "properly implemented",
                "no changes needed",
                "no fix needed",
            )
            for finding in findings:
                if not isinstance(finding, dict) or not finding.get("blocking"):
                    continue
                recommendation = str(finding.get("recommendation", ""))
                rec = recommendation.lower()
                desc = str(finding.get("description", "")).lower()
                title = str(finding.get("title", "")).lower()
                if any(s in rec or s in desc for s in _confirm_signals) or (
                    "confirmed" in title and "not" not in title
                ):
                    log_event(
                        root,
                        "finding_contradiction",
                        task=task_id,
                        auditor=auditor,
                        finding_id=finding.get("id"),
                        recommendation_snippet=recommendation[:120],
                    )
                    # NOTE: do NOT downgrade. Finding stays blocking=True.

            count = len(findings)
            findings_by_auditor[auditor] = findings_by_auditor.get(auditor, 0) + count
            total_findings += count
            total_blocking += sum(1 for f in findings if isinstance(f, dict) and f.get("blocking"))
            for finding in findings:
                if not isinstance(finding, dict):
                    continue
                fid = finding.get("id", "") or ""
                if finding.get("blocking") and isinstance(fid, str) and fid:
                    post_repair_blocking_ids.add(fid)
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

    # --- 2b. Route/source metadata + prior streak baselines ---
    prior_retro: dict | None = None
    try:
        retros = [
            item
            for item in collect_retrospectives(root)
            if isinstance(item, dict) and item.get("task_id") != task_id
        ]
        if retros:
            prior_retro = sorted(
                retros,
                key=lambda item: str(item.get("task_id", "")),
            )[-1]
    except Exception:
        prior_retro = None

    prior_auditor_streaks = (
        prior_retro.get("auditor_zero_finding_streaks", {})
        if isinstance(prior_retro, dict)
        else {}
    )
    if not isinstance(prior_auditor_streaks, dict):
        prior_auditor_streaks = {}
    prior_executor_zero = 0
    if isinstance(prior_retro, dict):
        try:
            prior_executor_zero = int(prior_retro.get("executor_zero_repair_streak", 0) or 0)
        except (TypeError, ValueError):
            prior_executor_zero = 0

    def _learned_source_from_path(agent_path: str | None) -> str:
        if not isinstance(agent_path, str) or not agent_path:
            return "generic"
        return f"learned:{Path(agent_path).stem}"

    def _finding_key(finding: dict) -> str | None:
        if not isinstance(finding, dict):
            return None
        file_path = str(finding.get("file", "") or "")
        line = finding.get("line")
        line_s = str(line) if line is not None else "?"
        fid = str(finding.get("id", "") or "")
        category = fid.split("-")[0] if "-" in fid else fid
        if not file_path and not category:
            return None
        return f"{file_path}:{line_s}:{category}"

    agent_source: dict[str, str] = {}
    alongside_overlap: dict[str, dict] = {}

    audit_routing = read_receipt(task_dir, "audit-routing")
    if isinstance(audit_routing, dict):
        auditors = audit_routing.get("auditors", [])
        if isinstance(auditors, list):
            for entry in auditors:
                if not isinstance(entry, dict):
                    continue
                if entry.get("action") != "spawn":
                    continue
                name = entry.get("name")
                if not isinstance(name, str) or not name:
                    continue
                route_mode = str(entry.get("route_mode", "") or "")
                learned_source = _learned_source_from_path(entry.get("agent_path"))
                if route_mode == "alongside":
                    agent_source[name] = "generic"
                    if learned_source != "generic":
                        agent_source[f"{name}:learned"] = learned_source
                    report_path = reports_dir / f"{name}.json"
                    merged_keys: list[str] = []
                    if report_path.exists():
                        try:
                            report = load_json(report_path)
                            findings = report.get("findings", []) if isinstance(report, dict) else []
                            if isinstance(findings, list):
                                merged_keys = sorted({
                                    key for finding in findings
                                    for key in [_finding_key(finding)]
                                    if key
                                })
                        except (json.JSONDecodeError, OSError):
                            merged_keys = []
                    alongside_overlap[name] = {
                        "generic_finding_keys": merged_keys,
                        "learned_finding_keys": merged_keys,
                        "learned_is_superset": True,
                        "alongside_task_count": 2,
                    }
                    continue
                agent_source[name] = "generic" if route_mode == "generic" else learned_source

    executor_routing = read_receipt(task_dir, "executor-routing")
    if isinstance(executor_routing, dict):
        segments = executor_routing.get("segments", [])
        by_executor: dict[str, str] = {}
        if isinstance(segments, list):
            for entry in segments:
                if not isinstance(entry, dict):
                    continue
                executor = entry.get("executor")
                if not isinstance(executor, str) or not executor:
                    continue
                route_mode = str(entry.get("route_mode", "") or "")
                learned_source = _learned_source_from_path(entry.get("agent_path"))
                candidate = "generic" if route_mode == "generic" else learned_source
                if route_mode == "alongside":
                    by_executor[executor] = "generic"
                    if learned_source != "generic":
                        by_executor[f"{executor}:learned"] = learned_source
                    continue
                if candidate != "generic" or executor not in by_executor:
                    by_executor[executor] = candidate
        agent_source.update(by_executor)

    auditor_zero_finding_streaks = {
        key: int(value)
        for key, value in prior_auditor_streaks.items()
        if isinstance(key, str) and isinstance(value, (int, float))
    }
    if isinstance(audit_routing, dict):
        auditors = audit_routing.get("auditors", [])
        if isinstance(auditors, list):
            for entry in auditors:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("name")
                if not isinstance(name, str) or not name:
                    continue
                prior_value = int(auditor_zero_finding_streaks.get(name, 0))
                if entry.get("action") == "skip":
                    auditor_zero_finding_streaks[name] = prior_value
                    continue
                auditor_zero_finding_streaks[name] = prior_value + 1 if findings_by_auditor.get(name, 0) == 0 else 0

    executor_zero_repair_streak = prior_executor_zero + 1 if not executor_repair_frequency else 0

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
    if repair_cycle_count > 0 and "repair-coordinator" in token_usage_by_agent:
        agent_source.setdefault("repair-coordinator", "generic")

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
        # AC 15: surviving_blocking is the SET INTERSECTION of
        # (findings that were blocking BEFORE repair) ∩ (findings that are
        # STILL blocking in the current audit-reports). The prior formula
        # `total_blocking - repair_cycle_count` was incoherent: repair_cycle
        # is a count of *cycles run*, not a count of *findings fixed*.
        pre_repair_path = task_dir / "repair" / "pre-repair-blocking.json"
        pre_repair_ids: set[str] | None = None
        if pre_repair_path.exists():
            try:
                raw = load_json(pre_repair_path)
                if isinstance(raw, list):
                    pre_repair_ids = {
                        str(item) for item in raw if isinstance(item, str) and item
                    }
                else:
                    # Malformed shape (not a list) — fall back.
                    pre_repair_ids = None
            except (json.JSONDecodeError, OSError):
                # Missing/unreadable/malformed JSON — fall back.
                pre_repair_ids = None

        if pre_repair_ids is not None:
            surviving_blocking = len(pre_repair_ids & post_repair_blocking_ids)
        else:
            # No repair ran (or file unreadable) — semantically every blocking
            # finding is "surviving".
            surviving_blocking = total_blocking
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
        "auditor_zero_finding_streaks": auditor_zero_finding_streaks,
        "executor_zero_repair_streak": executor_zero_repair_streak,
        "token_usage_by_agent": token_usage_by_agent,
        "total_token_usage": total_token_usage,
        # Alias exposed for self-compute callers (e.g. receipt_retrospective
        # under B-002). The canonical key remains ``total_token_usage`` for
        # backwards compatibility with dashboards and downstream consumers.
        "total_tokens": total_token_usage,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "input_tokens_by_agent": input_tokens_by_agent,
        "output_tokens_by_agent": output_tokens_by_agent,
        "token_usage_by_model": token_usage_by_model,
        "model_used_by_agent": model_used_by_agent,
        "agent_source": agent_source,
        "alongside_overlap": alongside_overlap,
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
    root = task_dir.parent.parent
    for segment in graph.get("segments", []):
        if segment.get("id") == segment_id:
            executor = segment.get("executor")
            if not isinstance(executor, str) or not executor.strip():
                raise ValueError(f"{segment_id}: segment missing executor")
            allowed = set(segment.get("files_expected", []))
            abs_paths: list[Path] = []
            for file_path in files:
                p = Path(file_path)
                if p.is_absolute():
                    abs_paths.append(p)
                    continue
                rel = p.as_posix()
                if (
                    rel in {"manifest.json", "execution-graph.json", "repair-log.json", "external-solution-gate.json", "token-usage.json", "events.jsonl"}
                    or rel.startswith("receipts/")
                    or rel.startswith("handoff-")
                    or rel.startswith("evidence/")
                    or rel.startswith("audit-reports/")
                ):
                    abs_paths.append(task_dir / p)
                else:
                    abs_paths.append(root / p)
            violations = find_write_violations(
                role=executor,
                task_dir=task_dir,
                paths=abs_paths,
                source="agent",
            )
            for file_path, abs_path in zip(files, abs_paths):
                try:
                    rel_to_task = abs_path.resolve().relative_to(task_dir.resolve()).as_posix()
                except Exception:
                    rel_to_task = None
                if rel_to_task is not None and rel_to_task.startswith("evidence/"):
                    continue
                if file_path not in allowed:
                    violations.append(str(file_path))
            return violations
    raise ValueError(f"Unknown segment id: {segment_id}")
