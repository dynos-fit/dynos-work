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
    finding_is_blocking,
    load_accepted_findings,
    load_json,
    require,
    write_ctl_json,
    write_json,
)
from lib_models import ALL_TIERS, HOST_CLAUDE, valid_models_for_host
from lib_tool_budget import would_overflow
from write_policy import find_write_violations

# Stages at which the plan-time tool-budget overflow guard fires. After
# PRE_EXECUTION_SNAPSHOT (or any later stage), in-flight tasks that already
# passed plan validation are grandfathered through unaffected.
_OVERFLOW_GUARD_STAGES: set[str] = {"PLANNING", "PLAN_REVIEW", "EXECUTION_GRAPH_BUILD"}

def _glob_entry_regex(entry: str) -> "re.Pattern[str]":
    """Translate a files_expected glob to a regex with PATH semantics:
    `*`/`?` do not cross `/`; `**` does. (fnmatch's `*` crosses slashes,
    which would make `src/*.py` silently claim `src/sub/a.py`.)"""
    out: list[str] = []
    i = 0
    n = len(entry)
    while i < n:
        ch = entry[i]
        if ch == "*":
            if entry[i : i + 3] == "**/":
                out.append(r"(?:[^/]+/)*")
                i += 3
                continue
            if entry[i : i + 2] == "**":
                out.append(r".*")
                i += 2
                continue
            out.append(r"[^/]*")
        elif ch == "?":
            out.append(r"[^/]")
        elif ch == "[":
            end = entry.find("]", i + 1)
            if end == -1:
                out.append(re.escape(ch))
            else:
                out.append(entry[i : end + 1])
                i = end
        else:
            out.append(re.escape(ch))
        i += 1
    return re.compile("".join(out) + r"\Z")


# Known shorthand -> canonical-value hints for enum errors. difflib catches
# typos; this table catches *conceptual* aliases it cannot (e.g. "ci" is not
# string-similar to "infra"). Per the 2026-06 decision, "ci" maps to "infra"
# — it is intentionally NOT added to VALID_DOMAINS.
_ENUM_ALIAS_HINTS: dict[str, str] = {
    "ci": "infra",
    "cicd": "infra",
    "ci/cd": "infra",
    "devops": "infra",
    "ops": "infra",
    "frontend": "ui",
    "front-end": "ui",
    "database": "db",
    "documentation": "docs",
}


def invalid_enum_error(field: str, value: object, valid: Iterable[str]) -> str:
    """Uniform enum-violation message: names the valid set and, when
    possible, a did-you-mean hint (D7-4 — errors must be self-correcting,
    a bare 'invalid: ci' sends the model guessing)."""
    import difflib

    valid_sorted = sorted(valid)
    msg = f"{field} invalid: {value!r} — valid values: [{', '.join(valid_sorted)}]"
    if isinstance(value, str) and value:
        lowered = value.strip().lower()
        hint = _ENUM_ALIAS_HINTS.get(lowered)
        if hint is None and f"{lowered}-executor" in valid_sorted:
            hint = f"{lowered}-executor"
        if hint is None:
            close = difflib.get_close_matches(lowered, valid_sorted, n=1, cutoff=0.6)
            hint = close[0] if close else None
        if hint and hint in valid_sorted:
            msg += f"; did you mean {hint!r}?"
    return msg


def files_expected_entry_matches(entry: str, file_path: str) -> bool:
    """True when a files_expected entry covers a concrete file path (D7-2).

    - trailing '/'  -> directory prefix containment
    - glob chars    -> path-aware glob (`*` within a segment, `**` across)
    - plain entry   -> exact equality (original behavior)
    """
    if entry == file_path:
        return True
    if entry.endswith("/"):
        return file_path.startswith(entry)
    if any(ch in entry for ch in "*?["):
        return _glob_entry_regex(entry).match(file_path) is not None
    return False


def files_expected_entries_overlap(a: str, b: str) -> bool:
    """True when two files_expected entries could claim the same file.

    Used by the cross-segment exclusivity rule: directory/glob entries
    overlap any entry they contain or match, in either direction.
    """
    if a == b:
        return True
    return files_expected_entry_matches(a, b.rstrip("/")) or files_expected_entry_matches(
        b, a.rstrip("/")
    ) or files_expected_entry_matches(a, b) or files_expected_entry_matches(b, a)


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


def require_nonblank_str(value, *, field_name: str) -> str:
    """Raise ValueError if value is not a non-empty string. Returns stripped value.

    Use this when migrating inline raise patterns of the form
    `if not isinstance(X, str) or not X: raise ValueError("X must be a non-empty string")`.

    Differs from require_nonblank() in that non-string inputs raise ValueError
    (not TypeError), preserving compatibility with code paths that use
    `pytest.raises(ValueError)` on the original inline pattern.

    Raises:
        ValueError: if value is not a str OR is empty/whitespace-only.
    Returns:
        The stripped string.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


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

# Domains that describe supporting work rather than a risk surface. They
# never count toward risk floors, planning-path selection, or conditional
# plan sections: nearly every task touches tests and docs, and counting
# them pushed 20 of 22 observed tasks onto the staged high-risk path.
NON_RISK_DOMAINS: frozenset[str] = frozenset({"testing", "docs"})

# Path fragments that mark an execution-graph file as an API surface or a
# persisted schema. Conditional plan sections are required only when the
# plan actually touches one of these, not merely because a domain is listed.
_API_SURFACE_MARKERS: tuple[str, ...] = (
    "/routes/", "/route/", "/routers/", "/router/", "/api/", "/apis/",
    "/controllers/", "/controller/", "/endpoints/", "/endpoint/",
    "/handlers/", "/views/", "/graphql/", "/rpc/", "/grpc/",
)
_API_SURFACE_BASENAMES: tuple[str, ...] = (
    "routes.", "router.", "urls.", "api.", "endpoints.", "controller.",
    "controllers.", "handlers.", "views.", "server.", "app.",
)
_DATA_SURFACE_MARKERS: tuple[str, ...] = (
    "/migrations/", "/migration/", "/models/", "/model/", "/schema/",
    "/schemas/", "/db/", "/database/", "/prisma/", "/alembic/", "/entities/",
)
_DATA_SURFACE_BASENAMES: tuple[str, ...] = (
    "schema.", "models.", "model.", "migration.", "migrations.",
)


_HIGH_RISK_LEVELS: set[str] = {"high", "critical"}

# Artifact size ceilings. Observed specs ran 20–150 acceptance criteria and
# plans up to 48k words from requests of a few hundred words; a single
# planner spawn spent 58 minutes writing one 94-criterion spec that a human
# then approved in three minutes. Above these ceilings the task is too big
# for one approval round and must be split. Policy keys of the same name
# (``max_acceptance_criteria``, ``max_spec_words``, ``max_plan_words``)
# override the defaults per project.
MAX_ACCEPTANCE_CRITERIA: int = 20
MAX_SPEC_WORDS: int = 2500
MAX_PLAN_WORDS: int = 4000


def artifact_ceilings(policy: dict | None = None) -> dict[str, int]:
    """Resolve the three artifact ceilings, honouring positive policy overrides."""
    policy = policy if isinstance(policy, dict) else {}
    out = {
        "max_acceptance_criteria": MAX_ACCEPTANCE_CRITERIA,
        "max_spec_words": MAX_SPEC_WORDS,
        "max_plan_words": MAX_PLAN_WORDS,
    }
    for key in out:
        value = policy.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value > 0:
            out[key] = value
    return out


def _word_count(text: str) -> int:
    return len(text.split())


def risk_bearing_domains(domains: object) -> list[str]:
    """Return the classification domains that carry risk, in order.

    Drops ``NON_RISK_DOMAINS`` and anything that is not a non-empty string.
    """
    if not isinstance(domains, (list, tuple, set, frozenset)):
        return []
    out: list[str] = []
    for item in domains:
        if isinstance(item, str) and item.strip() and item not in NON_RISK_DOMAINS and item not in out:
            out.append(item)
    return out


def _touches_surface(files: Iterable[str], markers: tuple[str, ...], basenames: tuple[str, ...]) -> bool:
    for raw in files:
        if not isinstance(raw, str) or not raw.strip():
            continue
        path = "/" + raw.strip().replace("\\", "/").lstrip("./").lower()
        if any(marker in path for marker in markers):
            return True
        base = path.rsplit("/", 1)[-1]
        if any(base.startswith(prefix) for prefix in basenames):
            return True
    return False


def plan_touches_api_surface(files: Iterable[str]) -> bool:
    """True when any planned file looks like a route/controller/API module."""
    return _touches_surface(files, _API_SURFACE_MARKERS, _API_SURFACE_BASENAMES)


def plan_touches_data_surface(files: Iterable[str]) -> bool:
    """True when any planned file looks like a migration/model/schema module."""
    return _touches_surface(files, _DATA_SURFACE_MARKERS, _DATA_SURFACE_BASENAMES)


def graph_files_expected(task_dir: Path | None) -> list[str]:
    """Flatten ``files_expected`` across every segment of execution-graph.json."""
    if task_dir is None:
        return []
    try:
        graph = load_json(Path(task_dir) / "execution-graph.json")
    except Exception:
        return []
    files: list[str] = []
    for seg in graph.get("segments", []) if isinstance(graph, dict) else []:
        if not isinstance(seg, dict):
            continue
        for item in seg.get("files_expected", []) or []:
            if isinstance(item, str):
                files.append(item)
    return files


def conditional_plan_headings(
    domains: Iterable[str],
    risk_level: str = "medium",
    files: Iterable[str] | None = None,
) -> list[str]:
    """Return additional required plan headings for this task.

    - ``API Contracts`` is required only when a planned file (``files``,
      normally the execution graph's ``files_expected``) looks like an API
      surface: routes, controllers, handlers, endpoints.
    - ``Data Model`` is required only when a planned file looks like a
      migration, model, or schema module.
    - ``Architecture Decisions`` is required when risk_level is critical.

    ``domains`` is accepted for compatibility and no longer forces either
    table: domain membership produced ``N/A`` boilerplate sections in every
    backend/ui/db plan without making the gap analysis any stronger.
    """
    extra: list[str] = []
    planned = list(files) if files is not None else []
    if plan_touches_api_surface(planned):
        extra.append("API Contracts")
    if plan_touches_data_surface(planned):
        extra.append("Data Model")
    if risk_level == "critical":
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


def derive_user_summary(audit_summary: dict) -> str:
    """Derive the verbatim user-facing summary from audit-summary.json content.

    Pure function — no I/O, no env reads, no logging. Reads task_id and reports
    from audit_summary. Returns multi-line string WITHOUT a trailing newline.

    Format (em-dash is U+2014). Per-auditor lines aggregate by auditor_name
    (handles multi-cycle reaudit reports). Skipped auditors are absent because
    they don't appear in reports. Empty reports yields header + blank + footer.

    Reports carrying ``superseded_by`` (an older attempt, or a voting tier
    overruled by a binding deep-tier verdict) are ignored, and when the
    summary carries ``blocking_by_auditor`` (the findings gate's deduplicated
    counts) those counts win over the per-report sums, so the summary agrees
    with ``run-audit-findings-gate``. Summaries written before either field
    existed derive exactly as they did.
    """
    task_id = str(audit_summary.get("task_id", ""))
    reports = audit_summary.get("reports", []) or []
    gate_counts = audit_summary.get("blocking_by_auditor")

    by_auditor: dict[str, int] = {}
    for r in reports:
        if not isinstance(r, dict) or r.get("superseded_by"):
            continue
        name = str(r.get("auditor_name") or r.get("auditor") or "")
        if not name:
            continue
        by_auditor[name] = by_auditor.get(name, 0) + int(r.get("blocking_count", 0) or 0)
    if isinstance(gate_counts, dict):
        by_auditor = {name: int(gate_counts.get(name, 0) or 0) for name in by_auditor}

    total_blocking = sum(by_auditor.values())

    if total_blocking == 0:
        header = "Audit complete — ALL PASSED"
    else:
        header = f"Audit complete — FAILED — {total_blocking} blocking findings"

    # Per-spec ordering: lines in the order of first appearance of each
    # auditor_name in the reports array (insertion order — Python 3.7+
    # dicts preserve insertion order, so iterate by_auditor directly).
    auditor_lines: list[str] = []
    for name in by_auditor:
        bc = by_auditor[name]
        if bc == 0:
            auditor_lines.append(f"  {name}: PASS")
        else:
            auditor_lines.append(f"  {name}: FAIL ({bc} blocking)")

    footer = (
        f"Task complete. Snapshot branch dynos/{task_id}-snapshot "
        f"can be deleted if desired."
    )

    parts: list[str] = [header]
    if auditor_lines:
        parts.append("")
        parts.extend(auditor_lines)
    parts.append("")
    parts.append(footer)
    return "\n".join(parts)


# Filename pattern: {auditor}-{model}-attempt-{n}.json (ADR-2: hard cutover, no legacy)
_REPORT_FILENAME_RE: re.Pattern[str] = re.compile(
    r"^([a-zA-Z0-9_-]+)-([a-zA-Z0-9_-]+)-attempt-(\d+)\.json$"
)


def _normalize_auditor_name(candidate: str, routed: set[str]) -> str | None:
    """Map a report-derived auditor name onto a routed auditor name (D3).

    A routed ``name`` matches when ``candidate`` equals it or starts with
    ``name + "-"``, or equals / starts with ``short + "-"`` where
    ``short = name.removesuffix("-auditor")``. When several routed names
    match, the longest routed name wins (ties broken lexicographically for
    determinism). Returns None when nothing matches, so normalization can
    never introduce a name absent from ``routed``.
    """
    if not isinstance(candidate, str) or not candidate:
        return None
    best: str | None = None
    for name in sorted(routed):
        if not isinstance(name, str) or not name:
            continue
        short = name.removesuffix("-auditor")
        matched = (
            candidate == name
            or candidate.startswith(name + "-")
            or (bool(short) and (candidate == short or candidate.startswith(short + "-")))
        )
        if matched and (best is None or len(name) > len(best)):
            best = name
    return best


def select_eligible_reports(
    task_dir: Path,
    stage: str | None = None,
) -> list[tuple]:
    """Return eligible audit report tuples (report_path, payload) for gating or repair-planning.

    Two paths:
    (A) GATE PATH (stage is not None): requires status=='complete', matching receipt
        (min_version=2) with report_path/report_sha256 match, and receipt stage match.
    (B) REPAIR-PLANNING PATH (stage is None): returns every report with any matching
        receipt (any lifecycle status: complete, partial, in_progress) as advisory context.

    Returns empty list (not an error) when no eligible reports exist.
    Receipts are checked at both audit-{auditor_name} and audit-{shard_step_name}
    (the filename stem without -attempt-N suffix doubles as the shard step key prefix).
    """
    from lib_receipts import read_receipt as _read_receipt

    audit_dir = task_dir / "audit-reports"
    if not audit_dir.is_dir():
        return []

    results: list[tuple] = []
    for report_file in sorted(audit_dir.glob("*.json")):
        try:
            payload = load_json(report_file)
        except (json.JSONDecodeError, OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue

        # REPAIR-PLANNING PATH (stage is None): permissive. Every readable
        # report is advisory context regardless of filename scheme, lifecycle
        # status, or receipt presence — legacy ``{auditor}.json`` reports and
        # receipt-less reports stay visible to repair-log cross-checks — and
        # satisfies AC 1/AC 2 ("repair planning sees all findings, including
        # incomplete ones, as advisory context"). collect_repair_findings then
        # decides which of these reports are binding.
        if stage is None:
            results.append((report_file, payload))
            continue

        # GATE PATH (stage is not None): strict attempt-isolated naming +
        # status=='complete' + receipted + receipt-stage match (AC 1; ADR-2
        # hard cutover — legacy filename patterns are silently inert).
        m = _REPORT_FILENAME_RE.match(report_file.name)
        if not m:
            continue
        auditor_name = m.group(1)
        model_name = m.group(2)

        if payload.get("status") != "complete":
            continue

        # Compute sha256 of this report file for receipt matching
        try:
            report_bytes = report_file.read_bytes()
        except OSError:
            continue
        import hashlib as _hashlib
        report_sha256 = _hashlib.sha256(report_bytes).hexdigest()
        report_path_str = str(report_file)

        # Candidate receipt step names to check:
        # 1. audit-{auditor_name} (canonical per-auditor receipt)
        # 2. audit-{auditor_name}-{model_name} (per-model shard receipt)
        candidate_step_names = [
            f"audit-{auditor_name}",
            f"audit-{auditor_name}-{model_name}",
        ]

        matched_receipt: dict | None = None
        for step_name in candidate_step_names:
            receipt = _read_receipt(task_dir, step_name, min_version=2)
            if receipt is None:
                continue
            # SEC-003: the content hash is the ONLY integrity-binding term. The
            # receipt's report_sha256 must equal the hash recomputed from the
            # report's CURRENT bytes — a path-only match would gate a report whose
            # bytes were mutated after the receipt was written (audit-reports/ is
            # auditor-writable, so an auditor could rewrite its own report post-
            # receipt to hide or inject findings). Path identity is required too so
            # a receipt cannot vouch for a different file that happens to share a hash.
            r_path = receipt.get("report_path")
            r_sha = receipt.get("report_sha256")
            if r_sha == report_sha256 and (r_path is None or r_path == report_path_str):
                matched_receipt = receipt
                break

        if matched_receipt is None:
            continue

        # GATE PATH: require receipt stage to match exactly
        if matched_receipt.get("stage") != stage:
            continue

        results.append((report_file, payload))

    return results


_ATTEMPT_SUFFIX_RE: re.Pattern[str] = re.compile(r"-attempt-(\d+)$")

# Audit reports use critical|major|minor (agents/_shared/audit-report.md);
# repair logs use critical|high|medium|low.
_REPAIR_SEVERITY_ALIASES = {"major": "high", "minor": "low", "blocking": "high", "non-blocking": "low"}


def normalize_repair_severity(severity: str) -> str:
    """Map an audit-report severity onto the repair-log scale."""
    return _REPAIR_SEVERITY_ALIASES.get(severity, severity)


def _report_shard(report_file: Path, auditor_name: str) -> tuple[str | None, int | None]:
    """Return ``(model, attempt)`` parsed from ``{auditor}-{model}-attempt-{n}.json``.

    The auditor name is stripped as a known prefix first, so model names that
    contain hyphens parse correctly. Legacy names yield ``(None, None)``.
    """
    stem = report_file.stem
    match = _ATTEMPT_SUFFIX_RE.search(stem)
    if not match:
        return None, None
    attempt = int(match.group(1))
    head = stem[: match.start()]
    prefix = auditor_name + "-"
    if head.startswith(prefix) and len(head) > len(prefix):
        return head[len(prefix):], attempt
    named = _REPORT_FILENAME_RE.match(report_file.name)
    return (named.group(2) if named else None), attempt


def _completed_cascades(task_dir: Path) -> dict[str, dict]:
    """Map auditor -> ``{"verdict", "escalation_model", "voting_models"}`` for
    every ensemble auditor whose cascade is complete.

    Reads the cascade from ``lib_ensemble`` (the single source of truth) so
    repair planning applies the same verdict ``ctl ensemble-next`` reports.
    A cascade with gaps is not trusted and is left out.
    """
    import lib_ensemble  # noqa: PLC0415 — deferred: lib_ensemble reads receipts

    try:
        entries = lib_ensemble.load_plan_entries(task_dir)
    except (OSError, ValueError):
        return {}
    completed: dict[str, dict] = {}
    for entry in entries:
        name = entry.get("name")
        if not isinstance(name, str) or not name or entry.get("action") != "spawn":
            continue
        if not lib_ensemble.is_ensemble_entry(entry):
            continue
        result = lib_ensemble.ensemble_next(task_dir, name, entry)
        if result.get("status") != lib_ensemble.STATUS_COMPLETE or result.get("gaps"):
            continue
        verdict = result.get("verdict")
        if verdict == lib_ensemble.VERDICT_ESCALATED and not result.get("escalation_model"):
            continue
        completed[name] = {
            "verdict": verdict,
            "escalation_model": str(result.get("escalation_model") or ""),
            "voting_models": list(result.get("voting_models") or []),
        }
    return completed


def _finding_location_file(finding: dict) -> str:
    """Return the file a finding points at, or ``""``.

    Accepts the canonical ``location: "file:line"`` (agents/_shared/
    audit-report.md) plus the ``file`` / ``evidence.file`` forms older
    reports and fixtures use.
    """
    direct = finding.get("file")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    evidence = finding.get("evidence")
    if isinstance(evidence, dict):
        evidence_file = evidence.get("file")
        if isinstance(evidence_file, str) and evidence_file.strip():
            return evidence_file.strip()
    location = finding.get("location")
    if isinstance(location, str) and location.strip():
        path, sep, line = location.strip().rpartition(":")
        if sep and path and line.strip().isdigit():
            return path.strip()
        return location.strip()
    return ""


def _finding_location_key(finding: dict) -> tuple[str, str, str] | None:
    """``(file, line, category)`` used to merge duplicate reports of one
    defect, or None when the finding names no file (never merged).

    A finding that names a line merges on ``(file, line)`` alone: auditors
    file the same defect under their own categories (one fix at
    ``pagination.py:81`` came back as twelve findings from twelve auditors).
    Without a line, the category keeps distinct defects in one file apart.
    """
    path = _finding_location_file(finding)
    if not path:
        return None
    line = finding.get("line")
    line_text = "" if line is None else str(line)
    location = finding.get("location")
    if not line_text and isinstance(location, str):
        tail = location.strip().rpartition(":")[2]
        line_text = tail if tail.isdigit() else ""
    if line_text:
        return path, line_text, ""
    return path, "", str(finding.get("category") or "")


def _normalize_finding_path(path: str) -> str:
    path = path.strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def finding_class_keys(auditor: str, finding: dict) -> set[tuple[str, str, str]]:
    """``(auditor, class, file)`` keys that identify a finding's issue class.

    The class is the finding's ``category`` and its ID prefix (``PERF`` of
    ``PERF-3``), lower-cased; either one matching counts, so a re-audit that
    rewords a finding under a new ID still lands on the same key. Empty when
    the finding names no file: without a file the class alone is too broad
    to match on.
    """
    path = _normalize_finding_path(_finding_location_file(finding))
    if not path or not auditor:
        return set()
    classes: set[str] = set()
    category = finding.get("category")
    if isinstance(category, str) and category.strip():
        classes.add(category.strip().lower())
    fid = finding.get("id") if isinstance(finding.get("id"), str) else finding.get("finding_id")
    if isinstance(fid, str) and "-" in fid:
        prefix = fid.rsplit("-", 1)[0].strip().lower()
        if prefix:
            classes.add(prefix)
    return {(auditor, cls, path) for cls in classes}


def match_accepted_finding(auditor: str, finding: dict, accepted: list[dict]) -> dict | None:
    """The ``ctl accept-finding`` entry covering this finding, or None.

    Matches by ``(auditor, finding_id)``, or by ``(auditor, class, file)``
    (``finding_class_keys``) so a re-audit's reworded variant of an accepted
    finding stays accepted.
    """
    fid = finding.get("id")
    keys = finding_class_keys(auditor, finding)
    for entry in accepted:
        if str(entry.get("auditor")) != auditor:
            continue
        if entry.get("finding_id") == fid:
            return {**entry, "matched_by": "id"}
        probe = {"id": entry.get("finding_id"), "category": entry.get("category"), "file": entry.get("file") or ""}
        if keys and keys & finding_class_keys(auditor, probe):
            return {**entry, "matched_by": "class_and_file"}
    return None


def collect_repair_findings(task_dir: Path) -> dict:
    """The one view of audit findings that repair planning acts on.

    The findings gate, the repair-cycle plan, the repair-log builder and the
    repair-log validator all read this, so they cannot disagree about which
    findings exist, how many times, or at what severity.

    Report selection (each superseded report is listed with ``superseded_by``):

    - Ensemble auditor whose cascade verdict is ``escalated``: the deep tier's
      verdict is final and binding, so the voting-tier reports are superseded.
      Fail closed: if the escalation tier wrote no report, nothing is
      superseded and the voting tiers keep blocking.
    - Ensemble auditor whose cascade verdict is ``pass``: a re-audit restarts
      the cascade at the fast tier, and a clean voting run never reaches the
      deep tier. Deep-tier reports from attempts older than the current
      fast-tier attempt belong to a finished cascade and are superseded.
    - Several attempts of the same ``(auditor, model)`` shard: the highest
      attempt supersedes the older ones (a re-audit after repair).

    Findings from the binding reports are keyed by ``(auditor, id)``, so the
    same ID from two auditors stays two findings. A finding pointing at the
    same file and line as an earlier one (or, with no line, the same file and
    category) is merged into it (recorded in ``_merged``); blocking findings are visited first so a non-blocking
    duplicate can never shadow a blocking one.

    Returns ``{"reports": [...], "findings": [...]}``. Each finding is a copy
    of the report entry plus ``_auditor``, ``_report_path``, ``_model``,
    ``_attempt``, ``_merged`` and, when the report carried one, ``_repair_task``
    (the auditor's own ``repair_tasks`` entry for that finding). A finding a
    human accepted (``ctl accept-finding``) carries ``accepted`` and
    ``blocking: False``, so no blocking count includes it.
    """
    cascades = _completed_cascades(task_dir)
    rows: list[dict] = []
    for report_file, payload in select_eligible_reports(task_dir, stage=None):
        reported = str(payload.get("auditor_name") or payload.get("auditor") or report_file.stem)
        auditor = reported if reported in cascades else (_normalize_auditor_name(reported, set(cascades)) or reported)
        model, attempt = _report_shard(report_file, reported)
        if model is None and auditor != reported:
            model, attempt = _report_shard(report_file, auditor)
        raw = payload.get("findings")
        findings = [f for f in raw if isinstance(f, dict)] if isinstance(raw, list) else []
        repair_tasks = payload.get("repair_tasks")
        rows.append({
            "auditor": auditor,
            "path": report_file,
            "model": model,
            "attempt": attempt,
            "findings": findings,
            "repair_tasks": [t for t in repair_tasks if isinstance(t, dict)] if isinstance(repair_tasks, list) else [],
            "superseded_by": None,
        })

    for auditor, cascade in cascades.items():
        escalation_model = cascade["escalation_model"]
        voting_models = cascade["voting_models"]
        own = [r for r in rows if r["auditor"] == auditor]
        if cascade["verdict"] == "escalated":
            if not any(r["model"] == escalation_model for r in own):
                continue
            for row in own:
                if row["model"] in voting_models and row["model"] != escalation_model:
                    row["superseded_by"] = f"escalated cascade: {escalation_model} verdict is binding"
            continue
        if not escalation_model or escalation_model in voting_models or not voting_models:
            continue
        fast_attempts = [r["attempt"] for r in own if r["model"] == voting_models[0] and r["attempt"] is not None]
        if not fast_attempts:
            continue
        current = max(fast_attempts)
        for row in own:
            if row["model"] == escalation_model and row["attempt"] is not None and row["attempt"] < current:
                row["superseded_by"] = f"cascade pass at attempt-{current}: {escalation_model} did not run"

    latest: dict[tuple[str, str], int] = {}
    for row in rows:
        if row["superseded_by"] is None and row["model"] is not None and row["attempt"] is not None:
            key = (row["auditor"], row["model"])
            latest[key] = max(latest.get(key, row["attempt"]), row["attempt"])
    for row in rows:
        if row["superseded_by"] is None and row["model"] is not None and row["attempt"] is not None:
            newest = latest[(row["auditor"], row["model"])]
            if row["attempt"] < newest:
                row["superseded_by"] = f"attempt-{newest}"

    binding = sorted(
        (r for r in rows if r["superseded_by"] is None),
        key=lambda r: (r["auditor"], r["model"] or "", -(r["attempt"] or 0), r["path"].name),
    )
    candidates: list[dict] = []
    for row in binding:
        tasks_by_id = {
            str(t.get("finding_id")): t for t in row["repair_tasks"] if isinstance(t.get("finding_id"), str)
        }
        for finding in row["findings"]:
            finding_id = finding.get("id")
            if not isinstance(finding_id, str) or not finding_id.strip():
                continue
            entry = dict(finding)
            entry["_auditor"] = row["auditor"]
            entry["_report_path"] = str(row["path"])
            entry["_model"] = row["model"]
            entry["_attempt"] = row["attempt"]
            entry["_merged"] = []
            if finding_id in tasks_by_id:
                entry["_repair_task"] = tasks_by_id[finding_id]
            candidates.append(entry)
    # A human-accepted finding (``ctl accept-finding``) no longer blocks; it
    # stays listed with an ``accepted`` marker so summaries can show it.
    accepted = load_accepted_findings(task_dir)
    if accepted:
        for entry in candidates:
            match = match_accepted_finding(entry["_auditor"], entry, accepted)
            if match is not None:
                entry["accepted"] = {
                    "finding_id": match.get("finding_id"),
                    "reason": match.get("reason", ""),
                    "decision_ref": match.get("decision_ref"),
                    "matched_by": match["matched_by"],
                }
                entry["blocking"] = False
    # Stable: blocking first, otherwise the binding-report order above.
    candidates.sort(key=lambda f: 0 if finding_is_blocking(f) else 1)

    kept: list[dict] = []
    by_key: set[tuple[str, str]] = set()
    by_location: dict[tuple[str, str, str], dict] = {}
    for entry in candidates:
        key = (entry["_auditor"], entry["id"])
        if key in by_key:
            continue
        by_key.add(key)
        location = _finding_location_key(entry)
        if location is not None and location in by_location:
            by_location[location]["_merged"].append({"auditor": entry["_auditor"], "id": entry["id"]})
            continue
        if location is not None:
            by_location[location] = entry
        kept.append(entry)

    reports = []
    for row in rows:
        reports.append({
            "auditor_name": row["auditor"],
            "report_path": str(row["path"]),
            "model": row["model"],
            "attempt": row["attempt"],
            "finding_count": len(row["findings"]),
            "blocking_count": sum(1 for f in row["findings"] if finding_is_blocking(f)),
            "superseded_by": row["superseded_by"],
        })
    return {"reports": reports, "findings": kept}


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
            errors.append(invalid_enum_error("classification.type", ctype, VALID_CLASSIFICATION_TYPES))
        risk = classification.get("risk_level")
        if risk not in VALID_RISK_LEVELS:
            errors.append(invalid_enum_error("classification.risk_level", risk, VALID_RISK_LEVELS))
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
                    errors.append(invalid_enum_error("classification domain", domain, VALID_DOMAINS))
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
        if "subsystems" in classification:
            subsystems = classification.get("subsystems")
            if not isinstance(subsystems, list):
                errors.append("classification.subsystems must be an array")
            else:
                owners = set()
                for index, subsystem in enumerate(subsystems):
                    if (not isinstance(subsystem, dict) or not isinstance(subsystem.get("name"), str)
                            or not isinstance(subsystem.get("paths"), list)
                            or not isinstance(subsystem.get("independent"), bool)):
                        errors.append(f"classification.subsystems[{index}] is malformed")
                        continue
                    for path in subsystem["paths"]:
                        if not isinstance(path, str) or not path.strip():
                            errors.append(f"classification.subsystems[{index}].paths entries must be non-empty strings")
                        elif path in owners:
                            errors.append(f"classification.subsystems path overlaps: {path}")
                        owners.add(path)
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


def compute_planning_path(manifest: dict, task_dir: Path | None = None) -> str:
    """Choose combined planning independently from the audit fast_track flag."""
    override = None
    if task_dir is not None:
        try:
            from lib_core import _persistent_project_dir
            policy = load_json(_persistent_project_dir(Path(task_dir).parent.parent) / "policy.json")
            override = policy.get("planning_path_override") if isinstance(policy, dict) else None
        except Exception:
            override = None
    if override in {"combined", "staged"}:
        return override
    classification = manifest.get("classification")
    if not isinstance(classification, dict):
        return "staged"
    return planning_path_for(
        str(classification.get("risk_level") or ""),
        classification.get("domains", []),
        str(classification.get("type") or ""),
    )


# Domains that, together with high risk, still earn a separate spec review
# before planning: persisted schema and trust-boundary changes.
_STAGED_HIGH_RISK_DOMAINS: frozenset[str] = frozenset({"db", "security"})


def planning_path_for(risk_level: str, domains: object, task_type: str) -> str:
    """Pure planning-path rule.

    ``combined`` (one Spec + Plan spawn, one joint review) is the default.
    ``staged`` is reserved for the cases where a human should approve the
    spec before any plan exists: migrations, critical risk, and high risk
    that touches persisted schema or a trust boundary. Domain count and
    the ``testing``/``docs`` domains no longer influence the choice —
    observed tasks carried 3–9 domains and 0 of 22 reached the combined
    path under the old ``risk in {low, medium} and len(domains) <= 2`` rule.
    """
    if task_type == "migration":
        return "staged"
    if risk_level == "critical":
        return "staged"
    if not isinstance(domains, list) or not domains:
        # Malformed classification: keep the separate spec review.
        return "staged"
    bearing = set(risk_bearing_domains(domains))
    if risk_level == "high" and bearing & _STAGED_HIGH_RISK_DOMAINS:
        return "staged"
    return "combined"


def tdd_required_default(risk_level: str, policy: dict | None = None) -> bool:
    """Default for ``classification.tdd_required`` when the planner left it unset.

    Opt-in: ``False`` unless the project policy lists the risk level under
    ``tdd_default_risk_levels`` (for example ``["critical"]``). The old
    rule derived ``True`` for every high/critical task, which added a
    testing-executor spawn and a fourth approval gate before execution on
    20 of 22 observed tasks; tests are still written by ``testing-executor``
    during execute when the flag is false.
    """
    policy = policy if isinstance(policy, dict) else {}
    levels = policy.get("tdd_default_risk_levels", [])
    if not isinstance(levels, list):
        return False
    return risk_level in {str(level) for level in levels}


def arch_design_required(risk_level: str, policy: dict | None = None) -> bool:
    """Whether the Architectural Design Doc planner phase runs for this task.

    Default: only ``critical``. ``policy.arch_design_min_risk`` may be
    ``"never"``, ``"critical"`` (default), ``"high"``, or ``"always"``.
    """
    policy = policy if isinstance(policy, dict) else {}
    setting = str(policy.get("arch_design_min_risk", "critical") or "critical").lower()
    if setting == "never":
        return False
    if setting == "always":
        return True
    order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    threshold = order.get(setting, 3)
    return order.get(risk_level, 0) >= threshold


def load_project_policy(task_dir: Path | None) -> dict:
    """Best-effort read of the persistent project ``policy.json``."""
    if task_dir is None:
        return {}
    try:
        from lib_core import _persistent_project_dir
        loaded = load_json(_persistent_project_dir(Path(task_dir).parent.parent) / "policy.json")
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def compute_planning_mode(risk_level: str, subsystems: object, policy: dict | None = None) -> str:
    policy = policy if isinstance(policy, dict) else {}
    independent = sum(1 for row in subsystems if isinstance(row, dict) and row.get("independent") is True) if isinstance(subsystems, list) else 0
    min_default = int(policy.get("hierarchical_min_subsystems", 3))
    min_high = int(policy.get("hierarchical_min_subsystems_high", 2))
    return "hierarchical" if (
        (risk_level in {"high", "critical"} and independent >= min_high)
        or independent >= min_default
    ) else "standard"


def apply_planning_flags(task_dir: Path) -> bool:
    """Compute fast-track eligibility AND persist the result to ``manifest.json``.

    This is NOT a pure computation. As a side-effect, this function writes the
    boolean result back to ``task_dir/manifest.json`` under the ``fast_track``
    key (overwriting any prior value). Callers that only want the eligibility
    decision without persisting it should use ``compute_fast_track`` directly.

    Eligibility rule (delegated to ``compute_fast_track``): a task is
    fast-track-eligible when its ``classification.risk_level`` is ``"low"`` AND
    its ``classification.domains`` list contains exactly one element.

    Side-effect — TDD default (AC 12, revised):
      When ``manifest.classification.tdd_required`` is absent (key not present
      in the dict), derive it with ``tdd_required_default`` (opt-in through
      ``policy.tdd_default_risk_levels``; ``False`` otherwise) and persist it
      alongside ``fast_track`` in the same atomic write. Explicit values
      (``True`` or ``False``) are preserved and never overwritten.

    Side-effect — ``arch_design_required`` is persisted on the manifest so
    the start skill reads one flag instead of re-deriving it from risk.

    Returns the same boolean that was written to the manifest: ``True`` if
    fast-track-eligible, ``False`` otherwise.
    """
    manifest_path = task_dir / "manifest.json"
    manifest = load_json(manifest_path)
    policy = load_project_policy(task_dir)
    fast = compute_fast_track(manifest)
    manifest["fast_track"] = fast
    manifest["planning_path"] = compute_planning_path(manifest, task_dir)

    classification = manifest.get("classification")
    risk_level = str(classification.get("risk_level") or "") if isinstance(classification, dict) else ""
    if isinstance(classification, dict) and "tdd_required" not in classification:
        classification["tdd_required"] = tdd_required_default(risk_level, policy)
    manifest["arch_design_required"] = arch_design_required(risk_level, policy)

    write_ctl_json(task_dir, manifest_path, manifest)
    return fast


def apply_fast_track(task_dir: Path) -> bool:
    """Backward-compatible name for persisting both planning flags."""
    return apply_planning_flags(task_dir)


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
    ceilings = artifact_ceilings(load_project_policy(task_dir))
    if not criteria_numbers:
        errors.append("spec has no acceptance criteria")
    else:
        expected = list(range(1, len(criteria_numbers) + 1))
        if criteria_numbers != expected:
            errors.append(f"acceptance criteria numbering must be contiguous from 1: got {criteria_numbers}")
        if len(criteria_numbers) > ceilings["max_acceptance_criteria"]:
            errors.append(
                f"spec.md has {len(criteria_numbers)} acceptance criteria; ceiling is "
                f"{ceilings['max_acceptance_criteria']} — split the task or raise "
                "policy max_acceptance_criteria"
            )
    spec_words = _word_count(spec_text)
    if spec_words > ceilings["max_spec_words"]:
        errors.append(
            f"spec.md is {spec_words} words; ceiling is {ceilings['max_spec_words']} — "
            "cut restated context and keep only falsifiable requirements, or raise "
            "policy max_spec_words"
        )

    # AC 15: if a spec-validated receipt exists, its recorded spec_sha256 must
    # match the current sha256(spec.md). Hash-drift is a validation error.
    # `amend-artifact spec` updates the canonical receipt's artifact_sha256
    # (and appends to its amendments trail) — prefer that field so a
    # receipted amendment can re-validate; the original spec_sha256 is the
    # pre-amendment fallback.
    try:
        from lib_receipts import hash_file, read_receipt  # local import to avoid cycles
        receipt = read_receipt(task_dir, "spec-validated")
        if receipt is not None:
            recorded = receipt.get("artifact_sha256") or receipt.get("spec_sha256")
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
        all_required = REQUIRED_PLAN_HEADINGS + conditional_plan_headings(
            domains, risk_level, files=graph_files_expected(task_dir)
        )
        # Hoisted out of the inner loop — collect_headings is a regex scan
        # over plan_text and was being re-executed on every required heading.
        plan_headings = collect_headings(plan_text)
        for heading in all_required:
            if heading not in plan_headings:
                errors.append(f"plan missing heading: {heading}")
        plan_words = _word_count(plan_text)
        if plan_words > ceilings["max_plan_words"]:
            errors.append(
                f"plan.md is {plan_words} words; ceiling is {ceilings['max_plan_words']} — "
                "drop N/A tables and restated spec content, or raise policy max_plan_words"
            )
        # Section-level exemption (D7-4): every backtick path listed under a
        # "Files to be created" heading (any level) is exempt everywhere, in
        # addition to the per-line "to-be-created" marker.
        to_be_created: set[str] = set()
        in_create_section = False
        for line in plan_text.splitlines():
            heading_match = re.match(r"#{2,4}\s+(.*)", line)
            if heading_match:
                title = heading_match.group(1).strip().lower()
                in_create_section = (
                    "files to be created" in title or "to-be-created" in title
                )
                continue
            if in_create_section:
                for match in re.finditer(r"`([^`]+)`", line):
                    to_be_created.add(match.group(1).strip())

        in_ref_section = False
        for line in plan_text.splitlines():
            if line.startswith("## "):
                in_ref_section = line.strip() == "## Reference Code"
                continue
            if not in_ref_section:
                continue
            for match in re.finditer(r"`([^`]+\.[a-zA-Z]{1,5})`", line):
                ref_path = match.group(1)
                # Only slash-containing tokens are treated as repo paths —
                # bare tokens like `policy.json` are frequently concepts or
                # basenames, and path-checking them produced false rejects.
                if "/" not in ref_path:
                    continue
                if "to-be-created" in line.lower():
                    continue
                if ref_path in to_be_created:
                    continue
                full = task_dir.parent.parent / ref_path
                if not full.exists():
                    errors.append(
                        f"plan Reference Code path does not exist: {ref_path} "
                        "(mark new files as to-be-created on the same line, or "
                        "list them under a 'Files to be created' heading)"
                    )

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
                    errors.append(invalid_enum_error(f"{segment_id}: executor", executor, VALID_EXECUTORS))
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
                        # Cross-segment exclusivity. Directory ("dir/") and
                        # glob entries participate by containment, so a
                        # directory segment cannot silently share files with
                        # a per-file segment (D7-2 — stricter, not looser).
                        for other_path, owner in seen_files.items():
                            if owner != segment_id and files_expected_entries_overlap(file_path, other_path):
                                errors.append(
                                    f"file {file_path} appears in multiple segments: {owner}, {segment_id}"
                                )
                                break
                        seen_files.setdefault(file_path, segment_id)
                    # AC-9 / AC-10: plan-time overflow guard. Fires only at
                    # plan-creation/review stages so that in-flight tasks at
                    # PRE_EXECUTION_SNAPSHOT or later are grandfathered.
                    if stage in _OVERFLOW_GUARD_STAGES:
                        files_count = len(files_expected)
                        if would_overflow(files_count):
                            errors.append(
                                f"segment {segment_id} would overflow tool budget: "
                                f"{files_count} files exceeds 11-file ceiling, decompose"
                            )
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


def validate_task_artifacts_structured(
    task_dir: Path, strict: bool = False, *, run_gap: bool = True
) -> list[dict]:
    """Return stable, artifact-scoped validation errors for repair prompts.

    The human-readable validator remains the source of truth. This adapter
    assigns deterministic rule identifiers while preserving every message.
    """
    result = []
    for message in validate_task_artifacts(task_dir, strict=strict, run_gap=run_gap):
        lowered = message.lower()
        artifact = next(
            (name for name in ("execution-graph.json", "spec.md", "plan.md", "manifest.json") if name in lowered),
            "manifest.json",
        )
        prefix = message.split(":", 1)[0].strip().lower()
        rule = re.sub(r"[^a-z0-9]+", "_", prefix).strip("_")[:48] or "validation_error"
        loc_match = re.search(r"(?:^|\s)([^\s:]+:\d+)(?:\b|$)", message)
        result.append({
            "artifact": artifact,
            "rule": rule,
            "message": message,
            "location": loc_match.group(1) if loc_match else None,
        })
    return result


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
    # Same view the repair-log builder uses (collect_repair_findings), keyed by
    # (auditor, id): two auditors may legitimately reuse an id.
    live_by_key: dict[tuple[str, str], dict] = {}
    live_by_id: dict[str, list[dict]] = {}
    for live in collect_repair_findings(task_dir)["findings"]:
        live_by_key[(live["_auditor"], live["id"])] = live
        live_by_id.setdefault(live["id"], []).append(live)
    batches = data.get("batches")
    if not isinstance(batches, list):
        return ["repair-log batches must be an array"]
    seen_batch_ids: set[str] = set()
    seen_findings: set[tuple[str, str]] = set()
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
            auditor = task.get("auditor")
            live_finding: dict | None = None
            if auditor is not None and (not isinstance(auditor, str) or not auditor.strip()):
                errors.append(f"{batch_id}: auditor must be a non-empty string when present")
                auditor = None
            if not isinstance(finding_id, str) or not finding_id.strip():
                errors.append(f"{batch_id}: finding_id must be a non-empty string")
            else:
                if isinstance(auditor, str):
                    live_finding = live_by_key.get((auditor, finding_id))
                    if live_finding is None and live_by_id.get(finding_id):
                        reported_by = sorted(f["_auditor"] for f in live_by_id[finding_id])
                        errors.append(
                            f"{batch_id}: auditor mismatch for {finding_id}: "
                            f"repair-log={auditor!r} audit-report={', '.join(reported_by)!r}"
                        )
                        live_finding = live_by_id[finding_id][0]
                else:
                    matches = live_by_id.get(finding_id, [])
                    if len(matches) > 1:
                        errors.append(
                            f"{batch_id}: finding_id {finding_id} is reported by several auditors "
                            f"({', '.join(sorted(f['_auditor'] for f in matches))}); the task must name its auditor"
                        )
                    live_finding = matches[0] if matches else None
                if live_finding is None:
                    errors.append(f"{batch_id}: finding_id not found in live audit reports: {finding_id}")
                key = (live_finding["_auditor"] if live_finding is not None else str(auditor or ""), finding_id)
                if key in seen_findings:
                    errors.append(f"duplicate finding_id across repair-log batches: {finding_id}")
                else:
                    seen_findings.add(key)
            severity = task.get("severity")
            severity = normalize_repair_severity(severity) if severity is not None else None
            live_sev = live_finding.get("severity") if live_finding is not None else None
            live_sev_norm = normalize_repair_severity(live_sev) if isinstance(live_sev, str) else live_sev
            if severity is not None and severity not in {"critical", "high", "medium", "low"}:
                errors.append(f"{batch_id}: invalid severity {severity!r}")
            elif (
                severity is not None
                and live_finding is not None
                and isinstance(live_finding.get("severity"), str)
                and severity != live_sev_norm
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
                errors.append(invalid_enum_error(f"{batch_id}: assigned_executor", executor, VALID_EXECUTORS))
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
            _VALID_MODEL_OVERRIDES: frozenset[str] = (
                valid_models_for_host(HOST_CLAUDE) | frozenset(ALL_TIERS)
            )
            if model_override is not None and model_override not in _VALID_MODEL_OVERRIDES:
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


def _timeline_time(value: object):
    from datetime import datetime, timezone
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def compute_planning_timeline(task_dir: Path) -> dict:
    """Compute pre-execution time buckets from hook and ctl telemetry.

    Spawn durations come from ``stop`` rows in ``spawn-log.jsonl`` (written
    by the SubagentStop hook from transcript timestamps). The older
    ``pre``/``post`` pair measures only the Agent tool call, which returns
    within a second of the spawn starting; such pairs are reported with
    ``seconds: None`` and ``source: "unmeasured"`` and counted in
    ``unmeasured_spawns`` instead of silently contributing zero. ``errors``
    names missing inputs so a caller never mistakes a degraded result for a
    fast task.
    """
    td = Path(task_dir)
    out: dict = {
        "pre_execution_seconds": None, "planner_spawns": [], "model_seconds": 0,
        "approval_waits": [], "approval_seconds": 0, "idle_gaps": [],
        "idle_seconds": 0, "orchestrator_seconds": 0, "planner_respawns": 0,
        "human_rounds": 0, "unmeasured_spawns": 0, "errors": [],
    }
    errors: list[str] = out["errors"]
    start = None
    try:
        manifest = load_json(td / "manifest.json")
        start = _timeline_time(manifest.get("created_at")) if isinstance(manifest, dict) else None
    except Exception:
        errors.append("manifest.json missing or unreadable")
    if start is None and not errors:
        errors.append("manifest.created_at missing")
    transitions = []
    transitions_path = td / "stage-transitions.jsonl"
    if transitions_path.exists():
        for line in transitions_path.read_text().splitlines():
            try:
                item = json.loads(line); stamp = _timeline_time(item.get("at"))
                if stamp: transitions.append((stamp, item))
            except Exception: pass
    else:
        errors.append("stage-transitions.jsonl missing; pre-execution window unknown")
    pre_end = next((stamp for stamp, item in transitions if item.get("to") == "PRE_EXECUTION_SNAPSHOT"), None)
    if transitions_path.exists() and pre_end is None:
        errors.append("no PRE_EXECUTION_SNAPSHOT transition recorded; task has not finished planning")
    if start and pre_end:
        out["pre_execution_seconds"] = max(0, int((pre_end-start).total_seconds()))
    tracked = ("planning", "spec-completion-auditor", "testing-executor")
    rows: list[dict] = []
    spawn_log = td / "spawn-log.jsonl"
    if spawn_log.exists():
        for line in spawn_log.read_text().splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict) and str(row.get("subagent_type", "")).endswith(tracked):
                rows.append(row)
    else:
        errors.append("spawn-log.jsonl missing; no spawn durations")
    spawns: list[dict] = []
    # Preferred source: stop rows with measured durations.
    stop_rows = [r for r in rows if r.get("phase") == "stop"]
    consumed_pre: set[int] = set()
    pre_rows = [(i, r) for i, r in enumerate(rows) if r.get("phase") == "pre"]
    for row in stop_rows:
        role = str(row.get("subagent_type", ""))
        begin = _timeline_time(row.get("started_at")); finish = _timeline_time(row.get("ended_at"))
        if not begin or not finish:
            continue
        if pre_end and begin > pre_end:
            continue
        seconds = row.get("seconds")
        if not isinstance(seconds, int):
            seconds = max(0, int((finish-begin).total_seconds()))
        # Consume the nearest earlier pre row for the same role so the
        # fallback pairing below does not double-count the spawn.
        best = None
        for i, pre in pre_rows:
            if i in consumed_pre or str(pre.get("subagent_type", "")) != role:
                continue
            stamp = _timeline_time(pre.get("timestamp"))
            if stamp and stamp <= finish and (best is None or stamp > best[1]):
                best = (i, stamp)
        if best is not None:
            consumed_pre.add(best[0])
        spawns.append({"phase_guess": role.split(":")[-1], "subagent_type": role,
                       "start": begin.isoformat(), "end": finish.isoformat(),
                       "seconds": seconds, "source": "transcript"})
    # Fallback: pre/post pairs for spawns without a stop row.
    paired: dict[str, list] = {}
    for i, row in enumerate(rows):
        role = str(row.get("subagent_type", ""))
        stamp = _timeline_time(row.get("timestamp"))
        if not stamp or (pre_end and stamp > pre_end):
            continue
        if row.get("phase") == "pre" and i not in consumed_pre:
            paired.setdefault(role, []).append(stamp)
        elif row.get("phase") == "post" and paired.get(role):
            begin = paired[role].pop(0)
            seconds = max(0, int((stamp-begin).total_seconds()))
            measured = seconds > 1
            spawns.append({"phase_guess": role.split(":")[-1], "subagent_type": role,
                           "start": begin.isoformat(), "end": stamp.isoformat(),
                           "seconds": seconds if measured else None,
                           "source": "tool-return" if measured else "unmeasured"})
    spawns.sort(key=lambda row: row["start"])
    out["planner_spawns"] = spawns
    out["unmeasured_spawns"] = sum(1 for row in spawns if row["seconds"] is None)
    out["model_seconds"] = sum(row["seconds"] for row in spawns if isinstance(row["seconds"], int))
    if out["unmeasured_spawns"]:
        errors.append(
            f"{out['unmeasured_spawns']} spawn(s) have no measured duration (pre/post rows only); "
            "model_seconds is a lower bound"
        )
    approval_rows=[]
    for receipt in (td/"receipts").glob("*-approval-*.json") if (td/"receipts").exists() else []:
        try:
            data=load_json(receipt); stamp=_timeline_time(data.get("written_at"))
            if stamp: approval_rows.append((stamp,receipt.name))
        except Exception: pass
    waits=[]
    for stage in ("SPEC_REVIEW","PLAN_REVIEW","TDD_REVIEW"):
        requested=next((stamp for stamp,item in transitions if item.get("to")==stage),None)
        approval=next((stamp for stamp,name in approval_rows if stage in name and (not requested or stamp>=requested)),None)
        seconds=max(0,int((approval-requested).total_seconds())) if requested and approval else 0
        waits.append({"stage":stage,"requested_at":requested.isoformat() if requested else None,
                      "granted_at":approval.isoformat() if approval else None,"seconds":seconds})
    out["approval_waits"]=waits
    out["approval_seconds"]=sum(w["seconds"] for w in waits)
    receipts_at=[stamp for stamp,_ in approval_rows]
    points=sorted([stamp for stamp,_ in transitions]+receipts_at+[ _timeline_time(s[k]) for s in spawns for k in ("start","end") if _timeline_time(s[k])])
    if start: points=[start]+points
    if pre_end: points.append(pre_end)
    points=sorted(set(p for p in points if p and (not start or p>=start) and (not pre_end or p<=pre_end)))
    idle=[]
    for left,right in zip(points,points[1:]):
        seconds=int((right-left).total_seconds())
        if seconds>1800: idle.append({"start":left.isoformat(),"end":right.isoformat(),"seconds":seconds})
    out["idle_gaps"]=idle; out["idle_seconds"]=sum(x["seconds"] for x in idle)
    if out["pre_execution_seconds"] is not None:
        out["orchestrator_seconds"]=max(0,out["pre_execution_seconds"]-out["model_seconds"]-out["approval_seconds"]-out["idle_seconds"])
    phase_count=len({row["phase_guess"] for row in spawns})
    out["planner_respawns"]=max(0,len(spawns)-phase_count)
    human_markers=0
    log=td/"execution-log.md"
    if log.exists():
        try: human_markers=sum(1 for line in log.read_text().splitlines() if "[HUMAN]" in line or "AskUserQuestion" in line)
        except OSError: pass
    out["human_rounds"]=len(approval_rows)+human_markers
    return out


def compute_pipeline_budget(task_dir: Path) -> dict:
    """Compute audit-phase latency / token KPIs from token-usage events.

    Surfaces the metrics that the latency investigation
    (/tmp/bug_report.json recommendation #5) names as the missing signal:
    without these, every well-meaning "perf:" or "fix:" commit can
    silently grow the audit phase and no regression alarm fires.

    Returns a dict with at minimum:
      - audit_phase_llm_calls: count of token-usage events whose
        phase=='audit' AND type=='spawn'. This is the cascade
        multiplication factor: under fast→balanced→deep tiers, a single auditor
        role can fan out into 3 of these.
      - audit_phase_input_tokens: sum of input_tokens across audit-phase
        events. Pre-loaded prompt context (CG-013) shows up here.
      - audit_phase_output_tokens: sum of output_tokens across audit-phase
        events.
      - audit_phase_total_tokens: convenience sum.

    Graceful degradation: missing token-usage.json or empty events list
    returns zeros for all fields. Malformed events (non-dicts, missing
    keys) are skipped silently — token-usage.json is append-only telemetry
    and unrelated parser bugs upstream should not corrupt the budget.

    Audit-only by intent. Execution and planning have different latency
    shapes; the bug investigation specifically named the audit phase as
    the regression hotspot, so the KPI is targeted there.
    """
    usage_path = Path(task_dir) / "token-usage.json"
    if not usage_path.is_file():
        return {
            "audit_phase_llm_calls": 0,
            "audit_phase_input_tokens": 0,
            "audit_phase_output_tokens": 0,
            "audit_phase_total_tokens": 0,
        }
    try:
        data = load_json(usage_path)
    except (json.JSONDecodeError, OSError):
        return {
            "audit_phase_llm_calls": 0,
            "audit_phase_input_tokens": 0,
            "audit_phase_output_tokens": 0,
            "audit_phase_total_tokens": 0,
        }
    events = data.get("events", []) if isinstance(data, dict) else []
    if not isinstance(events, list):
        events = []

    llm_calls = 0
    input_tokens = 0
    output_tokens = 0
    for e in events:
        if not isinstance(e, dict):
            continue
        if e.get("phase") != "audit":
            continue
        try:
            input_tokens += int(e.get("input_tokens") or 0)
        except (TypeError, ValueError):
            pass
        try:
            output_tokens += int(e.get("output_tokens") or 0)
        except (TypeError, ValueError):
            pass
        if e.get("type") == "spawn":
            llm_calls += 1

    return {
        "audit_phase_llm_calls": llm_calls,
        "audit_phase_input_tokens": input_tokens,
        "audit_phase_output_tokens": output_tokens,
        "audit_phase_total_tokens": input_tokens + output_tokens,
    }


# A repair "converges" when the fix sticks. A finding whose retry_count reaches
# this bound has been repaired and re-flagged repeatedly — the repair loop is
# churning without resolving it. This is the genuine "wasted spawn" signal that
# replaced the old (inverted) clean-audit count. Kept in lockstep with the
# retry_count>=2 deep-tier escalation trigger in ctl build-repair-log.
REPAIR_NONCONVERGENCE_RETRIES = 2

# Bump when the wasted_spawns SIGNAL changes meaning, so the learning engine
# cold-starts (see memory/policy_engine._build_spawn_budget_policy_data) instead
# of mixing incomparable observations across the definition change.
WASTED_SPAWNS_SIGNAL_VERSION = 2


def count_nonconverging_repairs(task_dir: Path) -> int:
    """Count distinct findings whose repair is not converging.

    Reads repair-log.json and returns the number of finding_ids whose maximum
    retry_count across all batches is >= REPAIR_NONCONVERGENCE_RETRIES — findings
    repaired repeatedly that keep coming back. This is the genuine no-progress
    signal that replaced the inverted "clean audit == wasted" count. Returns 0
    when the repair log is absent or unreadable: a task that never needed repair
    has, by definition, no non-converging repairs.
    """
    repair_log_path = task_dir / "repair-log.json"
    if not repair_log_path.exists():
        return 0
    try:
        repair_log = load_json(repair_log_path)
    except Exception:
        return 0
    if not isinstance(repair_log, dict):
        return 0
    max_retry: dict[str, int] = {}
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
                rc = int(task.get("retry_count", 0) or 0)
            except (TypeError, ValueError):
                rc = 0
            if rc > max_retry.get(fid, -1):
                max_retry[fid] = rc
    return sum(1 for rc in max_retry.values() if rc >= REPAIR_NONCONVERGENCE_RETRIES)


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
    raw_blocking = 0
    raw_blocking_ids: set[str] = set()
    # Report files that passed the routing cross-check (AC 16).
    routed_report_paths: set[str] = set()
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

            # D3: map legacy/timestamped/model-suffixed names onto the routed
            # auditor name. Left untouched when the routing check is disabled.
            if not _auditor_check_disabled and valid_auditor_names is not None:
                _mapped = _normalize_auditor_name(auditor, valid_auditor_names)
                if _mapped is not None:
                    auditor = _mapped

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
                if not finding_is_blocking(finding):
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
            routed_report_paths.add(str(report_path))
            raw_blocking += sum(1 for f in findings if finding_is_blocking(f))
            for finding in findings:
                if not isinstance(finding, dict):
                    continue
                fid = finding.get("id", "") or ""
                if finding_is_blocking(finding) and isinstance(fid, str) and fid:
                    raw_blocking_ids.add(fid)
                category = fid.split("-")[0] if "-" in fid else fid
                if category:
                    findings_by_category[category] = findings_by_category.get(category, 0) + 1

    # Blocking counts use the findings gate's selection: an attempt a re-audit
    # replaced and a voting-tier report a binding deep-tier verdict overruled
    # do not block, and neither does a human-accepted finding. Summing raw
    # report files counted those as "surviving" on a task the gate cleared,
    # which scored quality 0.0. Reports the routing cross-check dropped stay
    # dropped.
    try:
        binding_blocking = [
            f for f in collect_repair_findings(task_dir)["findings"]
            if finding_is_blocking(f) and f.get("_report_path") in routed_report_paths
        ]
    except Exception:  # noqa: BLE001 — never fail the retrospective on selection
        binding_blocking = None
    if binding_blocking is None:
        total_blocking = raw_blocking
        post_repair_blocking_ids = raw_blocking_ids
    else:
        total_blocking = len(binding_blocking)
        post_repair_blocking_ids = {
            str(f["id"]) for f in binding_blocking if isinstance(f.get("id"), str) and f["id"]
        }

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
            def _retro_sort_key(item: dict) -> tuple:
                tid = str(item.get("task_id", ""))
                m = re.match(r"task-(\d{8})-(\d+)$", tid)
                return (m.group(1), int(m.group(2))) if m else ("", 0)

            prior_retro = sorted(retros, key=_retro_sort_key)[-1]
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

    # #24: If no repair-log.json was present, fall back to the manifest's own
    # repair_cycle_count field (written by the pipeline before any repair log
    # is created).  The repair-log value wins when the file exists.
    if repair_cycle_count == 0:
        try:
            repair_cycle_count = int(manifest.get("repair_cycle_count", 0) or 0)
        except (ValueError, TypeError):
            pass

    stage = manifest.get("stage", "")
    # #24: change_failure is True when the task reached FAILED after at least one
    # repair cycle. 'REPAIR_FAILED' is not a real stage; the terminal failure
    # stage emitted by the pipeline is 'FAILED'.
    change_failure = stage == "FAILED" and repair_cycle_count > 0

    recovery_time_seconds: int | None = None
    if change_failure and log_path.exists():
        try:
            from datetime import datetime, timezone
            fail_time = None
            done_time = None
            for line in log_path.read_text().splitlines():
                # _auto_log emits '[FAILED]' (via the FAILED transition tag).
                if "[FAILED]" in line and fail_time is None:
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

    # wasted_spawns := repair non-convergence, NOT clean audits. A clean audit
    # is a passing dimension (the goal), never waste; the genuine no-progress
    # signal is a finding the repair loop keeps re-flagging. See
    # docs/spawn-budget-convergence-design.md.
    wasted_spawns = count_nonconverging_repairs(task_dir)

    # --- 7. Reward vector ---
    # quality_score — only blocking findings affect quality.
    # Non-blocking findings are informational and don't penalize.
    if total_blocking == 0:
        # #47: zero findings = perfect quality; non-zero informational-only
        # findings still score 0.9 (non-blocking don't penalise quality).
        quality_score = 1.0 if total_findings == 0 else 0.9
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
        "wasted_spawns_signal_version": WASTED_SPAWNS_SIGNAL_VERSION,
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
        "pipeline_budget": compute_pipeline_budget(task_dir),
        "planning_timeline": compute_planning_timeline(task_dir),
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
                if not any(
                    files_expected_entry_matches(entry, str(file_path))
                    for entry in allowed
                ):
                    violations.append(str(file_path))
            return violations
    raise ValueError(f"Unknown segment id: {segment_id}")
