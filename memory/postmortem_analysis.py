#!/usr/bin/env python3
"""LLM-powered postmortem analysis for dynos-work.

Reads task artifacts (retrospective, audit reports, repair log) and
produces a structured analysis with prevention rules, root causes,
and improvement proposals. Designed to be called from the audit skill
which has Agent tool access.

Two modes:
  build-prompt  — reads artifacts, outputs a structured prompt for an LLM
  apply         — reads LLM output (JSON from stdin), writes prevention rules
"""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent)); _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "hooks"))

import argparse
import fcntl
import json
import os
from pathlib import Path

from lib_core import (
    _persistent_project_dir,
    load_json,
    now_iso,
    write_json,
)
from lib_log import log_event
from lib_receipts import (
    hash_file,
    receipt_postmortem_analysis,
    receipt_postmortem_skipped,
)

VALID_CATEGORIES = {"sec", "cq", "dc", "perf", "comp", "ui", "db", "test", "process", "unknown"}
VALID_SEVERITIES = {"high", "medium", "low"}
VALID_MODELS = {"haiku", "sonnet", "opus"}
VALID_ENFORCEMENT = {
    "test",
    "lint",
    "static-check",
    "runtime-guard",
    "ci-gate",
    "review-checklist",
    "prompt-constraint",
}
VALID_EXECUTORS = {
    "backend-executor",
    "ui-executor",
    "db-executor",
    "integration-executor",
    "testing-executor",
    "docs-executor",
    "refactor-executor",
    "ml-executor",
    "all",
}

# Mirrors `hooks.rules_engine.TEMPLATE_SCHEMAS`. Kept inline here to keep
# postmortem writes independent of the rules_engine module (which is
# authored in seg-1 of this task, running in parallel). A seg-6 test
# verifies that the two dicts stay structurally aligned; if you change
# one you MUST change the other.
TEMPLATE_SCHEMAS: dict = {
    "every_name_in_X_satisfies_Y": {
        "required": ["module", "container", "predicate"],
        "enums": {"predicate": ["callable", "hasattr", "in_registry"]},
        "types": {"module": str, "container": str, "predicate": str},
    },
    "pattern_must_not_appear": {
        "required": ["regex", "scope"],
        "types": {"regex": str, "scope": str},
    },
    "co_modification_required": {
        "required": ["trigger_glob", "must_modify_glob"],
        "types": {"trigger_glob": str, "must_modify_glob": str},
    },
    "signature_lock": {
        "required": ["module", "function", "expected_params"],
        "types": {"module": str, "function": str, "expected_params": list},
    },
    "caller_count_required": {
        "required": ["symbol", "scope", "min_count"],
        "types": {"symbol": str, "scope": str, "min_count": int},
    },
    "import_constant_only": {
        "required": ["literal_pattern", "allowed_files"],
        "types": {"literal_pattern": str, "allowed_files": list},
    },
    "advisory": {"required": []},
}


def _validate_rule_schema(rule: object) -> tuple[bool, str]:
    """Validate one prevention rule against TEMPLATE_SCHEMAS.

    Returns (ok, reason). When ok is False, *reason* names the exact
    check that failed so the REJECT stderr line is actionable.
    """
    if not isinstance(rule, dict):
        return False, "rule is not a dict"
    template = rule.get("template")
    if template is None:
        return False, "missing template field"
    if template not in TEMPLATE_SCHEMAS:
        return False, f"unknown template: {template}"
    schema = TEMPLATE_SCHEMAS[template]
    params = rule.get("params")
    if not isinstance(params, dict):
        # advisory has no required keys, but still must have a (possibly
        # empty) params dict — callers may extend advisory later.
        if schema.get("required"):
            return False, "params is not a dict"
        params = {}
    # (b) required keys present & non-None
    for key in schema.get("required", []):
        if params.get(key) is None:
            return False, f"missing required param: {key}"
    # (c) enum check
    for key, allowed in schema.get("enums", {}).items():
        if params.get(key) not in allowed:
            return False, f"param {key}={params.get(key)!r} not in enum {allowed}"
    # (d) type check
    for key, typ in schema.get("types", {}).items():
        value = params.get(key)
        # bool is a subclass of int; reject bools where int is required
        # unless the schema explicitly permits bool.
        if typ is int and isinstance(value, bool):
            return False, f"param {key} expected {typ.__name__}, got bool"
        if not isinstance(value, typ):
            return False, (
                f"param {key} expected {typ.__name__}, "
                f"got {type(value).__name__}"
            )
    return True, ""


def _read_artifact(path: Path) -> dict | list | None:
    """Read a JSON artifact, returning None if missing or broken."""
    try:
        return load_json(path)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _collect_audit_findings(task_dir: Path) -> list[dict]:
    """Collect all findings from audit reports."""
    reports_dir = task_dir / "audit-reports"
    if not reports_dir.is_dir():
        return []
    findings = []
    for report_path in sorted(reports_dir.glob("*.json")):
        report = _read_artifact(report_path)
        if not isinstance(report, dict):
            continue
        auditor = report.get("auditor_name", report_path.stem)
        for f in report.get("findings", []):
            if isinstance(f, dict):
                f["_auditor"] = auditor
                findings.append(f)
    return findings


def _clean_str(value: object, limit: int | None = None) -> str:
    """Convert value to a compact single-line string."""
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        text = str(value)
    cleaned = " ".join(text.strip().split())
    if limit is not None:
        return cleaned[:limit].strip()
    return cleaned


def _normalize_category(value: object) -> str:
    category = _clean_str(value, 32).lower()
    return category if category in VALID_CATEGORIES else "unknown"


def _normalize_executor(value: object, *, allow_null: bool = False) -> str | None:
    executor = _clean_str(value, 64).lower()
    if not executor:
        return None if allow_null else "all"
    if executor in VALID_EXECUTORS:
        return executor
    if executor == "null" and allow_null:
        return None
    return None if allow_null else "all"


def _normalize_severity(value: object) -> str:
    severity = _clean_str(value, 16).lower()
    return severity if severity in VALID_SEVERITIES else "medium"


def _normalize_evidence(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    evidence: list[str] = []
    for item in value:
        text = _clean_str(item, 240)
        if text:
            evidence.append(text)
    return evidence[:5]


def _normalize_rule(item: object) -> dict | None:
    if not isinstance(item, dict):
        return None
    rule = _clean_str(item.get("rule"), 100)
    if not rule:
        return None
    enforcement = _clean_str(item.get("enforcement"), 32).lower()
    if enforcement not in VALID_ENFORCEMENT:
        enforcement = "prompt-constraint"
    normalized: dict = {
        "executor": _normalize_executor(item.get("executor")) or "all",
        "category": _normalize_category(item.get("category")),
        "rule": rule,
        "source_finding": _clean_str(item.get("source_finding"), 240),
        "rationale": _clean_str(item.get("rationale"), 300),
        "enforcement": enforcement,
    }
    # Preserve structured template + params for AC 11 validation and AC 13
    # template-aware routing. We deliberately keep the raw LLM values so
    # the validator can reject ill-formed rules with a precise reason.
    if "template" in item:
        normalized["template"] = item.get("template")
    if isinstance(item.get("params"), dict):
        normalized["params"] = item.get("params")
    return normalized


def _normalize_analysis(analysis: object) -> dict:
    """Sanitize LLM output into the supported postmortem schema."""
    payload = analysis if isinstance(analysis, dict) else {}

    root_causes: list[dict] = []
    for item in payload.get("root_causes", []):
        if not isinstance(item, dict):
            continue
        root_causes.append({
            "finding_category": _normalize_category(item.get("finding_category")),
            "root_cause": _clean_str(item.get("root_cause"), 240),
            "immediate_cause": _clean_str(item.get("immediate_cause"), 240),
            "detection_failure": _clean_str(item.get("detection_failure"), 240),
            "affected_executor": _normalize_executor(item.get("affected_executor"), allow_null=True),
            "severity": _normalize_severity(item.get("severity")),
            "evidence": _normalize_evidence(item.get("evidence")),
        })
    root_causes = [item for item in root_causes if item["root_cause"]][:20]

    # Parity hardening: LLM outputs vary in top-level rule key. Accept both
    # `prevention_rules` (canonical per build_analysis_prompt) AND
    # `derived_rules` (commonly produced by general-purpose LLM agents).
    # Deduplicate by rule text so neither side inflates counts. Also
    # track each DROP so apply_analysis can report the derived-vs-added
    # delta — silent drops were the root cause of task-20260419-002's
    # zero-rules-promoted outcome.
    prevention_rules: list[dict] = []
    _normalization_drops: list[dict] = []
    seen_rule_texts: set[str] = set()
    raw_rule_items: list[object] = list(payload.get("prevention_rules", []))
    if isinstance(payload.get("derived_rules"), list):
        raw_rule_items.extend(payload["derived_rules"])
    for item in raw_rule_items:
        normalized = _normalize_rule(item)
        if normalized is None:
            # Dropped by _normalize_rule — not a dict, or missing 'rule' text.
            _normalization_drops.append({
                "item": item,
                "reason": "normalize_returned_none",
            })
            continue
        text_key = normalized["rule"].strip().lower()
        if text_key in seen_rule_texts:
            _normalization_drops.append({
                "item": item,
                "reason": "duplicate_text_within_analysis",
            })
            continue
        seen_rule_texts.add(text_key)
        prevention_rules.append(normalized)

    repair_failures: list[dict] = []
    for item in payload.get("repair_failures", []):
        if not isinstance(item, dict):
            continue
        failure = _clean_str(item.get("failure"), 240)
        why = _clean_str(item.get("why_it_failed"), 240)
        evidence = _normalize_evidence(item.get("evidence"))
        if failure or why or evidence:
            repair_failures.append({
                "failure": failure,
                "why_it_failed": why,
                "evidence": evidence,
            })
    repair_failures = repair_failures[:20]

    model_suggestions: list[dict] = []
    for item in payload.get("model_suggestions", []):
        if not isinstance(item, dict):
            continue
        agent = _clean_str(item.get("agent"), 128)
        suggested_model = _clean_str(item.get("suggested_model"), 16).lower()
        if not agent or suggested_model not in VALID_MODELS:
            continue
        current_model = _clean_str(item.get("current_model"), 16).lower()
        if current_model not in VALID_MODELS:
            current_model = None
        model_suggestions.append({
            "agent": agent,
            "current_model": current_model,
            "suggested_model": suggested_model,
            "reason": _clean_str(item.get("reason"), 240),
        })
    model_suggestions = model_suggestions[:20]

    return {
        "summary": _clean_str(payload.get("summary"), 400),
        "root_causes": root_causes,
        "prevention_rules": prevention_rules,
        "repair_failures": repair_failures,
        "model_suggestions": model_suggestions,
        "hard_truth": _clean_str(payload.get("hard_truth"), 240),
        # Parity metadata — consumers (apply_analysis) use these to
        # detect silent drops. `_input_rule_count` is the RAW count from
        # the LLM output before any normalization; `_normalization_drops`
        # lists every item that was rejected and why. The leading
        # underscore signals internal bookkeeping that shouldn't leak
        # into persisted prevention-rules.json entries.
        "_input_rule_count": len(raw_rule_items),
        "_normalization_drops": _normalization_drops,
    }


def build_analysis_prompt(task_dir: Path) -> dict:
    """Build a structured prompt for LLM analysis of a completed task.

    Returns {"prompt": str, "has_findings": bool, "task_id": str}.
    If the task had no findings or repairs, returns has_findings=False
    and the caller can skip the LLM spawn.
    """
    task_dir = Path(task_dir).resolve()

    retro = _read_artifact(task_dir / "task-retrospective.json") or {}
    repair_log = _read_artifact(task_dir / "repair-log.json")
    findings = _collect_audit_findings(task_dir)
    manifest = _read_artifact(task_dir / "manifest.json") or {}

    # Read deterministic postmortem (Step 5a runs before us)
    root = task_dir.parent.parent
    persistent = _persistent_project_dir(root)
    task_id_for_pm = retro.get("task_id", manifest.get("task_id", ""))
    postmortem = _read_artifact(persistent / "postmortems" / f"{task_id_for_pm}.json") if task_id_for_pm else None

    task_id = retro.get("task_id", manifest.get("task_id", "unknown"))
    repair_count = int(retro.get("repair_cycle_count", 0))
    quality = retro.get("quality_score", 1.0)

    # Skip analysis if task was clean — nothing to learn from
    has_findings = bool(findings) or repair_count > 0 or (quality is not None and float(quality) < 0.8)
    if not has_findings:
        return {"prompt": "", "has_findings": False, "task_id": task_id}

    # Build context sections
    sections = []

    sections.append(f"Task: {task_id}")
    sections.append(
        f"Title: {manifest.get('title', manifest.get('task', 'unknown'))}"
    )
    sections.append(
        f"Type: {retro.get('task_type', 'unknown')}, "
        f"Domains: {retro.get('task_domains', 'unknown')}, "
        f"Risk: {retro.get('task_risk_level', 'unknown')}"
    )
    sections.append(
        f"Stage: {manifest.get('stage', retro.get('task_outcome', 'unknown'))}, "
        f"Quality: {quality}, Repairs: {repair_count}, "
        f"Tokens: {retro.get('total_token_usage', 0)}"
    )
    sections.append(
        f"Spawn Count: {retro.get('subagent_spawn_count', 0)}, "
        f"Wasted Spawns: {retro.get('wasted_spawns', 0)}, "
        f"Spec Reviews: {retro.get('spec_review_iterations', 0)}"
    )
    sections.append("")

    if findings:
        sections.append(f"## Audit Findings ({len(findings)} total)")
        for f in findings[:20]:  # cap to avoid huge prompts
            description = f.get("description", f.get("message", f.get("id", "?")))
            sections.append(
                f"- [{f.get('_auditor', '?')}] "
                f"id={f.get('id', '?')} "
                f"category={f.get('category', '?')} "
                f"severity={f.get('severity', '?')} "
                f"blocking={f.get('blocking', False)} "
                f"title={f.get('title', '?')} "
                f"detail={description}"
            )
            if f.get("file"):
                sections.append(f"  File: {f['file']}:{f.get('line', '?')}")
            if f.get("evidence"):
                evidence = f.get("evidence")
                if isinstance(evidence, list):
                    for item in evidence[:3]:
                        sections.append(f"  Evidence: {item}")
                else:
                    sections.append(f"  Evidence: {evidence}")
            if f.get("recommendation"):
                sections.append(f"  Recommendation: {f['recommendation']}")
        sections.append("")

    if repair_log and isinstance(repair_log, dict):
        sections.append("## Repair Log")
        for batch in repair_log.get("batches", [])[:5]:
            cycle = batch.get("repair_cycle", "?")
            tasks = batch.get("tasks", [])
            for t in tasks[:10]:
                line = (
                    f"- Cycle {cycle}: "
                    f"{t.get('finding_id', '?')} -> "
                    f"{t.get('executor', '?')} "
                    f"status={t.get('status', '?')}"
                )
                if t.get("route_mode"):
                    line += f" route={t.get('route_mode')}"
                if t.get("model"):
                    line += f" model={t.get('model')}"
                sections.append(line)
                for key in ("error", "failure_reason", "reason", "notes"):
                    if t.get(key):
                        sections.append(f"  {key}: {t[key]}")
        sections.append("")

    findings_by_cat = retro.get("findings_by_category", {})
    if findings_by_cat:
        sections.append("## Finding Categories")
        for cat, count in sorted(findings_by_cat.items(), key=lambda x: -x[1]):
            sections.append(f"- {cat}: {count}")
        sections.append("")

    executor_repairs = retro.get("executor_repair_frequency", {})
    if executor_repairs:
        sections.append("## Executor Repair Frequency")
        for ex, count in sorted(executor_repairs.items(), key=lambda x: -x[1]):
            sections.append(f"- {ex}: {count} repairs")
        sections.append("")

    findings_by_auditor = retro.get("findings_by_auditor", {})
    if findings_by_auditor:
        sections.append("## Findings By Auditor")
        for auditor, count in sorted(findings_by_auditor.items(), key=lambda x: -x[1]):
            sections.append(f"- {auditor}: {count}")
        sections.append("")

    model_used = retro.get("model_used_by_agent", {})
    if model_used:
        sections.append("## Models Used")
        for agent, model in sorted(model_used.items()):
            sections.append(f"- {agent}: {model}")
        sections.append("")

    # Include deterministic postmortem insights (anomalies, patterns, similar tasks)
    if postmortem and isinstance(postmortem, dict):
        anomalies = postmortem.get("anomalies", [])
        if anomalies:
            sections.append(f"## Detected Anomalies ({len(anomalies)})")
            for a in anomalies[:10]:
                if isinstance(a, dict):
                    sections.append(f"- [{a.get('type', '?')}] {a.get('description', a.get('message', '?'))}")
                else:
                    sections.append(f"- {a}")
            sections.append("")

        patterns = postmortem.get("recurring_patterns", [])
        if patterns:
            sections.append(f"## Recurring Patterns ({len(patterns)})")
            for p in patterns[:10]:
                if isinstance(p, dict):
                    sections.append(f"- [{p.get('type', '?')}] {p.get('description', p.get('message', '?'))} (seen in {p.get('task_count', '?')} tasks)")
                else:
                    sections.append(f"- {p}")
            sections.append("")

        similar = postmortem.get("similar_tasks", [])
        if similar:
            sections.append(f"## Similar Past Tasks ({len(similar)})")
            for s in similar[:5]:
                if isinstance(s, dict):
                    sections.append(f"- {s.get('task_id', '?')} (similarity={s.get('similarity', '?')}, quality={s.get('quality_score', '?')})")
                else:
                    sections.append(f"- {s}")
            sections.append("")

    context = "\n".join(sections)

    prompt = f"""You are a ruthless engineering postmortem analyst.

Your job is to analyze a completed task using its audit findings, repair attempts,
retrospective data, and execution outcomes. Be severe, specific, and evidence-driven.
Do not praise the task. Do not soften failures. Do not give generic advice.
Find what broke, why it broke, why it was not caught earlier, and what exact rule
would have made repetition materially harder.

Only analyze what actually appears in the task data below.

TASK DATA
{context}

Return ONLY a JSON object. No markdown. No commentary. No prose outside JSON.

The JSON must have this exact top-level shape:
{{
  "summary": "1-2 sentence blunt assessment of the task failure pattern",
  "root_causes": [
    {{
      "finding_category": "sec|cq|dc|perf|comp|ui|db|test|process|unknown",
      "root_cause": "Single sentence naming the underlying mechanism",
      "immediate_cause": "What directly produced the failure",
      "detection_failure": "Why checks, tests, review, or routing did not catch it",
      "affected_executor": "backend-executor|ui-executor|db-executor|integration-executor|testing-executor|docs-executor|refactor-executor|ml-executor|all|null",
      "severity": "high|medium|low",
      "evidence": [
        "Concrete fact from the task data"
      ]
    }}
  ],
  "prevention_rules": [
    {{
      "template": "every_name_in_X_satisfies_Y|pattern_must_not_appear|co_modification_required|signature_lock|caller_count_required|import_constant_only|advisory",
      "params": {{}},
      "executor": "backend-executor|ui-executor|db-executor|integration-executor|testing-executor|docs-executor|refactor-executor|ml-executor|all",
      "rule": "Specific preventive rule in imperative voice, max 100 chars",
      "category": "sec|cq|dc|perf|comp|ui|db|test|process|unknown",
      "source_finding": "Finding ID, repair entry, or exact finding description",
      "rationale": "Why this rule would have prevented the failure",
      "enforcement": "test|lint|static-check|runtime-guard|ci-gate|review-checklist|prompt-constraint"
    }}
  ],
  "repair_failures": [
    {{
      "failure": "Why an initial repair attempt failed or was incomplete",
      "why_it_failed": "Specific reason the first fix missed the mechanism",
      "evidence": [
        "Concrete fact from repair log or findings"
      ]
    }}
  ],
  "model_suggestions": [
    {{
      "agent": "agent-name",
      "current_model": "haiku|sonnet|opus|null",
      "suggested_model": "haiku|sonnet|opus",
      "reason": "Only include when task data supports a real model-capability mismatch"
    }}
  ],
  "hard_truth": "One sentence naming the biggest systemic weakness exposed by this task"
}}

## Prevention rule schema

Each prevention_rules entry MUST use this structured format:
{{
  "template": "<one of: every_name_in_X_satisfies_Y | pattern_must_not_appear | co_modification_required | signature_lock | caller_count_required | import_constant_only | advisory>",
  "params": {{...template-specific keys...}},
  "rule": "<short human-readable rule text, max 100 chars>",
  "executor": "<executor name or 'all'>",
  "category": "sec|cq|dc|perf|comp|test|process|unknown",
  "source_finding": "<finding ID or description>",
  "rationale": "<why this rule prevents recurrence>",
  "enforcement": "test|lint|static-check|runtime-guard|ci-gate|review-checklist|prompt-constraint"
}}

### Template params:
- every_name_in_X_satisfies_Y: {{module: str, container: str, predicate: "callable"|"hasattr"|"in_registry"}}
    Example: {{"template": "every_name_in_X_satisfies_Y", "params": {{"module": "hooks.lib_receipts", "container": "__all__", "predicate": "callable"}}, ...}}
- pattern_must_not_appear: {{regex: str, scope: glob-str, context_required?: regex-str}}
    Example: {{"template": "pattern_must_not_appear", "params": {{"regex": "time\\.time\\(\\)", "scope": "hooks/*.py"}}, ...}}
- co_modification_required: {{trigger_glob: str, must_modify_glob: str}}
    Example: {{"template": "co_modification_required", "params": {{"trigger_glob": "hooks/lib_*.py", "must_modify_glob": ".dynos/task-*/spec.md"}}, ...}}
- signature_lock: {{module: str, function: str, expected_params: list[str]}}
    Example: {{"template": "signature_lock", "params": {{"module": "hooks.lib_receipts", "function": "receipt_post_completion", "expected_params": ["task_dir", "handlers_run"]}}, ...}}
- caller_count_required: {{symbol: str, scope: glob-str, min_count: int}}
    Example: {{"template": "caller_count_required", "params": {{"symbol": "receipt_calibration_applied", "scope": "hooks/*.py", "min_count": 1}}, ...}}
- import_constant_only: {{literal_pattern: regex-str, allowed_files: list[glob-str]}}
    Example: {{"template": "import_constant_only", "params": {{"literal_pattern": "receipts/_injected-", "allowed_files": ["hooks/lib_receipts.py", "hooks/router.py"]}}, ...}}
- advisory: {{}} (free-text rule; used when failure pattern is judgment-based)

When the rule cannot be structurally expressed (e.g. "apply scrutiny for SEC-class issues"), you MUST emit `template: "advisory"` explicitly. NEVER omit the template field.

Rules:
- Every claim must be grounded in the provided task data.
- Do not invent files, tests, incidents, agents, or causes not present in the input.
- Distinguish root cause from immediate cause.
- Distinguish detection failure from root cause.
- Do not use "human error", "oversight", "missed it", or "be more careful" as causes.
- Do not say "needs more testing" unless you name the missing behavior that needed coverage.
- Only propose prevention rules for failures that actually occurred, not hypotheticals.
- If a finding category appeared 2+ times, treat it as a pattern, not an isolated miss.
- If repairs failed or were retried, explain why the first repair was shallow or wrong.
- Prefer system-level guardrails over reminders or vague best practices.
- Keep prevention rules under 100 characters each.
- If model suggestions are weakly supported, return an empty array.
- If there is not enough evidence, say so inside the evidence field instead of guessing.
- Good output names the broken mechanism. Bad output just restates the finding.

Assume this team will repeat the same failure unless the rule is strong enough to stop them."""

    return {"prompt": prompt, "has_findings": True, "task_id": task_id}


def apply_analysis(task_dir: Path, analysis: dict) -> dict:
    """Apply LLM analysis output to project state.

    Reads the LLM's JSON output and writes prevention rules.
    Returns summary of what was applied.

    Emits one of two receipts on the task dir:
      - `postmortem-analysis` when rules are merged (or analysis sanitized)
      - `postmortem-skipped` when there is no analysis input to apply
    """
    task_dir = Path(task_dir).resolve()
    retro_path = task_dir / "task-retrospective.json"
    retro = _read_artifact(retro_path) or {}
    task_id = retro.get("task_id", "unknown")

    # Determine project root from task dir
    root = task_dir.parent.parent
    persistent = _persistent_project_dir(root)

    # SKIP branch: no analysis at all to consume. We still emit a receipt
    # because the orchestrator must be able to prove this stage ran.
    if not isinstance(analysis, dict) or not analysis:
        if retro_path.exists() and task_dir.is_dir():
            try:
                retro_sha = hash_file(retro_path)
                # G2: subsumed_by=[] is valid for no-findings reason.
                receipt_postmortem_skipped(
                    task_dir, "no-findings", retro_sha, subsumed_by=[]
                )
            except (FileNotFoundError, OSError, ValueError, TypeError) as exc:
                log_event(root, "postmortem_analysis_skip_receipt_failed", task=task_id, error=str(exc))
        return {"task_id": task_id, "rules_added": 0, "analysis_written": False, "skipped": True, "skip_reason": "no-findings"}

    # Sanitize model output before persisting or promoting rules.
    sanitized = _normalize_analysis(analysis)
    sanitized["analyzed_at"] = now_iso()
    sanitized["task_id"] = task_id

    # Parity accounting — how many rule items did the LLM actually emit,
    # regardless of which top-level key it used. task-20260419-002 landed
    # with rules_added=0 because the LLM used `derived_rules` while the
    # ingester read `prevention_rules`, and nothing reported the silent
    # drop. These counters close that hole: every emit → either promoted,
    # rejected-by-schema, normalization-dropped, or duplicate-dropped.
    _input_rule_count = int(sanitized.pop("_input_rule_count", 0))
    _normalization_drops = sanitized.pop("_normalization_drops", [])

    analysis_path = task_dir / "postmortem-analysis.json"
    write_json(analysis_path, sanitized)

    # Emit one event per normalization drop so the signal is not silent.
    for drop in _normalization_drops:
        log_event(
            root,
            "postmortem_rule_dropped",
            task=task_id,
            task_id=task_id,
            stage="normalize",
            reason=drop.get("reason", "unknown"),
        )

    # Extract and merge prevention rules
    new_rules = sanitized.get("prevention_rules", [])

    # AC 11: schema-validate every LLM-authored rule against
    # TEMPLATE_SCHEMAS. Rejected rules are NOT merged, are logged to
    # stderr with the failing check, and tallied into the return dict
    # so downstream callers (and tests) can assert on rejection counts.
    #
    # Why this lives BEFORE the lock: validation is pure / side-effect
    # free aside from stderr; we want rejections visible regardless of
    # whether the merge ultimately runs. The merge below operates only
    # on the validated `new_rules` slice.
    rejected_rules: list[dict] = []
    accepted_rules: list[dict] = []
    for rule in new_rules:
        ok, reason = _validate_rule_schema(rule)
        if ok:
            accepted_rules.append(rule)
        else:
            template = rule.get("template") if isinstance(rule, dict) else None
            print(
                f"[postmortem_analysis] REJECT rule template={template} "
                f"reason={reason}",
                file=_sys.stderr,
            )
            rejected_rules.append({
                "rule": rule,
                "reason": reason,
            })
            # Parity: every schema rejection emits an event so no drop is
            # silent. The event carries the template + reason so a CI
            # reviewer can trace which normalized rule failed which rule.
            log_event(
                root,
                "postmortem_rule_dropped",
                task=task_id,
                task_id=task_id,
                stage="schema",
                template=str(template) if template is not None else "none",
                reason=reason,
            )
    new_rules = accepted_rules
    rejected_count = len(rejected_rules)

    rules_path = persistent / "prevention-rules.json"
    if not new_rules:
        # Analysis was sanitized to zero usable rules — still emit the
        # analysis receipt so the chain is provable. Loudly flag the
        # derived-vs-added drop so the signal doesn't silently vanish.
        # task-20260419-002 hit this path with _input_rule_count=8 and
        # rules_added=0 (wrong top-level key); now it's at least an
        # event a reviewer can grep for.
        if _input_rule_count > 0:
            log_event(
                root,
                "postmortem_rule_promotion_dropped",
                task=task_id,
                task_id=task_id,
                input_rule_count=_input_rule_count,
                rules_added=0,
                rejected_count=rejected_count,
                normalization_drops=len(_normalization_drops),
                reason=(
                    "input had rules but zero were promoted — see "
                    "postmortem_rule_dropped events for per-rule reason"
                ),
            )
        try:
            if task_dir.is_dir():
                receipt_postmortem_analysis(
                    task_dir,
                    analysis_path=analysis_path,
                    rules_path=rules_path,
                    rules_added=0,
                )
        except (FileNotFoundError, OSError, ValueError) as exc:
            log_event(root, "postmortem_analysis_receipt_failed", task=task_id, error=str(exc))
        return {
            "task_id": task_id,
            "rules_added": 0,
            "analysis_written": True,
            "input_rule_count": _input_rule_count,
            "rejected_count": rejected_count,
            "normalization_drops": len(_normalization_drops),
        }
    # Lost-update protection: two processes (e.g., two worktrees, or one
    # task completing while another is mid-analysis) can both call
    # apply_analysis concurrently. write_json is atomic (tempfile + rename)
    # so neither reader observes a torn file, but without this lock the
    # read-modify-write window can drop rules — reader A and reader B both
    # see the same pre-merge state, each appends its rules, and the later
    # writer overwrites the earlier one with only its own additions.
    #
    # fcntl.LOCK_EX on the rules file (or a sibling .lock if the file
    # doesn't exist yet) serializes the RMW. Unix-only; dynos is
    # Unix-only throughout.
    persistent.mkdir(parents=True, exist_ok=True)
    lock_path = rules_path.with_suffix(rules_path.suffix + ".lock")
    added = 0
    # MA-007: emit one event per successful rule promotion. The drop-
    # side already emits `postmortem_rule_dropped` (stage=normalize
    # and stage=schema); parity demands the add-side be equally visible.
    # A reviewer grepping events.jsonl for `postmortem_rule_promoted`
    # sees exactly which rules landed, from which task, under what
    # category — the same chain-of-custody PR #131 established for
    # the drop path but cut off before the promote path.
    promoted_records: list[dict] = []
    lock_fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "w") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            existing: dict = {}
            try:
                existing = load_json(rules_path)
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                existing = {}

            current_rules = existing.get("rules", [])
            existing_rule_texts = {r.get("rule", "").lower() for r in current_rules}

            for rule in new_rules:
                if not isinstance(rule, dict):
                    continue
                text = rule.get("rule", "").strip()
                if not text or text.lower() in existing_rule_texts:
                    continue
                merged = {
                    "executor": rule.get("executor", "all"),
                    "category": rule.get("category", "unknown"),
                    "rule": text,
                    "source_task": task_id,
                    "source_finding": rule.get("source_finding", ""),
                    "rationale": rule.get("rationale", ""),
                    "enforcement": rule.get("enforcement", "prompt-constraint"),
                    "added_at": now_iso(),
                    # Persist the validated template + params so the
                    # router (AC 13) can route enforced rules out of
                    # the executor prompt and the rules engine (seg-1)
                    # can compile them into runtime guards.
                    "template": rule.get("template", "advisory"),
                    "params": rule.get("params", {}) if isinstance(rule.get("params"), dict) else {},
                }
                current_rules.append(merged)
                existing_rule_texts.add(text.lower())
                added += 1
                promoted_records.append({
                    "category": merged["category"],
                    "executor": merged["executor"],
                    "template": merged["template"],
                    "enforcement": merged["enforcement"],
                    "source_finding": merged["source_finding"],
                })

            if added:
                write_json(rules_path, {"rules": current_rules, "updated_at": now_iso()})
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)

    # MA-007: emit promotion events AFTER lock release so a failed log
    # write doesn't hold the rules-file lock. One event per promoted
    # rule — no batch aggregation — so a reviewer can trace each rule's
    # individual provenance without parsing a compound event.
    for record in promoted_records:
        log_event(
            root,
            "postmortem_rule_promoted",
            task=task_id,
            task_id=task_id,
            category=record["category"],
            executor=record["executor"],
            template=record["template"],
            enforcement=record["enforcement"],
            source_finding=record["source_finding"],
        )

    # Hash analysis + rules POST-merge so the receipt records the on-disk
    # state that downstream verifiers will see. If any hashing or receipt
    # write fails, log it but do not raise — the merge already succeeded
    # and re-running this branch would double-count rules.
    try:
        if task_dir.is_dir():
            receipt_postmortem_analysis(
                task_dir,
                analysis_path=analysis_path,
                rules_path=rules_path,
                rules_added=int(added),
            )
    except (FileNotFoundError, OSError, ValueError) as exc:
        log_event(root, "postmortem_analysis_receipt_failed", task=task_id, error=str(exc))

    return {
        "task_id": task_id,
        "rules_added": added,
        "analysis_written": True,
        "input_rule_count": _input_rule_count,
        "rejected_count": rejected_count,
        "normalization_drops": len(_normalization_drops),
    }


def cmd_build_prompt(args: argparse.Namespace) -> int:
    result = build_analysis_prompt(Path(args.task_dir))
    print(json.dumps(result, indent=2))
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    analysis = json.load(_sys.stdin)
    result = apply_analysis(Path(args.task_dir), analysis)
    print(json.dumps(result, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    bp = sub.add_parser("build-prompt", help="Build analysis prompt from task artifacts")
    bp.add_argument("task_dir")
    bp.set_defaults(func=cmd_build_prompt)

    ap = sub.add_parser("apply", help="Apply LLM analysis output (reads JSON from stdin)")
    ap.add_argument("task_dir")
    ap.set_defaults(func=cmd_apply)

    return parser


if __name__ == "__main__":
    from cli_base import cli_main
    raise SystemExit(cli_main(build_parser))
