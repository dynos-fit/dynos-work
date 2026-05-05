"""Sentinel test: AST-walk hooks/lib_receipts.py for inline raise patterns.

Covers AC 12 of task-20260504-006.

After seg-2 migrates all 13 inline sites to require_nonblank_str, this test
must pass.  Before migration it will fail (reporting matched line numbers) —
that is the expected TDD-First failure.

Pattern detected (exactly 2-operand BoolOp):

    if not isinstance(X, str) or not X[.strip()]:
        raise ValueError("... must be a non-empty string")

Allowlist: BoolOp nodes with 3+ operands are skipped (those carry an
additional None guard and are out of scope for this migration).
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_RECEIPTS = REPO_ROOT / "hooks" / "lib_receipts.py"


def _is_target_pattern(node: ast.If) -> bool:
    """Return True if *node* matches the inline isinstance+raise pattern.

    Pattern (2-operand Or only):
      test  : BoolOp(Or, [UnaryOp(Not, Call(isinstance, ..., Name('str'))),
                          UnaryOp(Not, <anything>)])
      body[0]: Raise(Call(ValueError, [Constant(<contains "must be a non-empty string">)]))

    3+-operand BoolOp nodes are allowlisted (additional None checks).
    """
    test = node.test

    # Must be a BoolOp(Or) with EXACTLY 2 operands.
    if not isinstance(test, ast.BoolOp):
        return False
    if not isinstance(test.op, ast.Or):
        return False
    if len(test.values) != 2:
        # 3+ operands → allowlisted (None-guard pattern)
        return False

    op0, op1 = test.values

    # operand[0]: UnaryOp(Not, Call(isinstance, ..., Name('str')))
    if not isinstance(op0, ast.UnaryOp):
        return False
    if not isinstance(op0.op, ast.Not):
        return False
    call0 = op0.operand
    if not isinstance(call0, ast.Call):
        return False
    func0 = call0.func
    if not (isinstance(func0, ast.Name) and func0.id == "isinstance"):
        return False
    # isinstance must have at least 2 args; second must be Name('str')
    if len(call0.args) < 2:
        return False
    second_arg = call0.args[1]
    if not (isinstance(second_arg, ast.Name) and second_arg.id == "str"):
        return False

    # operand[1]: UnaryOp(Not, <anything>)
    if not isinstance(op1, ast.UnaryOp):
        return False
    if not isinstance(op1.op, ast.Not):
        return False

    # body[0] must be a Raise of ValueError with "must be a non-empty string"
    if not node.body:
        return False
    stmt = node.body[0]
    if not isinstance(stmt, ast.Raise):
        return False
    exc = stmt.exc
    if not isinstance(exc, ast.Call):
        return False
    exc_func = exc.func
    if not (isinstance(exc_func, ast.Name) and exc_func.id == "ValueError"):
        return False
    if not exc.args:
        return False
    first_arg = exc.args[0]
    if not isinstance(first_arg, ast.Constant):
        return False
    if not isinstance(first_arg.value, str):
        return False
    if "must be a non-empty string" not in first_arg.value:
        return False

    return True


def test_no_inline_isinstance_str_raise_valueerror_pattern_in_lib_receipts() -> None:
    """No two-operand inline isinstance+ValueError pattern remains in lib_receipts.py (AC 12).

    Walks every If node in hooks/lib_receipts.py.  Fails with the matched
    line number(s) if any matching pattern is found.  3+-operand BoolOp
    nodes are excluded (allowlisted None-guard patterns).
    """
    source = LIB_RECEIPTS.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(LIB_RECEIPTS))

    matched_lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _is_target_pattern(node):
            matched_lines.append(node.lineno)

    assert not matched_lines, (
        f"hooks/lib_receipts.py still contains {len(matched_lines)} inline "
        f"isinstance+ValueError pattern(s) that should be migrated to "
        f"require_nonblank_str. Matched line(s): {matched_lines}"
    )
