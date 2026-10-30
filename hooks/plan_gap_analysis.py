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
import ast
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from lib_core import _UNTRACKED_SKIP_DIRS, write_json

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
    # Explicit table names: SQLAlchemy __tablename__, Django Meta.db_table
    (re.compile(r"""__tablename__\s*=\s*['"](\w+)['"]"""), "sqlalchemy-tablename"),
    (re.compile(r"""db_table\s*=\s*['"](\w+)['"]"""), "django-db-table"),
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

# Tool caches, virtualenvs and build output. A repo's ``backend/.venv``
# alone holds thousands of .py files, enough to spend the whole scan
# budget before reaching the project's own models.
_SKIP_DIRS = set(_UNTRACKED_SKIP_DIRS) | {"vendor"}


def _walk_dirs(dirpath: str, dirnames: list[str]) -> list[str]:
    """Subdirectories of ``dirpath`` worth scanning: not a skip dir and not
    a virtualenv under any name (one is marked by ``pyvenv.cfg``)."""
    return [
        d for d in dirnames
        if d not in _SKIP_DIRS and not os.path.exists(os.path.join(dirpath, d, "pyvenv.cfg"))
    ]


# ---------------------------------------------------------------------------
# Markdown table parser
# ---------------------------------------------------------------------------

def _parse_single_table(table_lines: list[str]) -> list[dict[str, str]]:
    """Parse a single contiguous block of markdown table lines into row dicts.

    table_lines must include the header row, separator row, and data rows.
    Returns empty list if fewer than 3 lines or header is empty.
    """
    if len(table_lines) < 3:
        return []

    headers = [h.strip() for h in table_lines[0].strip("|").split("|")]
    if not headers:
        return []

    # Skip separator (line 1)
    rows: list[dict[str, str]] = []
    for line in table_lines[2:]:
        cells = [c.strip() for c in line.strip("|").split("|")]
        n_headers = len(headers)
        n_cells = len(cells)
        if n_cells == n_headers:
            rows.append(dict(zip(headers, cells)))
        elif n_cells > n_headers:
            # Overflow: join trailing cells into the last column
            merged = cells[: n_headers - 1] + ["|".join(cells[n_headers - 1 :])]
            rows.append(dict(zip(headers, merged)))
        elif n_cells < n_headers:
            # Pad with empty strings for missing columns
            padded = cells + [""] * (n_headers - n_cells)
            rows.append(dict(zip(headers, padded)))

    return rows


def parse_table_rows(section_text: str) -> list[dict[str, str]]:
    """Parse ALL markdown tables in a section into a unified list of row dicts.

    Accumulates every pipe-delimited table block in the section (including
    tables separated by prose), parses each independently, and unions the
    rows. Short rows (fewer cells than headers) are padded with empty strings;
    overflow rows (more cells than headers) have trailing cells merged into
    the last column. No row is silently discarded.

    Returns empty list if no table found or all tables are malformed.
    """
    lines = [ln.strip() for ln in section_text.splitlines()]
    all_rows: list[dict[str, str]] = []
    current_block: list[str] = []

    for line in lines:
        if line.startswith("|") and line.endswith("|"):
            current_block.append(line)
        else:
            if current_block:
                all_rows.extend(_parse_single_table(current_block))
                current_block = []
            # Non-table line: continue accumulating for next block

    # Flush any trailing block
    if current_block:
        all_rows.extend(_parse_single_table(current_block))

    return all_rows


def parse_markdown_table(section_text: str) -> list[dict[str, str]]:
    """Parse markdown tables from a plan section into list of row dicts.

    Delegates to parse_table_rows which handles multiple tables, padded
    short rows, and merged overflow rows.

    Returns empty list if no table found or all tables are malformed.
    """
    return parse_table_rows(section_text)


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

_SCAN_FILE_LIMIT = 2000


def _scan_code_files(
    root: Path, extensions: set[str], limit: int = _SCAN_FILE_LIMIT
) -> tuple[list[Path], bool]:
    """Source files under ``root`` (skip dirs excluded), at most ``limit``.

    Returns ``(files, truncated)``; ``truncated`` is True when at least one
    more matching file exists beyond the cap, so a "not found" verdict
    drawn from ``files`` is not conclusive.
    """
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = _walk_dirs(dirpath, dirnames)
        for name in filenames:
            if Path(name).suffix in extensions:
                if len(files) >= limit:
                    return files, True
                files.append(Path(dirpath) / name)
    return files, False


def _iter_code_files(root: Path, extensions: set[str], limit: int = _SCAN_FILE_LIMIT) -> list[Path]:
    """Iterate source files, respecting skip dirs and file limit."""
    return _scan_code_files(root, extensions, limit)[0]


def _scan_status(truncated: bool, scanned: int) -> dict[str, Any]:
    """Scan-coverage fields for an analysis result.

    When the repo walk stopped at the file cap, a claim the scanned files
    do not define may still be defined in a file the walk never reached.
    Such claims are reported as ``unverified_inconclusive`` (not
    ``unverified``) with a warning: they do not fail the plan, because
    sending a plan back to PLANNING over a definition the scanner never
    looked for is the failure this check exists to avoid.
    """
    status: dict[str, Any] = {
        "scan_truncated": truncated,
        "files_scanned": scanned,
        "scan_limit": _SCAN_FILE_LIMIT,
    }
    if truncated:
        status["warnings"] = [
            f"gap analysis scanned only the first {scanned} source files "
            f"(limit {_SCAN_FILE_LIMIT}); claims not found in them are "
            "reported as unverified_inconclusive, not as failures"
        ]
    return status


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
    root: Path, plan_paths: set[str], extensions: set[str], limit: int = 2000
) -> list[Path]:
    """Resolve the plan-implied paths against the repo, expanding directories.

    For each plan-mentioned path:
    - if it's a file with a matching extension, include it.
    - if it's a directory, walk its contents up to 2 levels deep (prevents
      unbounded traversal when a plan mentions a large directory).
    """
    files: list[Path] = []
    seen: set[Path] = set()
    root_resolved = root.resolve()
    for rel in plan_paths:
        target = (root / rel).resolve()
        try:
            target.relative_to(root_resolved)
        except ValueError:
            continue  # outside repo
        if target.is_file() and target.suffix in extensions:
            if target not in seen:
                seen.add(target)
                files.append(target)
                if len(files) >= limit:
                    return files
        elif target.is_dir():
            for dirpath, dirnames, filenames in os.walk(target):
                depth = len(Path(dirpath).relative_to(target).parts)
                if depth >= 2:
                    dirnames[:] = []  # don't recurse deeper than 2 levels
                else:
                    dirnames[:] = _walk_dirs(dirpath, dirnames)
                for name in filenames:
                    p = Path(dirpath) / name
                    if p.suffix in extensions and p not in seen:
                        seen.add(p)
                        files.append(p)
                        if len(files) >= limit:
                            return files
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

    Strips backticks, collapses path params like :id / {id} / [id] /
    <int:pk> to a canonical placeholder, and lowercases.
    """
    path = path.strip("`").strip()
    # Collapse param variants
    path = re.sub(r"<[^>]+>", ":param", path)
    path = re.sub(r":\w+", ":param", path)
    path = re.sub(r"\{[^}]+\}", ":param", path)
    path = re.sub(r"\[[^\]]+\]", ":param", path)
    return path.lower().rstrip("/")


_DJANGO_ROUTE_RE = re.compile(
    r"""\b(?:re_)?path\s*\(\s*r?['"]([^'"]*)['"]\s*,\s*"""
    r"""(include\s*\(\s*\(?\s*(?:['"]([\w.]+)['"]|([A-Za-z_][\w.]*)))?"""
)
# Path-like tokens in an endpoint cell: "/a, /b" or "`/a` and `/b`".
_CELL_PATH_RE = re.compile(r"/[^\s`,;|]*")
# Separators between several paths in one cell: "`/a` / `/b`", "/a, /b".
_CELL_SPLIT_RE = re.compile(r"\s+/\s+|[,;]")
# A Python file worth parsing for Django URL structure.
_DJANGO_URLS_HINT_RE = re.compile(r"\b(?:re_)?path\s*\(|\burlpatterns\b|\binclude\s*\(")


def _cell_endpoints(value: str) -> list[str]:
    """Every path in an endpoint cell; the cell itself when it holds none.

    A cell may list alternatives as "`/a` / `/b`" or "/a, /b"; it is split
    on those separators first so the separating " / " is not read as the
    root path. A bare "/" counts only when it is the cell's sole path.
    """
    text = value.strip().strip("`").strip()
    paths: list[str] = []
    for piece in _CELL_SPLIT_RE.split(value):
        paths.extend(p.strip("`") for p in _CELL_PATH_RE.findall(piece))
    real = [p for p in paths if p != "/"]
    if real:
        return real
    if paths:
        return ["/"]
    return [text] if text else []


def _module_name(fpath: Path, root: Path) -> str:
    try:
        rel = fpath.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        rel = Path(fpath.name)
    parts = rel.with_suffix("").parts
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _dotted(expr: ast.AST) -> str:
    """``a.b.c`` for a Name/Attribute chain; "" for anything else."""
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        base = _dotted(expr.value)
        return f"{base}.{expr.attr}" if base else ""
    return ""


def _call_name(call: ast.AST) -> str:
    func = getattr(call, "func", None)
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


class _DjangoUrlModule:
    """Django URL structure of one module.

    Routes are grouped per URL *list* (a module-level variable such as
    ``urlpatterns`` or ``workspace_urlpatterns``), not per module, because
    ``include(workspace_urlpatterns)`` mounts one list of a module under a
    prefix that differs from the one its ``urlpatterns`` gets.
    """

    def __init__(self, module: str, is_package: bool) -> None:
        self.module = module
        self.is_package = is_package
        # list name -> relative routes defined in it
        self.routes: dict[str, list[str]] = {}
        self.absolute: list[str] = []
        # (container list, prefix, kind, reference); kind is "module" (a
        # dotted module path from a string include), "name" (a name or
        # dotted name in this module's namespace) or "local" (a list of
        # this module, e.g. an inline ``include([...])``).
        self.mounts: list[tuple[str, str, str, str]] = []
        self.lists: set[str] = set()
        # local alias -> (module path, imported attribute or None)
        self.imports: dict[str, tuple[str, str | None]] = {}
        self._inline = 0

    def _abs_module(self, level: int, name: str | None) -> str:
        if level == 0:
            return name or ""
        parts = self.module.split(".") if self.module else []
        if not self.is_package:
            parts = parts[:-1]
        drop = level - 1
        parts = parts[: len(parts) - drop] if drop <= len(parts) else []
        if name:
            parts = parts + name.split(".")
        return ".".join(parts)

    # -- AST walk ---------------------------------------------------------

    def visit(self, tree: ast.Module) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                base = self._abs_module(node.level, node.module)
                for alias in node.names:
                    self.imports[alias.asname or alias.name] = (base, alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        self.imports[alias.asname] = (alias.name, None)
                    else:
                        head = alias.name.split(".")[0]
                        self.imports.setdefault(head, (head, None))
        for stmt in tree.body:
            self._visit_stmt(stmt, "urlpatterns")

    def _visit_stmt(self, stmt: ast.stmt, container: str) -> None:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return
        if isinstance(stmt, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            if stmt.value is None:
                return
            if not names:
                self._visit_expr(stmt.value, container)
                return
            self.lists.update(names)
            self._visit_expr(stmt.value, names[0], top=True)
            for extra in names[1:]:
                self.mounts.append((extra, "", "local", names[0]))
            return
        if isinstance(stmt, ast.Expr):
            call = stmt.value
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr in ("extend", "append", "insert")
                and isinstance(call.func.value, ast.Name)
            ):
                target = call.func.value.id
                self.lists.add(target)
                for arg in call.args:
                    self._visit_expr(arg, target, top=call.func.attr == "extend")
                return
            self._visit_expr(call, container)
            return
        # if / try / for / with: their bodies still build module-level lists.
        for child in ast.iter_child_nodes(stmt):
            if isinstance(child, ast.stmt):
                self._visit_stmt(child, container)
            elif isinstance(child, ast.excepthandler):
                for sub in child.body:
                    self._visit_stmt(sub, container)

    def _visit_expr(self, expr: ast.AST, container: str, top: bool = False) -> None:
        if isinstance(expr, ast.Call) and self._is_path_call(expr):
            self._record_path(expr, container)
            return
        if top:
            # ``urlpatterns = other`` / ``+= other.urls`` / ``[*other, ...]``
            ref = _dotted(expr)
            if ref:
                self.mounts.append((container, "", "name", ref))
                return
            if isinstance(expr, ast.BinOp):
                self._visit_expr(expr.left, container, top=True)
                self._visit_expr(expr.right, container, top=True)
                return
            if isinstance(expr, (ast.List, ast.Tuple)):
                for elt in expr.elts:
                    if isinstance(elt, ast.Starred):
                        self._visit_expr(elt.value, container, top=True)
                    else:
                        self._visit_expr(elt, container)
                return
            if isinstance(expr, ast.Call):
                # ``urlpatterns = format_suffix_patterns(api_patterns)``
                for arg in expr.args:
                    self._visit_expr(arg, container, top=True)
                return
        for child in ast.iter_child_nodes(expr):
            self._visit_expr(child, container)

    @staticmethod
    def _is_path_call(call: ast.Call) -> bool:
        return (
            _call_name(call) in ("path", "re_path")
            and bool(call.args)
            and isinstance(call.args[0], ast.Constant)
            and isinstance(call.args[0].value, str)
        )

    def _record_path(self, call: ast.Call, container: str) -> None:
        route = call.args[0].value
        include: ast.AST | None = None
        if len(call.args) > 1:
            second = call.args[1]
            if isinstance(second, ast.Call) and _call_name(second) == "include" and second.args:
                include = second.args[0]
        if include is None:
            if route.startswith("/"):
                self.absolute.append(route)
            else:
                self.routes.setdefault(container, []).append(route.lstrip("^").rstrip("$"))
            return
        prefix = route.lstrip("^")
        # include((patterns, "app_name")) mounts its first element.
        if isinstance(include, ast.Tuple) and include.elts:
            include = include.elts[0]
        if isinstance(include, ast.Constant) and isinstance(include.value, str):
            self.mounts.append((container, prefix, "module", include.value))
        elif isinstance(include, (ast.List, ast.Tuple)):
            self._inline += 1
            key = f"<inline:{self._inline}>"
            self.lists.add(key)
            self.mounts.append((container, prefix, "local", key))
            for elt in include.elts:
                self._visit_expr(elt, key)
        else:
            ref = _dotted(include)
            if ref:
                self.mounts.append((container, prefix, "name", ref))

    # -- regex fallback for a file that does not parse ---------------------

    def visit_regex(self, content: str) -> None:
        self.lists.add("urlpatterns")
        for match in _DJANGO_ROUTE_RE.finditer(content):
            route, has_include, by_string, by_name = match.groups()
            if has_include:
                kind, ref = ("module", by_string) if by_string else ("name", by_name)
                self.mounts.append(("urlpatterns", route.lstrip("^"), kind, ref))
            elif route.startswith("/"):
                self.absolute.append(route)
            else:
                self.routes.setdefault("urlpatterns", []).append(route.lstrip("^").rstrip("$"))


def _scan_routes(files: list[Path], root: Path) -> tuple[set[str], set[str]]:
    """Return ``(routes, unmounted)`` defined in ``files``, normalized.

    ``routes`` are absolute. Django ``path()`` routes are relative and
    mounted through ``include()`` under a parent prefix; mounting is
    resolved per URL list. ``include("pkg.urls")`` mounts that module's
    ``urlpatterns``; ``include(name)`` follows the name to a list in the
    same module or through an import (``from .x import workspace_urls``,
    ``from app import urls``); ``include((patterns, "app"))`` tuples and
    inline ``include([...])`` lists are followed too. A relative route
    whose list nothing scanned mounts keeps no known mount point and lands
    in ``unmounted``; a claim may match it by suffix.
    """
    found: set[str] = set()
    modules: dict[str, _DjangoUrlModule] = {}
    for fpath in files:
        content = _read_safe(fpath)
        if not content:
            continue
        for pattern, kind in _ROUTE_PATTERNS:
            if kind == "django-urls":
                continue
            for match in pattern.finditer(content):
                for g in match.groups():
                    if g and g.startswith("/"):
                        found.add(_normalize_path(g))
        if fpath.suffix != ".py" or not _DJANGO_URLS_HINT_RE.search(content):
            continue
        name = _module_name(fpath, root)
        is_package = fpath.name == "__init__.py"
        mod = _DjangoUrlModule(name, is_package)
        try:
            mod.visit(ast.parse(content))
        except (SyntaxError, ValueError, RecursionError):
            mod = _DjangoUrlModule(name, is_package)
            mod.visit_regex(content)
        modules[name] = mod
        for route in mod.absolute:
            found.add(_normalize_path(route))

    def find_modules(target: str) -> list[str]:
        if not target:
            return []
        if target in modules:
            return [target]
        return [m for m in modules if m.endswith("." + target)]

    def resolve_dotted(path: str, depth: int) -> list[tuple[str, str]]:
        """A dotted module path, or module path + attribute, to list keys."""
        mods = find_modules(path)
        if mods:
            return [(m, "urlpatterns") for m in mods]
        if "." in path:
            base, attr = path.rsplit(".", 1)
            return [k for m in find_modules(base) for k in resolve_name(m, attr, depth + 1)]
        return []

    def resolve_name(module: str, ref: str, depth: int = 0) -> list[tuple[str, str]]:
        """A name in ``module``'s namespace to list keys, following imports."""
        if depth > 8 or module not in modules:
            return []
        mod = modules[module]
        head, _, rest = ref.partition(".")
        if not rest and head in mod.lists:
            return [(module, head)]
        if head in mod.imports:
            base, attr = mod.imports[head]
            full = base if attr is None else (f"{base}.{attr}" if base else attr)
            return resolve_dotted(f"{full}.{rest}" if rest else full, depth)
        return resolve_dotted(ref, depth) if rest else []

    # Mount graph: child list -> [(parent list, prefix)].
    parents: dict[tuple[str, str], list[tuple[tuple[str, str], str]]] = {}
    for module, mod in modules.items():
        for container, prefix, kind, ref in mod.mounts:
            if kind == "module":
                targets = resolve_dotted(ref, 0)
            elif kind == "local":
                targets = [(module, ref)]
            else:
                targets = resolve_name(module, ref)
            for target in targets:
                parents.setdefault(target, []).append(((module, container), prefix))

    def _prefixes(key: tuple[str, str], seen: frozenset[tuple[str, str]]) -> list[str] | None:
        entries = parents.get(key)
        if not entries:
            return None
        out: list[str] = []
        for parent, route in entries:
            if parent in seen:
                continue
            above = _prefixes(parent, seen | {parent})
            out.extend(prefix + route for prefix in (above if above is not None else [""]))
        return out

    unmounted: set[str] = set()
    for module, mod in modules.items():
        for container, routes in mod.routes.items():
            key = (module, container)
            prefixes = _prefixes(key, frozenset({key}))
            for route in routes:
                if prefixes is None:
                    if route:
                        unmounted.add(_normalize_path("/" + route))
                    continue
                for prefix in prefixes:
                    full = prefix + route
                    found.add(_normalize_path(full if full.startswith("/") else "/" + full))
    return found, unmounted


def _route_matches(claim: str, routes: set[str], unmounted: set[str]) -> bool:
    """Segment-level match: same segment count, non-param segments equal.

    Suffix matches are allowed in both directions, on segment boundaries
    only: an unmounted relative route may match the tail of a claim, and a
    claim written relative to its mount point (``/attachments/initiate``)
    may match the tail of a fully mounted route. Either way the shorter
    side must pin at least one literal segment equal to the aligned
    segment of the longer side, so a bare ``/:id`` matches nothing by
    suffix and ``/initiate`` does not match ``/reinitiate``.
    """
    norm = _normalize_path(claim)
    norm_parts = [p for p in norm.split("/") if p]

    def _same(a: list[str], b: list[str]) -> bool:
        return len(a) == len(b) and all(x == y or x == ":param" or y == ":param" for x, y in zip(a, b))

    def _tail(longer: list[str], shorter: list[str]) -> bool:
        if not shorter or len(shorter) > len(longer):
            return False
        tail = longer[len(longer) - len(shorter):]
        return _same(tail, shorter) and any(s == t != ":param" for s, t in zip(shorter, tail))

    for fr in routes:
        fr_parts = [p for p in fr.split("/") if p]
        if norm == fr or _same(norm_parts, fr_parts):
            return True
        if len(norm_parts) < len(fr_parts) and _tail(fr_parts, norm_parts):
            return True
    for fr in unmounted:
        if _tail(norm_parts, [p for p in fr.split("/") if p]):
            return True
    return False


def analyze_api_contracts(
    plan_text: str,
    root: Path,
    *,
    _precomputed_files: list[Path] | None = None,
    _scan_truncated: bool = False,
) -> dict[str, Any]:
    """Check API Contracts section claims against actual route definitions.

    Rows marked ``to-be-created`` (in any cell) are reported separately and
    exempted from the codebase match, as in the Data Model check: a new
    endpoint cannot exist until its execution segment runs. A cell naming
    several paths is several endpoints. When the repo walk hit its file
    cap, unmatched claims are reported as ``unverified_inconclusive``
    rather than ``unverified``; see ``_scan_status``.
    """
    section = extract_section(plan_text, "API Contracts")
    if not section.strip():
        return {"skipped": True, "reason": "no API Contracts section"}

    rows = parse_markdown_table(section)
    if not rows:
        return {"skipped": True, "reason": "no parseable table in API Contracts section"}

    # Extract claimed endpoints
    claimed: list[dict[str, str]] = []
    to_be_created: list[dict[str, str]] = []
    for row in rows:
        endpoints: list[str] = []
        method = ""
        for key, val in row.items():
            k = key.lower().strip()
            if k in ("endpoint", "path", "url", "route"):
                endpoints = _cell_endpoints(val)
            elif k in ("method", "http method", "verb"):
                method = val.strip("`").strip().upper()
        target = to_be_created if any("to-be-created" in value.lower() for value in row.values()) else claimed
        target.extend({"endpoint": endpoint, "method": method} for endpoint in endpoints)

    if not claimed and not to_be_created:
        return {"skipped": True, "reason": "no endpoints found in API Contracts table"}
    if not claimed:
        return {
            "skipped": False,
            "claimed_endpoints": 0,
            "to_be_created": to_be_created,
            "verified": 0,
            "unverified": [],
            "routes_found_in_code": 0,
        }

    # Two-pass scan: try plan-implied paths first (cheap), fall back to the
    # full repo walk only if at least one claim was unmatched. The full-scan
    # behavior is preserved as a strict superset — same correctness, less
    # work in the common case.
    plan_paths = _extract_plan_paths(plan_text)

    found_routes: set[str] = set()
    unmounted: set[str] = set()
    narrow_files: list[Path] = []
    truncated = False
    full_scan = False
    if plan_paths:
        narrow_files = _iter_plan_implied_files(root, plan_paths, _CODE_EXTENSIONS)
        found_routes, unmounted = _scan_routes(narrow_files, root)

    if not narrow_files or not all(_route_matches(c["endpoint"], found_routes, unmounted) for c in claimed):
        full_scan = True
        if _precomputed_files is not None:
            code_files = [f for f in _precomputed_files if f.suffix in _CODE_EXTENSIONS]
            truncated = _scan_truncated
        else:
            code_files, truncated = _scan_code_files(root, _CODE_EXTENSIONS)
        found_routes, unmounted = _scan_routes(code_files, root)

    claimed_found = [c for c in claimed if _route_matches(c["endpoint"], found_routes, unmounted)]
    claimed_not_found = [c for c in claimed if c not in claimed_found]

    result: dict[str, Any] = {
        "skipped": False,
        "claimed_endpoints": len(claimed),
        "to_be_created": to_be_created,
        "verified": len(claimed_found),
        "unverified": claimed_not_found,
        "routes_found_in_code": len(found_routes) + len(unmounted),
    }
    if full_scan:
        result.update(_scan_status(truncated, len(code_files)))
        if truncated:
            result["unverified_inconclusive"] = claimed_not_found
            result["unverified"] = []
    return result


# ---------------------------------------------------------------------------
# Data Model gap analysis
# ---------------------------------------------------------------------------

_MODEL_NAME_RE = re.compile(r"[A-Za-z_][\w.]*")
_LEADING_NAME_RE = re.compile(r"[*_~\s]*([A-Za-z_][\w.]*)")
# Bases that make a Python class a model: Django / SQLAlchemy / auth bases.
_MODEL_BASE_RE = re.compile(r"Model|Base|AbstractUser|AbstractBaseUser")
# Cheap pre-check before parsing a .py file for model classes.
_MODEL_HINT_RE = re.compile(r"Model|Base|AbstractUser|class\s+Meta\b|Field\s*\(|Key\s*\(")
_TO_BE_CREATED = "to-be-created"
_EXISTING_MODIFIED = "existing-modified"


def _cell_model_name(value: str) -> str:
    """The model or table a Data Model cell names, lowercased.

    The name is the cell's leading identifier, last dotted part:
    "Conversation (add `triage_state`)" names ``conversation`` (not the
    backticked column), "`Organization` (existing, app/models.py)" names
    ``organization``, "models.Membership" names ``membership``. A cell that
    does not start with an identifier falls back to its first backticked
    token; a placeholder ("—") names nothing.
    """
    text = value.strip()
    ticked = re.match(r"[*_~\s]*`([^`]+)`", text)
    if ticked:
        match = _MODEL_NAME_RE.search(ticked.group(1))
        name = match.group(0) if match else ""
    else:
        match = _LEADING_NAME_RE.match(text)
        if match:
            name = match.group(1)
        else:
            ticked = re.search(r"`([^`]+)`", text)
            match = _MODEL_NAME_RE.search(ticked.group(1)) if ticked else None
            name = match.group(0) if match else ""
    return name.rstrip(".").rsplit(".", 1)[-1].lower()


def _django_app_label(path: Path) -> str:
    """The Django app a models file belongs to: the directory holding
    ``models.py``, or holding the ``models/`` package."""
    if path.name == "models.py":
        return path.parent.name
    if path.parent.name == "models":
        return path.parent.parent.name
    return ""


def _is_model_field(value: ast.AST, django: bool) -> bool:
    """``models.CharField(...)`` / ``models.ForeignKey(...)`` (or a bare
    ``CharField(...)`` in a file that imports Django)."""
    if not isinstance(value, ast.Call):
        return False
    func = value.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "models":
        return func.attr.endswith(("Field", "Key"))
    if django and isinstance(func, ast.Name):
        return func.id.endswith("Field") or func.id == "ForeignKey"
    return False


def _python_model_names(path: Path, content: str) -> set[str]:
    """Model class names (and Django default table names) in a .py file.

    A class is a model when its bases mention Model / Base / AbstractUser /
    AbstractBaseUser, or its body declares ``class Meta`` or Django model
    fields. For a model in a Django app's models module the default table
    name ``<app_label>_<modelname>`` is reported too (``app_label`` from
    ``Meta.app_label`` when set, else the app directory); abstract models
    have no table.
    """
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError, RecursionError):
        return set()
    django = "django" in content
    app_label = _django_app_label(path) if django else ""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name == "Meta":
            continue
        bases = " ".join(_dotted(b) or "" for b in node.bases)
        meta = next((s for s in node.body if isinstance(s, ast.ClassDef) and s.name == "Meta"), None)
        has_fields = any(
            isinstance(s, (ast.Assign, ast.AnnAssign)) and s.value is not None and _is_model_field(s.value, django)
            for s in node.body
        )
        if not (_MODEL_BASE_RE.search(bases) or meta is not None or has_fields):
            continue
        model = node.name.lower()
        names.add(model)
        if not django:
            continue
        label, abstract = app_label, False
        for stmt in meta.body if meta is not None else []:
            if not isinstance(stmt, ast.Assign) or not isinstance(stmt.value, ast.Constant):
                continue
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id == "app_label" and isinstance(stmt.value.value, str):
                    label = stmt.value.value
                elif isinstance(target, ast.Name) and target.id == "abstract" and stmt.value.value is True:
                    abstract = True
        if label and not abstract:
            names.add(f"{label.lower()}_{model}")
    return names


def _scan_model_names(files: list[Path]) -> set[str]:
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
        if fpath.suffix == ".py" and "class " in content and _MODEL_HINT_RE.search(content):
            found |= _python_model_names(fpath, content)
    return found


def analyze_data_model(
    plan_text: str,
    root: Path,
    *,
    _precomputed_files: list[Path] | None = None,
    _scan_truncated: bool = False,
) -> dict[str, Any]:
    """Check existing Data Model claims against schema/model definitions.

    A model declared ``to-be-created`` in any row is reported separately
    and exempted from the codebase match in every row that names it,
    because its execution segment has not run yet. A row marked
    ``existing-modified`` names a model that must already exist (its new
    columns are not checked), even if another cell mentions
    ``to-be-created``. When the repo walk hit its file cap, unmatched
    claims are reported as ``unverified_inconclusive`` rather than
    ``unverified``; see ``_scan_status``.
    """
    section = extract_section(plan_text, "Data Model")
    if not section.strip():
        return {"skipped": True, "reason": "no Data Model section"}

    rows = parse_markdown_table(section)
    if not rows:
        return {"skipped": True, "reason": "no parseable table in Data Model section"}

    # Extract claimed tables
    claimed_tables: set[str] = set()
    to_be_created_tables: set[str] = set()
    existing_modified: set[str] = set()
    for row in rows:
        names = [
            name
            for key, val in row.items()
            if key.lower().strip() in ("table", "model", "collection", "entity", "schema")
            for name in [_cell_model_name(val)]
            if name
        ]
        markers = " ".join(row.values()).lower()
        if _EXISTING_MODIFIED in markers:
            existing_modified.update(names)
            claimed_tables.update(names)
        # Greenfield schemas cannot exist in the repository until their
        # execution segment runs. Honor the same explicit marker the plan
        # validation instructions require for planned files. The marker may
        # appear in the table/model cell or another cell in the row.
        elif _TO_BE_CREATED in markers:
            to_be_created_tables.update(names)
        else:
            claimed_tables.update(names)
    # A model declared new anywhere is new in every row that names it.
    claimed_tables -= to_be_created_tables
    existing_modified -= to_be_created_tables

    if not claimed_tables and not to_be_created_tables:
        return {"skipped": True, "reason": "no tables found in Data Model table"}
    if not claimed_tables:
        return {
            "skipped": False,
            "claimed_tables": [],
            "to_be_created": sorted(to_be_created_tables),
            "existing_modified": [],
            "verified": [],
            "unverified": [],
            "models_found_in_code": 0,
        }

    # Two-pass scan: plan-implied paths first, full repo only on fallback.
    plan_paths = _extract_plan_paths(plan_text)

    found_models: set[str] = set()
    narrow_files: list[Path] = []
    truncated = False
    full_scan = False
    if plan_paths:
        narrow_files = _iter_plan_implied_files(
            root, plan_paths, _CODE_EXTENSIONS | _MIGRATION_EXTENSIONS
        )
        found_models = _scan_model_names(narrow_files)

    if not narrow_files or not claimed_tables.issubset(found_models):
        full_scan = True
        if _precomputed_files is not None:
            all_files, truncated = _precomputed_files, _scan_truncated
        else:
            all_files, truncated = _scan_code_files(root, _CODE_EXTENSIONS | _MIGRATION_EXTENSIONS)
        found_models = _scan_model_names(all_files)

    # Cross-reference
    verified = claimed_tables & found_models
    unverified = claimed_tables - found_models

    result: dict[str, Any] = {
        "skipped": False,
        "claimed_tables": sorted(claimed_tables),
        "to_be_created": sorted(to_be_created_tables),
        "existing_modified": sorted(existing_modified),
        "verified": sorted(verified),
        "unverified": sorted(unverified),
        "models_found_in_code": len(found_models),
    }
    if full_scan:
        result.update(_scan_status(truncated, len(all_files)))
        if truncated:
            result["unverified_inconclusive"] = result["unverified"]
            result["unverified"] = []
    return result


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
        write_json(cache_path, {"fingerprint": fingerprint, "report": report})
    except OSError:
        pass  # Cache failure must not break gap analysis


def run_gap_analysis(root: Path, task_dir: Path) -> dict[str, Any]:
    """Run full gap analysis on plan.md. Returns structured report.

    Cached per-task by a fingerprint over (plan.md content, top-level repo
    dir mtimes). The same plan + same repo state produces the same report,
    so the three per-task call sites (planning, plan-audit, execute
    preflight) only pay the ~2000-file walk cost once. Set the env var
    DYNOS_GAP_CACHE=0 to disable caching for debugging.

    When the walk stops at the file cap, the report carries
    ``scan_truncated``/``files_scanned``/``scan_limit``/``warnings`` and
    each analysis moves its not-found claims to ``unverified_inconclusive``
    (see ``_scan_status``).
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

    # Single traversal shared by both analyses; covers the superset of extensions.
    shared_files, truncated = _scan_code_files(root, _CODE_EXTENSIONS | _MIGRATION_EXTENSIONS)
    report: dict[str, Any] = {
        "api_contracts": analyze_api_contracts(
            plan_text, root, _precomputed_files=shared_files, _scan_truncated=truncated
        ),
        "data_model": analyze_data_model(
            plan_text, root, _precomputed_files=shared_files, _scan_truncated=truncated
        ),
    }
    if truncated:
        report.update(_scan_status(truncated, len(shared_files)))

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
    parser.add_argument("--json", action="store_true", help="Include structured validation errors")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    task_dir = Path(args.task_dir).resolve()

    report = run_gap_analysis(root, task_dir)
    if args.json:
        report["errors"] = [
            {
                "artifact": "plan.md",
                "rule": "plan_gap_" + re.sub(r"[^a-z0-9]+", "_", error.lower()).strip("_")[:48],
                "message": error,
                "location": None,
            }
            for error in findings_from_report(report)
        ]
        report["ok"] = not report["errors"]
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
