from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"

# Tests that legitimately use only structural assertions (callable, isinstance, hasattr).
# Each entry: "filename::function_name" → justification string.
# Add to this allowlist only when the test is intentionally a crash-guard or
# a function-pointer registry contract test — not to silence false positives.
_GHOST_TEST_ALLOWLIST: dict[str, str] = {
    "test_plug_and_play.py::test_builtin_handler_callables_accept_root_and_payload": (
        "Legitimate function-pointer registry contract test: verifies callable + 2-param "
        "signature for each handler entry. Structural assertions are the correct contract "
        "here because the registry stores function references, not return values."
    ),
    "test_receipts_exports.py::test_every_receipt_name_is_callable": (
        "API surface contract: verifies every exported receipt_* writer is callable. "
        "Catches accidental replacement of a function with a constant or class instance."
    ),
    "test_refactor_decomposition.py::test_project_dir_importable_via_facade": (
        "Facade re-export contract: verifies lib.project_dir is callable after import. "
        "Meaningful because the facade could accidentally export a non-callable alias."
    ),
    "test_refactor_decomposition.py::test_is_pid_running_importable_via_facade": (
        "Facade re-export contract: verifies lib.is_pid_running is callable after import. "
        "Meaningful because the facade could accidentally export a non-callable alias."
    ),
}

_BANNED_SNIPPETS = (
    "TODAY these tests FAIL",
    "This import MUST fail today",
    "MUST fail today",
    "collection itself will error",
)


def _iter_test_functions(tree: ast.Module) -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Yield (name, node) for every test_* function at module level and inside classes."""
    results = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test_"):
                results.append((node.name, node))
    return results


def _count_asserts(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Count meaningful assertion nodes anywhere in the function body.

    Counts:
    - ast.Assert statements
    - with pytest.raises(...) context managers (exception assertion)
    - pytest.fail(...) calls (explicit failure trigger)
    """
    count = 0
    for n in ast.walk(func_node):
        if isinstance(n, ast.Assert):
            count += 1
        elif isinstance(n, ast.With):
            for item in n.items:
                ce = item.context_expr
                if isinstance(ce, ast.Call):
                    func = ce.func
                    if (isinstance(func, ast.Attribute) and func.attr == "raises") or (
                        isinstance(func, ast.Name) and func.id == "raises"
                    ):
                        count += 1
        elif isinstance(n, ast.Expr) and isinstance(n.value, ast.Call):
            call = n.value
            func = call.func
            # pytest.fail(...) is an explicit failure assertion
            if isinstance(func, ast.Attribute) and func.attr == "fail":
                count += 1
    return count


def _is_tautological_assert(assert_node: ast.Assert) -> bool:
    """Return True if this assert is a tautological structural pattern.

    Only flags assertions that always pass for any successfully imported symbol:
    - assert callable(x)       — any imported function is callable
    - assert isinstance(x, T)  — always True once the type is known

    NOT flagged (these are real assertions):
    - assert x is not None     — meaningful for function return values
    - assert hasattr(x, name)  — tests API surface with a real key
    """
    test = assert_node.test
    # assert callable(x)
    if (
        isinstance(test, ast.Call)
        and isinstance(test.func, ast.Name)
        and test.func.id == "callable"
    ):
        return True
    # assert isinstance(x, ...)
    if (
        isinstance(test, ast.Call)
        and isinstance(test.func, ast.Name)
        and test.func.id == "isinstance"
    ):
        return True
    return False


def test_no_stale_intentional_red_state_scaffolding_in_default_tests() -> None:
    offenders: list[str] = []
    for path in sorted(TESTS.glob("test_*.py")):
        if path.name == "test_test_suite_hygiene.py":
            continue
        text = path.read_text(encoding="utf-8")
        for snippet in _BANNED_SNIPPETS:
            if snippet in text:
                offenders.append(f"{path.relative_to(ROOT)}: {snippet}")
    assert not offenders, (
        "Default test suite contains stale intentional-red scaffolding:\n"
        + "\n".join(offenders)
    )


def test_no_zero_assertion_test_functions() -> None:
    """Every test_* function must contain at least one ast.Assert node.

    Tests with no assertions always pass regardless of implementation correctness.
    If a test is intentionally a crash-guard or structural contract test, add it
    to _GHOST_TEST_ALLOWLIST with a justification.
    """
    offenders: list[str] = []
    for path in sorted(TESTS.glob("test_*.py")):
        if path.name == "test_test_suite_hygiene.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            offenders.append(f"{path.name}: SyntaxError: {exc}")
            continue
        for fn_name, fn_node in _iter_test_functions(tree):
            if _count_asserts(fn_node) == 0:
                key = f"{path.name}::{fn_name}"
                if key not in _GHOST_TEST_ALLOWLIST:
                    offenders.append(key)
    assert not offenders, (
        "Test functions with zero assertions (ghost tests) found.\n"
        "Add to _GHOST_TEST_ALLOWLIST with justification if intentional:\n"
        + "\n".join(offenders)
    )


def test_no_tautological_only_assertions() -> None:
    """Test functions whose every assertion is a tautological structural check
    (callable, is not None, isinstance) always pass for any non-None importable object.

    A test is flagged if: it has >= 1 assert AND every assert is tautological.
    Add to _GHOST_TEST_ALLOWLIST with justification if the structural assertion
    is the correct behavioral contract (e.g. function-pointer registries).
    """
    offenders: list[str] = []
    for path in sorted(TESTS.glob("test_*.py")):
        if path.name == "test_test_suite_hygiene.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for fn_name, fn_node in _iter_test_functions(tree):
            assert_nodes = [n for n in ast.walk(fn_node) if isinstance(n, ast.Assert)]
            if not assert_nodes:
                continue  # zero-assertion case handled by test_no_zero_assertion_test_functions
            if all(_is_tautological_assert(a) for a in assert_nodes):
                key = f"{path.name}::{fn_name}"
                if key not in _GHOST_TEST_ALLOWLIST:
                    offenders.append(key)
    assert not offenders, (
        "Test functions with only tautological assertions found.\n"
        "These always pass regardless of implementation correctness.\n"
        "Add to _GHOST_TEST_ALLOWLIST with justification if intentional:\n"
        + "\n".join(offenders)
    )
