"""
code_evidence — turn the files, line references and identifiers a bug report
names into citable source evidence.

The investigator agent may only cite evidence IDs minted in the dossier and
cannot read source on its own, so the dossier must carry the code the bug
report talks about. This module resolves:

    * mentioned files   -> file records (path, size, line count, a short head)
    * file:line[-end]   -> snippets around the referenced lines
    * identifiers       -> symbol definitions (file, line range, bounded source)

Public API:
    collect(repo_path, mentioned_files, mentioned_symbols, bug_text,
            extra_files=None) -> dict
        Returns {"files": [...], "snippets": [...], "symbols": [...],
                 "resolved_paths": [...], "stats": {...}}

Symbol lookup order:
    1. definitions inside the mentioned files (any identifier);
    2. repo-wide, only for distinctive identifiers (contain "_" or a
       camelCase hump, length >= 6) that were not found in step 1.

Python definitions come from ``ast`` (exact line ranges, including
module-level and class-level constant assignments). Other languages use
per-language definition regexes with brace / indentation based end
detection. Everything is bounded by per-entry and total budgets so the
dossier size tracks the relevant evidence, not the repo size.

No network, no third-party dependencies. Never raises for unreadable files.
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------

MAX_SYMBOL_LINES = 120          # source lines kept per symbol definition
MAX_HEAD_LINES = 30             # lines of file head per mentioned file
SNIPPET_CONTEXT = 15            # lines of context around a file:line ref
MAX_SNIPPET_LINES = 120         # cap on an explicit file:start-end range
MAX_DEFS_PER_SYMBOL = 3         # repo-wide definitions kept per symbol
MAX_SYMBOL_ENTRIES = 150        # total symbol evidence entries
MAX_TOTAL_SOURCE_CHARS = 600_000  # total source text across all entries
MAX_FILE_BYTES = 2_000_000      # files larger than this are not parsed
MAX_REPO_FILES = 20_000         # bound for the repo-wide file listing

_SOURCE_EXTS: frozenset[str] = frozenset({
    "py", "pyi", "js", "jsx", "mjs", "cjs", "ts", "tsx", "go", "rs", "java",
    "kt", "kts", "rb", "dart", "c", "cc", "cpp", "h", "hpp", "cs", "php",
    "swift", "scala",
})

_SKIP_DIRS: frozenset[str] = frozenset({
    ".git", ".hg", ".svn", "node_modules", "vendor", "dist", "build",
    "target", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".tox", ".idea", ".vscode", ".next", ".nuxt", ".cache", ".dynos",
    ".claude", "worktrees",
})

_CONST_NAME_RE = re.compile(r"^_*[A-Z][A-Z0-9_]*$")

_KEBAB_RE = re.compile(r"(?<![\w/.-])([a-z][a-z0-9]*(?:-[a-z0-9]+){1,6})(?![\w/.-])")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_distinctive(symbol: str) -> bool:
    """True when an identifier is specific enough to search repo-wide."""
    name = symbol.rsplit(".", 1)[-1]
    if len(name) < 6:
        return False
    if "_" in name.strip("_"):
        return True
    return bool(re.search(r"[a-z0-9][A-Z]", name))


def _norm_rel(path: str, repo: Path) -> str:
    """Normalise a path (absolute or relative) to a repo-relative posix path."""
    p = path.strip()
    if not p:
        return ""
    if os.path.isabs(p):
        try:
            p = os.path.relpath(p, str(repo))
        except ValueError:
            return p.replace(os.sep, "/")
    p = p.replace(os.sep, "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def normalize_path(path: Any, repo_path: str) -> str:
    """Public wrapper used by triage to compare finding paths."""
    if not isinstance(path, str):
        return ""
    return _norm_rel(path, Path(repo_path).resolve())


def _list_repo_files(repo: Path) -> list[str]:
    """Repo-relative paths of tracked + untracked-not-ignored files.

    Falls back to a bounded os.walk when git is unavailable.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "-co", "--exclude-standard"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            files = [
                ln for ln in proc.stdout.splitlines()
                if ln and not any(part in _SKIP_DIRS for part in ln.split("/")[:-1])
            ]
            return files[:MAX_REPO_FILES]
    except (OSError, subprocess.SubprocessError):
        pass
    out: list[str] = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
        for f in sorted(files):
            out.append(_norm_rel(os.path.join(root, f), repo))
            if len(out) >= MAX_REPO_FILES:
                return out
    return out


def _read_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _ext(path: str) -> str:
    return path.rsplit(".", 1)[-1].lower() if "." in path else ""


def _numbered(lines: list[str], start: int, end: int) -> str:
    """Render lines[start-1:end] with 1-based line-number prefixes."""
    width = len(str(end))
    return "\n".join(
        f"{n:>{width}}| {lines[n - 1]}" for n in range(start, end + 1)
    )


# ---------------------------------------------------------------------------
# Definition finding
# ---------------------------------------------------------------------------

def _python_defs(text: str) -> dict[str, list[tuple[str, int, int]]]:
    """Map name -> [(kind, start, end)] for a Python source.

    Both bare names and qualified names ("Class.method") are keys.
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return {}
    defs: dict[str, list[tuple[str, int, int]]] = {}

    def add(name: str, kind: str, node: ast.AST) -> None:
        start = getattr(node, "lineno", None)
        if start is None:
            return
        decorators = getattr(node, "decorator_list", None) or []
        if decorators:
            start = min([start] + [d.lineno for d in decorators])
        end = getattr(node, "end_lineno", None) or start
        defs.setdefault(name, []).append((kind, start, end))

    def visit(body: Iterable[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "method" if prefix else "function"
                add(node.name, kind, node)
                if prefix:
                    add(f"{prefix}{node.name}", kind, node)
            elif isinstance(node, ast.ClassDef):
                add(node.name, "class", node)
                if prefix:
                    add(f"{prefix}{node.name}", "class", node)
                visit(node.body, f"{prefix}{node.name}.")
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name):
                        add(t.id, "assignment", node)
                        if prefix:
                            add(f"{prefix}{t.id}", "assignment", node)
            elif isinstance(node, (ast.If, ast.Try)) and not prefix:
                # Module-level conditional definitions (try/except imports,
                # `if TYPE_CHECKING:` blocks, platform switches).
                visit(getattr(node, "body", []), prefix)
                visit(getattr(node, "orelse", []), prefix)

    visit(tree.body, "")

    # Function-local UPPER_CASE constants ("MAX_RETRIES = 3" inside a
    # function): bug reports name them as if they were module constants.
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if (
                    isinstance(t, ast.Name)
                    and _CONST_NAME_RE.match(t.id)
                    and t.id not in defs
                ):
                    add(t.id, f"local_constant in {fn.name}", node)
    return defs


def _regex_def_patterns(name: str, ext: str) -> list[re.Pattern[str]]:
    n = re.escape(name)
    if ext in {"js", "jsx", "mjs", "cjs", "ts", "tsx"}:
        return [
            re.compile(rf"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*{n}\b"),
            re.compile(rf"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+{n}\b"),
            re.compile(rf"^\s*(?:export\s+)?(?:const|let|var)\s+{n}\b"),
            re.compile(rf"^\s*(?:export\s+)?(?:interface|type|enum)\s+{n}\b"),
            re.compile(rf"^\s*(?:(?:public|private|protected|static|async|readonly)\s+)*{n}\s*\([^;]*\)\s*(?::[^={{]*)?\{{"),
        ]
    if ext == "go":
        return [
            re.compile(rf"^func\s+(?:\([^)]*\)\s*)?{n}\s*[\[(]"),
            re.compile(rf"^\s*type\s+{n}\b"),
            re.compile(rf"^\s*(?:const|var)\s+{n}\b"),
        ]
    if ext == "rs":
        return [
            re.compile(rf"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+{n}\b"),
            re.compile(rf"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|type|const|static)\s+{n}\b"),
        ]
    if ext == "rb":
        return [
            re.compile(rf"^\s*def\s+(?:self\.)?{n}\b"),
            re.compile(rf"^\s*(?:class|module)\s+{n}\b"),
            re.compile(rf"^\s*{n}\s*="),
        ]
    if ext in {"java", "kt", "kts", "cs", "scala", "dart", "swift", "php", "c", "cc", "cpp", "h", "hpp"}:
        return [
            re.compile(rf"^\s*(?:[\w<>\[\],?@]+\s+)*(?:class|interface|enum|record|struct|object|trait)\s+{n}\b"),
            re.compile(rf"^\s*(?:(?:fun|func|function|def)\s+){n}\s*[(<]"),
            re.compile(rf"^\s*(?:[\w<>\[\],?.*&@]+\s+)+\**{n}\s*\([^;]*$"),
        ]
    return []


def _brace_end(lines: list[str], start: int) -> int:
    """Return the 1-based end line of a brace-delimited block starting at
    `start` (1-based). Falls back to a bounded window."""
    depth = 0
    opened = False
    limit = min(len(lines), start + MAX_SYMBOL_LINES * 4)
    for i in range(start - 1, limit):
        line = lines[i]
        for ch in line:
            if ch == "{":
                depth += 1
                opened = True
            elif ch == "}":
                depth -= 1
        if opened and depth <= 0:
            return i + 1
        if not opened and i > start - 1 + 3:
            # No block opened within a few lines: single-line declaration.
            return start
        if not opened and line.rstrip().endswith(";"):
            return i + 1
    return min(len(lines), start + MAX_SYMBOL_LINES - 1)


def _ruby_end(lines: list[str], start: int) -> int:
    indent = len(lines[start - 1]) - len(lines[start - 1].lstrip())
    for i in range(start, min(len(lines), start + MAX_SYMBOL_LINES * 4)):
        stripped = lines[i].strip()
        cur = len(lines[i]) - len(lines[i].lstrip())
        if stripped == "end" and cur == indent:
            return i + 1
    return start


def _regex_defs(name: str, path: str, lines: list[str]) -> list[tuple[str, int, int]]:
    ext = _ext(path)
    patterns = _regex_def_patterns(name, ext)
    if not patterns:
        return []
    out: list[tuple[str, int, int]] = []
    for idx, line in enumerate(lines, start=1):
        if name not in line:
            continue
        if any(p.search(line) for p in patterns):
            end = _ruby_end(lines, idx) if ext == "rb" else _brace_end(lines, idx)
            out.append(("definition", idx, max(idx, end)))
    return out


class _FileCache:
    """Read + parse each file at most once."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self._text: dict[str, str | None] = {}
        self._lines: dict[str, list[str]] = {}
        self._pydefs: dict[str, dict[str, list[tuple[str, int, int]]]] = {}

    def text(self, rel: str) -> str | None:
        if rel not in self._text:
            self._text[rel] = _read_text(self.repo / rel)
        return self._text[rel]

    def lines(self, rel: str) -> list[str]:
        if rel not in self._lines:
            t = self.text(rel)
            self._lines[rel] = t.splitlines() if t is not None else []
        return self._lines[rel]

    def defs(self, rel: str, name: str) -> list[tuple[str, int, int]]:
        text = self.text(rel)
        if text is None:
            return []
        short = name.rsplit(".", 1)[-1]
        if short not in text:
            return []
        if _ext(rel) in {"py", "pyi"}:
            if rel not in self._pydefs:
                self._pydefs[rel] = _python_defs(text)
            table = self._pydefs[rel]
            return list(table.get(name, []))
        return _regex_defs(short, rel, self.lines(rel))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _resolve_file(raw: str, repo: Path, repo_files: list[str] | None) -> str | None:
    rel = _norm_rel(raw, repo)
    if not rel:
        return None
    candidate = repo / rel
    try:
        if candidate.is_file() and candidate.resolve().is_relative_to(repo):
            return rel
    except (OSError, ValueError):
        return None
    if repo_files is None:
        return None
    # Suffix match ("lib_qlearn.py" -> "memory/lib_qlearn.py"). Only accept
    # an unambiguous match.
    suffix = "/" + rel.lstrip("/")
    matches = [f for f in repo_files if f == rel or f.endswith(suffix)]
    return matches[0] if len(matches) == 1 else None


def _kebab_command_candidates(bug_text: str) -> list[tuple[str, str]]:
    """Derive ``cmd_<snake>`` handler names from kebab-case CLI subcommands
    in the text ("clear-role" -> "cmd_clear_role")."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in _KEBAB_RE.finditer(bug_text or ""):
        token = m.group(1)
        name = "cmd_" + token.replace("-", "_")
        if name not in seen:
            seen.add(name)
            out.append((name, token))
    return out


def collect(
    repo_path: str,
    mentioned_files: list[dict[str, Any]] | None,
    mentioned_symbols: list[str] | None,
    bug_text: str = "",
    extra_files: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build file, snippet and symbol-definition evidence payloads."""
    repo = Path(repo_path).resolve()
    stats: dict[str, Any] = {
        "files_mentioned": 0,
        "files_resolved": 0,
        "files_unresolved": [],
        "symbols_considered": 0,
        "symbols_resolved": 0,
        "symbols_unresolved": [],
        "truncated": False,
    }
    result: dict[str, Any] = {
        "files": [], "snippets": [], "symbols": [], "resolved_paths": [],
        "stats": stats,
    }
    if not repo.is_dir():
        return result

    refs: list[dict[str, Any]] = []
    for item in list(mentioned_files or []) + list(extra_files or []):
        if isinstance(item, str):
            refs.append({"path": item, "line": None, "end_line": None})
        elif isinstance(item, dict):
            p = item.get("path") or item.get("file")
            if isinstance(p, str) and p:
                refs.append({
                    "path": p,
                    "line": item.get("line") if isinstance(item.get("line"), int) else None,
                    "end_line": item.get("end_line") if isinstance(item.get("end_line"), int) else None,
                })

    repo_files: list[str] | None = None
    cache = _FileCache(repo)
    budget = {"chars": MAX_TOTAL_SOURCE_CHARS}

    def spend(text: str) -> bool:
        if len(text) > budget["chars"]:
            stats["truncated"] = True
            return False
        budget["chars"] -= len(text)
        return True

    # --- files + line-reference snippets ---------------------------------
    resolved: list[str] = []
    seen_raw: set[str] = set()
    seen_snippet: set[tuple[str, int, int]] = set()
    for ref in refs:
        raw = ref["path"]
        if raw not in seen_raw:
            stats["files_mentioned"] += 1
        rel = _resolve_file(raw, repo, None)
        if rel is None:
            if repo_files is None:
                repo_files = _list_repo_files(repo)
            rel = _resolve_file(raw, repo, repo_files)
        if rel is None:
            if raw not in seen_raw and raw not in stats["files_unresolved"]:
                stats["files_unresolved"].append(raw)
            seen_raw.add(raw)
            continue
        seen_raw.add(raw)
        lines = cache.lines(rel)
        if rel not in resolved:
            resolved.append(rel)
            try:
                size = (repo / rel).stat().st_size
            except OSError:
                size = None
            head_end = min(len(lines), MAX_HEAD_LINES)
            head = _numbered(lines, 1, head_end) if head_end else ""
            if not spend(head):
                head = ""
            result["files"].append({
                "path": rel,
                "mentioned_as": raw,
                "size_bytes": size,
                "line_count": len(lines),
                "head": {"start_line": 1, "end_line": head_end, "text": head},
            })
        line = ref.get("line")
        if isinstance(line, int) and lines:
            end_ref = ref.get("end_line") if isinstance(ref.get("end_line"), int) else None
            if end_ref is not None and end_ref >= line:
                start = max(1, line)
                end = min(len(lines), end_ref, start + MAX_SNIPPET_LINES - 1)
            else:
                start = max(1, line - SNIPPET_CONTEXT)
                end = min(len(lines), line + SNIPPET_CONTEXT)
            if start > len(lines):
                continue
            key = (rel, start, end)
            if key in seen_snippet:
                continue
            seen_snippet.add(key)
            text = _numbered(lines, start, end)
            if not spend(text):
                continue
            result["snippets"].append({
                "path": rel,
                "referenced_line": line,
                "referenced_end_line": end_ref,
                "start_line": start,
                "end_line": end,
                "source": text,
            })
    stats["files_resolved"] = len(resolved)
    result["resolved_paths"] = resolved

    # --- symbol definitions ----------------------------------------------
    candidates: list[tuple[str, str | None]] = []
    seen_sym: set[str] = set()
    for sym in mentioned_symbols or []:
        if isinstance(sym, str) and sym and sym not in seen_sym:
            seen_sym.add(sym)
            candidates.append((sym, None))
    for name, token in _kebab_command_candidates(bug_text):
        if name not in seen_sym:
            seen_sym.add(name)
            candidates.append((name, token))

    def symbol_entries(sym: str, files: list[str]) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for rel in files:
            for kind, start, end in cache.defs(rel, sym):
                lines = cache.lines(rel)
                shown_end = min(end, start + MAX_SYMBOL_LINES - 1, len(lines))
                found.append({
                    "symbol": sym,
                    "kind": kind,
                    "path": rel,
                    "start_line": start,
                    "end_line": end,
                    "source_end_line": shown_end,
                    "source_truncated": shown_end < end,
                    "_lines": (lines, start, shown_end),
                })
        return found

    source_files: list[str] | None = None
    for sym, derived_from in candidates:
        stats["symbols_considered"] += 1 if derived_from is None else 0
        defs = symbol_entries(sym, resolved)
        if not defs and "." in sym:
            # "module.func" / "obj.method" -> fall back to the last segment.
            defs = symbol_entries(sym.rsplit(".", 1)[-1], resolved)
        scope = "mentioned_file"
        if not defs and (is_distinctive(sym) or derived_from is not None):
            if source_files is None:
                if repo_files is None:
                    repo_files = _list_repo_files(repo)
                source_files = [f for f in repo_files if _ext(f) in _SOURCE_EXTS]
            defs = symbol_entries(sym, [f for f in source_files if f not in resolved])
            # Outside the mentioned files a lower-case module-level variable
            # ("task_dir = ...") is almost always an unrelated namesake; only
            # functions, classes and UPPER_CASE constants count there.
            if not _CONST_NAME_RE.match(sym.rsplit(".", 1)[-1]):
                defs = [
                    d for d in defs
                    if d["kind"] != "assignment"
                    and not d["kind"].startswith("local_constant")
                ]
            # Prefer non-test definitions, then shorter paths.
            defs.sort(key=lambda d: ("test" in d["path"].lower(), d["path"].count("/"), d["path"]))
            defs = defs[:MAX_DEFS_PER_SYMBOL]
            scope = "repo"
        if not defs:
            if derived_from is None:
                stats["symbols_unresolved"].append(sym)
            continue
        if derived_from is None:
            stats["symbols_resolved"] += 1
        for d in defs:
            if len(result["symbols"]) >= MAX_SYMBOL_ENTRIES:
                stats["truncated"] = True
                break
            lines, start, shown_end = d.pop("_lines")
            text = _numbered(lines, start, shown_end)
            if not spend(text):
                continue
            d["source"] = text
            d["resolved_in"] = scope
            if derived_from is not None:
                d["derived_from"] = derived_from
            result["symbols"].append(d)

    return result


__all__ = ["collect", "is_distinctive", "normalize_path"]
