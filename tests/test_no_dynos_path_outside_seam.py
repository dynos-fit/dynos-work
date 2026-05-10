"""AST regression tests for path-construction discipline and shell=True absence.

These tests run against the EXISTING codebase. They will fail because the
code hasn't been rewired yet — that is the expected RED state.

AC 41 — no module outside the seam constructs ~/.dynos/projects paths.
AC 42 — no shell=True in identity code subprocess calls.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = ROOT / "hooks"
MEMORY_DIR = ROOT / "memory"

# Permitted files that ARE allowed to construct the projects path.
_SEAM_FILES = {
    HOOKS_DIR / "lib_core.py",
    HOOKS_DIR / "lib_project_id.py",   # new file (may not exist yet; if absent, OK)
    HOOKS_DIR / "worktree.py",          # migration helpers are an explicit exception
}

# Files to scan for shell=True violations.
_IDENTITY_FILES = [
    HOOKS_DIR / "lib_project_id.py",
    HOOKS_DIR / "lib_core.py",
    HOOKS_DIR / "worktree.py",
    HOOKS_DIR / "registry.py",
]

# Patterns that indicate a dynos projects-path construction.
_PROJECTS_PATH_MARKERS = (
    '.dynos/projects',
    '.dynos" / "projects',
    ".dynos' / 'projects",
    '"projects"',  # overly broad but combined with context checks below.
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _all_py_files_under(*dirs: Path):
    """Yield all .py files under the given directories."""
    for d in dirs:
        if d.is_dir():
            yield from d.rglob("*.py")


def _contains_dynos_projects_path(source: str) -> bool:
    """Return True if *source* contains a literal string referencing .dynos/projects."""
    return '.dynos/projects' in source or (
        '.dynos"' in source and '"projects"' in source
    ) or (
        ".dynos'" in source and "'projects'" in source
    )


def _find_dynos_path_constructions_ast(source: str) -> list[int]:
    """Return line numbers where .dynos/projects path constructions appear in AST."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    offending_lines: list[int] = []

    for node in ast.walk(tree):
        # Check string constants containing .dynos/projects.
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if ".dynos/projects" in node.value or (
                ".dynos" in node.value and "projects" in node.value
            ):
                offending_lines.append(node.lineno)

    return offending_lines


def _has_shell_true_subprocess_call(source: str, filename: str) -> list[tuple[int, str]]:
    """Return list of (lineno, call_str) for subprocess calls using shell=True."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    offenders: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Check for subprocess.run(...) or subprocess.Popen(...) calls.
        func = node.func
        is_subprocess_call = False
        if isinstance(func, ast.Attribute) and func.attr in ("run", "Popen", "call", "check_call", "check_output"):
            is_subprocess_call = True
        if not is_subprocess_call:
            continue
        # Check for shell=True keyword argument.
        for kw in node.keywords:
            if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                offenders.append((node.lineno, f"{filename}:{node.lineno}"))
    return offenders


# ---------------------------------------------------------------------------
# AC 41 — no module constructs dynos/projects path outside seam
# ---------------------------------------------------------------------------


def test_no_module_constructs_dynos_project_path_outside_seam():
    """AC 41 / T-17 generalization — no module under hooks/ or memory/ constructs
    a '.dynos/projects' path outside the permitted seam files.
    """
    violations: list[str] = []

    for py_file in _all_py_files_under(HOOKS_DIR, MEMORY_DIR):
        # Skip files that are explicitly permitted.
        if py_file in _SEAM_FILES:
            continue
        # Skip test files (they reference the path in assertions).
        if py_file.parent.name == "tests":
            continue

        source = py_file.read_text(encoding="utf-8", errors="replace")
        if not _contains_dynos_projects_path(source):
            continue

        offending_lines = _find_dynos_path_constructions_ast(source)
        for lineno in offending_lines:
            violations.append(f"{py_file.relative_to(ROOT)}:{lineno}")

    assert not violations, (
        "Modules outside the seam construct .dynos/projects paths:\n"
        + "\n".join(f"  {v}" for v in violations)
    )


# ---------------------------------------------------------------------------
# AC 42 — no shell=True in identity code subprocess calls
# ---------------------------------------------------------------------------


def test_no_shell_true_in_identity_code_path():
    """AC 42 / T-17 — no subprocess.run or subprocess.Popen call in the four identity
    files uses shell=True.
    """
    violations: list[str] = []

    for path in _IDENTITY_FILES:
        if not path.exists():
            # If the file doesn't exist yet, no violations possible.
            continue
        source = path.read_text(encoding="utf-8")
        offenders = _has_shell_true_subprocess_call(source, str(path.relative_to(ROOT)))
        violations.extend(desc for _, desc in offenders)

    assert not violations, (
        "subprocess calls with shell=True found in identity code:\n"
        + "\n".join(f"  {v}" for v in violations)
    )
