#!/usr/bin/env python3
"""Deterministic performance pattern detection for the performance-auditor.

Statically scans source files for common performance anti-patterns.
Works for ANY project type — detects patterns across Python, JS/TS, Go, Java, Ruby.

Usage:
    python3 hooks/performance_check.py --root <project-root> --changed-files file1.py,file2.ts

Outputs JSON to stdout with detected patterns and file:line locations.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".rb", ".java",
    ".kt", ".swift", ".cs", ".php", ".ex", ".exs", ".dart",
}

SKIP_DIRS = {".git", "node_modules", ".dynos", "__pycache__", "dist", "build", "vendor"}

# Files only scan for N+1 / unbounded-query patterns if they import a real DB
# driver or ORM. Without this gate the detector fires on `dict.get()` and
# similar in-memory field access, which is the dominant pattern in
# JSON-processing codebases.
_DB_IMPORT_RE = re.compile(
    r"\b(?:import|from)\s+("
    r"sqlite3|psycopg2|psycopg|pymongo|sqlalchemy|MySQLdb|mysql\.connector|"
    r"asyncpg|aiomysql|aiosqlite|motor|peewee|tortoise|databases|redis|"
    r"django\.db|flask_sqlalchemy|google\.cloud\.(?:firestore|bigquery|datastore|spanner)"
    r")\b"
)


def _file_uses_db(content: str) -> bool:
    """Return True when the file imports a known DB driver or ORM."""
    return bool(_DB_IMPORT_RE.search(content))


# ---------------------------------------------------------------------------
# Pattern detectors
# ---------------------------------------------------------------------------

def _detect_n_plus_one(content: str, filepath: str) -> list[dict[str, Any]]:
    """Detect N+1 query patterns: DB calls inside loops.

    Only fires when (a) the file imports a known DB driver/ORM, AND
    (b) the call has a receiver name that looks like a DB handle. This
    avoids flagging `dict.get()` and similar in-memory access.
    """
    if not _file_uses_db(content):
        return []

    findings: list[dict[str, Any]] = []
    lines = content.splitlines()
    in_loop = False
    loop_start = 0

    # Match <db-handle>.<query-method>( — receiver must be DB-shaped.
    db_call_re = re.compile(
        r"\b(cursor|conn|connection|session|db|database|client|query|queryset|"
        r"repo|repository|objects|Model|Entity)\.(query|execute|find|findOne|"
        r"findAll|filter|select|fetch|where|raw|all|get|first|last|count|"
        r"exists)\s*\(",
        re.IGNORECASE,
    )
    loop_re = re.compile(r"^\s*(for |while |\.forEach|\.map\(|\.each\b)")

    for i, line in enumerate(lines, 1):
        if loop_re.search(line):
            in_loop = True
            loop_start = i
        elif in_loop and line.strip() and not line.strip().startswith(("#", "//", "*")):
            indent_match = re.match(r"^(\s*)", line)
            loop_indent_match = re.match(r"^(\s*)", lines[loop_start - 1]) if loop_start > 0 else None
            if indent_match and loop_indent_match:
                if len(indent_match.group(1)) <= len(loop_indent_match.group(1)) and line.strip():
                    in_loop = False

        if in_loop and db_call_re.search(line):
            findings.append({
                "pattern": "n_plus_one",
                "location": f"{filepath}:{i}",
                "line": line.strip()[:120],
                "severity": "major",
                "description": f"Database/query call inside loop (loop starts at line {loop_start})",
            })

    return findings


def _detect_missing_timeout(content: str, filepath: str) -> list[dict[str, Any]]:
    """Detect HTTP/subprocess/DB calls without timeout parameters."""
    findings: list[dict[str, Any]] = []
    lines = content.splitlines()

    # Patterns that should have timeout
    call_patterns = [
        (re.compile(r"requests\.(get|post|put|patch|delete)\s*\("), "requests"),
        (re.compile(r"fetch\s*\("), "fetch"),
        (re.compile(r"axios\.(get|post|put|patch|delete)\s*\("), "axios"),
        (re.compile(r"subprocess\.run\s*\("), "subprocess.run"),
        (re.compile(r"subprocess\.Popen\s*\("), "subprocess.Popen"),
        (re.compile(r"http\.(?:Get|Post|Do)\s*\("), "go-http"),
    ]

    for i, line in enumerate(lines, 1):
        for pattern, name in call_patterns:
            if pattern.search(line) and "timeout" not in line.lower():
                # Check next 3 lines for timeout param
                context = "\n".join(lines[i - 1:min(i + 3, len(lines))])
                if "timeout" not in context.lower():
                    findings.append({
                        "pattern": "missing_timeout",
                        "location": f"{filepath}:{i}",
                        "line": line.strip()[:120],
                        "severity": "major",
                        "description": f"{name} call without timeout parameter",
                    })
    return findings


def _detect_unbounded_query(content: str, filepath: str) -> list[dict[str, Any]]:
    """Detect queries that return all rows without LIMIT/pagination.

    Only fires when the file imports a known DB driver/ORM — the
    `.all()`/`.findAll()` pattern otherwise matches list operations.
    """
    if not _file_uses_db(content):
        # Allow raw SQL strings in any file — those are unambiguous.
        findings: list[dict[str, Any]] = []
        select_re = re.compile(r"SELECT\s+.*\s+FROM\s+", re.IGNORECASE)
        for i, line in enumerate(content.splitlines(), 1):
            if select_re.search(line) and "LIMIT" not in line.upper() and "COUNT" not in line.upper():
                findings.append({
                    "pattern": "unbounded_query",
                    "location": f"{filepath}:{i}",
                    "line": line.strip()[:120],
                    "severity": "major",
                    "description": "Query without LIMIT — may return unbounded rows",
                })
        return findings

    findings: list[dict[str, Any]] = []
    lines = content.splitlines()

    # SELECT without LIMIT
    select_re = re.compile(r"SELECT\s+.*\s+FROM\s+", re.IGNORECASE)
    find_all_re = re.compile(r"\.(findAll|find_all|all|objects\.all|select)\s*\(\s*\)", re.IGNORECASE)

    for i, line in enumerate(lines, 1):
        if select_re.search(line) and "LIMIT" not in line.upper() and "COUNT" not in line.upper():
            context = "\n".join(lines[i - 1:min(i + 2, len(lines))])
            if "LIMIT" not in context.upper() and "limit" not in context.lower():
                findings.append({
                    "pattern": "unbounded_query",
                    "location": f"{filepath}:{i}",
                    "line": line.strip()[:120],
                    "severity": "major",
                    "description": "Query without LIMIT — may return unbounded rows",
                })
        if find_all_re.search(line):
            context = "\n".join(lines[i - 1:min(i + 3, len(lines))])
            if "limit" not in context.lower() and "paginate" not in context.lower() and "[::" not in context:
                findings.append({
                    "pattern": "unbounded_query",
                    "location": f"{filepath}:{i}",
                    "line": line.strip()[:120],
                    "severity": "major",
                    "description": "Query returning all rows without pagination",
                })

    return findings


def _detect_quadratic(content: str, filepath: str) -> list[dict[str, Any]]:
    """Detect O(n^2) nested loop patterns via an indentation stack.

    Tracks each open loop as (indent, line_number). Before considering
    a new line, pops entries whose indent is >= the current line's indent
    (i.e. the loop body has ended). A loop is "nested" only when the
    stack is non-empty at the moment of match.

    JS array methods (.forEach / .map / .filter / .some / .every) are not
    counted as loops — they are method calls, not syntactic blocks.
    Sequential chains like `.filter(...).map(...)` are flat, not nested.
    If a callback inside such a chain contains a real for/while, the
    stack-based detector catches that on its own.
    """
    findings: list[dict[str, Any]] = []
    lines = content.splitlines()
    loop_re = re.compile(r"^\s*(for |while )")

    loop_stack: list[tuple[int, int]] = []  # (indent, line_number)

    for i, line in enumerate(lines, 1):
        stripped = line.lstrip()
        if not stripped or stripped.startswith(("#", "//", "*", "/*")):
            continue
        indent = len(line) - len(stripped)

        # Pop any loops whose body has ended (indent no longer inside them).
        while loop_stack and indent <= loop_stack[-1][0]:
            loop_stack.pop()

        if loop_re.match(line):
            depth = len(loop_stack) + 1
            if depth >= 2:
                findings.append({
                    "pattern": "quadratic_loop",
                    "location": f"{filepath}:{i}",
                    "line": line.strip()[:120],
                    "severity": "major",
                    "description": f"Nested loop (depth {depth}) — potential O(n^2) or worse. Outer loop at line {loop_stack[0][1]}",
                })
            loop_stack.append((indent, i))

    return findings


def _detect_missing_connection_cleanup(content: str, filepath: str) -> list[dict[str, Any]]:
    """Detect database/HTTP connections opened without close/cleanup."""
    findings: list[dict[str, Any]] = []
    lines = content.splitlines()

    open_patterns = [
        (re.compile(r"(connect|createConnection|createPool|open)\s*\("), "close"),
        (re.compile(r"(psycopg2\.connect|sqlite3\.connect|pymongo\.MongoClient)\s*\("), "close"),
    ]

    full = content.lower()
    for pattern, close_word in open_patterns:
        for i, line in enumerate(lines, 1):
            if pattern.search(line):
                # Check if there's a corresponding close/with/finally in the same function scope
                scope = "\n".join(lines[i - 1:min(i + 30, len(lines))])
                if (close_word not in scope.lower() and "with " not in scope.lower() and
                        "finally" not in scope.lower() and "context" not in scope.lower()):
                    findings.append({
                        "pattern": "missing_connection_cleanup",
                        "location": f"{filepath}:{i}",
                        "line": line.strip()[:120],
                        "severity": "critical",
                        "description": "Connection opened without visible close/cleanup in scope",
                    })
    return findings


# ---------------------------------------------------------------------------
# Main scanner
# ---------------------------------------------------------------------------

def scan_files(root: Path, changed_files: list[str] | None = None) -> dict[str, Any]:
    """Scan files for performance anti-patterns."""
    targets: list[Path] = []
    if changed_files:
        for f in changed_files:
            p = root / f
            if p.exists() and p.suffix in CODE_EXTENSIONS:
                targets.append(p)
    else:
        count = 0
        for f in root.rglob("*"):
            if any(d in f.parts for d in SKIP_DIRS):
                continue
            if f.suffix in CODE_EXTENSIONS and f.is_file():
                targets.append(f)
                count += 1
                if count >= 500:
                    break

    all_findings: list[dict[str, Any]] = []
    detectors = [
        _detect_n_plus_one,
        _detect_missing_timeout,
        _detect_unbounded_query,
        _detect_quadratic,
        _detect_missing_connection_cleanup,
    ]

    for fpath in targets:
        try:
            content = fpath.read_text(errors="ignore")
        except (OSError, PermissionError):
            continue
        rel = str(fpath.relative_to(root)) if fpath.is_relative_to(root) else str(fpath)
        for detector in detectors:
            all_findings.extend(detector(content, rel))

    return {
        "files_scanned": len(targets),
        "findings": all_findings,
        "patterns_checked": ["n_plus_one", "missing_timeout", "unbounded_query", "quadratic_loop", "missing_connection_cleanup"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Deterministic performance pattern detection")
    parser.add_argument("--root", required=True, help="Project root directory")
    parser.add_argument("--changed-files", help="Comma-separated list of changed files")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    changed = args.changed_files.split(",") if args.changed_files else None
    result = scan_files(root, changed)
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
