#!/usr/bin/env python3
"""Deterministic gap analysis for plan.md conditional sections.

Validates that claims in API Contracts and Data Model sections correspond to
real code in the project.  Catches the "trust-me-LLM-bro" problem: a planner
can write a beautiful API table that describes endpoints that don't exist, or
a schema table referencing tables that were never created.

Usage:
    python3 hooks/plan_gap_analysis.py --root <project-root> --task-dir <.dynos/task-id>

Returns JSON to stdout with gap findings. Exit code 0 = ran successfully
(even if gaps found); exit code 1 = couldn't parse plan.

Works for ANY project type — route/model detection adapts to ecosystem.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Route pattern detection — covers major frameworks across ecosystems
# ---------------------------------------------------------------------------

# Each pattern is (compiled regex, description).  The regex should capture the
# HTTP method and path where possible, but at minimum should match lines that
# define routes.

_ROUTE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Express / Koa / Fastify / Hono  (JS/TS)
    (re.compile(r"""(?:app|router|server)\s*\.\s*(get|post|put|patch|delete|head|options)\s*\(\s*['"`]([^'"`]+)['"`]""", re.IGNORECASE), "js-framework"),
    # Next.js / Nuxt / SvelteKit file-based routing
    (re.compile(r"""export\s+(?:async\s+)?function\s+(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b""", re.IGNORECASE), "nextjs-route-handler"),
    # Flask / FastAPI / Starlette (Python)
    (re.compile(r"""@(?:app|router|blueprint)\s*\.\s*(get|post|put|patch|delete|head|options|route)\s*\(\s*['"]([^'"]+)['"]""", re.IGNORECASE), "python-framework"),
    # Django urls.py
    (re.compile(r"""path\s*\(\s*['"]([^'"]+)['"]"""), "django-urls"),
    # Spring Boot (Java/Kotlin)
    (re.compile(r"""@(GetMapping|PostMapping|PutMapping|PatchMapping|DeleteMapping|RequestMapping)\s*\(\s*(?:value\s*=\s*)?['"]([^'"]+)['"]""", re.IGNORECASE), "spring"),
    # Go net/http, gin, echo, chi
    (re.compile(r"""\.(?:GET|POST|PUT|PATCH|DELETE|Handle|HandleFunc)\s*\(\s*['"`]([^'"`]+)['"`]"""), "go-framework"),
    # Ruby on Rails routes.rb
    (re.compile(r"""(?:get|post|put|patch|delete|resources|resource)\s+['"]([^'"]+)['"]"""), "rails-routes"),
    # Phoenix (Elixir)
    (re.compile(r"""(?:get|post|put|patch|delete)\s+['"]([^'"]+)['"]"""), "phoenix-routes"),
    # ASP.NET (C#)
    (re.compile(r"""\[Http(Get|Post|Put|Patch|Delete)\s*\(\s*['"]([^'"]+)['"]""", re.IGNORECASE), "aspnet"),
    # Generic route-like patterns
    (re.compile(r"""['"`](/api/[^'"`\s]+)['"`]"""), "generic-api-path"),
]

# ---------------------------------------------------------------------------
# Model / schema pattern detection
# ---------------------------------------------------------------------------

_MODEL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # SQLAlchemy / Django ORM
    (re.compile(r"""class\s+(\w+)\s*\(.*(?:Model|Base|DeclarativeBase|db\.Model)"""), "python-orm"),
    # Django model field
    (re.compile(r"""(\w+)\s*=\s*models\.\w+Field"""), "django-field"),
    # SQLAlchemy column
    (re.compile(r"""(\w+)\s*=\s*(?:Column|mapped_column)\s*\("""), "sqlalchemy-column"),
    # ActiveRecord (Ruby)
    (re.compile(r"""class\s+(\w+)\s*<\s*(?:ApplicationRecord|ActiveRecord::Base)"""), "activerecord"),
    # Sequelize / TypeORM / Prisma (JS/TS)
    (re.compile(r"""@Entity\s*\(\s*\)?\s*(?:export\s+)?class\s+(\w+)"""), "typeorm-entity"),
    (re.compile(r"""model\s+(\w+)\s*\{"""), "prisma-model"),
    # Ecto (Elixir)
    (re.compile(r"""schema\s+['"](\w+)['"]"""), "ecto-schema"),
    # GORM (Go)
    (re.compile(r"""type\s+(\w+)\s+struct\s*\{"""), "go-struct"),
    # SQL migration files
    (re.compile(r"""CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`"']?(\w+)[`"']?""", re.IGNORECASE), "sql-create-table"),
    (re.compile(r"""ALTER\s+TABLE\s+[`"']?(\w+)[`"']?""", re.IGNORECASE), "sql-alter-table"),
    # Knex / Drizzle / generic migration
    (re.compile(r"""(?:createTable|table)\s*\(\s*['"](\w+)['"]"""), "js-migration"),
]

# File extensions and directories to scan.
_CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".rb", ".java",
    ".kt", ".swift", ".cs", ".php", ".ex", ".exs", ".dart", ".vue", ".svelte",
}

_MIGRATION_EXTENSIONS = {".sql", ".py", ".js", ".ts", ".rb", ".ex"}

_SKIP_DIRS = {".git", "node_modules", ".dynos", "__pycache__", "dist", "build", ".next", "vendor"}


# ---------------------------------------------------------------------------
# Markdown table parser
# ---------------------------------------------------------------------------

def parse_markdown_table(section_text: str) -> list[dict[str, str]]:
    """Parse a markdown table from a plan section into list of row dicts.

    Returns empty list if no table found or table is malformed.
    """
    lines = [l.strip() for l in section_text.splitlines() if l.strip()]
    table_lines: list[str] = []
    in_table = False

    for line in lines:
        if line.startswith("|") and line.endswith("|"):
            in_table = True
            table_lines.append(line)
        elif in_table:
            break  # End of table block

    if len(table_lines) < 3:  # header + separator + at least 1 row
        return []

    # Parse header
    headers = [h.strip() for h in table_lines[0].strip("|").split("|")]

    # Skip separator (line 1)
    rows: list[dict[str, str]] = []
    for line in table_lines[2:]:
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) == len(headers):
            rows.append(dict(zip(headers, cells)))

    return rows


def extract_section(plan_text: str, heading: str) -> str:
    """Extract text under a specific ## heading until the next ## heading."""
    pattern = re.compile(rf"^## {re.escape(heading)}\s*$", re.MULTILINE)
    match = pattern.search(plan_text)
    if not match:
        return ""
    start = match.end()
    next_heading = re.search(r"^## ", plan_text[start:], re.MULTILINE)
    if next_heading:
        return plan_text[start:start + next_heading.start()]
    return plan_text[start:]


# ---------------------------------------------------------------------------
# File scanning
# ---------------------------------------------------------------------------

def _iter_code_files(root: Path, extensions: set[str], limit: int = 2000) -> list[Path]:
    """Iterate source files, respecting skip dirs and file limit."""
    files: list[Path] = []
    for f in root.rglob("*"):
        if any(d in f.parts for d in _SKIP_DIRS):
            continue
        if f.suffix in extensions and f.is_file():
            files.append(f)
            if len(files) >= limit:
                break
    return files


def _extract_plan_paths(plan_text: str) -> set[str]:
    """Extract repo-relative file paths and directories named in the plan's
    Reference Code and Components/Modules sections.

    Used to narrow gap-analysis scans before falling back to the full repo
    walk. Pulls anything that looks like a file path inside backticks
    (`some/path.py`, `dir/`, etc.) from the relevant sections.
    """
    paths: set[str] = set()
    for section_name in ("Reference Code", "Components / Modules", "Data Model"):
        section = extract_section(plan_text, section_name)
        if not section:
            continue
        # Pull anything in backticks that contains a slash or has a known code extension.
        for match in re.finditer(r"`([^`\n]+)`", section):
            candidate = match.group(1).strip()
            # Filter to plausible paths: must contain a slash or a code extension
            if not ("/" in candidate or any(candidate.endswith(ext) for ext in _CODE_EXTENSIONS)):
                continue
            # Reject path traversal BEFORE any stripping (a leading "../"
            # must not be silently stripped to "etc/passwd").
            parts = candidate.split("/")
            if any(p == ".." for p in parts):
                continue
            # Strip exactly one leading "./" if present, plus trailing slash.
            if candidate.startswith("./"):
                candidate = candidate[2:]
            candidate = candidate.rstrip("/")
            if candidate:
                paths.add(candidate)
    return paths


def _iter_plan_implied_files(
    root: Path, plan_paths: set[str], extensions: set[str]
) -> list[Path]:
    """Resolve the plan-implied paths against the repo, expanding directories.

    For each plan-mentioned path:
    - if it's a file with a matching extension, include it.
    - if it's a directory, walk its contents (depth-1 by default — most plans
      reference modules, not deep trees).
    """
    files: list[Path] = []
    seen: set[Path] = set()
    for rel in plan_paths:
        target = (root / rel).resolve()
        try:
            target.relative_to(root.resolve())
        except ValueError:
            continue  # outside repo
        if target.is_file() and target.suffix in extensions:
            if target not in seen:
                seen.add(target)
                files.append(target)
        elif target.is_dir():
            for f in target.rglob("*"):
                if any(d in f.parts for d in _SKIP_DIRS):
                    continue
                if f.suffix in extensions and f.is_file() and f not in seen:
                    seen.add(f)
                    files.append(f)
    return files


def _read_safe(path: Path) -> str:
    """Read file, ignoring encoding errors."""
    try:
        return path.read_text(errors="ignore")
    except (OSError, PermissionError):
        return ""


# ---------------------------------------------------------------------------
# API Contracts gap analysis
# ---------------------------------------------------------------------------

def _normalize_path(path: str) -> str:
    """Normalize an API path for comparison.

    Strips backticks, collapses path params like :id / {id} / [id] to a
    canonical placeholder, and lowercases.
    """
    path = path.strip("`").strip()
    # Collapse param variants
    path = re.sub(r":\w+", ":param", path)
    path = re.sub(r"\{[^}]+\}", ":param", path)
    path = re.sub(r"\[[^\]]+\]", ":param", path)
    return path.lower().rstrip("/")


def analyze_api_contracts(plan_text: str, root: Path) -> dict[str, Any]:
    """Check API Contracts section claims against actual route definitions."""
    section = extract_section(plan_text, "API Contracts")
    if not section.strip():
        return {"skipped": True, "reason": "no API Contracts section"}

    rows = parse_markdown_table(section)
    if not rows:
        return {"skipped": True, "reason": "no parseable table in API Contracts section"}

    # Extract claimed endpoints
    claimed: list[dict[str, str]] = []
    for row in rows:
        endpoint = ""
        method = ""
        for key, val in row.items():
            k = key.lower().strip()
            if k in ("endpoint", "path", "url", "route"):
                endpoint = val.strip("`").strip()
            elif k in ("method", "http method", "verb"):
                method = val.strip("`").strip().upper()
        if endpoint:
            claimed.append({"endpoint": endpoint, "method": method})

    if not claimed:
        return {"skipped": True, "reason": "no endpoints found in API Contracts table"}

    # Two-pass scan: try plan-implied paths first (cheap), fall back to the
    # full repo walk only if at least one claim was unmatched. The full-scan
    # behavior is preserved as a strict superset — same correctness, less
    # work in the common case.
    plan_paths = _extract_plan_paths(plan_text)

    def _scan_files(files: list[Path]) -> set[str]:
        found: set[str] = set()
        for fpath in files:
            content = _read_safe(fpath)
            if not content:
                continue
            for pattern, _ in _ROUTE_PATTERNS:
                for match in pattern.finditer(content):
                    for g in match.groups():
                        if g and g.startswith("/"):
                            found.add(_normalize_path(g))
        return found

    found_routes: set[str] = set()
    narrow_files: list[Path] = []
    if plan_paths:
        narrow_files = _iter_plan_implied_files(root, plan_paths, _CODE_EXTENSIONS)
        found_routes = _scan_files(narrow_files)

    def _all_claims_matched(claimed_list: list[dict[str, str]], routes: set[str]) -> bool:
        for c in claimed_list:
            norm = _normalize_path(c["endpoint"])
            norm_parts = [p for p in norm.split("/") if p]
            matched = False
            for fr in routes:
                if norm == fr:
                    matched = True
                    break
                fr_parts = [p for p in fr.split("/") if p]
                if len(norm_parts) == len(fr_parts) and all(
                    np == fp or np == ":param" or fp == ":param"
                    for np, fp in zip(norm_parts, fr_parts)
                ):
                    matched = True
                    break
            if not matched:
                return False
        return True

    if not narrow_files or not _all_claims_matched(claimed, found_routes):
        # Fall back to full repo scan
        code_files = _iter_code_files(root, _CODE_EXTENSIONS)
        found_routes = _scan_files(code_files)

    # Cross-reference — match by path segments, not string prefix
    claimed_not_found: list[dict[str, str]] = []
    claimed_found: list[dict[str, str]] = []
    for c in claimed:
        norm = _normalize_path(c["endpoint"])
        norm_parts = [p for p in norm.split("/") if p]
        matched = False
        for fr in found_routes:
            if norm == fr:
                matched = True
                break
            # Segment-level match: same number of segments, non-param segments equal
            fr_parts = [p for p in fr.split("/") if p]
            if len(norm_parts) == len(fr_parts):
                if all(
                    np == fp or np == ":param" or fp == ":param"
                    for np, fp in zip(norm_parts, fr_parts)
                ):
                    matched = True
                    break
        if matched:
            claimed_found.append(c)
        else:
            claimed_not_found.append(c)

    return {
        "skipped": False,
        "claimed_endpoints": len(claimed),
        "verified": len(claimed_found),
        "unverified": claimed_not_found,
        "routes_found_in_code": len(found_routes),
    }


# ---------------------------------------------------------------------------
# Data Model gap analysis
# ---------------------------------------------------------------------------

def analyze_data_model(plan_text: str, root: Path) -> dict[str, Any]:
    """Check Data Model section claims against actual schema/model definitions."""
    section = extract_section(plan_text, "Data Model")
    if not section.strip():
        return {"skipped": True, "reason": "no Data Model section"}

    rows = parse_markdown_table(section)
    if not rows:
        return {"skipped": True, "reason": "no parseable table in Data Model section"}

    # Extract claimed tables
    claimed_tables: set[str] = set()
    for row in rows:
        for key, val in row.items():
            k = key.lower().strip()
            if k in ("table", "model", "collection", "entity", "schema"):
                name = val.strip("`").strip()
                if name and name != "—":
                    claimed_tables.add(name.lower())

    if not claimed_tables:
        return {"skipped": True, "reason": "no tables found in Data Model table"}

    # Two-pass scan: plan-implied paths first, full repo only on fallback.
    plan_paths = _extract_plan_paths(plan_text)

    def _scan_files(files: list[Path]) -> set[str]:
        found: set[str] = set()
        for fpath in files:
            content = _read_safe(fpath)
            if not content:
                continue
            for pattern, _ in _MODEL_PATTERNS:
                for match in pattern.finditer(content):
                    name = match.group(1)
                    if name:
                        found.add(name.lower())
        return found

    found_models: set[str] = set()
    narrow_files: list[Path] = []
    if plan_paths:
        narrow_files = _iter_plan_implied_files(
            root, plan_paths, _CODE_EXTENSIONS | _MIGRATION_EXTENSIONS
        )
        found_models = _scan_files(narrow_files)

    if not narrow_files or not claimed_tables.issubset(found_models):
        # Fall back to full repo scan
        all_files = _iter_code_files(root, _CODE_EXTENSIONS | _MIGRATION_EXTENSIONS)
        found_models = _scan_files(all_files)

    # Cross-reference
    verified = claimed_tables & found_models
    unverified = claimed_tables - found_models

    return {
        "skipped": False,
        "claimed_tables": sorted(claimed_tables),
        "verified": sorted(verified),
        "unverified": sorted(unverified),
        "models_found_in_code": len(found_models),
    }


# ---------------------------------------------------------------------------
# Unified gap report
# ---------------------------------------------------------------------------

def _compute_fingerprint(root: Path, plan_text: str) -> str:
    """Stable fingerprint over plan.md content + repo structure for caching.

    Plan content drives the WHAT (which endpoints/tables to verify); repo
    file mtimes drive the WHERE (which files to scan). If neither has
    changed since the last gap analysis, the result is reusable.

    We sample top-level directory mtimes rather than every file's mtime
    because rglob over 2000 files for fingerprinting would defeat the
    purpose. Top-level dir mtimes change on file add/remove, which is the
    only repo-level change that can invalidate a cached result. Edits to
    file CONTENT can also invalidate (a route definition changes), but
    capturing all file content hashes here would re-introduce the cost
    we're trying to avoid. The tradeoff: cache may be slightly stale on
    pure content edits within an unchanged file set; the staleness window
    is bounded by the next plan.md edit (which changes the fingerprint
    and forces a re-scan).
    """
    h = hashlib.sha256()
    h.update(plan_text.encode("utf-8", errors="ignore"))
    h.update(b"\x00")
    try:
        for entry in sorted(root.iterdir()):
            if entry.name in _SKIP_DIRS or entry.name.startswith("."):
                continue
            try:
                st = entry.stat()
                h.update(f"{entry.name}|{int(st.st_mtime)}|{st.st_size}\x00".encode("utf-8"))
            except OSError:
                continue
    except OSError:
        pass
    return h.hexdigest()


def _cache_path(task_dir: Path) -> Path:
    return task_dir / "gap-analysis-cache.json"


def _read_cache(task_dir: Path, fingerprint: str) -> dict[str, Any] | None:
    """Return cached report if fingerprint matches; None otherwise."""
    cache_path = _cache_path(task_dir)
    if not cache_path.exists():
        return None
    try:
        cached = json.loads(cache_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if cached.get("fingerprint") != fingerprint:
        return None
    report = cached.get("report")
    return report if isinstance(report, dict) else None


def _write_cache(task_dir: Path, fingerprint: str, report: dict[str, Any]) -> None:
    """Persist gap-analysis report keyed by fingerprint. Best-effort."""
    cache_path = _cache_path(task_dir)
    try:
        cache_path.write_text(json.dumps({
            "fingerprint": fingerprint,
            "report": report,
        }))
    except OSError:
        pass  # Cache failure must not break gap analysis


def run_gap_analysis(root: Path, task_dir: Path) -> dict[str, Any]:
    """Run full gap analysis on plan.md. Returns structured report.

    Cached per-task by a fingerprint over (plan.md content, top-level repo
    dir mtimes). The same plan + same repo state produces the same report,
    so the three per-task call sites (planning, plan-audit, execute
    preflight) only pay the ~2000-file walk cost once. Set the env var
    DYNOS_GAP_CACHE=0 to disable caching for debugging.
    """
    plan_path = task_dir / "plan.md"
    if not plan_path.exists():
        return {"error": "plan.md not found", "api_contracts": None, "data_model": None}

    plan_text = plan_path.read_text(errors="ignore")

    cache_enabled = os.environ.get("DYNOS_GAP_CACHE", "1") != "0"
    fingerprint = _compute_fingerprint(root, plan_text) if cache_enabled else ""
    if cache_enabled:
        cached = _read_cache(task_dir, fingerprint)
        if cached is not None:
            return cached

    report = {
        "api_contracts": analyze_api_contracts(plan_text, root),
        "data_model": analyze_data_model(plan_text, root),
    }

    if cache_enabled:
        _write_cache(task_dir, fingerprint, report)
    return report


def findings_from_report(report: dict[str, Any]) -> list[str]:
    """Convert a gap report into human-readable validation error strings.

    These are formatted to match validate_task_artifacts error conventions.
    """
    errors: list[str] = []

    api = report.get("api_contracts")
    if api and not api.get("skipped") and api.get("unverified"):
        for ep in api["unverified"]:
            method = ep.get("method", "?")
            endpoint = ep.get("endpoint", "?")
            errors.append(
                f"plan API Contracts: endpoint {method} {endpoint} not found in codebase "
                f"(claimed in plan but no matching route definition detected)"
            )

    dm = report.get("data_model")
    if dm and not dm.get("skipped") and dm.get("unverified"):
        for table in dm["unverified"]:
            errors.append(
                f"plan Data Model: table/model '{table}' not found in codebase "
                f"(claimed in plan but no matching model/schema/migration detected)"
            )

    return errors


# ---------------------------------------------------------------------------
# Main (CLI entry point)
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Plan gap analysis — verify plan claims against codebase")
    parser.add_argument("--root", required=True, help="Project root directory")
    parser.add_argument("--task-dir", required=True, help="Task directory (.dynos/task-{id})")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    task_dir = Path(args.task_dir).resolve()

    report = run_gap_analysis(root, task_dir)
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
