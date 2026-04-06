#!/usr/bin/env python3
"""Autonomous autofix scanner for dynos-work.

Detects technical debt across six categories (syntax errors, recurring audit
findings, dependency vulnerabilities, dead code, architectural drift, and
LLM code review), then routes each finding through a risk-based pipeline:
low/medium actionable findings go through the autofix pipeline
(git worktree + Claude foundry pipeline + PR), while recurring patterns and
high/critical findings open GitHub issues for human review.

All logging goes to stderr. Only final JSON goes to stdout.
"""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import argparse
import ast
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dynoslib_core import (
    _persistent_project_dir,
    collect_retrospectives,
    load_json,
    now_iso,
    write_json,
)
from dynopatterns import local_patterns_path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCAN_TIMEOUT_SECONDS = 600
MAX_ATTEMPTS = 2
VALID_SEVERITIES = {"low", "medium", "high", "critical"}
VALID_CATEGORIES = {"recurring-audit", "dependency-vuln", "dead-code", "architectural-drift", "syntax-error", "llm-review"}
VALID_STATUSES = {
    "new", "fixed", "issue-opened", "failed",
    "skipped-dedup", "already-exists", "permanently_failed",
    "rate-limited", "suppressed-policy",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    """Log to stderr so stdout stays clean for JSON."""
    print(f"[proactive] {msg}", file=sys.stderr, flush=True)


def _findings_path(root: Path) -> Path:
    return root / ".dynos" / "proactive-findings.json"


def _autofix_policy_path(root: Path) -> Path:
    return _persistent_project_dir(root) / "autofix-policy.json"


def _autofix_metrics_path(root: Path) -> Path:
    return _persistent_project_dir(root) / "autofix-metrics.json"


def _autofix_benchmarks_path(root: Path) -> Path:
    return _persistent_project_dir(root) / "autofix-benchmarks.json"


def _default_category_policy(category: str) -> dict:
    mode = "autofix"
    base_confidence = 0.75
    if category == "syntax-error":
        base_confidence = 0.95
    elif category == "dead-code":
        base_confidence = 0.88
    elif category == "llm-review":
        base_confidence = 0.70
    elif category in {"dependency-vuln", "architectural-drift", "recurring-audit"}:
        mode = "issue-only"
        base_confidence = 0.35
    return {
        "enabled": True,
        "mode": mode,
        "min_confidence_autofix": 0.65,
        "confidence": base_confidence,
        "stats": {
            "proposed": 0,
            "merged": 0,
            "closed_unmerged": 0,
            "reverted": 0,
            "verification_failed": 0,
            "issues_opened": 0,
        },
    }


def _default_autofix_policy() -> dict:
    return {
        "max_prs_per_day": 100,
        "max_open_prs": 100,
        "cooldown_after_failures": 2,
        "allow_dependency_file_changes": False,
        "suppressions": [],
        "categories": {
            category: _default_category_policy(category)
            for category in sorted(VALID_CATEGORIES)
        },
    }


def _normalize_autofix_policy(data: dict | None) -> dict:
    default = _default_autofix_policy()
    if not isinstance(data, dict):
        return default
    merged = dict(default)
    if isinstance(data.get("max_prs_per_day"), int) and data["max_prs_per_day"] > 0:
        merged["max_prs_per_day"] = data["max_prs_per_day"]
    if isinstance(data.get("max_open_prs"), int) and data["max_open_prs"] >= 0:
        merged["max_open_prs"] = data["max_open_prs"]
    if isinstance(data.get("cooldown_after_failures"), int) and data["cooldown_after_failures"] >= 0:
        merged["cooldown_after_failures"] = data["cooldown_after_failures"]
    if isinstance(data.get("allow_dependency_file_changes"), bool):
        merged["allow_dependency_file_changes"] = data["allow_dependency_file_changes"]
    if isinstance(data.get("suppressions"), list):
        merged["suppressions"] = data["suppressions"]

    categories = dict(default["categories"])
    data_categories = data.get("categories", {})
    if isinstance(data_categories, dict):
        for category in VALID_CATEGORIES:
            base = dict(categories[category])
            incoming = data_categories.get(category, {})
            if isinstance(incoming, dict):
                if isinstance(incoming.get("enabled"), bool):
                    base["enabled"] = incoming["enabled"]
                if incoming.get("mode") in {"autofix", "issue-only", "disabled"}:
                    base["mode"] = incoming["mode"]
                if isinstance(incoming.get("min_confidence_autofix"), (int, float)):
                    base["min_confidence_autofix"] = float(incoming["min_confidence_autofix"])
                if isinstance(incoming.get("confidence"), (int, float)):
                    base["confidence"] = round(float(incoming["confidence"]), 3)
                stats = dict(base["stats"])
                incoming_stats = incoming.get("stats", {})
                if isinstance(incoming_stats, dict):
                    for key in stats:
                        if isinstance(incoming_stats.get(key), int) and incoming_stats[key] >= 0:
                            stats[key] = incoming_stats[key]
                base["stats"] = stats
            categories[category] = base
    merged["categories"] = categories
    return merged


def _load_autofix_policy(root: Path) -> dict:
    path = _autofix_policy_path(root)
    if not path.exists() or not path.read_text().strip():
        data = _default_autofix_policy()
        write_json(path, data)
        return data
    try:
        raw = load_json(path)
    except (json.JSONDecodeError, FileNotFoundError, OSError):
        raw = {}
    data = _normalize_autofix_policy(raw)
    if data != raw:
        write_json(path, data)
    return data


def _save_autofix_policy(root: Path, policy: dict) -> None:
    write_json(_autofix_policy_path(root), _normalize_autofix_policy(policy))


def _load_findings(root: Path) -> list[dict]:
    path = _findings_path(root)
    if not path.exists():
        return []
    try:
        data = load_json(path)
    except (json.JSONDecodeError, OSError) as exc:
        _log(f"Warning: could not load findings file: {exc}")
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "findings" in data:
        findings_list = data["findings"]
        if isinstance(findings_list, list):
            return findings_list
    return []


def _save_findings(root: Path, findings: list[dict]) -> None:
    write_json(_findings_path(root), findings)


def _cleanup_merged_branches(root: Path) -> None:
    """Delete local and remote autofix branches whose PRs are merged/closed."""
    if not shutil.which("gh") or not shutil.which("git"):
        return
    try:
        result = subprocess.run(
            ["git", "branch", "-r"],
            capture_output=True, text=True, timeout=15, cwd=str(root),
        )
        if result.returncode != 0:
            return
        remote_branches = [
            line.strip() for line in result.stdout.splitlines()
            if "dynos/auto-fix-" in line.strip()
        ]
    except (subprocess.TimeoutExpired, OSError):
        return

    for remote_ref in remote_branches:
        # remote_ref looks like "origin/dynos/auto-fix-xxx"
        branch_name = remote_ref.replace("origin/", "", 1)
        try:
            pr_result = subprocess.run(
                ["gh", "pr", "list", "--search", f"{branch_name} in:head",
                 "--state", "merged", "--json", "number"],
                capture_output=True, text=True, timeout=30, cwd=str(root),
            )
            if pr_result.returncode != 0:
                continue
            prs = json.loads(pr_result.stdout) if pr_result.stdout.strip() else []
            if not isinstance(prs, list) or len(prs) == 0:
                # Also check closed state
                closed_result = subprocess.run(
                    ["gh", "pr", "list", "--search", f"{branch_name} in:head",
                     "--state", "closed", "--json", "number"],
                    capture_output=True, text=True, timeout=30, cwd=str(root),
                )
                if closed_result.returncode != 0:
                    continue
                closed_prs = json.loads(closed_result.stdout) if closed_result.stdout.strip() else []
                if not isinstance(closed_prs, list) or len(closed_prs) == 0:
                    continue
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
            continue

        # PR is merged or closed, clean up branches
        _log(f"Cleaning up merged/closed branch: {branch_name}")
        try:
            subprocess.run(
                ["git", "branch", "-D", branch_name],
                capture_output=True, text=True, timeout=10, cwd=str(root),
            )
        except (subprocess.TimeoutExpired, OSError):
            pass
        try:
            subprocess.run(
                ["git", "push", "origin", "--delete", branch_name],
                capture_output=True, text=True, timeout=15, cwd=str(root),
            )
        except (subprocess.TimeoutExpired, OSError):
            pass


def _prune_findings(findings: list[dict], max_age_days: int = 30, max_entries: int = 500) -> list[dict]:
    """Remove stale findings. Never prune 'fixed' or 'issue-opened' entries."""
    now = datetime.now(timezone.utc)
    preserved_statuses = {"fixed", "issue-opened"}

    pruned: list[dict] = []
    for f in findings:
        status = f.get("status", "")
        if status in preserved_statuses:
            pruned.append(f)
            continue
        found_at = f.get("found_at", "")
        if found_at:
            try:
                dt = datetime.fromisoformat(found_at.replace("Z", "+00:00"))
                age_days = (now - dt).total_seconds() / 86400
                if age_days > max_age_days:
                    continue  # Skip old entry
            except (ValueError, TypeError):
                pass
        pruned.append(f)

    # If still over max_entries, keep preserved + newest non-preserved
    if len(pruned) > max_entries:
        preserved = [f for f in pruned if f.get("status", "") in preserved_statuses]
        non_preserved = [f for f in pruned if f.get("status", "") not in preserved_statuses]
        # Sort non-preserved by found_at descending (newest first)
        def _sort_key(f: dict) -> str:
            return f.get("found_at", "")
        non_preserved.sort(key=_sort_key, reverse=True)
        budget = max_entries - len(preserved)
        if budget < 0:
            budget = 0
        pruned = preserved + non_preserved[:budget]

    return pruned


def _description_hash(description: str) -> str:
    return hashlib.sha256(description.encode("utf-8")).hexdigest()[:16]


def _make_finding(
    finding_id: str,
    severity: str,
    category: str,
    description: str,
    evidence: dict,
) -> dict:
    return {
        "finding_id": finding_id,
        "severity": severity,
        "category": category,
        "description": description,
        "evidence": evidence,
        "status": "new",
        "found_at": now_iso(),
        "processed_at": None,
        "attempt_count": 0,
        "pr_number": None,
        "pr_url": None,
        "pr_state": None,
        "merge_outcome": None,
        "branch_name": None,
        "issue_number": None,
        "issue_url": None,
        "suppressed_until": None,
        "suppression_reason": None,
        "fail_reason": None,
        "fixability": None,
        "confidence_score": None,
        "rollout_mode": None,
        "verification": {},
        "pr_quality_score": None,
    }


# ---------------------------------------------------------------------------
# Detection: Recurring audit findings (AC 2)
# ---------------------------------------------------------------------------

def _detect_recurring_audit(root: Path) -> list[dict]:
    findings: list[dict] = []
    retros = collect_retrospectives(root)
    if not retros:
        return findings
    recent = retros[-10:]
    task_count = len(recent)
    if task_count == 0:
        return findings

    category_tasks: dict[str, list[str]] = {}
    for retro in recent:
        fbc = retro.get("findings_by_category", {})
        if not isinstance(fbc, dict):
            continue
        task_id = str(retro.get("task_id", "unknown"))
        for cat, count in fbc.items():
            if isinstance(cat, str) and isinstance(count, (int, float)) and count > 0:
                category_tasks.setdefault(cat, []).append(task_id)

    threshold = task_count * 0.5
    for cat, task_ids in category_tasks.items():
        if len(task_ids) > threshold:
            rate = round(len(task_ids) / task_count, 2)
            finding_id = f"recurring-audit-{cat}-{datetime.now(timezone.utc).strftime('%Y%m%d')}"
            findings.append(_make_finding(
                finding_id=finding_id,
                severity="medium",
                category="recurring-audit",
                description=f"Audit category '{cat}' appeared in {len(task_ids)} of last {task_count} tasks ({rate:.0%} rate)",
                evidence={
                    "category": cat,
                    "occurrence_rate": rate,
                    "task_ids": task_ids,
                },
            ))
    return findings


# ---------------------------------------------------------------------------
# Detection: Dependency vulnerabilities (AC 3)
# ---------------------------------------------------------------------------

def _detect_dependency_vulns(root: Path) -> list[dict]:
    findings: list[dict] = []

    # pip-audit
    if shutil.which("pip-audit"):
        try:
            result = subprocess.run(
                ["pip-audit", "--format=json"],
                capture_output=True, text=True, timeout=120, cwd=str(root),
            )
            if result.stdout.strip():
                try:
                    data = json.loads(result.stdout)
                except json.JSONDecodeError:
                    data = {}
                vulns = data.get("dependencies", []) if isinstance(data, dict) else data if isinstance(data, list) else []
                for vuln in vulns:
                    if not isinstance(vuln, dict):
                        continue
                    vuln_list = vuln.get("vulns", [])
                    if not vuln_list:
                        continue
                    pkg_name = str(vuln.get("name", "unknown"))
                    pkg_version = str(vuln.get("version", "unknown"))
                    for v in vuln_list:
                        if not isinstance(v, dict):
                            continue
                        vuln_id = str(v.get("id", "unknown"))
                        # Map pip-audit aliases field to severity
                        vuln_aliases = v.get("aliases", [])
                        vuln_desc = str(v.get("description", "No description"))
                        # Infer severity from description keywords
                        desc_lower = vuln_desc.lower()
                        if any(w in desc_lower for w in ("critical", "remote code", "rce", "arbitrary code")):
                            severity = "critical"
                        elif any(w in desc_lower for w in ("high", "injection", "overflow", "bypass")):
                            severity = "high"
                        elif any(w in desc_lower for w in ("medium", "moderate", "denial")):
                            severity = "medium"
                        else:
                            severity = "low"
                        finding_id = f"dep-vuln-{pkg_name}-{vuln_id}"
                        findings.append(_make_finding(
                            finding_id=finding_id,
                            severity=severity,
                            category="dependency-vuln",
                            description=f"Vulnerability {vuln_id} in {pkg_name}=={pkg_version}: {vuln_desc[:200]}",
                            evidence={
                                "package": pkg_name,
                                "version": pkg_version,
                                "vuln_id": vuln_id,
                                "source": "pip-audit",
                            },
                        ))
        except (subprocess.TimeoutExpired, OSError) as exc:
            _log(f"pip-audit failed: {exc}")

    # npm audit
    lockfile_exists = (root / "package-lock.json").exists() or (root / "yarn.lock").exists()
    if lockfile_exists and shutil.which("npm"):
        try:
            result = subprocess.run(
                ["npm", "audit", "--json"],
                capture_output=True, text=True, timeout=120, cwd=str(root),
            )
            if result.stdout.strip():
                try:
                    data = json.loads(result.stdout)
                except json.JSONDecodeError:
                    data = {}
                if isinstance(data, dict):
                    advisories = data.get("advisories", {})
                    if isinstance(advisories, dict):
                        for adv_id, adv in advisories.items():
                            if not isinstance(adv, dict):
                                continue
                            raw_sev = str(adv.get("severity", "moderate"))
                            sev_map = {"critical": "critical", "high": "high", "moderate": "medium", "low": "low"}
                            severity = sev_map.get(raw_sev, "medium")
                            module_name = str(adv.get("module_name", "unknown"))
                            title = str(adv.get("title", "No title"))
                            finding_id = f"dep-vuln-npm-{module_name}-{adv_id}"
                            findings.append(_make_finding(
                                finding_id=finding_id,
                                severity=severity,
                                category="dependency-vuln",
                                description=f"npm vulnerability in {module_name}: {title[:200]}",
                                evidence={
                                    "package": module_name,
                                    "advisory_id": str(adv_id),
                                    "source": "npm-audit",
                                },
                            ))
                    # npm audit v2 format (vulnerabilities key)
                    vulns_v2 = data.get("vulnerabilities", {})
                    if isinstance(vulns_v2, dict) and not advisories:
                        for pkg_name, vuln_data in vulns_v2.items():
                            if not isinstance(vuln_data, dict):
                                continue
                            raw_sev = str(vuln_data.get("severity", "moderate"))
                            sev_map = {"critical": "critical", "high": "high", "moderate": "medium", "low": "low"}
                            severity = sev_map.get(raw_sev, "medium")
                            finding_id = f"dep-vuln-npm-{pkg_name}-v2"
                            findings.append(_make_finding(
                                finding_id=finding_id,
                                severity=severity,
                                category="dependency-vuln",
                                description=f"npm vulnerability in {pkg_name} (severity: {raw_sev})",
                                evidence={
                                    "package": str(pkg_name),
                                    "severity_raw": raw_sev,
                                    "source": "npm-audit-v2",
                                },
                            ))
        except (subprocess.TimeoutExpired, OSError) as exc:
            _log(f"npm audit failed: {exc}")

    return findings


# ---------------------------------------------------------------------------
# Detection: Dead code (AC 4)
# ---------------------------------------------------------------------------

def _detect_syntax_errors(root: Path) -> list[dict]:
    """Check all Python files for syntax errors."""
    findings: list[dict] = []
    hooks_dir = root / "hooks"
    if not hooks_dir.is_dir():
        return findings

    for py_file in sorted(hooks_dir.glob("*.py")):
        try:
            source = py_file.read_text()
            ast.parse(source, filename=py_file.name)
        except SyntaxError as exc:
            fid = f"syntax-error-{py_file.name}-{exc.lineno or 0}"
            findings.append(_make_finding(
                finding_id=fid,
                severity="medium",
                category="syntax-error",
                description=f"Syntax error in {py_file.name} line {exc.lineno}: {exc.msg}",
                evidence={
                    "file": f"hooks/{py_file.name}",
                    "line": exc.lineno,
                    "message": exc.msg,
                    "text": (exc.text or "").strip(),
                },
            ))
        except (OSError, UnicodeDecodeError):
            continue

    return findings


def _detect_dead_code(root: Path) -> list[dict]:
    findings: list[dict] = []
    hooks_dir = root / "hooks"
    if not hooks_dir.is_dir():
        return findings

    py_files = sorted(hooks_dir.glob("*.py"))
    # Collect all top-level function definitions across all files
    all_defined_funcs: dict[str, list[str]] = {}  # func_name -> [defining files]
    all_source_texts: dict[str, str] = {}

    for py_file in py_files:
        try:
            source = py_file.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        all_source_texts[py_file.name] = source
        try:
            tree = ast.parse(source, filename=py_file.name)
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.FunctionDef):
                all_defined_funcs.setdefault(node.name, []).append(py_file.name)

    # Check for unused imports per file
    for py_file in py_files:
        try:
            source = py_file.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        try:
            tree = ast.parse(source, filename=py_file.name)
        except (SyntaxError, UnicodeDecodeError):
            continue

        # Gather imports
        imported_names: dict[str, int] = {}  # name -> lineno
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.asname if alias.asname else alias.name
                    imported_names[name] = node.lineno
            elif isinstance(node, ast.ImportFrom):
                # Skip __future__ imports (they are compiler directives, not runtime names)
                if node.module == "__future__":
                    continue
                for alias in node.names:
                    name = alias.asname if alias.asname else alias.name
                    imported_names[name] = node.lineno

        # Gather all Name references in the file (excluding import nodes themselves)
        used_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                used_names.add(node.id)
            elif isinstance(node, ast.Attribute):
                # Handle module.attr references
                if isinstance(node.value, ast.Name):
                    used_names.add(node.value.id)

        # Check for __all__ which re-exports names
        has_all = False
        all_names: set[str] = set()
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "__all__":
                        has_all = True
                        if isinstance(node.value, (ast.List, ast.Tuple)):
                            for elt in node.value.elts:
                                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                    all_names.add(elt.value)

        unused_imports: list[str] = []
        for name, lineno in imported_names.items():
            if name.startswith("_") and name != "__all__":
                continue  # Skip private imports (likely re-exports)
            if name in used_names:
                continue
            if has_all and name in all_names:
                continue
            # Skip if this is a facade re-export file (dynoslib.py pattern)
            if py_file.name == "dynoslib.py":
                continue
            # Check if any other file in the project imports this name from
            # this module (re-export pattern). The module name is the file
            # stem (e.g. "utils" for "utils.py").
            module_name = py_file.stem
            is_reexport = False
            for other_file in py_files:
                if other_file == py_file:
                    continue
                other_src = all_source_texts.get(other_file.name, "")
                if not other_src:
                    continue
                # Check for "from {module_name} import ... {name} ..."
                # or "import {module_name}"
                if re.search(
                    rf"from\s+{re.escape(module_name)}\s+import\s+.*\b{re.escape(name)}\b",
                    other_src,
                ):
                    is_reexport = True
                    break
                if re.search(
                    rf"^import\s+{re.escape(module_name)}\b",
                    other_src,
                    re.MULTILINE,
                ):
                    is_reexport = True
                    break
            if is_reexport:
                continue
            unused_imports.append(name)

        if unused_imports:
            finding_id = f"dead-code-unused-import-{py_file.name}-{_description_hash(','.join(sorted(unused_imports)))}"
            findings.append(_make_finding(
                finding_id=finding_id,
                severity="low",
                category="dead-code",
                description=f"Unused imports in {py_file.name}: {', '.join(sorted(unused_imports)[:5])}",
                evidence={
                    "file": str(py_file.relative_to(root)),
                    "unused_imports": sorted(unused_imports)[:10],
                },
            ))

    # Check for unreferenced top-level functions
    all_combined_source = "\n".join(all_source_texts.values())
    for func_name, defining_files in all_defined_funcs.items():
        if func_name.startswith("_"):
            continue  # Skip private functions (may be called dynamically)
        if func_name.startswith("cmd_"):
            continue  # Skip CLI handlers (called via argparse set_defaults)
        if func_name in ("build_parser", "cli_main", "main", "setup", "teardown"):
            continue  # Skip well-known entry points
        if func_name.startswith("test_"):
            continue  # Skip test functions

        # Count occurrences in all source text. If name appears only in def lines, likely unused.
        # Conservative: if the name appears anywhere outside a def statement, consider it referenced.
        occurrence_count = all_combined_source.count(func_name)
        definition_count = len(defining_files)
        if occurrence_count <= definition_count:
            finding_id = f"dead-code-unreferenced-{func_name}-{_description_hash(func_name)}"
            findings.append(_make_finding(
                finding_id=finding_id,
                severity="low",
                category="dead-code",
                description=f"Potentially unreferenced function '{func_name}' in {', '.join(defining_files)}",
                evidence={
                    "function": func_name,
                    "defined_in": defining_files,
                    "occurrence_count": occurrence_count,
                },
            ))

    return findings


# ---------------------------------------------------------------------------
# Detection: Architectural drift (AC 5)
# ---------------------------------------------------------------------------

def _detect_architectural_drift(root: Path) -> list[dict]:
    findings: list[dict] = []
    patterns_path = local_patterns_path(root)
    if not patterns_path.exists():
        _log(f"No patterns file at {patterns_path}, skipping drift detection")
        return findings

    try:
        content = patterns_path.read_text()
    except OSError as exc:
        _log(f"Could not read patterns file: {exc}")
        return findings

    # Extract prevention rules from markdown table
    prevention_rules: list[dict] = []
    in_prevention = False
    for line in content.splitlines():
        if "## Prevention Rules" in line:
            in_prevention = True
            continue
        if in_prevention and line.startswith("##"):
            break
        if in_prevention and line.startswith("|") and "---" not in line and "Executor" not in line:
            parts = [p.strip() for p in line.split("|") if p.strip()]
            if len(parts) >= 3:
                prevention_rules.append({
                    "executor": parts[0],
                    "rule": parts[1],
                    "source": parts[2],
                })

    # Extract gold standard task IDs
    gold_standards: list[str] = []
    in_gold = False
    for line in content.splitlines():
        if "## Gold Standard" in line:
            in_gold = True
            continue
        if in_gold and line.startswith("##"):
            break
        if in_gold and line.startswith("|") and "---" not in line and "Task ID" not in line:
            parts = [p.strip() for p in line.split("|") if p.strip()]
            if parts and parts[0] != "none":
                gold_standards.append(parts[0])

    # Check: are prevention rules being followed?
    # Look at recent retrospectives for violations
    retros = collect_retrospectives(root)
    recent = retros[-5:] if retros else []
    for retro in recent:
        fbc = retro.get("findings_by_category", {})
        repair_count = int(retro.get("repair_cycle_count", 0) or 0)
        task_id = str(retro.get("task_id", "unknown"))
        if repair_count > 2 and isinstance(fbc, dict) and sum(
            int(v) for v in fbc.values() if isinstance(v, (int, float))
        ) > 3:
            finding_id = f"arch-drift-high-repair-{task_id}"
            findings.append(_make_finding(
                finding_id=finding_id,
                severity="medium",
                category="architectural-drift",
                description=f"Task {task_id} required {repair_count} repair cycles with multiple finding categories, suggesting architectural drift from gold standards",
                evidence={
                    "task_id": task_id,
                    "repair_cycles": repair_count,
                    "findings_by_category": fbc,
                    "prevention_rules_count": len(prevention_rules),
                    "gold_standard_count": len(gold_standards),
                },
            ))

    return findings


# ---------------------------------------------------------------------------
# Category 5: LLM review pass (Haiku)
# ---------------------------------------------------------------------------

_HAIKU_REVIEW_PROMPT = """You are a thorough code auditor. Analyze the following source files for issues.

Follow the evidence — read the actual code, trace actual values, follow actual execution paths.

Look for:
1. **Logic bugs** — incorrect conditions, off-by-one errors, wrong variable used, missing edge cases
2. **Security issues** — injection risks, unvalidated input at system boundaries, secrets in code, unsafe deserialization
3. **Error handling gaps** — swallowed exceptions, missing try/except around I/O, bare except clauses that hide bugs
4. **Race conditions** — shared mutable state without locks, TOCTOU patterns, async gaps
5. **Data integrity** — writes without validation, missing null checks at boundaries, type confusion
6. **Anti-patterns** — God functions (>100 lines doing too much), circular dependencies, hidden side effects

Do NOT flag:
- Style preferences (naming conventions, import order)
- Missing type hints or docstrings
- Dead code or unused imports (handled by another scanner)
- Anything that is clearly intentional and correct

For each issue found, return ONLY a JSON array. Each item must have:
- "description": one sentence describing the issue
- "file": the filename
- "line": approximate line number
- "severity": "low", "medium", "high", or "critical"
- "category_detail": which of the 6 categories above

If no issues are found, return an empty array: []

Return ONLY the JSON array, no other text.

FILES TO REVIEW:
"""


def _scan_coverage_path(root: Path) -> Path:
    return root / ".dynos" / "scan-coverage.json"


def _load_scan_coverage(root: Path) -> dict:
    path = _scan_coverage_path(root)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"files": {}}


def _save_scan_coverage(root: Path, coverage: dict) -> None:
    path = _scan_coverage_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, coverage)


def _compute_file_scores(root: Path, coverage: dict) -> list[tuple[Path, float]]:
    """Score all Python files by risk priority. Higher = scan first."""

    # Find all tracked source files via git (respects .gitignore, skips hidden)
    _SOURCE_EXTENSIONS = {
        ".py", ".js", ".ts", ".tsx", ".jsx",
        ".go", ".rs", ".rb", ".java", ".kt",
        ".c", ".cpp", ".h", ".hpp", ".cs",
        ".swift", ".dart", ".lua", ".php",
        ".sh", ".bash", ".zsh",
    }

    unique_files: list[Path] = []
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            capture_output=True, text=True, timeout=15, cwd=root,
        )
        if result.returncode == 0:
            seen: set[str] = set()
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                p = root / line
                if p.suffix not in _SOURCE_EXTENSIONS:
                    continue
                # Skip hidden dirs (dotfiles)
                if any(part.startswith(".") for part in Path(line).parts):
                    continue
                if line not in seen and p.is_file():
                    seen.add(line)
                    unique_files.append(p)
    except (subprocess.TimeoutExpired, OSError):
        pass

    if not unique_files:
        return []

    now = datetime.now(timezone.utc)
    file_coverage = coverage.get("files", {})

    # 1. Git churn: commits per file in last 30 days
    churn: dict[str, int] = {}
    try:
        result = subprocess.run(
            ["git", "log", "--since=30 days ago", "--name-only", "--pretty=format:"],
            capture_output=True, text=True, timeout=15, cwd=root,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                line = line.strip()
                if line:
                    churn[line] = churn.get(line, 0) + 1
    except (subprocess.TimeoutExpired, OSError):
        pass

    # 2. Previous findings from proactive-findings.json
    prev_findings: dict[str, int] = {}
    existing = _load_findings(root)
    for f in existing:
        efile = f.get("evidence", {}).get("file", "")
        if efile:
            prev_findings[efile] = prev_findings.get(efile, 0) + 1

    # 3. Test coverage: check if a test file exists for each source file
    # Collect test file names (any language, common patterns)
    test_files: set[str] = set()
    for test_dir in ("tests", "test", "__tests__", "spec"):
        for tf in (root / test_dir).rglob("*") if (root / test_dir).is_dir() else []:
            if tf.is_file():
                stem = tf.stem
                # Strip common test prefixes/suffixes: test_foo, foo_test, foo.test, foo.spec
                for prefix in ("test_", "test-"):
                    if stem.startswith(prefix):
                        stem = stem[len(prefix):]
                for suffix in ("_test", "-test", ".test", "_spec", "-spec", ".spec"):
                    if stem.endswith(suffix):
                        stem = stem[:-len(suffix)]
                test_files.add(stem)

    scored: list[tuple[Path, float]] = []
    for f in unique_files:
        rel = str(f.relative_to(root))
        score = 0.0

        # Churn score (0-10, normalized)
        file_churn = churn.get(rel, 0)
        score += min(file_churn, 10) * 3  # max 30

        # Complexity score (line count, rough proxy)
        try:
            line_count = len(f.read_text().splitlines())
        except OSError:
            line_count = 0
        score += min(line_count / 50, 10) * 2  # max 20 (1000+ lines = max)

        # No test file
        stem = f.stem
        if f"test_{stem}" not in test_files:
            score += 5

        # Previous findings boost
        if rel in prev_findings:
            score += prev_findings[rel] * 3

        # Cooldown: recently scanned files get deprioritized
        file_info = file_coverage.get(rel, {})
        last_scanned = file_info.get("last_scanned_at", "")
        if last_scanned:
            try:
                scanned_dt = datetime.fromisoformat(last_scanned.replace("Z", "+00:00"))
                days_since = (now - scanned_dt).total_seconds() / 86400
                if days_since < 1:
                    score -= 100  # scanned today, skip
                elif days_since < 3:
                    score -= 30  # scanned recently, deprioritize
                elif days_since < 7:
                    score -= 10  # mild deprioritization
            except (ValueError, TypeError):
                pass

        # Last scan was clean? Minor deprioritization
        if file_info.get("last_result") == "clean":
            score -= 5

        scored.append((f, score))

    # Sort by score descending
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


def _detect_llm_review(root: Path) -> list[dict]:
    """Run a Haiku LLM pass over risk-prioritized files."""
    findings: list[dict] = []

    if not shutil.which("claude"):
        _log("Skipping LLM review: claude CLI not available")
        return findings

    # Load scan coverage and compute scores
    coverage = _load_scan_coverage(root)
    scored_files = _compute_file_scores(root, coverage)

    if not scored_files:
        return findings

    # Take top 5 files by risk score (skip files with score < 0)
    review_files: list[Path] = []
    for f, score in scored_files:
        if score < 0:
            continue
        review_files.append(f)
        if len(review_files) >= 5:
            break

    if not review_files:
        _log("All files recently scanned, skipping LLM review this cycle")
        return findings

    _log(f"File scores (top 10): {[(str(f.relative_to(root)), round(s, 1)) for f, s in scored_files[:10]]}")

    # Build the prompt with file contents
    prompt = _HAIKU_REVIEW_PROMPT

    # Append project-specific prevention rules from dynos_patterns.md
    try:
        patterns_path = _persistent_project_dir(root) / "dynos_patterns.md"
        if patterns_path.exists():
            patterns_content = patterns_path.read_text()
            prevention_text = ""
            in_prevention = False
            for pline in patterns_content.splitlines():
                if "## Prevention Rules" in pline:
                    in_prevention = True
                    continue
                if in_prevention and pline.startswith("##"):
                    break
                if in_prevention:
                    prevention_text += pline + "\n"
            prevention_text = prevention_text.strip()
            if prevention_text:
                prompt += (
                    f"\n## Project-specific patterns to watch for:\n"
                    f"{prevention_text}\n"
                )
    except OSError:
        pass

    # Append already-known finding descriptions so Haiku doesn't re-report
    known_findings = _load_findings(root)
    if known_findings:
        known_descs = [f.get("description", "") for f in known_findings if f.get("description")]
        if known_descs:
            known_list = "\n".join(f"- {d}" for d in known_descs[:50])
            prompt += (
                f"\n## Already known issues (do NOT re-report these):\n"
                f"{known_list}\n"
            )

    for f in review_files:
        try:
            content = f.read_text()
            rel = str(f.relative_to(root))
            # Truncate very large files to first 200 lines
            lines = content.splitlines()
            if len(lines) > 200:
                content = "\n".join(lines[:200]) + f"\n... (truncated, {len(lines)} total lines)"
            prompt += f"\n--- {rel} ---\n{content}\n"
        except OSError:
            continue

    _log(f"Running Haiku LLM review on {len(review_files)} files: {[str(f.relative_to(root)) for f in review_files]}")

    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--model", "haiku"],
            capture_output=True, text=True, timeout=600, cwd=root,
        )
    except subprocess.TimeoutExpired:
        _log("Haiku review timed out after 600s")
        return findings
    except OSError as exc:
        _log(f"Haiku review failed: {exc}")
        return findings

    if result.returncode != 0:
        _log(f"Haiku review exited {result.returncode}")
        return findings

    # Parse the JSON response
    output = result.stdout.strip()
    # Claude may wrap response in markdown code block
    if output.startswith("```"):
        lines = output.splitlines()
        output = "\n".join(l for l in lines if not l.startswith("```"))
        output = output.strip()

    try:
        issues = json.loads(output)
    except json.JSONDecodeError:
        # Try to find JSON array in the output
        start = output.find("[")
        end = output.rfind("]")
        if start >= 0 and end > start:
            try:
                issues = json.loads(output[start:end + 1])
            except json.JSONDecodeError:
                _log(f"Could not parse Haiku response as JSON")
                return findings
        else:
            _log(f"No JSON array found in Haiku response")
            return findings

    if not isinstance(issues, list):
        return findings

    for issue in issues:
        if not isinstance(issue, dict):
            continue
        desc = str(issue.get("description", ""))
        file_name = str(issue.get("file", ""))
        line_num = issue.get("line", 0)
        severity = str(issue.get("severity", "low"))
        cat_detail = str(issue.get("category_detail", ""))

        if not desc or not file_name:
            continue

        # Validate severity
        if severity not in ("low", "medium", "high", "critical"):
            severity = "medium"

        fid_raw = f"llm-review-{file_name}-{line_num}-{desc[:50]}"
        fid = f"llm-review-{hashlib.sha256(fid_raw.encode()).hexdigest()[:16]}"

        findings.append(_make_finding(
            finding_id=fid,
            severity=severity,
            category="llm-review",
            description=f"[{cat_detail}] {desc}",
            evidence={
                "file": file_name,
                "line": line_num,
                "category_detail": cat_detail,
                "reviewer": "haiku",
            },
        ))

    _log(f"Haiku review found {len(findings)} issues")

    # Update scan coverage
    file_coverage = coverage.get("files", {})
    files_with_findings = {f.get("evidence", {}).get("file", "") for f in findings}
    for f in review_files:
        rel = str(f.relative_to(root))
        file_coverage[rel] = {
            "last_scanned_at": now_iso(),
            "last_result": "findings" if rel in files_with_findings else "clean",
        }
    coverage["files"] = file_coverage
    coverage["last_scan_at"] = now_iso()
    _save_scan_coverage(root, coverage)

    return findings


# ---------------------------------------------------------------------------
# Multi-level dedup (AC 8)
# ---------------------------------------------------------------------------

def _dedup_finding(finding: dict, existing: list[dict]) -> str | None:
    """Return skip reason if finding should be skipped, or None to process it."""
    fid = finding["finding_id"]
    desc_h = _description_hash(finding["description"])
    cat = finding["category"]
    evidence_file = finding.get("evidence", {}).get("file", "")

    for ex in existing:
        ex_id = ex.get("finding_id", "")
        ex_status = ex.get("status", "")

        if ex_id != fid:
            continue

        # Level 1a: permanently_failed -> never retry (checked first)
        if ex_status == "permanently_failed":
            return "permanently_failed"

        # Level 1b: fixed with merged PR -> permanently suppressed
        if ex_status == "fixed" and ex.get("pr_number"):
            return "fixed with merged PR, permanently suppressed"

        # Level 1c: generic exact finding_id match
        return f"exact finding_id match (status={ex_status})"

    # Level 2: category + file + description_hash semantic match
    for ex in existing:
        ex_status = ex.get("status", "")
        ex_cat = ex.get("category", "")
        ex_file = ex.get("evidence", {}).get("file", "")
        ex_desc_h = _description_hash(ex.get("description", ""))

        if ex_cat == cat and ex_file == evidence_file and ex_desc_h == desc_h:
            if ex_status in ("fixed", "issue-opened", "failed", "permanently_failed"):
                return f"semantic match (category={cat}, desc_hash={desc_h}, status={ex_status})"

    return None


def _suppression_reason(finding: dict, policy: dict) -> str | None:
    for entry in policy.get("suppressions", []):
        if not isinstance(entry, dict):
            continue
        until = str(entry.get("until", "") or "")
        if until:
            try:
                until_dt = datetime.fromisoformat(until.replace("Z", "+00:00"))
                if until_dt < datetime.now(timezone.utc):
                    continue
            except ValueError:
                pass
        finding_id = str(entry.get("finding_id", "") or "")
        if finding_id and finding_id == finding.get("finding_id"):
            return str(entry.get("reason", "suppressed by finding id"))
        category = str(entry.get("category", "") or "")
        if category and category != finding.get("category"):
            continue
        path_prefix = str(entry.get("path_prefix", "") or "")
        evidence_file = str(finding.get("evidence", {}).get("file", "") or "")
        if path_prefix and not evidence_file.startswith(path_prefix):
            continue
        if category or path_prefix:
            return str(entry.get("reason", "suppressed by policy"))
    return None


def _rate_limit_snapshot(policy: dict, findings: list[dict]) -> dict:
    now = datetime.now(timezone.utc)
    today = now.date()
    open_prs = 0
    prs_today = 0
    recent_failures = 0
    for finding in findings:
        if finding.get("pr_number") and finding.get("merge_outcome") in (None, "open"):
            open_prs += 1
        processed_at = str(finding.get("processed_at", "") or "")
        if processed_at:
            try:
                processed_dt = datetime.fromisoformat(processed_at.replace("Z", "+00:00"))
                if processed_dt.date() == today and finding.get("pr_number"):
                    prs_today += 1
                if (
                    processed_dt > now - timedelta(days=1)
                    and finding.get("status") in {"failed", "permanently_failed"}
                ):
                    recent_failures += 1
            except ValueError:
                pass
    return {
        "prs_today": prs_today,
        "open_prs": open_prs,
        "recent_failures": recent_failures,
        "max_prs_per_day": int(policy.get("max_prs_per_day", 3) or 3),
        "max_open_prs": int(policy.get("max_open_prs", 5) or 5),
        "cooldown_after_failures": int(policy.get("cooldown_after_failures", 2) or 2),
    }


def _rate_limit_reason(policy: dict, findings: list[dict]) -> str | None:
    snapshot = _rate_limit_snapshot(policy, findings)
    if snapshot["prs_today"] >= snapshot["max_prs_per_day"]:
        return f"max_prs_per_day reached ({snapshot['prs_today']}/{snapshot['max_prs_per_day']})"
    if snapshot["open_prs"] >= snapshot["max_open_prs"]:
        return f"max_open_prs reached ({snapshot['open_prs']}/{snapshot['max_open_prs']})"
    if snapshot["recent_failures"] >= snapshot["cooldown_after_failures"]:
        return (
            f"cooldown_after_failures reached "
            f"({snapshot['recent_failures']}/{snapshot['cooldown_after_failures']})"
        )
    return None


def _recompute_category_confidence(policy: dict) -> dict:
    categories = policy.get("categories", {})
    for category, config in categories.items():
        if not isinstance(config, dict):
            continue
        stats = config.get("stats", {})
        if not isinstance(stats, dict):
            continue
        merged = int(stats.get("merged", 0) or 0)
        closed_unmerged = int(stats.get("closed_unmerged", 0) or 0)
        reverted = int(stats.get("reverted", 0) or 0)
        verification_failed = int(stats.get("verification_failed", 0) or 0)
        prior_success = 2.0
        prior_failure = 1.0
        failures = closed_unmerged + reverted + verification_failed
        confidence = (merged + prior_success) / (merged + failures + prior_success + prior_failure)
        if category == "syntax-error":
            confidence = max(confidence, 0.9)
        config["confidence"] = round(confidence, 3)
    return policy


def _build_autofix_benchmarks(root: Path, findings: list[dict], policy: dict) -> dict:
    categories: dict[str, dict] = {}
    recent_prs: list[dict] = []
    for finding in findings:
        category = str(finding.get("category", "unknown"))
        bucket = categories.setdefault(category, {
            "findings": 0,
            "autofix_prs": 0,
            "merged": 0,
            "closed_unmerged": 0,
            "reverted": 0,
            "issues_opened": 0,
            "verification_failed": 0,
            "avg_pr_quality_score": 0.0,
            "_quality_scores": [],
        })
        bucket["findings"] += 1
        status = finding.get("status")
        outcome = finding.get("merge_outcome")
        if status == "issue-opened":
            bucket["issues_opened"] += 1
        if str(finding.get("fail_reason", "")).startswith("verification_failed"):
            bucket["verification_failed"] += 1
        if finding.get("pr_number"):
            bucket["autofix_prs"] += 1
            if outcome == "merged":
                bucket["merged"] += 1
            elif outcome == "closed_unmerged":
                bucket["closed_unmerged"] += 1
            elif outcome == "reverted":
                bucket["reverted"] += 1
            pr_quality = finding.get("pr_quality_score")
            if isinstance(pr_quality, (int, float)):
                bucket["_quality_scores"].append(float(pr_quality))
            recent_prs.append({
                "finding_id": finding.get("finding_id"),
                "category": category,
                "number": finding.get("pr_number"),
                "state": (finding.get("pr_state") or "UNKNOWN").upper(),
                "merge_outcome": outcome,
                "title": finding.get("description", ""),
                "created_at": finding.get("processed_at"),
                "url": finding.get("pr_url"),
                "branch": finding.get("branch_name"),
            })
    for bucket in categories.values():
        scores = bucket.pop("_quality_scores")
        bucket["avg_pr_quality_score"] = round(sum(scores) / len(scores), 3) if scores else 0.0
        pr_count = bucket["autofix_prs"]
        bucket["merge_rate"] = round(bucket["merged"] / pr_count, 3) if pr_count else 0.0
    recent_prs.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
    benchmarks = {
        "generated_at": now_iso(),
        "categories": categories,
        "recent_prs": recent_prs[:10],
        "policy": {
            "max_prs_per_day": policy.get("max_prs_per_day"),
            "max_open_prs": policy.get("max_open_prs"),
        },
    }
    write_json(_autofix_benchmarks_path(root), benchmarks)
    return benchmarks


def _write_autofix_metrics(root: Path, findings: list[dict], policy: dict) -> dict:
    snapshot = _rate_limit_snapshot(policy, findings)
    suppressions = policy.get("suppressions", [])
    categories = {}
    for category, config in policy.get("categories", {}).items():
        if not isinstance(config, dict):
            continue
        stats = config.get("stats", {})
        categories[category] = {
            "mode": config.get("mode", "issue-only"),
            "enabled": bool(config.get("enabled", True)),
            "confidence": config.get("confidence", 0.0),
            "merged": int(stats.get("merged", 0) or 0),
            "closed_unmerged": int(stats.get("closed_unmerged", 0) or 0),
            "reverted": int(stats.get("reverted", 0) or 0),
            "issues_opened": int(stats.get("issues_opened", 0) or 0),
            "verification_failed": int(stats.get("verification_failed", 0) or 0),
        }
    totals = {
        "findings": len(findings),
        "open_prs": snapshot["open_prs"],
        "prs_today": snapshot["prs_today"],
        "recent_failures": snapshot["recent_failures"],
        "suppression_count": len(suppressions),
        "merged": sum(v["merged"] for v in categories.values()),
        "closed_unmerged": sum(v["closed_unmerged"] for v in categories.values()),
        "reverted": sum(v["reverted"] for v in categories.values()),
        "issues_opened": sum(v["issues_opened"] for v in categories.values()),
    }
    metrics = {
        "generated_at": now_iso(),
        "totals": totals,
        "rate_limits": snapshot,
        "categories": categories,
        "recent_prs": _build_autofix_benchmarks(root, findings, policy).get("recent_prs", []),
    }
    write_json(_autofix_metrics_path(root), metrics)
    return metrics


# ---------------------------------------------------------------------------
# Pre-action checks (AC 16)
# ---------------------------------------------------------------------------

def _check_existing_pr(finding_id: str, root: Path) -> bool:
    """Return True if a PR already exists for this finding."""
    if not shutil.which("gh"):
        return False
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--search", f"dynos/auto-fix-{finding_id} in:head", "--json", "number"],
            capture_output=True, text=True, timeout=30, cwd=str(root),
        )
        if result.returncode == 0 and result.stdout.strip():
            prs = json.loads(result.stdout)
            if isinstance(prs, list) and len(prs) > 0:
                return True
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as exc:
        _log(f"Warning: could not check existing PRs: {exc}")
    return False


def _check_existing_issue(finding_id: str, root: Path) -> bool:
    """Return True if an issue already exists for this finding."""
    if not shutil.which("gh"):
        return False
    try:
        result = subprocess.run(
            ["gh", "issue", "list", "--search", f"{finding_id} label:dynos-autofix", "--json", "number"],
            capture_output=True, text=True, timeout=30, cwd=str(root),
        )
        if result.returncode == 0 and result.stdout.strip():
            issues = json.loads(result.stdout)
            if isinstance(issues, list) and len(issues) > 0:
                return True
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as exc:
        _log(f"Warning: could not check existing issues: {exc}")
    return False


# ---------------------------------------------------------------------------
# Fixability classification (AC 9a)
# ---------------------------------------------------------------------------

def _classify_fixability(finding: dict) -> str:
    """Classify a finding's fixability into deterministic, likely-safe, or review-only."""
    category = finding.get("category", "")
    severity = finding.get("severity", "low")

    if category == "syntax-error":
        return "deterministic"

    if category == "dead-code":
        # Unused imports are deterministic; unreferenced functions are likely-safe
        evidence = finding.get("evidence", {})
        if evidence.get("unused_imports"):
            return "deterministic"
        return "likely-safe"

    if category == "llm-review":
        if severity in ("high", "critical"):
            return "review-only"
        return "likely-safe"

    if category in ("architectural-drift", "dependency-vuln", "recurring-audit"):
        return "review-only"

    return "review-only"


# ---------------------------------------------------------------------------
# Post-fix verification gate (AC 9b)
# ---------------------------------------------------------------------------

def _verify_fix(root: Path, worktree_path: str, finding: dict, policy: dict | None = None) -> tuple[bool, str, dict]:
    """Verify a fix before push. Returns (ok, reason, report)."""
    report: dict = {
        "changed_files": [],
        "python_files_checked": [],
        "targeted_tests": [],
        "total_changes": 0,
    }
    policy = policy or _load_autofix_policy(root)
    # 1. Syntax-check every changed .py file
    try:
        diff_result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD~1"],
            capture_output=True, text=True, timeout=10, cwd=worktree_path,
        )
        changed_files = [f.strip() for f in diff_result.stdout.splitlines() if f.strip()]
    except (subprocess.TimeoutExpired, OSError):
        return False, "could not determine changed files", report
    report["changed_files"] = changed_files

    if not changed_files:
        return False, "no changed files detected", report

    forbidden_prefixes = (".dynos/", ".git/")
    for changed in changed_files:
        if changed.startswith(forbidden_prefixes):
            return False, f"forbidden path changed: {changed}", report

    binary_exts = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".tar", ".gz", ".woff", ".woff2"}
    for changed in changed_files:
        if Path(changed).suffix.lower() in binary_exts:
            return False, f"binary file changed: {changed}", report

    dependency_files = {
        "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
        "requirements.txt", "requirements-dev.txt", "poetry.lock", "pyproject.toml",
    }
    if not policy.get("allow_dependency_file_changes", False):
        for changed in changed_files:
            if Path(changed).name in dependency_files and finding.get("category") != "dependency-vuln":
                return False, f"unexpected dependency file change: {changed}", report

    for changed in changed_files:
        if not changed.endswith(".py"):
            continue
        full_path = Path(worktree_path) / changed
        if not full_path.exists():
            continue  # File was deleted, that's fine
        try:
            source = full_path.read_text()
            ast.parse(source, filename=changed)
            report["python_files_checked"].append(changed)
        except SyntaxError as exc:
            return False, f"syntax error in {changed} line {exc.lineno}: {exc.msg}", report
        except (OSError, UnicodeDecodeError) as exc:
            return False, f"could not read {changed}: {exc}", report

    # 2. Check diff size: max 500 added+removed lines, max 10 files
    try:
        stat_result = subprocess.run(
            ["git", "diff", "--stat", "HEAD~1"],
            capture_output=True, text=True, timeout=10, cwd=worktree_path,
        )
        stat_lines = stat_result.stdout.strip().splitlines()
    except (subprocess.TimeoutExpired, OSError):
        return False, "could not get diff stat", report

    if len(changed_files) > 10:
        return False, f"too many files changed ({len(changed_files)} > 10)", report

    # Parse the summary line for insertions/deletions
    if stat_lines:
        summary = stat_lines[-1]
        total_changes = 0
        for match in re.finditer(r"(\d+) (?:insertion|deletion)", summary):
            total_changes += int(match.group(1))
        report["total_changes"] = total_changes
        if total_changes > 500:
            return False, f"diff too large ({total_changes} lines > 500)", report

    # 3. Changed files should be within scope (match finding's evidence.file)
    evidence_file = finding.get("evidence", {}).get("file", "")
    if evidence_file and changed_files and evidence_file not in changed_files:
        # Normalize paths for comparison
        evidence_dir = str(Path(evidence_file).parent)
        for changed in changed_files:
            changed_dir = str(Path(changed).parent)
            # Allow changes in same directory tree or test directories
            if (not changed.startswith(evidence_dir)
                    and not changed.startswith("tests/")
                    and not changed.startswith("test/")
                    and changed != evidence_file):
                return False, f"out-of-scope change: {changed} (expected near {evidence_file})", report

    # 4. Run a targeted test file when there is an obvious match.
    evidence_stem = Path(evidence_file).stem if evidence_file else ""
    candidate_tests = []
    if evidence_stem:
        candidate_tests.extend([
            Path(worktree_path) / "tests" / f"test_{evidence_stem}.py",
            Path(worktree_path) / "test" / f"test_{evidence_stem}.py",
        ])
    for test_path in candidate_tests:
        if not test_path.exists():
            continue
        result = subprocess.run(
            ["python3", "-m", "pytest", "-q", str(test_path)],
            capture_output=True, text=True, timeout=90, cwd=worktree_path,
        )
        report["targeted_tests"].append({
            "path": str(test_path.relative_to(Path(worktree_path))),
            "returncode": result.returncode,
        })
        if result.returncode != 0:
            return False, f"targeted test failed: {test_path.name}", report

    return True, "", report


# ---------------------------------------------------------------------------
# Risk-based routing: auto-fix for low/medium (AC 9)
# ---------------------------------------------------------------------------

def _is_git_dirty(root: Path) -> bool:
    """Check if the git working tree has uncommitted changes."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=15, cwd=str(root),
        )
        return bool(result.stdout.strip())
    except (subprocess.TimeoutExpired, OSError):
        return True  # Assume dirty on error to be safe


def _compute_pr_quality_score(verification: dict) -> float:
    score = 1.0
    total_changes = int(verification.get("total_changes", 0) or 0)
    changed_files = verification.get("changed_files", [])
    targeted_tests = verification.get("targeted_tests", [])
    score -= min(total_changes / 1000.0, 0.25)
    score -= min(max(len(changed_files) - 1, 0) * 0.03, 0.15)
    if targeted_tests:
        if all(t.get("returncode") == 0 for t in targeted_tests if isinstance(t, dict)):
            score += 0.05
        else:
            score -= 0.15
    if verification.get("python_files_checked"):
        score += 0.03
    return round(max(0.0, min(1.0, score)), 3)


def _sync_outcomes(root: Path, findings: list[dict], policy: dict) -> tuple[list[dict], dict]:
    if not shutil.which("gh"):
        metrics = _write_autofix_metrics(root, findings, _recompute_category_confidence(policy))
        return findings, metrics

    for finding in findings:
        category = str(finding.get("category", "") or "")
        if category not in policy.get("categories", {}):
            continue
        category_stats = policy["categories"][category]["stats"]
        pr_number = finding.get("pr_number")
        if pr_number:
            try:
                result = subprocess.run(
                    ["gh", "pr", "view", str(pr_number), "--json", "state,mergedAt,closedAt,url"],
                    capture_output=True, text=True, timeout=20, cwd=str(root),
                )
                if result.returncode == 0 and result.stdout.strip():
                    data = json.loads(result.stdout)
                    state = str(data.get("state", "OPEN")).upper()
                    finding["pr_state"] = state
                    finding["pr_url"] = data.get("url") or finding.get("pr_url")
                    if state == "MERGED" or data.get("mergedAt"):
                        if finding.get("merge_outcome") != "merged":
                            category_stats["merged"] += 1
                        finding["merge_outcome"] = "merged"
                        finding["merged_at"] = data.get("mergedAt")
                    elif state == "CLOSED":
                        if finding.get("merge_outcome") != "closed_unmerged":
                            category_stats["closed_unmerged"] += 1
                        finding["merge_outcome"] = "closed_unmerged"
                        finding["closed_at"] = data.get("closedAt")
                    else:
                        finding["merge_outcome"] = "open"
            except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
                pass
        issue_number = finding.get("issue_number")
        if issue_number:
            try:
                result = subprocess.run(
                    ["gh", "issue", "view", str(issue_number), "--json", "state,url,closedAt"],
                    capture_output=True, text=True, timeout=20, cwd=str(root),
                )
                if result.returncode == 0 and result.stdout.strip():
                    data = json.loads(result.stdout)
                    finding["issue_url"] = data.get("url") or finding.get("issue_url")
                    finding["issue_state"] = str(data.get("state", "OPEN")).upper()
                    finding["issue_closed_at"] = data.get("closedAt")
            except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
                pass

    policy = _recompute_category_confidence(policy)
    _save_autofix_policy(root, policy)
    metrics = _write_autofix_metrics(root, findings, policy)
    return findings, metrics


def _autofix_finding(finding: dict, root: Path, policy: dict | None = None) -> dict:
    """Attempt auto-fix for a low/medium severity finding. Returns updated finding."""
    policy = policy or _load_autofix_policy(root)
    finding_id = finding["finding_id"]
    description = finding["description"]
    evidence_str = json.dumps(finding.get("evidence", {}), indent=2)
    category = str(finding.get("category", "") or "")
    category_stats = policy.get("categories", {}).get(category, {}).get("stats", {})

    # Pre-checks
    if not shutil.which("claude"):
        finding["status"] = "failed"
        finding["fail_reason"] = "claude_not_available"
        finding["processed_at"] = now_iso()
        _log(f"Skipping fix for {finding_id}: claude CLI not available")
        return finding

    if not shutil.which("gh"):
        finding["status"] = "failed"
        finding["fail_reason"] = "gh_not_available"
        finding["processed_at"] = now_iso()
        _log(f"Skipping fix for {finding_id}: gh CLI not available")
        return finding

    # Note: dirty working tree is OK — worktrees provide full isolation.
    # The worktree is created from HEAD, not the working tree.

    # Check for existing PR (AC 16)
    if _check_existing_pr(finding_id, root):
        finding["status"] = "already-exists"
        finding["processed_at"] = now_iso()
        _log(f"PR already exists for {finding_id}, skipping")
        return finding

    # Repo-scoped names to avoid collisions across projects
    repo_slug = str(root).strip("/").replace("/", "-")[:40]
    branch_name = f"dynos/auto-fix-{finding_id}"
    worktree_path = f"/tmp/dynos-autofix-{repo_slug}-{finding_id}"
    finding["branch_name"] = branch_name
    category_stats["proposed"] = int(category_stats.get("proposed", 0) or 0) + 1

    # Detect current branch for PR base
    try:
        base_result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5, cwd=str(root),
        )
        base_branch = base_result.stdout.strip() if base_result.returncode == 0 else ""
        if not base_branch or base_branch == "HEAD":
            # Detached HEAD — find the default branch
            default_result = subprocess.run(
                ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
                capture_output=True, text=True, timeout=5, cwd=str(root),
            )
            if default_result.returncode == 0:
                # refs/remotes/origin/main → main
                base_branch = default_result.stdout.strip().split("/")[-1]
            else:
                # Last resort: check if main or master exists
                for candidate in ("main", "master"):
                    check = subprocess.run(
                        ["git", "rev-parse", "--verify", candidate],
                        capture_output=True, timeout=5, cwd=str(root),
                    )
                    if check.returncode == 0:
                        base_branch = candidate
                        break
                else:
                    base_branch = "main"
    except (subprocess.TimeoutExpired, OSError):
        base_branch = "main"

    # Prune stale worktrees before creating a new one
    subprocess.run(["git", "worktree", "prune"], capture_output=True, timeout=10, cwd=str(root))

    # Remove existing worktree directory if leftover from crash
    if Path(worktree_path).exists():
        shutil.rmtree(worktree_path, ignore_errors=True)

    try:
        # Fetch latest remote state so worktree starts from what's on the remote.
        # Without this, local-only branches cause PR creation to fail.
        subprocess.run(
            ["git", "fetch", "origin", base_branch],
            capture_output=True, text=True, timeout=30, cwd=str(root),
        )

        # Create worktree from the remote default branch, not the user's local copy.
        _log(f"Creating worktree at {worktree_path}")
        subprocess.run(
            ["git", "worktree", "add", "--detach", worktree_path, f"origin/{base_branch}"],
            capture_output=True, text=True, timeout=30, cwd=str(root), check=True,
        )
        # Delete stale branch from previous failed attempt if it exists
        subprocess.run(
            ["git", "branch", "-D", branch_name],
            capture_output=True, text=True, timeout=15, cwd=worktree_path,
        )
        subprocess.run(
            ["git", "checkout", "-b", branch_name],
            capture_output=True, text=True, timeout=15, cwd=worktree_path, check=True,
        )

        # Invoke claude with the dynos-work foundry pipeline
        # This runs the full cycle: discover → spec → plan → execute → audit
        # with auto-approved gates for low/medium findings.
        # Generates trajectories and retrospectives for learning.
        prompt = (
            f"/dynos-work:start Fix the following issue found by the proactive scanner. "
            f"Auto-approve the spec and plan without asking the user.\n\n"
            f"## Finding\n"
            f"**ID:** {finding_id}\n"
            f"**Category:** {finding['category']}\n"
            f"**Severity:** {finding['severity']}\n"
            f"**Description:** {description}\n\n"
            f"## Evidence\n```json\n{evidence_str}\n```\n\n"
            f"## CRITICAL RULES\n"
            f"- Keep changes minimal and focused on this single finding.\n"
            f"- Do NOT refactor surrounding code.\n"
            f"- Do NOT run `git push` or push to any remote. The caller handles pushing.\n"
            f"- Do NOT create PRs. The caller handles PR creation.\n"
            f"- Stay on the current branch `{branch_name}`. Do NOT create new branches.\n"
            f"- Commit message: [autofix] {description[:80]}"
        )
        # Select model based on severity
        severity = finding.get("severity", "low")
        claude_cmd = [
            "claude", "-p", prompt,
            "--permission-mode", "auto",
            "--allowedTools", "Read Edit Write Glob Grep Bash(python3 -m pytest*) Bash(git add*) Bash(git commit*) Bash(git diff*) Bash(git status*) Bash(git log*)",
        ]
        if severity in ("high", "critical"):
            claude_cmd.extend(["--model", "opus"])
            _log(f"Running foundry pipeline for {finding_id} (opus — {severity} severity)")
        else:
            _log(f"Running foundry pipeline for {finding_id}")
        # Tell session-start to skip registration and daemon for worktrees
        worktree_env = {**os.environ, "DYNOS_AUTOFIX_WORKTREE": "1"}
        claude_result = subprocess.run(
            claude_cmd,
            capture_output=True, text=True, timeout=600, cwd=worktree_path,
            env=worktree_env,
        )

        if claude_result.returncode == 0:
            # Check if Claude made any changes
            diff_check = subprocess.run(
                ["git", "diff", "--quiet"],
                capture_output=True, timeout=10, cwd=worktree_path,
            )
            staged_check = subprocess.run(
                ["git", "diff", "--cached", "--quiet"],
                capture_output=True, timeout=10, cwd=worktree_path,
            )
            has_changes = diff_check.returncode != 0 or staged_check.returncode != 0

            if not has_changes:
                # Claude ran but made no changes — check if it committed already
                log_check = subprocess.run(
                    ["git", "log", f"{base_branch}..HEAD", "--oneline"],
                    capture_output=True, text=True, timeout=10, cwd=worktree_path,
                )
                has_changes = bool(log_check.stdout.strip())

            if not has_changes:
                finding["status"] = "failed"
                finding["fail_reason"] = "claude_no_changes"
                _log(f"Claude produced no changes for {finding_id}")
                return finding

            # Check if Claude already committed (common with --permission-mode auto)
            log_check_2 = subprocess.run(
                ["git", "log", f"{base_branch}..HEAD", "--oneline"],
                capture_output=True, text=True, timeout=10, cwd=worktree_path,
            )
            already_committed = bool(log_check_2.stdout.strip())

            if not already_committed:
                # Stage and commit — Claude left uncommitted changes
                add_result = subprocess.run(
                    ["git", "add", "-A"],
                    capture_output=True, text=True, timeout=10, cwd=worktree_path,
                )
                if add_result.returncode != 0:
                    finding["status"] = "failed"
                    finding["fail_reason"] = f"git_add_failed: {add_result.stderr.strip()}"
                    finding["processed_at"] = now_iso()
                    _log(f"git add failed for {finding_id}: {add_result.stderr.strip()}")
                    return finding
                # Check if anything was actually staged
                staged_check_2 = subprocess.run(
                    ["git", "diff", "--cached", "--quiet"],
                    capture_output=True, timeout=10, cwd=worktree_path,
                )
                if staged_check_2.returncode == 0:
                    # Nothing staged after add — Claude made changes but they vanished
                    finding["status"] = "failed"
                    finding["fail_reason"] = "claude_no_changes"
                    finding["processed_at"] = now_iso()
                    _log(f"Nothing staged after git add for {finding_id}")
                    return finding
                commit_result = subprocess.run(
                    ["git", "commit", "-m", f"[autofix] {description[:80]}",
                     "--author", "dynos-autofix <autofix@dynos.fit>"],
                    capture_output=True, text=True, timeout=15, cwd=worktree_path,
                )
                if commit_result.returncode != 0:
                    finding["status"] = "failed"
                    finding["fail_reason"] = f"git_commit_failed: {commit_result.stderr.strip()}"
                    finding["processed_at"] = now_iso()
                    _log(f"git commit failed for {finding_id}: {commit_result.stderr.strip()}")
                    return finding
            else:
                _log(f"Claude already committed for {finding_id}, skipping commit step")

            # Post-fix verification gate
            verify_ok, verify_reason, verify_report = _verify_fix(root, worktree_path, finding, policy)
            finding["verification"] = verify_report
            if not verify_ok:
                finding["status"] = "failed"
                finding["fail_reason"] = f"verification_failed: {verify_reason}"
                finding["processed_at"] = now_iso()
                category_stats["verification_failed"] = int(category_stats.get("verification_failed", 0) or 0) + 1
                _log(f"Verification failed for {finding_id}: {verify_reason}")
                return finding
            finding["pr_quality_score"] = _compute_pr_quality_score(verify_report)

            # Push branch to remote
            push_result = subprocess.run(
                ["git", "push", "-u", "origin", branch_name],
                capture_output=True, text=True, timeout=30, cwd=worktree_path,
            )
            if push_result.returncode != 0:
                finding["status"] = "failed"
                finding["fail_reason"] = f"git_push_failed: {push_result.stderr.strip()}"
                _log(f"Push failed for {finding_id}: {push_result.stderr.strip()}")
                return finding

            # Create PR
            # Collect diff stat for PR body
            diff_stat_text = ""
            try:
                diff_stat_result = subprocess.run(
                    ["git", "diff", "--stat", "HEAD~1..HEAD"],
                    capture_output=True, text=True, timeout=10, cwd=worktree_path,
                )
                if diff_stat_result.returncode == 0 and diff_stat_result.stdout.strip():
                    diff_stat_text = diff_stat_result.stdout.strip()
            except (subprocess.TimeoutExpired, OSError):
                pass

            # Build a human-readable PR description
            category = finding['category']
            severity = finding['severity']
            evidence = finding.get('evidence', {})
            file_name = evidence.get('file', 'unknown file')
            line_num = evidence.get('line', '')
            location = f"`{file_name}:{line_num}`" if line_num else f"`{file_name}`"

            changes_section = ""
            if diff_stat_text:
                changes_section = (
                    f"## Changes\n\n"
                    f"```\n{diff_stat_text}\n```\n\n"
                )

            pr_body = (
                f"## What's wrong\n\n"
                f"{description}\n\n"
                f"**Where:** {location}\n"
                f"**Severity:** {severity}\n\n"
                f"## What this PR does\n\n"
                f"Fixes the issue above. The change was generated by the dynos-work autofix scanner "
                f"and verified by running the foundry pipeline (spec → plan → execute → audit).\n\n"
                f"{changes_section}"
                f"## Evidence\n\n"
                f"```json\n{evidence_str}\n```\n\n"
                f"---\n"
                f"*Auto-generated by [dynos-work](https://github.com/dynos-fit/dynos-work) proactive scanner.*"
            )
            _log(f"Creating PR for {finding_id}")
            pr_result = subprocess.run(
                [
                    "gh", "pr", "create",
                    "--base", base_branch,
                    "--head", branch_name,
                    "--title", f"[autofix] {description[:80]}",
                    "--body", pr_body,
                ],
                capture_output=True, text=True, timeout=30, cwd=worktree_path,
            )
            if pr_result.returncode == 0:
                # Extract PR number from output
                pr_url = pr_result.stdout.strip()
                pr_number = None
                if pr_url:
                    parts = pr_url.rstrip("/").split("/")
                    if parts:
                        try:
                            pr_number = int(parts[-1])
                        except ValueError:
                            pass
                finding["status"] = "fixed"
                finding["pr_number"] = pr_number
                finding["pr_url"] = pr_url
                finding["pr_state"] = "OPEN"
                finding["merge_outcome"] = "open"
                finding["processed_at"] = now_iso()
                _log(f"PR created for {finding_id}: {pr_url}")
            else:
                finding["status"] = "failed"
                finding["fail_reason"] = f"gh_pr_create_failed: {pr_result.stderr[:200]}"
                finding["processed_at"] = now_iso()
                _log(f"PR creation failed for {finding_id}: {pr_result.stderr[:200]}")
        else:
            finding["status"] = "failed"
            finding["fail_reason"] = f"claude_exit_{claude_result.returncode}"
            finding["processed_at"] = now_iso()
            _log(f"Claude fix failed for {finding_id}: exit {claude_result.returncode}")

    except subprocess.CalledProcessError as exc:
        finding["status"] = "failed"
        finding["fail_reason"] = f"subprocess_error: {exc}"
        finding["processed_at"] = now_iso()
        _log(f"Subprocess error for {finding_id}: {exc}")
    except subprocess.TimeoutExpired:
        finding["status"] = "failed"
        finding["fail_reason"] = "timeout"
        finding["processed_at"] = now_iso()
        _log(f"Timeout for {finding_id}")
    except OSError as exc:
        finding["status"] = "failed"
        finding["fail_reason"] = f"os_error: {exc}"
        finding["processed_at"] = now_iso()
        _log(f"OS error for {finding_id}: {exc}")
    finally:
        # Copy retrospectives back to main repo before cleanup
        try:
            wt_dynos = Path(worktree_path) / ".dynos"
            if wt_dynos.is_dir():
                import shutil as _sh
                for task_dir in wt_dynos.glob("task-*"):
                    retro = task_dir / "task-retrospective.json"
                    if retro.exists():
                        dest_dir = root / ".dynos" / task_dir.name
                        dest_dir.mkdir(parents=True, exist_ok=True)
                        _sh.copy2(str(retro), str(dest_dir / "task-retrospective.json"))
                        _log(f"Copied retrospective from worktree: {task_dir.name}")
                        # Copy full task artifacts for trajectory context
                        for extra in ("manifest.json", "spec.md", "plan.md",
                                      "execution-log.md", "execution-graph.json",
                                      "discovery-notes.md", "design-decisions.md",
                                      "raw-input.md", "completion.json", "audit-summary.json"):
                            src = task_dir / extra
                            if src.exists():
                                _sh.copy2(str(src), str(dest_dir / extra))
                        # Copy evidence directory
                        evidence_dir = task_dir / "evidence"
                        if evidence_dir.is_dir():
                            dest_evidence = dest_dir / "evidence"
                            if dest_evidence.exists():
                                _sh.rmtree(str(dest_evidence))
                            _sh.copytree(str(evidence_dir), str(dest_evidence))
        except OSError as exc:
            _log(f"Warning: retrospective copy failed: {exc}")

        # Always clean up worktree
        try:
            subprocess.run(
                ["git", "worktree", "remove", "--force", worktree_path],
                capture_output=True, text=True, timeout=15, cwd=str(root),
            )
            _log(f"Cleaned up worktree {worktree_path}")
        except (subprocess.TimeoutExpired, OSError) as exc:
            _log(f"Warning: worktree cleanup failed: {exc}")
            if Path(worktree_path).exists():
                shutil.rmtree(worktree_path, ignore_errors=True)
            subprocess.run(["git", "worktree", "prune"], capture_output=True, timeout=10, cwd=str(root))

    return finding


# ---------------------------------------------------------------------------
# Risk-based routing: GitHub issue for high/critical (AC 10)
# ---------------------------------------------------------------------------

def _open_github_issue(finding: dict, root: Path, policy: dict | None = None) -> dict:
    """Open a GitHub issue for findings that aren't actionable code fixes (e.g., recurring patterns)."""
    policy = policy or _load_autofix_policy(root)
    finding_id = finding["finding_id"]
    description = finding["description"]
    evidence_str = json.dumps(finding.get("evidence", {}), indent=2)
    category = str(finding.get("category", "") or "")
    category_stats = policy.get("categories", {}).get(category, {}).get("stats", {})

    if not shutil.which("gh"):
        finding["status"] = "failed"
        finding["fail_reason"] = "gh_not_available"
        finding["processed_at"] = now_iso()
        _log(f"Skipping issue for {finding_id}: gh CLI not available")
        return finding

    # Check for existing issue (AC 16)
    if _check_existing_issue(finding_id, root):
        finding["status"] = "already-exists"
        finding["processed_at"] = now_iso()
        _log(f"Issue already exists for {finding_id}, skipping")
        return finding

    category = finding['category']
    severity = finding['severity']
    evidence = finding.get('evidence', {})
    file_name = evidence.get('file', 'unknown file')
    line_num = evidence.get('line', '')
    location = f"`{file_name}:{line_num}`" if line_num else f"`{file_name}`"

    # Map category to plain English
    category_labels = {
        "recurring-audit": "Recurring pattern",
        "dependency-vuln": "Dependency vulnerability",
        "dead-code": "Dead code",
        "architectural-drift": "Architectural drift",
        "syntax-error": "Syntax error",
        "llm-review": "Code review finding",
    }
    category_label = category_labels.get(category, category)

    issue_body = (
        f"## {category_label}\n\n"
        f"{description}\n\n"
        f"**Where:** {location}\n"
        f"**Severity:** {severity}\n\n"
        f"## Why this needs human review\n\n"
        f"This was flagged as **{severity}** severity, which means the autofix scanner "
        f"won't attempt an automated fix. A human should review and decide how to address it.\n\n"
        f"## Evidence\n\n"
        f"```json\n{evidence_str}\n```\n\n"
        f"---\n"
        f"*Flagged by [dynos-work](https://github.com/dynos-fit/dynos-work) proactive scanner.*"
    )

    try:
        result = subprocess.run(
            [
                "gh", "issue", "create",
                "--title", f"[autofix] {description[:80]}",
                "--body", issue_body,
                "--label", "dynos-autofix",
            ],
            capture_output=True, text=True, timeout=30, cwd=str(root),
        )
        if result.returncode == 0:
            issue_url = result.stdout.strip()
            issue_number = None
            if issue_url:
                parts = issue_url.rstrip("/").split("/")
                if parts:
                    try:
                        issue_number = int(parts[-1])
                    except ValueError:
                        pass
            finding["status"] = "issue-opened"
            finding["issue_number"] = issue_number
            finding["issue_url"] = issue_url
            finding["processed_at"] = now_iso()
            category_stats["issues_opened"] = int(category_stats.get("issues_opened", 0) or 0) + 1
            _log(f"Issue created for {finding_id}: {issue_url}")
        else:
            finding["status"] = "failed"
            finding["fail_reason"] = f"gh_issue_create_failed: {result.stderr[:200]}"
            finding["processed_at"] = now_iso()
            _log(f"Issue creation failed for {finding_id}: {result.stderr[:200]}")
    except (subprocess.TimeoutExpired, OSError) as exc:
        finding["status"] = "failed"
        finding["fail_reason"] = f"gh_error: {exc}"
        finding["processed_at"] = now_iso()
        _log(f"Error creating issue for {finding_id}: {exc}")

    return finding


# ---------------------------------------------------------------------------
# Process a single finding with retry logic (AC 13)
# ---------------------------------------------------------------------------

def _check_category_health(category: str, findings: list[dict]) -> tuple[str, str]:
    """Check if a category is healthy. Returns (status, reason).

    status is 'ok' or 'disabled'.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)
    failure_count = 0
    for f in findings:
        if f.get("category") != category:
            continue
        status = f.get("status", "")
        if status not in ("failed", "permanently_failed"):
            continue
        found_at = f.get("found_at", "")
        if not found_at:
            continue
        try:
            dt = datetime.fromisoformat(found_at.replace("Z", "+00:00"))
            if dt >= cutoff:
                failure_count += 1
        except (ValueError, TypeError):
            continue
    if failure_count >= 3:
        reason = f"{failure_count} failures in last 30 days"
        return "disabled", reason
    return "ok", ""


def _process_finding(
    finding: dict,
    root: Path,
    policy: dict | None = None,
    findings: list[dict] | None = None,
) -> dict:
    """Route and process a single finding based on policy, confidence, and rate limits."""
    policy = policy or _load_autofix_policy(root)
    findings = findings or []
    finding["attempt_count"] = finding.get("attempt_count", 0) + 1

    # Retry guard: max 2 attempts
    if finding["attempt_count"] > MAX_ATTEMPTS:
        finding["status"] = "permanently_failed"
        finding["suppressed_until"] = (
            datetime.now(timezone.utc) + timedelta(days=36500)
        ).isoformat()
        finding["processed_at"] = now_iso()
        _log(f"Finding {finding['finding_id']} permanently failed after {MAX_ATTEMPTS} attempts")
        return finding

    category = finding.get("category", "")

    # Check category health (rollback memory)
    existing_findings = _load_findings(root)
    cat_status, cat_reason = _check_category_health(category, existing_findings)
    if cat_status == "disabled":
        finding["status"] = "failed"
        finding["fail_reason"] = f"category_disabled: {cat_reason}"
        finding["processed_at"] = now_iso()
        _log(f"Category '{category}' disabled: {cat_reason}")
        return finding

    # Classify fixability for routing
    fixability = _classify_fixability(finding)
    finding["fixability"] = fixability
    category_config = policy.get("categories", {}).get(category, _default_category_policy(category))
    confidence = float(category_config.get("confidence", 0.0) or 0.0)
    finding["confidence_score"] = round(confidence, 3)

    suppression = _suppression_reason(finding, policy)
    if suppression:
        finding["status"] = "suppressed-policy"
        finding["suppression_reason"] = suppression
        finding["processed_at"] = now_iso()
        return finding

    if not category_config.get("enabled", True) or category_config.get("mode") == "disabled":
        finding["status"] = "suppressed-policy"
        finding["suppression_reason"] = "category disabled by autofix policy"
        finding["processed_at"] = now_iso()
        return finding

    # Recurring audit findings are not actionable code fixes — open issue only
    if category == "recurring-audit":
        finding["rollout_mode"] = "issue-only"
        return _open_github_issue(finding, root, policy)

    # Route based on fixability classification
    if (
        fixability == "review-only"
        or category_config.get("mode") == "issue-only"
        or confidence < float(category_config.get("min_confidence_autofix", 0.65) or 0.65)
    ):
        finding["rollout_mode"] = "issue-only"
        return _open_github_issue(finding, root, policy)

    # "deterministic" and "likely-safe" go through autofix
    rate_limit = _rate_limit_reason(policy, findings)
    if rate_limit:
        finding["status"] = "rate-limited"
        finding["fail_reason"] = rate_limit
        finding["processed_at"] = now_iso()
        return finding
    finding["rollout_mode"] = "autofix"
    return _autofix_finding(finding, root, policy)


# ---------------------------------------------------------------------------
# Scan command (AC 1, 7, 17)
# ---------------------------------------------------------------------------

def cmd_scan(args: argparse.Namespace) -> int:
    """Run a full proactive scan."""
    root = Path(args.root).resolve()
    max_findings = int(args.max_findings)

    # Require claude CLI — autofix is useless without it
    if not shutil.which("claude"):
        print(json.dumps({
            "ok": False,
            "error": "claude CLI not found. Install it: https://docs.anthropic.com/en/docs/claude-code",
        }))
        return 1

    # Acquire exclusive scan lock to prevent concurrent scans
    lock_path = root / ".dynos" / "scan.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = open(lock_path, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(json.dumps({"error": "scan already running"}))
        lock_fd.close()
        return 1

    try:
        return _cmd_scan_locked(root, max_findings)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


def _cmd_scan_locked(root: Path, max_findings: int) -> int:
    """Scan logic, called while holding the scan lock."""
    start_time = time.monotonic()

    _log(f"Starting proactive scan on {root}")
    policy = _load_autofix_policy(root)

    # Cleanup merged autofix branches
    _cleanup_merged_branches(root)

    # Load existing findings for dedup
    existing_findings = _load_findings(root)
    existing_findings, pre_metrics = _sync_outcomes(root, existing_findings, policy)

    # Prune old findings
    existing_findings = _prune_findings(existing_findings)

    # Run all four detectors
    new_findings: list[dict] = []
    new_findings.extend(_detect_syntax_errors(root))
    new_findings.extend(_detect_recurring_audit(root))
    new_findings.extend(_detect_dependency_vulns(root))
    new_findings.extend(_detect_dead_code(root))
    new_findings.extend(_detect_architectural_drift(root))
    new_findings.extend(_detect_llm_review(root))

    _log(f"Detected {len(new_findings)} raw findings")

    # Dedup and collect processable findings
    to_process: list[dict] = []
    skipped_dedup = 0
    for finding in new_findings:
        skip_reason = _dedup_finding(finding, existing_findings)
        if skip_reason:
            _log(f"Skipping {finding['finding_id']}: {skip_reason}")
            finding["status"] = "skipped-dedup"
            finding["processed_at"] = now_iso()
            finding["fail_reason"] = skip_reason
            existing_findings.append(finding)
            skipped_dedup += 1
        else:
            to_process.append(finding)

    # Cap at max_findings (AC 7)
    to_process = to_process[:max_findings]
    _log(f"Processing {len(to_process)} findings (max={max_findings}, skipped_dedup={skipped_dedup})")

    # Process findings sequentially, respecting time budget (AC 17)
    summary_counts: dict[str, int] = {
        "processed": 0,
        "skipped_dedup": skipped_dedup,
        "fixed": 0,
        "issues_opened": 0,
        "failed": 0,
        "rate_limited": 0,
        "suppressed": 0,
    }
    by_category: dict[str, int] = {}
    by_severity: dict[str, int] = {}

    all_scan_findings: list[dict] = []

    for finding in to_process:
        elapsed = time.monotonic() - start_time
        remaining = SCAN_TIMEOUT_SECONDS - elapsed
        if remaining < 60:
            _log(f"Time budget low ({remaining:.0f}s remaining), stopping processing")
            finding["status"] = "failed"
            finding["fail_reason"] = "timeout_budget_exhausted"
            finding["processed_at"] = now_iso()
            existing_findings.append(finding)
            all_scan_findings.append(finding)
            summary_counts["failed"] += 1
            continue

        processed = _process_finding(finding, root, policy, existing_findings)

        # Handle retry: if failed and under attempt limit, keep as-is for next cycle
        if processed["status"] == "failed" and processed["attempt_count"] < MAX_ATTEMPTS:
            _log(f"Finding {processed['finding_id']} failed attempt {processed['attempt_count']}, will retry next cycle")

        existing_findings.append(processed)
        all_scan_findings.append(processed)
        summary_counts["processed"] += 1

        status = processed.get("status", "")
        if status == "fixed":
            summary_counts["fixed"] += 1
        elif status == "issue-opened":
            summary_counts["issues_opened"] += 1
        elif status == "rate-limited":
            summary_counts["rate_limited"] += 1
        elif status == "suppressed-policy":
            summary_counts["suppressed"] += 1
        elif status in ("failed", "permanently_failed"):
            summary_counts["failed"] += 1

    # Count categories and severities across all findings from this scan
    for f in all_scan_findings:
        cat = f.get("category", "unknown")
        sev = f.get("severity", "unknown")
        by_category[cat] = by_category.get(cat, 0) + 1
        by_severity[sev] = by_severity.get(sev, 0) + 1

    # Also count deduped findings in category/severity
    for f in new_findings:
        if f.get("status") == "skipped-dedup":
            cat = f.get("category", "unknown")
            sev = f.get("severity", "unknown")
            if cat not in by_category:
                by_category[cat] = 0
            if sev not in by_severity:
                by_severity[sev] = 0

    # Persist all findings (AC 13)
    # Store category health in findings file
    _save_findings(root, existing_findings)
    policy = _recompute_category_confidence(policy)
    _save_autofix_policy(root, policy)
    metrics = _write_autofix_metrics(root, existing_findings, policy)
    benchmarks = _build_autofix_benchmarks(root, existing_findings, policy)

    # Write category_health to the findings JSON
    all_categories = set()
    for f in existing_findings:
        cat = f.get("category", "")
        if cat:
            all_categories.add(cat)
    category_health: dict[str, dict] = {}
    for cat in sorted(all_categories):
        status, reason = _check_category_health(cat, existing_findings)
        if status == "disabled":
            category_health[cat] = {"status": status, "reason": reason}
    # Persist category_health alongside findings
    findings_path = _findings_path(root)
    try:
        raw_data = {"findings": existing_findings, "category_health": category_health}
        write_json(findings_path, raw_data)
    except OSError:
        pass

    # Cost tracking
    # Count haiku invocations: 1 if llm-review findings were attempted, 0 otherwise
    haiku_invocations = 1 if any(
        f.get("category") == "llm-review" for f in new_findings
    ) else 0
    # Count fix invocations (autofix calls)
    fix_invocations = 0
    opus_fix_invocations = 0
    for f in all_scan_findings:
        fixability = f.get("fixability", "")
        if fixability in ("deterministic", "likely-safe"):
            fix_invocations += 1
            if f.get("severity") in ("high", "critical"):
                opus_fix_invocations += 1
    default_fix_invocations = fix_invocations - opus_fix_invocations
    estimated_cost = (
        haiku_invocations * 0.03
        + default_fix_invocations * 0.50
        + opus_fix_invocations * 2.00
    )

    elapsed = time.monotonic() - start_time
    output = {
        "findings": all_scan_findings,
        "summary": {
            "by_category": by_category,
            "by_severity": by_severity,
            "processed": summary_counts["processed"],
            "skipped_dedup": summary_counts["skipped_dedup"],
            "fixed": summary_counts["fixed"],
            "issues_opened": summary_counts["issues_opened"],
            "failed": summary_counts["failed"],
            "rate_limited": summary_counts["rate_limited"],
            "suppressed": summary_counts["suppressed"],
        },
        "autofix_metrics": metrics,
        "autofix_benchmarks": benchmarks,
        "cost": {
            "haiku_invocations": haiku_invocations,
            "fix_invocations": fix_invocations,
            "estimated_cost_usd": round(estimated_cost, 2),
        },
        "scan_duration_seconds": round(elapsed, 2),
    }

    # Only JSON to stdout (AC 18, 27)
    print(json.dumps(output, indent=2))
    return 0


# ---------------------------------------------------------------------------
# List command (AC 14)
# ---------------------------------------------------------------------------

def cmd_list(args: argparse.Namespace) -> int:
    """List current proactive findings."""
    root = Path(args.root).resolve()
    findings = _load_findings(root)
    print(json.dumps(findings, indent=2))
    return 0


# ---------------------------------------------------------------------------
# Clear command (AC 15)
# ---------------------------------------------------------------------------

def cmd_clear(args: argparse.Namespace) -> int:
    """Clear proactive findings file."""
    root = Path(args.root).resolve()
    path = _findings_path(root)
    try:
        if path.exists():
            path.unlink()
    except OSError as exc:
        print(json.dumps({"cleared": False, "error": str(exc)}))
        return 1
    print(json.dumps({"cleared": True}))
    return 0


def cmd_policy(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    policy = _load_autofix_policy(root)
    print(json.dumps(policy, indent=2))
    return 0


def cmd_sync_outcomes(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    findings = _load_findings(root)
    policy = _load_autofix_policy(root)
    findings, metrics = _sync_outcomes(root, findings, policy)
    _save_findings(root, findings)
    print(json.dumps({"synced": True, "count": len(findings), "metrics": metrics}, indent=2))
    return 0


def cmd_benchmark(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    findings = _load_findings(root)
    policy = _load_autofix_policy(root)
    benchmarks = _build_autofix_benchmarks(root, findings, policy)
    print(json.dumps(benchmarks, indent=2))
    return 0


def cmd_suppress_add(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    policy = _load_autofix_policy(root)
    entry = {
        "finding_id": args.finding_id,
        "category": args.category,
        "path_prefix": args.path_prefix,
        "until": (
            datetime.now(timezone.utc) + timedelta(days=int(args.days))
        ).isoformat() if args.days else None,
        "reason": args.reason or "manual suppression",
    }
    policy.setdefault("suppressions", []).append(entry)
    _save_autofix_policy(root, policy)
    print(json.dumps({"added": entry}, indent=2))
    return 0


def cmd_suppress_list(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    policy = _load_autofix_policy(root)
    print(json.dumps(policy.get("suppressions", []), indent=2))
    return 0


def cmd_suppress_remove(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    policy = _load_autofix_policy(root)
    before = list(policy.get("suppressions", []))
    remaining = []
    for entry in before:
        if args.finding_id and entry.get("finding_id") == args.finding_id:
            continue
        if args.category and entry.get("category") == args.category:
            continue
        if args.path_prefix and entry.get("path_prefix") == args.path_prefix:
            continue
        remaining.append(entry)
    policy["suppressions"] = remaining
    _save_autofix_policy(root, policy)
    print(json.dumps({"removed": len(before) - len(remaining), "remaining": remaining}, indent=2))
    return 0


# ---------------------------------------------------------------------------
# CLI (argparse + set_defaults pattern)
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Autonomous autofix scanner for dynos-work",
    )
    sub = parser.add_subparsers(dest="subcommand")

    # scan
    p_scan = sub.add_parser("scan", help="Run proactive scan")
    p_scan.add_argument("--root", default=".", help="Project root path")
    p_scan.add_argument("--max-findings", default=100, type=int, help="Max findings to process per cycle")
    p_scan.set_defaults(func=cmd_scan)

    # list
    p_list = sub.add_parser("list", help="List current findings")
    p_list.add_argument("--root", default=".", help="Project root path")
    p_list.set_defaults(func=cmd_list)

    # clear
    p_clear = sub.add_parser("clear", help="Clear findings file")
    p_clear.add_argument("--root", default=".", help="Project root path")
    p_clear.set_defaults(func=cmd_clear)

    # policy
    p_policy = sub.add_parser("policy", help="Show current autofix policy")
    p_policy.add_argument("--root", default=".", help="Project root path")
    p_policy.set_defaults(func=cmd_policy)

    # sync-outcomes
    p_sync = sub.add_parser("sync-outcomes", help="Refresh PR/issue outcomes and metrics")
    p_sync.add_argument("--root", default=".", help="Project root path")
    p_sync.set_defaults(func=cmd_sync_outcomes)

    # benchmark
    p_benchmark = sub.add_parser("benchmark", help="Build autofix benchmark summary from findings")
    p_benchmark.add_argument("--root", default=".", help="Project root path")
    p_benchmark.set_defaults(func=cmd_benchmark)

    # suppress
    p_suppress = sub.add_parser("suppress", help="Manage autofix suppressions")
    suppress_sub = p_suppress.add_subparsers(dest="suppress_command", required=True)

    p_suppress_add = suppress_sub.add_parser("add", help="Add a suppression rule")
    p_suppress_add.add_argument("--root", default=".", help="Project root path")
    p_suppress_add.add_argument("--finding-id", default=None, help="Specific finding id to suppress")
    p_suppress_add.add_argument("--category", default=None, help="Category to suppress")
    p_suppress_add.add_argument("--path-prefix", default=None, help="Path prefix to suppress")
    p_suppress_add.add_argument("--days", type=int, default=30, help="Suppression duration in days")
    p_suppress_add.add_argument("--reason", default="manual suppression", help="Why this suppression exists")
    p_suppress_add.set_defaults(func=cmd_suppress_add)

    p_suppress_list = suppress_sub.add_parser("list", help="List suppressions")
    p_suppress_list.add_argument("--root", default=".", help="Project root path")
    p_suppress_list.set_defaults(func=cmd_suppress_list)

    p_suppress_remove = suppress_sub.add_parser("remove", help="Remove suppressions")
    p_suppress_remove.add_argument("--root", default=".", help="Project root path")
    p_suppress_remove.add_argument("--finding-id", default=None, help="Specific finding id to remove")
    p_suppress_remove.add_argument("--category", default=None, help="Category suppression to remove")
    p_suppress_remove.add_argument("--path-prefix", default=None, help="Path prefix suppression to remove")
    p_suppress_remove.set_defaults(func=cmd_suppress_remove)

    return parser


if __name__ == "__main__":
    from dyno_cli_base import cli_main
    raise SystemExit(cli_main(build_parser))
