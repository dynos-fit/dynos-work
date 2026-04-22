"""TDD-first tests for task-20260420-001 D2 — AST-based presence-check lint.

Covers acceptance criteria 8, 9, 10:

    AC8  lint walks hooks/*.py with ast.NodeVisitor (NOT regex) and flags
         `if not isinstance(X, str) or not X[.strip()]:` raises-ValueError
         bodies on caller-supplied-looking identifiers.
    AC9  ALLOWLIST_FILES is a module-level constant, <=3 entries, at minimum
         hooks/lib_validation.py after D1 ships.
    AC10 zero findings on the post-migration tree; failure messages include
         file, line, identifier.

TODAY these tests FAIL because the migration has not happened:
    hooks/lib_core.py:1150/1156, hooks/lib_receipts.py:2034/2036/2065/2070,
    hooks/ctl.py:110/116 all match the banned pattern.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / "hooks"

sys.path.insert(0, str(HOOKS_DIR))


# Exported module-level constant so the test author contract is auditable.
ALLOWLIST_FILES: frozenset[str] = frozenset(
    {
        "hooks/lib_validation.py",  # the helper module itself is the only
                                    # legitimate home of the raw pattern
    }
)


CALLER_SUPPLIED_IDENT_RE = re.compile(
    r"^(reason|approver|.*_reason|.*_approver|task_id|stage|"
    r"from_stage|to_stage|.*_sha256|.*_slug|.*_id)$"
)


class _InlinePresenceCheckVisitor(ast.NodeVisitor):
    """Flags `if not isinstance(X, str) or not X[.strip()]` or equivalent
    `if not X` when X is a caller-supplied-looking identifier and the
    body raises ValueError or returns an error sentinel (int 1 or 2)."""

    def __init__(self) -> None:
        self.findings: list[tuple[int, str]] = []

    @staticmethod
    def _body_raises_or_returns_error(body: list[ast.stmt]) -> bool:
        for stmt in body:
            if isinstance(stmt, ast.Raise):
                exc = stmt.exc
                if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name):
                    if exc.func.id in {"ValueError", "TypeError"}:
                        return True
                if isinstance(exc, ast.Name) and exc.id in {"ValueError", "TypeError"}:
                    return True
            if isinstance(stmt, ast.Return) and isinstance(
                stmt.value, ast.Constant
            ):
                if stmt.value.value in (1, 2):
                    return True
        return False

    @staticmethod
    def _extract_isinstance_name(node: ast.AST) -> str | None:
        """If node is `not isinstance(X, ...)` return 'X', else None."""
        if not isinstance(node, ast.UnaryOp) or not isinstance(node.op, ast.Not):
            return None
        operand = node.operand
        if not isinstance(operand, ast.Call):
            return None
        if not isinstance(operand.func, ast.Name) or operand.func.id != "isinstance":
            return None
        if not operand.args:
            return None
        target = operand.args[0]
        if isinstance(target, ast.Name):
            return target.id
        return None

    @staticmethod
    def _extract_not_name(node: ast.AST) -> str | None:
        """If node is `not X` or `not X.strip()` return 'X'."""
        if not isinstance(node, ast.UnaryOp) or not isinstance(node.op, ast.Not):
            return None
        operand = node.operand
        if isinstance(operand, ast.Name):
            return operand.id
        if (
            isinstance(operand, ast.Call)
            and isinstance(operand.func, ast.Attribute)
            and operand.func.attr == "strip"
            and isinstance(operand.func.value, ast.Name)
        ):
            return operand.func.value.id
        return None

    def visit_If(self, node: ast.If) -> None:  # noqa: N802 - NodeVisitor API
        test = node.test
        flagged_ident: str | None = None

        if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or):
            iso_idents = [self._extract_isinstance_name(v) for v in test.values]
            not_idents = [self._extract_not_name(v) for v in test.values]
            names = {n for n in (iso_idents + not_idents) if n}
            for n in names:
                if CALLER_SUPPLIED_IDENT_RE.match(n):
                    flagged_ident = n
                    break

        if (
            flagged_ident is None
            and isinstance(test, ast.UnaryOp)
            and isinstance(test.op, ast.Not)
        ):
            n = self._extract_not_name(test)
            if n and CALLER_SUPPLIED_IDENT_RE.match(n):
                flagged_ident = n

        if flagged_ident and self._body_raises_or_returns_error(node.body):
            self.findings.append((node.lineno, flagged_ident))

        self.generic_visit(node)


def _scan(path: Path) -> list[tuple[int, str]]:
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))
    v = _InlinePresenceCheckVisitor()
    v.visit(tree)
    return v.findings


def _is_allowlisted(path: Path) -> bool:
    rel = path.relative_to(REPO_ROOT).as_posix()
    return rel in ALLOWLIST_FILES


def _walk_hooks() -> list[tuple[Path, int, str]]:
    findings: list[tuple[Path, int, str]] = []
    for p in sorted(HOOKS_DIR.glob("*.py")):
        if _is_allowlisted(p):
            continue
        try:
            hits = _scan(p)
        except (SyntaxError, UnicodeDecodeError):
            continue
        for lineno, ident in hits:
            findings.append((p, lineno, ident))
    return findings


# ---------------------------------------------------------------------------
# AC10: zero findings on the post-migration tree
# ---------------------------------------------------------------------------


def test_no_inline_presence_checks_in_hooks():
    findings = _walk_hooks()
    if findings:
        diag = "\n".join(
            f"{p.relative_to(REPO_ROOT).as_posix()}:{ln}: {ident}"
            for (p, ln, ident) in findings
        )
        pytest.fail(
            f"{len(findings)} inline presence-check findings on hooks/*.py. "
            f"Every caller-supplied string field MUST route through a "
            f"hooks/lib_validation.py helper. Offenders:\n{diag}"
        )


# ---------------------------------------------------------------------------
# AC9: allowlist is minimal and named
# ---------------------------------------------------------------------------


def test_allowlist_is_minimal_and_named():
    assert isinstance(ALLOWLIST_FILES, (frozenset, set))
    assert len(ALLOWLIST_FILES) <= 3
    assert "hooks/lib_validation.py" in ALLOWLIST_FILES


def test_allowlist_entries_all_point_to_real_files():
    for rel in ALLOWLIST_FILES:
        p = REPO_ROOT / rel
        assert p.exists(), (
            f"allowlist entry {rel!r} must point to a real file "
            f"(was it moved? stale allowlist is declared-not-enforced drift)"
        )


# ---------------------------------------------------------------------------
# AC8: lint detects a synthetic violation in a tmp file
# ---------------------------------------------------------------------------


def test_lint_fires_on_synthetic_violation(tmp_path: Path):
    fake = tmp_path / "victim.py"
    fake.write_text(
        "def f(reason):\n"
        "    if not isinstance(reason, str) or not reason.strip():\n"
        "        raise ValueError('bad')\n"
    )
    findings = _scan(fake)
    assert findings, "lint must detect the synthetic violation"
    (ln, ident) = findings[0]
    assert ident == "reason"
    assert ln == 2


def test_lint_fires_on_synthetic_bare_not_identifier(tmp_path: Path):
    fake = tmp_path / "victim2.py"
    fake.write_text(
        "def f(force_reason):\n"
        "    if not force_reason:\n"
        "        raise ValueError('bad')\n"
    )
    findings = _scan(fake)
    assert findings, "lint must detect `if not X` on caller-supplied ident"
    (ln, ident) = findings[0]
    assert ident == "force_reason"


def test_lint_does_not_fire_on_non_caller_supplied_identifier(tmp_path: Path):
    """Flow-control `if not self.cache: ...` must not fire — only caller-
    supplied-looking identifiers are targeted. AC9 names this explicitly."""
    fake = tmp_path / "victim3.py"
    fake.write_text(
        "def f():\n"
        "    cache = None\n"
        "    if not cache:\n"
        "        raise ValueError('bad')\n"
    )
    findings = _scan(fake)
    assert findings == [], "lint must not fire on non-caller-supplied idents"


def test_lint_does_not_fire_when_body_is_not_error_raise(tmp_path: Path):
    """If the body of the `if not X` block doesn't raise or return error
    sentinel, we do not flag — this is regular flow control."""
    fake = tmp_path / "victim4.py"
    fake.write_text(
        "def f(reason):\n"
        "    if not reason:\n"
        "        reason = 'default'\n"
        "    return reason\n"
    )
    findings = _scan(fake)
    assert findings == [], "lint must not flag non-error-raising bodies"
