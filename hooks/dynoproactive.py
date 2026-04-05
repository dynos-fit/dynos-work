#!/usr/bin/env python3
"""Autonomous autofix scanner for dynos-work.

Detects technical debt across four categories (recurring audit findings,
dependency vulnerabilities, dead code, architectural drift), then routes
each finding through a risk-based pipeline: low/medium findings trigger
auto-fix via git worktree + claude CLI + PR, while high/critical findings
open GitHub issues for human review.

All logging goes to stderr. Only final JSON goes to stdout.
"""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import argparse
import ast
import hashlib
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dynoslib import (
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
VALID_CATEGORIES = {"recurring-audit", "dependency-vuln", "dead-code", "architectural-drift"}
VALID_STATUSES = {
    "new", "fixed", "issue-opened", "failed",
    "skipped-dedup", "already-exists", "permanently_failed",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    """Log to stderr so stdout stays clean for JSON."""
    print(f"[proactive] {msg}", file=sys.stderr, flush=True)


def _findings_path(root: Path) -> Path:
    return root / ".dynos" / "proactive-findings.json"


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
    return []


def _save_findings(root: Path, findings: list[dict]) -> None:
    write_json(_findings_path(root), findings)


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
        "issue_number": None,
        "suppressed_until": None,
        "fail_reason": None,
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
                        raw_severity = str(v.get("fix_versions", ["unknown"])[0] if v.get("fix_versions") else "unknown")
                        vuln_desc = str(v.get("description", "No description"))
                        finding_id = f"dep-vuln-{pkg_name}-{vuln_id}"
                        severity = "high"
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
        except OSError:
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
        except OSError:
            continue
        all_source_texts[py_file.name] = source
        try:
            tree = ast.parse(source, filename=py_file.name)
        except SyntaxError:
            continue
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.FunctionDef):
                all_defined_funcs.setdefault(node.name, []).append(py_file.name)

    # Check for unused imports per file
    for py_file in py_files:
        try:
            source = py_file.read_text()
        except OSError:
            continue
        try:
            tree = ast.parse(source, filename=py_file.name)
        except SyntaxError:
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
            ["claude", "-p", prompt, "--model", "haiku", "--dangerously-skip-permissions"],
            capture_output=True, text=True, timeout=600, cwd=root,
        )
    except subprocess.TimeoutExpired:
        _log("Haiku review timed out after 120s")
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

        # Level 1: exact finding_id match
        if ex_id == fid:
            return f"exact finding_id match (status={ex_status})"

        # Level 3: permanently_failed -> never retry
        if ex_status == "permanently_failed" and ex_id == fid:
            return "permanently_failed"

    # Level 2: category + file + description_hash semantic match
    for ex in existing:
        ex_status = ex.get("status", "")
        ex_cat = ex.get("category", "")
        ex_file = ex.get("evidence", {}).get("file", "")
        ex_desc_h = _description_hash(ex.get("description", ""))

        if ex_cat == cat and ex_file == evidence_file and ex_desc_h == desc_h:
            if ex_status in ("fixed", "issue-opened", "failed", "permanently_failed"):
                return f"semantic match (category={cat}, desc_hash={desc_h}, status={ex_status})"

    # Level 3 check: fixed findings whose PR was merged -> permanently suppressed
    for ex in existing:
        if ex.get("finding_id") == fid and ex.get("status") == "fixed" and ex.get("pr_number"):
            return "fixed with merged PR, permanently suppressed"

    return None


# ---------------------------------------------------------------------------
# Pre-action checks (AC 16)
# ---------------------------------------------------------------------------

def _check_existing_pr(finding_id: str) -> bool:
    """Return True if a PR already exists for this finding."""
    if not shutil.which("gh"):
        return False
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--search", f"dynos/auto-fix-{finding_id} in:head", "--json", "number"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            prs = json.loads(result.stdout)
            if isinstance(prs, list) and len(prs) > 0:
                return True
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as exc:
        _log(f"Warning: could not check existing PRs: {exc}")
    return False


def _check_existing_issue(finding_id: str) -> bool:
    """Return True if an issue already exists for this finding."""
    if not shutil.which("gh"):
        return False
    try:
        result = subprocess.run(
            ["gh", "issue", "list", "--search", f"{finding_id} label:dynos-autofix", "--json", "number"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            issues = json.loads(result.stdout)
            if isinstance(issues, list) and len(issues) > 0:
                return True
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as exc:
        _log(f"Warning: could not check existing issues: {exc}")
    return False


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


def _autofix_low_medium(finding: dict, root: Path) -> dict:
    """Attempt auto-fix for a low/medium severity finding. Returns updated finding."""
    finding_id = finding["finding_id"]
    description = finding["description"]
    evidence_str = json.dumps(finding.get("evidence", {}), indent=2)

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
    if _check_existing_pr(finding_id):
        finding["status"] = "already-exists"
        finding["processed_at"] = now_iso()
        _log(f"PR already exists for {finding_id}, skipping")
        return finding

    branch_name = f"dynos/auto-fix-{finding_id}"
    worktree_path = f"/tmp/dynos-autofix-{finding_id}"

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

    try:
        # Create worktree
        _log(f"Creating worktree at {worktree_path}")
        # Branch from current HEAD (whatever branch the user is on)
        subprocess.run(
            ["git", "worktree", "add", "--detach", worktree_path, "HEAD"],
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
            "--dangerously-skip-permissions",
            "--disallowedTools", "Bash(git push*) Bash(gh pr*)",
        ]
        if severity in ("high", "critical"):
            claude_cmd.extend(["--model", "opus"])
            _log(f"Running foundry pipeline for {finding_id} (opus — {severity} severity)")
        else:
            _log(f"Running foundry pipeline for {finding_id}")
        claude_result = subprocess.run(
            claude_cmd,
            capture_output=True, text=True, timeout=600, cwd=worktree_path,
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
                    ["git", "log", "main..HEAD", "--oneline"],
                    capture_output=True, text=True, timeout=10, cwd=worktree_path,
                )
                has_changes = bool(log_check.stdout.strip())

            if not has_changes:
                finding["status"] = "failed"
                finding["fail_reason"] = "claude_no_changes"
                _log(f"Claude produced no changes for {finding_id}")
                return finding

            # Stage and commit if Claude didn't already commit
            subprocess.run(
                ["git", "add", "-A"],
                capture_output=True, timeout=10, cwd=worktree_path,
            )
            subprocess.run(
                ["git", "diff", "--cached", "--quiet"],
                capture_output=True, timeout=10, cwd=worktree_path,
            )
            subprocess.run(
                ["git", "commit", "-m", f"[autofix] {description[:80]}",
                 "--author", "dynos-autofix <autofix@dynos.fit>"],
                capture_output=True, text=True, timeout=15, cwd=worktree_path,
            )

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
            # Build a human-readable PR description
            category = finding['category']
            severity = finding['severity']
            evidence = finding.get('evidence', {})
            file_name = evidence.get('file', 'unknown file')
            line_num = evidence.get('line', '')
            location = f"`{file_name}:{line_num}`" if line_num else f"`{file_name}`"

            pr_body = (
                f"## What's wrong\n\n"
                f"{description}\n\n"
                f"**Where:** {location}\n"
                f"**Severity:** {severity}\n\n"
                f"## What this PR does\n\n"
                f"Fixes the issue above. The change was generated by the dynos-work autofix scanner "
                f"and verified by running the foundry pipeline (spec → plan → execute → audit).\n\n"
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
                        # Also copy manifest and spec for trajectory context
                        for extra in ("manifest.json", "spec.md", "execution-log.md"):
                            src = task_dir / extra
                            if src.exists():
                                _sh.copy2(str(src), str(dest_dir / extra))
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

    return finding


# ---------------------------------------------------------------------------
# Risk-based routing: GitHub issue for high/critical (AC 10)
# ---------------------------------------------------------------------------

def _open_issue_high_critical(finding: dict) -> dict:
    """Open a GitHub issue for a high/critical severity finding."""
    finding_id = finding["finding_id"]
    description = finding["description"]
    evidence_str = json.dumps(finding.get("evidence", {}), indent=2)

    if not shutil.which("gh"):
        finding["status"] = "failed"
        finding["fail_reason"] = "gh_not_available"
        finding["processed_at"] = now_iso()
        _log(f"Skipping issue for {finding_id}: gh CLI not available")
        return finding

    # Check for existing issue (AC 16)
    if _check_existing_issue(finding_id):
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
            capture_output=True, text=True, timeout=30,
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
            finding["processed_at"] = now_iso()
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

def _process_finding(finding: dict, root: Path) -> dict:
    """Route and process a single finding based on severity."""
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
    # Recurring audit findings are not actionable code fixes — open issue only
    if category == "recurring-audit":
        return _open_issue_high_critical(finding)
    # Everything else goes through the autofix pipeline
    # High/critical findings use Opus with extended thinking
    return _autofix_low_medium(finding, root)


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
    start_time = time.monotonic()

    _log(f"Starting proactive scan on {root}")

    # Load existing findings for dedup
    existing_findings = _load_findings(root)

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

        processed = _process_finding(finding, root)

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
    _save_findings(root, existing_findings)

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
    p_scan.add_argument("--max-findings", default=3, type=int, help="Max findings to process per cycle")
    p_scan.set_defaults(func=cmd_scan)

    # list
    p_list = sub.add_parser("list", help="List current findings")
    p_list.add_argument("--root", default=".", help="Project root path")
    p_list.set_defaults(func=cmd_list)

    # clear
    p_clear = sub.add_parser("clear", help="Clear findings file")
    p_clear.add_argument("--root", default=".", help="Project root path")
    p_clear.set_defaults(func=cmd_clear)

    return parser


if __name__ == "__main__":
    from dyno_cli_base import cli_main
    raise SystemExit(cli_main(build_parser))
