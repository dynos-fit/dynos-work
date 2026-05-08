#!/usr/bin/env python3
"""Deterministic rules engine for dynos prevention-rules.json.

Converts prevention rules from "advice injected into LLM prompts" into
"code-evaluated gates at commit time and stage-transition time."

Six enforcement templates plus a pass-through `advisory` template cover ~80%
of existing rule shapes:

- every_name_in_X_satisfies_Y — every element of a container in a module
  satisfies a predicate (callable / hasattr / in_registry)
- pattern_must_not_appear — regex + optional AST context filter
- co_modification_required — if trigger glob changes, must-modify glob must too
  (staged mode only)
- signature_lock — exact ordered parameter names of a named function
- caller_count_required — minimum number of call sites summed across a glob
- import_constant_only — string literal allowed only in listed files
- advisory — always returns [] (injected into prompts, not enforced by engine)

Deterministic: same rules file + same tree → byte-identical output. Violations
sorted by (file, line, rule_id). Handlers warn-and-skip on import/parse
failure; never raise. No global state mutation in handlers. Pure Python except
for `git rev-parse --show-toplevel` (install-hook) and
`git diff --cached --name-only` (check --staged).

CLI: check, describe, install-hook, validate-rules. Exit codes per spec AC 17.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import importlib
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Optional

# lib_core lives in the same package (hooks/). When this file is executed as
# a script rather than imported, the package context isn't set up, so we add
# the hooks/ dir to sys.path before attempting the import.
#
# We ALSO add the repo root (parent of hooks/). Rules use module names like
# `hooks.scheduler` and `hooks.rules_engine` in their `params.module`, and
# importlib.import_module needs the repo root on sys.path for those dotted
# imports to resolve to the namespace package. Without the repo root, every
# such rule silently no-ops with a WARN ("No module named 'hooks'") at
# load time.
_HOOKS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _HOOKS_DIR.parent
for _p in (str(_REPO_ROOT), str(_HOOKS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from lib_core import _persistent_project_dir  # noqa: E402

# PRO-007: pin git binary to absolute path resolved at import time.
_GIT: str | None = shutil.which("git")


__all__ = [
    "RULES_ENGINE_VERSION",
    "TEMPLATES",
    "TEMPLATE_SCHEMAS",
    "Rule",
    "ScanScope",
    "Violation",
    "run_checks",
    "run_checks_with_stats",
    "main",
]


RULES_ENGINE_VERSION = "1"

_HOOK_MARKER = "# dynos-rules-engine v1"
_HOOK_BODY = (
    "#!/usr/bin/env bash\n"
    "# dynos-rules-engine v1\n"
    'exec python3 "$(git rev-parse --show-toplevel)/hooks/rules_engine.py" check --staged\n'
)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class Rule:
    """One prevention rule loaded from prevention-rules.json."""

    rule_id: str
    template: str
    params: dict
    severity: str = "error"
    source_finding: str = ""
    rationale: str = ""
    scope_override: Optional[dict] = None


@dataclass(frozen=True)
class ScanScope:
    """Immutable scan scope handed to every template handler."""

    root: Path
    files: tuple[Path, ...]
    mode: str

    def __post_init__(self) -> None:
        if self.mode not in {"staged", "all"}:
            raise ValueError(
                f"ScanScope.mode must be 'staged' or 'all', got {self.mode!r}"
            )


@dataclass
class Violation:
    """One rule violation produced by a template handler."""

    rule_id: str
    template: str
    file: str
    line: int
    message: str
    severity: str

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "template": self.template,
            "file": self.file,
            "line": int(self.line),
            "message": self.message,
            "severity": self.severity,
        }


# ---------------------------------------------------------------------------
# Template schemas (for validate-rules CLI and postmortem validator)
# ---------------------------------------------------------------------------

TEMPLATE_SCHEMAS: dict[str, dict] = {
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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _warn(msg: str) -> None:
    """Write a warning to stderr. Always flushes."""
    try:
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()
    except Exception:
        # Even stderr can fail (closed pipe during tests) — swallow.
        pass


# Mitigates SEC-001 (ReDoS): user-controlled regex from prevention-rules.json
# could catastrophically backtrack. Python's re module cannot be cancelled
# mid-match in-process, so we (a) statically flag known-pathological
# nested-quantifier shapes, and (b) run a short canary search under
# signal.alarm on Unix to catch the rest. Unix-only — dynos-work targets
# macOS/Linux. On other platforms the static-shape check still runs.
import signal as _signal

_REGEX_CANARY = "a" * 64 + "!"
_REGEX_BUDGET_S = 1  # 1s alarm budget (signal.alarm int-seconds only)

# Static detector for common catastrophic-backtracking shapes.
# Covers: (X+)+, (X*)*, (X+)*, (X*)+, and (X|Y)+ where X and Y overlap.
# Not exhaustive — the runtime alarm catches what the static check misses.
_REDOS_SHAPE_PATTERNS = [
    re.compile(r"\([^)]*\+[^)]*\)\s*[+*]"),   # (...+...)+ / (...+...)*
    re.compile(r"\([^)]*\*[^)]*\)\s*[+*]"),   # (...*...)+ / (...*...)*
]


def _regex_looks_pathological(pattern: str) -> bool:
    """Cheap static check for known catastrophic-backtracking shapes."""
    for shape in _REDOS_SHAPE_PATTERNS:
        if shape.search(pattern):
            return True
    return False


def _safe_compile_regex(pattern: str, rule_id: str, field: str) -> "re.Pattern | None":
    """Compile a user-supplied regex with static + runtime ReDoS defences.

    Returns compiled Pattern, or None on compile failure / pathological
    shape / canary alarm. Never raises.
    """
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        _warn(
            f"[rules_engine] WARN rule={rule_id}: invalid {field} "
            f"{pattern!r}: {exc}"
        )
        return None
    if _regex_looks_pathological(pattern):
        _warn(
            f"[rules_engine] WARN rule={rule_id}: {field} {pattern!r} "
            f"has catastrophic-backtracking shape (nested quantifiers); "
            f"refusing to evaluate"
        )
        return None
    # Runtime canary: run against adversarial input under a wall-clock alarm.
    # signal.alarm is main-thread-only on Unix; skip on other platforms and
    # rely on the static check alone.
    if hasattr(_signal, "SIGALRM"):
        def _on_alarm(signum, frame):
            raise TimeoutError("regex canary exceeded budget")
        prev_handler = _signal.signal(_signal.SIGALRM, _on_alarm)
        _signal.alarm(_REGEX_BUDGET_S)
        try:
            compiled.search(_REGEX_CANARY)
        except TimeoutError:
            _warn(
                f"[rules_engine] WARN rule={rule_id}: {field} {pattern!r} "
                f"exceeded {_REGEX_BUDGET_S}s canary (suspected "
                f"catastrophic backtracking); refusing to evaluate"
            )
            return None
        except Exception:
            # Pathological regex raising in search — treat as unsafe.
            _warn(
                f"[rules_engine] WARN rule={rule_id}: {field} canary raised; "
                f"refusing to evaluate {pattern!r}"
            )
            return None
        finally:
            _signal.alarm(0)
            _signal.signal(_signal.SIGALRM, prev_handler)
    return compiled


def _info(msg: str) -> None:
    _warn(msg)


def _truncate(text: str, limit: int = 80) -> str:
    if len(text) <= limit:
        return text
    return text[:limit]


def _relative_path(root: Path, file: Path) -> str:
    """Return file relative to root when possible; absolute otherwise."""
    try:
        return str(file.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(file)


def _staged_files(root: Path) -> list[Path]:
    """Return staged files as absolute paths via `git diff --cached --name-only`.

    Returns [] on any git failure — never raises. Missing files (staged delete)
    are filtered out; downstream handlers that read file contents would choke.
    """
    try:
        result = subprocess.run(
            [_GIT or "git", "-C", str(root), "diff", "--cached", "--name-only"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    out = []
    for line in result.stdout.splitlines():
        rel = line.strip()
        if not rel:
            continue
        p = (root / rel)
        if p.exists() and p.is_file():
            out.append(p)
    return out


def _all_tracked_files(root: Path) -> list[Path]:
    """Return all tracked files in the repo (git ls-files), fall back to walk.

    Filters to files only. Never raises.
    """
    try:
        result = subprocess.run(
            [_GIT or "git", "-C", str(root), "ls-files"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode == 0:
            out = []
            for line in result.stdout.splitlines():
                rel = line.strip()
                if not rel:
                    continue
                p = root / rel
                if p.exists() and p.is_file():
                    out.append(p)
            if out:
                return out
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        pass
    # Fallback: walk the tree (used outside git repos, e.g. tests).
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Skip hidden dirs (.git, .venv, __pycache__, .dynos, etc.)
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "__pycache__"]
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.is_file():
                out.append(p)
    return out


def _resolve_files(root: Path, mode: str) -> tuple[Path, ...]:
    """Resolve the file list for a scan scope. Deterministic (sorted)."""
    if mode == "staged":
        files = _staged_files(root)
    else:
        files = _all_tracked_files(root)
    return tuple(sorted(files))


def _glob_match(file: Path, root: Path, pattern: str) -> bool:
    """Match a file against a repo-relative glob using fnmatch semantics."""
    rel = _relative_path(root, file)
    # fnmatch does not treat '/' specially; for glob-like behaviour we test
    # against the relative path directly.
    if fnmatch.fnmatch(rel, pattern):
        return True
    # Also match against the basename for simple patterns like "*.py".
    if fnmatch.fnmatch(file.name, pattern):
        return True
    return False


def _read_text(file: Path) -> Optional[str]:
    """Read text from a file, returning None on any IO error."""
    try:
        return file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _inspect_source_location(obj: Any) -> tuple[str, int]:
    """Return (file, line) for an object, tolerating None / failure."""
    try:
        src = inspect.getsourcefile(obj)
    except TypeError:
        src = None
    file = src if src else "<unknown>"
    try:
        _, line = inspect.getsourcelines(obj)
    except (TypeError, OSError):
        line = 0
    return file, line


# ---------------------------------------------------------------------------
# Template handlers
# ---------------------------------------------------------------------------


def check_every_name_in_X_satisfies_Y(rule: Rule, scope: ScanScope) -> list[Violation]:
    """AC 4: every element of `container` in `module` satisfies `predicate`."""
    params = rule.params or {}
    module_name = params.get("module")
    container_name = params.get("container")
    predicate = params.get("predicate")
    if not module_name or not container_name or not predicate:
        return []

    try:
        mod = importlib.import_module(module_name)
    except Exception as exc:  # ImportError, SyntaxError, anything.
        _warn(
            f"[rules_engine] WARN rule={rule.rule_id}: import of "
            f"'{module_name}' failed: {exc}"
        )
        return []

    if not hasattr(mod, container_name):
        _warn(
            f"[rules_engine] WARN rule={rule.rule_id}: container "
            f"'{container_name}' not found in module '{module_name}'"
        )
        return []

    container = getattr(mod, container_name)
    try:
        elements = list(container)
    except TypeError:
        _warn(
            f"[rules_engine] WARN rule={rule.rule_id}: container "
            f"'{module_name}.{container_name}' is not iterable"
        )
        return []

    file_path, line_no = _inspect_source_location(mod)
    severity = rule.severity

    violations: list[Violation] = []

    if predicate == "callable":
        for name in elements:
            try:
                obj = getattr(mod, name, None)
                ok = callable(obj)
            except Exception:
                ok = False
            if not ok:
                violations.append(
                    Violation(
                        rule_id=rule.rule_id,
                        template=rule.template,
                        file=file_path,
                        line=line_no,
                        message=(
                            f"every_name_in_X_satisfies_Y: '{name}' in "
                            f"{module_name}.{container_name} fails predicate "
                            f"'{predicate}'"
                        ),
                        severity=severity,
                    )
                )
    elif predicate == "hasattr":
        attr = params.get("attr")
        if not attr:
            _warn(
                f"[rules_engine] WARN rule={rule.rule_id}: predicate=hasattr "
                f"requires 'attr' param"
            )
            return []
        for name in elements:
            try:
                obj = getattr(mod, name, None)
                ok = obj is not None and hasattr(obj, attr)
            except Exception:
                ok = False
            if not ok:
                violations.append(
                    Violation(
                        rule_id=rule.rule_id,
                        template=rule.template,
                        file=file_path,
                        line=line_no,
                        message=(
                            f"every_name_in_X_satisfies_Y: '{name}' in "
                            f"{module_name}.{container_name} fails predicate "
                            f"'{predicate}'"
                        ),
                        severity=severity,
                    )
                )
    elif predicate == "in_registry":
        reg_mod_name = params.get("registry_module")
        reg_attr_name = params.get("registry_attr")
        if not reg_mod_name or not reg_attr_name:
            _warn(
                f"[rules_engine] WARN rule={rule.rule_id}: predicate=in_registry "
                f"requires 'registry_module' and 'registry_attr' params"
            )
            return []
        try:
            reg_mod = importlib.import_module(reg_mod_name)
        except Exception as exc:
            _warn(
                f"[rules_engine] WARN rule={rule.rule_id}: import of "
                f"registry '{reg_mod_name}' failed: {exc}"
            )
            return []
        registry = getattr(reg_mod, reg_attr_name, None)
        if registry is None:
            _warn(
                f"[rules_engine] WARN rule={rule.rule_id}: registry "
                f"'{reg_mod_name}.{reg_attr_name}' not found"
            )
            return []
        try:
            membership = set(registry) if not isinstance(registry, dict) else set(registry.keys())
        except TypeError:
            _warn(
                f"[rules_engine] WARN rule={rule.rule_id}: registry "
                f"'{reg_mod_name}.{reg_attr_name}' is not iterable"
            )
            return []
        for name in elements:
            if name not in membership:
                violations.append(
                    Violation(
                        rule_id=rule.rule_id,
                        template=rule.template,
                        file=file_path,
                        line=line_no,
                        message=(
                            f"every_name_in_X_satisfies_Y: '{name}' in "
                            f"{module_name}.{container_name} fails predicate "
                            f"'{predicate}'"
                        ),
                        severity=severity,
                    )
                )
    else:
        _warn(
            f"[rules_engine] WARN rule={rule.rule_id}: unknown predicate "
            f"{predicate!r}; expected callable|hasattr|in_registry"
        )
        return []

    return violations


def check_pattern_must_not_appear(rule: Rule, scope: ScanScope) -> list[Violation]:
    """AC 5: regex match with optional AST-context filter."""
    params = rule.params or {}
    regex_src = params.get("regex")
    glob = params.get("scope")
    context_required = params.get("context_required")
    if not regex_src or not glob:
        return []

    regex = _safe_compile_regex(regex_src, rule.rule_id, "regex")
    if regex is None:
        return []

    if context_required:
        ctx_re = _safe_compile_regex(context_required, rule.rule_id, "context_required")
        if ctx_re is None:
            return []
    else:
        ctx_re = None

    violations: list[Violation] = []
    for file in scope.files:
        if not _glob_match(file, scope.root, glob):
            continue
        text = _read_text(file)
        if text is None:
            continue

        matches = list(regex.finditer(text))
        if not matches:
            continue

        rel = _relative_path(scope.root, file)

        if ctx_re is None:
            for m in matches:
                line_no = text.count("\n", 0, m.start()) + 1
                matched_text = _truncate(m.group(0))
                violations.append(
                    Violation(
                        rule_id=rule.rule_id,
                        template=rule.template,
                        file=rel,
                        line=line_no,
                        message=(
                            f"pattern_must_not_appear: regex "
                            f"{regex_src!r} matched in {rel}:{line_no} "
                            f"(text: {matched_text!r})"
                        ),
                        severity=rule.severity,
                    )
                )
            continue

        # context_required: parse AST once per file with hits.
        try:
            tree = ast.parse(text, filename=str(file))
        except SyntaxError as exc:
            _warn(
                f"[rules_engine] WARN rule={rule.rule_id}: syntax error "
                f"parsing {rel}: {exc}; skipping"
            )
            continue

        # Build a list of all nodes with line span info for containment search.
        nodes_with_span = []
        for node in ast.walk(tree):
            if hasattr(node, "lineno"):
                start = node.lineno
                end = getattr(node, "end_lineno", start)
                nodes_with_span.append((start, end, node))

        for m in matches:
            line_no = text.count("\n", 0, m.start()) + 1
            # Find smallest enclosing node whose range contains line_no.
            best = None
            best_span = None
            for start, end, node in nodes_with_span:
                if start <= line_no <= end:
                    span = end - start
                    if best is None or span < best_span:
                        best = node
                        best_span = span
            if best is None:
                continue
            try:
                source_slice = ast.unparse(best)
            except Exception:
                continue
            if not ctx_re.search(source_slice):
                continue
            matched_text = _truncate(m.group(0))
            violations.append(
                Violation(
                    rule_id=rule.rule_id,
                    template=rule.template,
                    file=rel,
                    line=line_no,
                    message=(
                        f"pattern_must_not_appear: regex {regex_src!r} "
                        f"matched in {rel}:{line_no} "
                        f"(text: {matched_text!r})"
                    ),
                    severity=rule.severity,
                )
            )

    return violations


def check_co_modification_required(rule: Rule, scope: ScanScope) -> list[Violation]:
    """AC 6: trigger glob change implies must-modify glob change. Staged-only."""
    params = rule.params or {}
    trigger_glob = params.get("trigger_glob")
    must_modify_glob = params.get("must_modify_glob")
    if not trigger_glob or not must_modify_glob:
        return []

    if scope.mode == "all":
        _info(
            f"[rules_engine] INFO rule={rule.rule_id}: "
            f"co_modification_required skipped in --all mode"
        )
        return []

    trigger_files = sorted(
        [f for f in scope.files if _glob_match(f, scope.root, trigger_glob)]
    )
    if not trigger_files:
        return []

    modified = [f for f in scope.files if _glob_match(f, scope.root, must_modify_glob)]
    if modified:
        return []

    trigger_file = _relative_path(scope.root, trigger_files[0])
    return [
        Violation(
            rule_id=rule.rule_id,
            template=rule.template,
            file=trigger_file,
            line=0,
            message=(
                f"co_modification_required: {trigger_file} changed but "
                f"no file matching '{must_modify_glob}' was modified"
            ),
            severity=rule.severity,
        )
    ]


def check_signature_lock(rule: Rule, scope: ScanScope) -> list[Violation]:
    """AC 7: function signature matches expected_params exactly."""
    params = rule.params or {}
    module_name = params.get("module")
    function_name = params.get("function")
    expected = params.get("expected_params")
    if not module_name or not function_name or expected is None:
        return []
    if not isinstance(expected, list):
        _warn(
            f"[rules_engine] WARN rule={rule.rule_id}: expected_params "
            f"must be a list"
        )
        return []

    try:
        mod = importlib.import_module(module_name)
    except Exception as exc:
        _warn(
            f"[rules_engine] WARN rule={rule.rule_id}: import of "
            f"'{module_name}' failed: {exc}"
        )
        return []

    try:
        func = getattr(mod, function_name)
    except AttributeError as exc:
        _warn(
            f"[rules_engine] WARN rule={rule.rule_id}: attribute "
            f"'{module_name}.{function_name}' not found: {exc}"
        )
        return []

    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError) as exc:
        _warn(
            f"[rules_engine] WARN rule={rule.rule_id}: signature of "
            f"'{module_name}.{function_name}' unavailable: {exc}"
        )
        return []

    actual = [p for p in sig.parameters.keys() if p != "self"]
    if actual == list(expected):
        return []

    file, line = _inspect_source_location(func)
    return [
        Violation(
            rule_id=rule.rule_id,
            template=rule.template,
            file=file,
            line=line,
            message=(
                f"signature_lock: {module_name}.{function_name} "
                f"params={actual!r} expected={list(expected)!r}"
            ),
            severity=rule.severity,
        )
    ]


def check_caller_count_required(rule: Rule, scope: ScanScope) -> list[Violation]:
    """AC 8: sum ast.Call sites for `symbol` across glob ≥ min_count."""
    params = rule.params or {}
    symbol = params.get("symbol")
    glob = params.get("scope")
    min_count = params.get("min_count")
    if not symbol or not glob or min_count is None:
        return []
    if not isinstance(min_count, int):
        _warn(
            f"[rules_engine] WARN rule={rule.rule_id}: min_count must be int"
        )
        return []

    total = 0
    for file in scope.files:
        if not _glob_match(file, scope.root, glob):
            continue
        text = _read_text(file)
        if text is None:
            continue
        try:
            tree = ast.parse(text, filename=str(file))
        except SyntaxError as exc:
            _warn(
                f"[rules_engine] WARN rule={rule.rule_id}: syntax error "
                f"parsing {_relative_path(scope.root, file)}: {exc}; skipping"
            )
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id == symbol:
                total += 1
            elif isinstance(func, ast.Attribute) and func.attr == symbol:
                total += 1

    if total >= min_count:
        return []

    return [
        Violation(
            rule_id=rule.rule_id,
            template=rule.template,
            file="(repo-wide)",
            line=0,
            message=(
                f"caller_count_required: '{symbol}' called {total} time(s) "
                f"across {glob}, expected >= {min_count}"
            ),
            severity=rule.severity,
        )
    ]


def check_import_constant_only(rule: Rule, scope: ScanScope) -> list[Violation]:
    """AC 9: string literal matching pattern allowed only in listed files."""
    params = rule.params or {}
    literal_pattern = params.get("literal_pattern")
    allowed_files = params.get("allowed_files")
    if not literal_pattern or allowed_files is None:
        return []
    if not isinstance(allowed_files, list):
        _warn(
            f"[rules_engine] WARN rule={rule.rule_id}: allowed_files must be a list"
        )
        return []

    pat = _safe_compile_regex(literal_pattern, rule.rule_id, "literal_pattern")
    if pat is None:
        return []

    violations: list[Violation] = []
    for file in scope.files:
        if file.suffix != ".py":
            continue
        rel = _relative_path(scope.root, file)
        # If file matches any allowed glob, skip (file is allowed).
        if any(_glob_match(file, scope.root, g) for g in allowed_files):
            continue
        text = _read_text(file)
        if text is None:
            continue
        try:
            tree = ast.parse(text, filename=str(file))
        except SyntaxError as exc:
            _warn(
                f"[rules_engine] WARN rule={rule.rule_id}: syntax error "
                f"parsing {rel}: {exc}; skipping"
            )
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant):
                continue
            value = node.value
            if not isinstance(value, str):
                continue
            if not pat.search(value):
                continue
            line_no = getattr(node, "lineno", 0) or 0
            literal_trunc = _truncate(value)
            violations.append(
                Violation(
                    rule_id=rule.rule_id,
                    template=rule.template,
                    file=rel,
                    line=line_no,
                    message=(
                        f"import_constant_only: literal {literal_trunc!r} "
                        f"matching pattern {literal_pattern!r} appears in "
                        f"{rel}:{line_no}; only allowed in: {allowed_files}"
                    ),
                    severity=rule.severity,
                )
            )

    return violations


def check_advisory(rule: Rule, scope: ScanScope) -> list[Violation]:
    """AC 10: advisory rules are NEVER enforced by the engine."""
    return []


TEMPLATES: dict[str, Callable[[Rule, ScanScope], list[Violation]]] = {
    "every_name_in_X_satisfies_Y": check_every_name_in_X_satisfies_Y,
    "pattern_must_not_appear": check_pattern_must_not_appear,
    "co_modification_required": check_co_modification_required,
    "signature_lock": check_signature_lock,
    "caller_count_required": check_caller_count_required,
    "import_constant_only": check_import_constant_only,
    "advisory": check_advisory,
}


# ---------------------------------------------------------------------------
# Rule loading
# ---------------------------------------------------------------------------


def _load_rules_file(root: Path, rules_path: Optional[Path] = None) -> tuple[list[dict], Optional[Path]]:
    """Load the raw rules list and return (list, path).

    If rules_path not given, uses _persistent_project_dir(root) / prevention-rules.json.
    Missing file → ([], path). Caller is responsible for exit-code semantics when
    the JSON is malformed (we raise ValueError in that case).
    """
    if rules_path is None:
        rules_path = _persistent_project_dir(root) / "prevention-rules.json"
    if not rules_path.exists():
        return [], rules_path
    try:
        with rules_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read prevention-rules at {rules_path}: {exc}") from exc
    rules = data.get("rules") if isinstance(data, dict) else None
    if rules is None:
        return [], rules_path
    if not isinstance(rules, list):
        raise ValueError(
            f"prevention-rules.json at {rules_path} has non-list 'rules' field"
        )
    return rules, rules_path


def _rule_from_dict(d: dict) -> Optional[Rule]:
    """Build a Rule from a raw dict; return None on missing required fields."""
    if not isinstance(d, dict):
        return None
    rule_id = d.get("rule_id") or d.get("id")
    template = d.get("template")
    params = d.get("params") or {}
    if not rule_id or not template:
        return None
    if not isinstance(params, dict):
        return None
    return Rule(
        rule_id=str(rule_id),
        template=str(template),
        params=params,
        severity=str(d.get("severity", "error")),
        source_finding=str(d.get("source_finding", "")),
        rationale=str(d.get("rationale", "")),
        scope_override=d.get("scope_override"),
    )


# ---------------------------------------------------------------------------
# Top-level run_checks
# ---------------------------------------------------------------------------


def run_checks(
    root: Path,
    mode: Literal["staged", "all"],
    rule_filter: Optional[str] = None,
    *,
    raw_rules: Optional[list[dict]] = None,
) -> list[Violation]:
    """Load prevention-rules.json, dispatch each rule, return sorted violations.

    Missing rules file → []. Unknown templates are skipped with a stderr warn.
    Handlers themselves never raise; this function tolerates per-rule errors by
    logging and moving on.

    Output is sorted by (file, line, rule_id) for byte-identical determinism.

    Closes residual b2910674 (perf / TOCTOU defense-in-depth): callers may
    pass an already-loaded ``raw_rules`` list to avoid a second
    ``_load_rules_file`` invocation. ``run_checks_with_stats`` uses this to
    guarantee a single read of ``prevention-rules.json`` per
    ``rules-check-passed`` receipt write.
    """
    root = Path(root)
    if raw_rules is None:
        raw_rules, _ = _load_rules_file(root)
    if not raw_rules:
        return []

    files = _resolve_files(root, mode)
    scope = ScanScope(root=root.resolve(), files=files, mode=mode)

    violations: list[Violation] = []
    for raw in raw_rules:
        rule = _rule_from_dict(raw)
        if rule is None:
            # Malformed rule entry; skip with warning. Do NOT raise — AC 17
            # distinguishes malformed JSON (exit 2) from individual bad rule
            # (skipped).
            _warn(
                f"[rules_engine] WARN: skipping malformed rule entry "
                f"(missing rule_id or template): {raw!r}"
            )
            continue
        if rule_filter is not None and rule.rule_id != rule_filter:
            continue
        handler = TEMPLATES.get(rule.template)
        if handler is None:
            _warn(
                f"[rules_engine] WARN rule={rule.rule_id}: unknown template "
                f"{rule.template!r}; skipping"
            )
            continue
        try:
            result = handler(rule, scope)
        except Exception as exc:
            # Defensive: template handlers promise not to raise, but if one
            # does, treat as warn-and-skip rather than exploding the engine.
            _warn(
                f"[rules_engine] WARN rule={rule.rule_id}: handler for "
                f"{rule.template!r} raised {type(exc).__name__}: {exc}"
            )
            continue
        if result:
            violations.extend(result)

    violations.sort(key=lambda v: (v.file, int(v.line), v.rule_id))
    return violations


def run_checks_with_stats(
    root: Path,
    mode: str = "all",
) -> "tuple[list[Violation], int, int]":
    """Like run_checks but also returns loaded and skipped rule counts.

    Returns (violations, loaded_count, skipped_count) where:
      - loaded_count: number of Rule objects successfully constructed by
        _rule_from_dict (entries that returned a non-None Rule).
      - skipped_count: number of raw entries for which _rule_from_dict
        returned None.
      - violations: result of run_checks(root, mode) — callers of
        run_checks continue to work unchanged and monkeypatching
        run_checks in tests propagates through this function.

    Implementation note: the rules file is read EXACTLY ONCE per call.
    The parsed rule list flows through to run_checks via the new
    ``raw_rules`` kwarg, eliminating the double-read window that
    residual b2910674 flagged. Closes that residual.
    """
    root = Path(root)
    raw_rules, _ = _load_rules_file(root)
    loaded_count = 0
    skipped_count = 0
    for raw in raw_rules:
        rule = _rule_from_dict(raw)
        if rule is None:
            skipped_count += 1
        else:
            loaded_count += 1

    # Pass the pre-loaded rules through so run_checks does NOT re-read
    # the file. Test code that monkey-patches run_checks still takes
    # effect because the function lookup happens at the call site, not
    # at parse time.
    violations = run_checks(root, mode, raw_rules=raw_rules)
    return violations, loaded_count, skipped_count


# ---------------------------------------------------------------------------
# CLI subcommand: check
# ---------------------------------------------------------------------------


def _format_violation_line(v: Violation) -> str:
    # Spec AC 16: "{file}:{line}: [{severity}] {rule_id} ({template}) — {message}"
    return (
        f"{v.file}:{v.line}: [{v.severity}] {v.rule_id} "
        f"({v.template}) \u2014 {v.message}"
    )


def _cmd_check(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve() if args.root else Path.cwd()
    mode = "all" if args.all else "staged"
    try:
        violations = run_checks(root, mode, rule_filter=args.rule)
    except ValueError as exc:
        _warn(f"[rules_engine] ERROR: {exc}")
        return 2
    except (OSError, json.JSONDecodeError) as exc:
        _warn(f"[rules_engine] ERROR: {exc}")
        return 2

    # Emit per-violation lines to stdout.
    for v in violations:
        print(_format_violation_line(v))

    # Summary to stderr. Count distinct rule ids across violations.
    error_count = sum(1 for v in violations if v.severity == "error")
    warn_count = sum(1 for v in violations if v.severity == "warn")
    rule_ids = {v.rule_id for v in violations}
    if violations:
        _warn(
            f"{len(violations)} violations "
            f"({error_count} error, {warn_count} warn) "
            f"across {len(rule_ids)} rules"
        )
    sys.stdout.flush()
    sys.stderr.flush()
    return 1 if error_count > 0 else 0


# ---------------------------------------------------------------------------
# CLI subcommand: describe
# ---------------------------------------------------------------------------


def _enforcement_for_template(template: str) -> str:
    if template == "advisory":
        return "advisory"
    if template in TEMPLATES:
        return "enforced"
    return "unknown"


def _cmd_describe(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve() if args.root else Path.cwd()
    try:
        raw_rules, _ = _load_rules_file(root)
    except ValueError as exc:
        _warn(f"[rules_engine] ERROR: {exc}")
        return 2

    if not raw_rules:
        # AC 3 / AC 17: no rules → exit 0, no output.
        return 0

    # Tab-separated header and rows.
    header = "rule_id\ttemplate\tscope\tenforcement\tseverity\tsource_finding"
    print(header)

    rows = []
    for idx, raw in enumerate(raw_rules):
        if not isinstance(raw, dict):
            continue
        # Per AC 13 backward-compat: rules without a `template` field are
        # treated as advisory. We surface them in describe output too so
        # operators can see what the engine sees.
        rule_id = raw.get("rule_id") or raw.get("id") or f"<unkeyed-{idx}>"
        template = raw.get("template") or "advisory"
        params = raw.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        scope_val = (
            params.get("scope")
            or params.get("trigger_glob")
            or params.get("module")
            or (raw.get("scope_override") or {}).get("scope")
            or raw.get("scope", "")
            or ""
        )
        severity = str(raw.get("severity", "error"))
        source_finding = str(raw.get("source_finding", ""))
        rows.append(
            (
                str(rule_id),
                str(template),
                str(scope_val),
                _enforcement_for_template(str(template)),
                severity,
                source_finding,
            )
        )
    rows.sort(key=lambda r: (r[0],))
    for row in rows:
        print("\t".join(row))
    sys.stdout.flush()
    return 0


# ---------------------------------------------------------------------------
# CLI subcommand: install-hook
# ---------------------------------------------------------------------------


def _repo_toplevel(root: Path) -> Optional[Path]:
    try:
        result = subprocess.run(
            [_GIT or "git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    top = result.stdout.strip()
    if not top:
        return None
    return Path(top)


def _cmd_install_hook(args: argparse.Namespace) -> int:
    cwd = Path.cwd()
    toplevel = _repo_toplevel(cwd)
    if toplevel is None:
        _warn(
            "[rules_engine] ERROR: not inside a git repository "
            "(git rev-parse --show-toplevel failed)"
        )
        return 2

    hooks_dir = toplevel / ".git" / "hooks"
    try:
        hooks_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _warn(f"[rules_engine] ERROR: cannot create hooks dir: {exc}")
        return 2

    hook_path = hooks_dir / "pre-commit"

    # Idempotency / conflict check.
    if hook_path.exists():
        try:
            head = hook_path.read_bytes()[:200]
        except OSError as exc:
            _warn(f"[rules_engine] ERROR: cannot read existing hook: {exc}")
            return 2
        if _HOOK_MARKER.encode("utf-8") in head:
            # Our hook already — no-op.
            return 0
        if not args.force:
            _warn(
                f"[rules_engine] refusal: {hook_path} exists and is not a "
                f"dynos-rules-engine hook. Re-run with --force to overwrite."
            )
            return 1

    # Atomic write: tempfile in same dir, then os.replace.
    tmp_path = hook_path.with_suffix(hook_path.suffix + ".tmp")
    try:
        fd = os.open(
            str(tmp_path),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o755,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(_HOOK_BODY)
        except Exception:
            # fdopen consumed fd; fall through to cleanup below.
            raise
        # Ensure exec bit on filesystems that mask O_CREAT mode.
        os.chmod(tmp_path, 0o755)
        os.replace(tmp_path, hook_path)
    except OSError as exc:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        _warn(f"[rules_engine] ERROR: cannot install hook: {exc}")
        return 2

    return 0


# ---------------------------------------------------------------------------
# CLI subcommand: validate-rules
# ---------------------------------------------------------------------------


def _validate_rule_entry(raw: Any) -> Optional[str]:
    """Validate one raw rule entry against TEMPLATE_SCHEMAS.

    Returns an error string on failure, None on success.
    """
    if not isinstance(raw, dict):
        return "entry is not a dict"
    rule_id = raw.get("rule_id") or raw.get("id")
    if not rule_id:
        return "missing rule_id"
    template = raw.get("template")
    if not template:
        return f"rule {rule_id!r}: missing template"
    schema = TEMPLATE_SCHEMAS.get(template)
    if schema is None:
        return f"rule {rule_id!r}: unknown template {template!r}"
    params = raw.get("params") or {}
    if not isinstance(params, dict):
        return f"rule {rule_id!r}: params must be a dict"
    for key in schema.get("required", []):
        if key not in params:
            return f"rule {rule_id!r}: missing required param {key!r}"
    for key, enum_values in (schema.get("enums") or {}).items():
        if key in params and params[key] not in enum_values:
            return (
                f"rule {rule_id!r}: param {key!r}={params[key]!r} "
                f"not in enum {enum_values}"
            )
    for key, declared_type in (schema.get("types") or {}).items():
        if key in params and not isinstance(params[key], declared_type):
            return (
                f"rule {rule_id!r}: param {key!r} must be "
                f"{declared_type.__name__}, got {type(params[key]).__name__}"
            )
    return None


def _cmd_validate_rules(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve() if args.root else Path.cwd()
    rules_path = Path(args.rules_path) if args.rules_path else None
    try:
        raw_rules, resolved_path = _load_rules_file(root, rules_path)
    except ValueError as exc:
        _warn(f"[rules_engine] ERROR: {exc}")
        return 2
    except OSError as exc:
        _warn(f"[rules_engine] ERROR: {exc}")
        return 2

    errors: list[str] = []
    for raw in raw_rules:
        err = _validate_rule_entry(raw)
        if err:
            errors.append(err)

    if errors:
        for err in errors:
            print(err)
        sys.stdout.flush()
        return 1

    print(f"OK ({len(raw_rules)} rules valid)")
    sys.stdout.flush()
    return 0


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rules_engine",
        description="Deterministic rules engine for dynos prevention-rules.json.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_check = sub.add_parser("check", help="Evaluate rules against the tree.")
    grp = p_check.add_mutually_exclusive_group()
    grp.add_argument("--staged", dest="staged", action="store_true")
    grp.add_argument("--all", dest="all", action="store_true")
    p_check.add_argument("--rule", dest="rule", default=None)
    p_check.add_argument("--root", dest="root", default=None)

    p_desc = sub.add_parser("describe", help="List all rules.")
    p_desc.add_argument("--root", dest="root", default=None)

    p_install = sub.add_parser("install-hook", help="Install pre-commit hook.")
    p_install.add_argument("--force", dest="force", action="store_true")

    p_validate = sub.add_parser("validate-rules", help="Validate schemas of all rules.")
    p_validate.add_argument("--root", dest="root", default=None)
    p_validate.add_argument("--rules-path", dest="rules_path", default=None)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.cmd == "check":
            # Default is --staged when neither explicit flag given.
            if not args.staged and not args.all:
                args.staged = True
            return _cmd_check(args)
        if args.cmd == "describe":
            return _cmd_describe(args)
        if args.cmd == "install-hook":
            return _cmd_install_hook(args)
        if args.cmd == "validate-rules":
            return _cmd_validate_rules(args)
    except KeyboardInterrupt:
        _warn("[rules_engine] interrupted")
        return 2
    except Exception as exc:
        _warn(f"[rules_engine] internal error: {type(exc).__name__}: {exc}")
        return 2

    parser.error(f"unknown command: {args.cmd!r}")
    return 2  # unreachable, but pleases type-checkers


if __name__ == "__main__":
    sys.exit(main())
