"""Static analysis regression seal enforcing derivation-quality of every
.relative_to() call site in hooks/receipts/*.py.

## SEC-001 lineage

This check is a follow-on to PR #171 (task-20260506-003), which introduced
the path-traversal bounds-check pattern in hooks/receipts/approval.py, and
PR #175, which extended it to fixture detection. The residual trust item
`1616a422` noted that derivation quality of .relative_to() arguments was
not statically verified — any argument whose root was not a canonical helper
would slip through undetected. This file closes that gap.

## Six-step detection algorithm

The shared checker function _collect_derivation_violations() applies the
following six steps to every FunctionDef / AsyncFunctionDef in the parsed
source:

  Step 1: ast.parse(source_text); on SyntaxError return [].
  Step 2: Walk all nodes; for each FunctionDef or AsyncFunctionDef, inspect
          that function body in isolation.
  Step 3: Inside each function, collect all Call nodes whose func is an
          Attribute with attr == "relative_to". If none are found, skip the
          function entirely (no violation emitted for that function).
  Step 4: For each .relative_to() call found, take the first positional
          argument as arg_node. Resolve arg_name:
            - If arg_node is a Call whose func is an Attribute with
              attr == "resolve" and value is a bare Name, peel one level:
              arg_name = arg_node.func.value.id.
            - If arg_node is a bare Name, arg_name = arg_node.id.
            - Otherwise skip this call site (cannot analyze non-name args).
  Step 5: Walk Assign statements in the same function body to find the
          assignment whose target name == arg_name. Take its RHS. Apply one
          optional intermediate peel: if RHS is <name2>.resolve() (an
          Attribute call with attr == "resolve" on a bare Name value), set
          arg_name = name2 and find name2's assignment instead. Classify the
          (possibly peeled) RHS:
            - Pattern A (task_dir-rooted): RHS is Path(<Name id="task_dir">).resolve().
            - Pattern B (persistent-dir-rooted): RHS is a BinOp or Call whose
              depth-first subtree contains any Call whose func is a Name with
              id == "_persistent_project_dir".
            - Anything else: violation.
  Step 6: If no compliant assignment is found for arg_name in the function
          scope, emit a violation string with the exact format:
            {rel_path}:{lineno} — .relative_to() called with argument
            '{arg_name}' whose derivation root is unclassified
            (not task_dir or _persistent_project_dir)
          where lineno is the .relative_to() Call node's lineno and arg_name
          is the Step-4 name (before any Step-5 intermediate peel).

## Accepted limitations

- Single-level alias chains only. If arg_name is a multi-hop alias
  (e.g. a = _persistent_project_dir(...); b = a; x.relative_to(b)), the
  checker will flag it even though it is semantically compliant. Engineers
  adding new .relative_to() sites must use the canonical two-hop pattern
  (BinOp assignment directly from _persistent_project_dir, then .resolve()).
- Functions without any .relative_to() call are invisible to this check.
  The "must have a bounds check" rule is owned by PR #171 / PR #175; this
  check only validates derivation quality when a .relative_to() call is
  present.

## Negative-case approach

test_static_check_detects_unclassified_root feeds a synthetic Python source
string (containing a .relative_to() call whose argument traces to
some_config_path.resolve(), an unclassified root) through
_collect_derivation_violations() via ast.parse() and asserts that a
violation is returned. This matches the negative-case approach used by
PR #171 and PR #175.
"""

import ast
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Module-level constant documenting the two compliant derivation patterns.
# Each entry is a human-readable description of the pattern; the canonical
# AST structure is enforced by _collect_derivation_violations() below.
# Presence of this constant is structurally verifiable by inspection (AC-3).
# ---------------------------------------------------------------------------

_TRACKED_ROOTS: frozenset[str] = frozenset({
    "Path(task_dir).resolve() — Pattern A: task_dir-rooted derivation",
    "_persistent_project_dir(...) — Pattern B: persistent-dir-rooted derivation",
})


# ---------------------------------------------------------------------------
# Shared checker implementation
# ---------------------------------------------------------------------------


def _collect_derivation_violations(source_text: str, rel_path: str) -> list[str]:
    """Parse *source_text* and return a list of violation strings.

    Each violation has the exact form (AC-14):
        {rel_path}:{lineno} — .relative_to() called with argument '{arg_name}'
        whose derivation root is unclassified (not task_dir or _persistent_project_dir)

    *rel_path* is used verbatim as the file prefix. *arg_name* is the Step-4
    name (pre-peel), *lineno* is the integer line number of the .relative_to()
    Call node.

    On SyntaxError from ast.parse, returns [] (Step 1 safety requirement).
    """
    # Step 1: parse
    try:
        tree = ast.parse(source_text)
    except SyntaxError:
        return []

    violations: list[str] = []

    # Step 2: walk all function definitions in isolation
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        # Step 3: collect .relative_to() call nodes in this function
        relative_to_calls: list[ast.Call] = []
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "relative_to"
            ):
                relative_to_calls.append(inner)

        # Skip functions with no .relative_to() calls
        if not relative_to_calls:
            continue

        # Pre-collect all Assign / AnnAssign / AugAssign statements in this function body
        assign_stmts: list[ast.Assign | ast.AnnAssign | ast.AugAssign] = []
        for inner in ast.walk(node):
            if isinstance(inner, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                assign_stmts.append(inner)

        for call in relative_to_calls:
            # Step 4: resolve arg_name from the first positional argument
            if not call.args:
                continue
            arg_node = call.args[0]

            # Step-4 peel: <name>.resolve() -> arg_name = name
            if (
                isinstance(arg_node, ast.Call)
                and isinstance(arg_node.func, ast.Attribute)
                and arg_node.func.attr == "resolve"
                and isinstance(arg_node.func.value, ast.Name)
            ):
                arg_name = arg_node.func.value.id
            elif isinstance(arg_node, ast.Name):
                arg_name = arg_node.id
            else:
                # Cannot analyze non-name arguments — skip
                continue

            # Step-4 name is fixed; used for violation message and Step-5 lookup
            step4_arg_name = arg_name
            lineno = call.lineno

            # Step 5: find the assignment for arg_name in this function scope
            def _find_rhs(name: str) -> ast.expr | None:
                """Return the RHS of the LAST Assign / AnnAssign / AugAssign that
                binds *name* in this function scope, or None if no binding found.

                LAST-match-wins is load-bearing: an AugAssign that rebinds a name
                previously assigned to a compliant value must override the earlier
                compliant binding so the violation fires.
                """
                found_rhs: ast.expr | None = None
                for stmt in assign_stmts:
                    if isinstance(stmt, ast.Assign):
                        for target in stmt.targets:
                            if isinstance(target, ast.Name) and target.id == name:
                                found_rhs = stmt.value
                                break
                            if isinstance(target, (ast.Tuple, ast.List)):
                                for elt in target.elts:
                                    if isinstance(elt, ast.Name) and elt.id == name:
                                        found_rhs = stmt.value
                                        break
                    elif isinstance(stmt, ast.AnnAssign):
                        # Bare annotation `x: int` has value=None — not a binding.
                        if (
                            stmt.value is not None
                            and isinstance(stmt.target, ast.Name)
                            and stmt.target.id == name
                        ):
                            found_rhs = stmt.value
                    elif isinstance(stmt, ast.AugAssign):
                        if isinstance(stmt.target, ast.Name) and stmt.target.id == name:
                            # Strict interpretation: AugAssign rebinds to .value
                            # (an operand, not the synthesized BinOp). _classify_rhs
                            # will not match Pattern A or B → violation fires.
                            found_rhs = stmt.value
                return found_rhs

            rhs = _find_rhs(arg_name)

            if rhs is None:
                # No assignment found for arg_name — unclassified
                violations.append(
                    f"{rel_path}:{lineno} — .relative_to() called with argument "
                    f"'{step4_arg_name}' whose derivation root is unclassified "
                    f"(not task_dir or _persistent_project_dir)"
                )
                continue

            # Step-5 optional intermediate peel: if RHS is <name2>.resolve(),
            # reassign arg_name = name2 and look up name2's assignment instead.
            # This handles: resolved_safe = safe_postmortems_dir.resolve()
            if (
                isinstance(rhs, ast.Call)
                and isinstance(rhs.func, ast.Attribute)
                and rhs.func.attr == "resolve"
                and isinstance(rhs.func.value, ast.Name)
            ):
                peeled_name = rhs.func.value.id
                peeled_rhs = _find_rhs(peeled_name)
                if peeled_rhs is not None:
                    # Use the peeled RHS for classification
                    rhs = peeled_rhs

            # Step-5 classification
            compliant = _classify_rhs(rhs)

            if not compliant:
                violations.append(
                    f"{rel_path}:{lineno} — .relative_to() called with argument "
                    f"'{step4_arg_name}' whose derivation root is unclassified "
                    f"(not task_dir or _persistent_project_dir)"
                )

    return violations


def _is_pattern_b_compliant(node: ast.expr) -> bool:
    """Return True iff *node* is a direct Call to _persistent_project_dir,
    or an ast.BinOp whose .left chain (recursing through nested BinOps)
    terminates in such a Call. Mirrors the canonical
    _persistent_project_dir(...) / "x" [/ "y" ...] grammar in approval.py.
    """
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_persistent_project_dir"
    ):
        return True
    if isinstance(node, ast.BinOp):
        return _is_pattern_b_compliant(node.left)
    return False


def _classify_rhs(rhs: ast.expr) -> bool:
    """Return True if *rhs* matches Pattern A (task_dir-rooted) or Pattern B
    (persistent-dir-rooted).

    Pattern A: Path(task_dir).resolve()
      - rhs is ast.Call
      - rhs.func is ast.Attribute with attr == "resolve"
      - rhs.func.value is ast.Call
      - rhs.func.value.func is ast.Name with id == "Path"
      - rhs.func.value has exactly one positional arg that is ast.Name id == "task_dir"

    Pattern B: direct Call to _persistent_project_dir, OR BinOp whose .left chain
      (recursing through nested BinOps) terminates in such a Call
    """
    # Pattern A
    if (
        isinstance(rhs, ast.Call)
        and isinstance(rhs.func, ast.Attribute)
        and rhs.func.attr == "resolve"
        and isinstance(rhs.func.value, ast.Call)
        and isinstance(rhs.func.value.func, ast.Name)
        and rhs.func.value.func.id == "Path"
        and len(rhs.func.value.args) == 1
        and isinstance(rhs.func.value.args[0], ast.Name)
        and rhs.func.value.args[0].id == "task_dir"
    ):
        return True

    # Pattern B: direct Call to _persistent_project_dir, OR a BinOp whose
    # .left chain (recursing through nested BinOps) terminates in such a Call.
    # Tightened from the prior ast.walk-over-subtree form (which accepted any
    # descendant Call and was bypassable via wrapper Calls / IfExp / lambda
    # discards / helper-arg passthrough; see SEAL-001 from task-20260506-005).
    if _is_pattern_b_compliant(rhs):
        return True

    return False


# ---------------------------------------------------------------------------
# Test 1 (AC-7, AC-8, AC-13): Real scan of hooks/receipts/*.py
# ---------------------------------------------------------------------------


def test_receipts_relative_to_args_have_compliant_derivation() -> None:
    """Scan hooks/receipts/*.py and assert that every .relative_to() call
    derives its argument from either Path(task_dir).resolve() (Pattern A)
    or _persistent_project_dir(...) (Pattern B).

    Asserts at least one .py file was found (catches misconfigured paths).
    On any violation calls pytest.fail() with a bullet-list message.

    Against the current codebase (all three existing sites compliant), this
    test passes with zero violations (GREEN-at-write).
    """
    receipts_dir = Path(__file__).parent.parent / "hooks" / "receipts"
    py_files = sorted(receipts_dir.glob("*.py"))

    assert py_files, f"No .py files found under {receipts_dir}"

    repo_root = Path(__file__).parent.parent
    all_violations: list[str] = []

    for py_file in py_files:
        source = py_file.read_text(encoding="utf-8")
        rel_path = str(py_file.relative_to(repo_root))
        violations = _collect_derivation_violations(source, rel_path)
        all_violations.extend(violations)

    if all_violations:
        bullet_list = "\n".join(f"  • {v}" for v in all_violations)
        pytest.fail(
            f"Found {len(all_violations)} .relative_to() site(s) with "
            f"unclassified derivation root:\n{bullet_list}"
        )


# ---------------------------------------------------------------------------
# Test 2 (AC-9): Negative case — checker detects unclassified derivation root
# ---------------------------------------------------------------------------


def test_static_check_detects_unclassified_root() -> None:
    """Feed a synthetic source with a .relative_to() argument derived from
    some_config_path.resolve() — an unclassified root — and assert that at
    least one violation is returned naming 'resolved_config' and 'unclassified'.
    """
    source = """\
def bad_checker(task_dir, some_config_path):
    resolved_config = some_config_path.resolve()
    x.relative_to(resolved_config)
"""
    violations = _collect_derivation_violations(source, "hooks/receipts/fake.py")

    assert violations, (
        "Expected at least one violation for the synthetic source whose "
        ".relative_to() argument traces to some_config_path.resolve() "
        "(unclassified root), but the checker returned no violations."
    )

    combined = "\n".join(violations)
    assert "resolved_config" in combined, (
        f"Expected 'resolved_config' in violation message, got: {combined!r}"
    )
    assert "unclassified" in combined, (
        f"Expected 'unclassified' in violation message, got: {combined!r}"
    )


# ---------------------------------------------------------------------------
# Test 3 (AC-10): False-positive guard — Pattern A (task_dir-rooted)
# ---------------------------------------------------------------------------


def test_static_check_passes_on_task_dir_rooted_relative_to() -> None:
    """A function whose .relative_to() argument traces to Path(task_dir).resolve()
    must produce zero violations (Pattern A compliant).

    This guards against the checker regressing and falsely flagging the two
    existing stage.py sites.
    """
    source = """\
def good_checker(task_dir, report_path):
    report_file = Path(report_path)
    resolved_report = report_file.resolve()
    resolved_task = Path(task_dir).resolve()
    resolved_report.relative_to(resolved_task)
"""
    violations = _collect_derivation_violations(source, "hooks/receipts/stage.py")

    assert violations == [], (
        f"Expected zero violations for Pattern A (task_dir-rooted) .relative_to() "
        f"site, but got: {violations}"
    )


# ---------------------------------------------------------------------------
# Test 4 (AC-11): False-positive guard — Pattern B (persistent-dir-rooted)
# ---------------------------------------------------------------------------


def test_static_check_passes_on_persistent_dir_rooted_relative_to() -> None:
    """A function whose .relative_to() argument traces to
    _persistent_project_dir(...) / "postmortems" via a two-hop chain
    (BinOp assignment -> .resolve() assignment -> .relative_to() argument)
    must produce zero violations (Pattern B compliant via intermediate peel).

    This is the load-bearing test for the Step-5 intermediate peel in the
    checker. Without the peel, the approval.py site would be falsely flagged.
    """
    source = """\
def good_checker(task_dir, postmortem_json_path):
    root = task_dir.parent.parent
    safe_postmortems_dir = _persistent_project_dir(root) / "postmortems"
    json_path = Path(postmortem_json_path)
    resolved_json = json_path.resolve()
    resolved_safe = safe_postmortems_dir.resolve()
    resolved_json.relative_to(resolved_safe)
"""
    violations = _collect_derivation_violations(source, "hooks/receipts/approval.py")

    assert violations == [], (
        f"Expected zero violations for Pattern B (persistent-dir-rooted) "
        f".relative_to() site, but got: {violations}"
    )


# ---------------------------------------------------------------------------
# Test 5 (AC-12): False-positive guard — function with no .relative_to() call
# ---------------------------------------------------------------------------


def test_static_check_passes_on_function_without_relative_to() -> None:
    """A function that contains no .relative_to() call must produce zero
    violations. The derivation-quality check is invisible to such functions;
    the 'must have a bounds check' rule is owned by PR #171.
    """
    source = """\
def no_bounds_check(task_dir, report_path):
    report_file = Path(report_path)
    with report_file.open("r", encoding="utf-8") as fh:
        data = fh.read()
"""
    violations = _collect_derivation_violations(source, "hooks/receipts/fake.py")

    assert violations == [], (
        f"Expected zero violations for a function with no .relative_to() call, "
        f"but got: {violations}"
    )


# ---------------------------------------------------------------------------
# Test 6 (AC-6, AC-7): Parametrized negative tests — bypass shapes are flagged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "param_id,source",
    [
        (
            "bypass_max_wrapper",
            """\
def bad_checker(task_dir, some_unsafe_path):
    root = task_dir.parent.parent
    pm = max(some_unsafe_path, _persistent_project_dir(root))
    resolved_pm = pm.resolve()
    x.relative_to(resolved_pm)
""",
        ),
        (
            "bypass_ifexp",
            """\
def bad_checker(task_dir, some_unsafe_path, cond):
    root = task_dir.parent.parent
    pm = _persistent_project_dir(root) if cond else some_unsafe_path
    resolved_pm = pm.resolve()
    x.relative_to(resolved_pm)
""",
        ),
        (
            "bypass_lambda",
            """\
def bad_checker(task_dir, some_unsafe_path):
    root = task_dir.parent.parent
    pm = (lambda p: some_unsafe_path)(_persistent_project_dir(root))
    resolved_pm = pm.resolve()
    x.relative_to(resolved_pm)
""",
        ),
        (
            "bypass_helper_arg",
            """\
def bad_checker(task_dir, some_unsafe_path):
    root = task_dir.parent.parent
    pm = some_helper(_persistent_project_dir(root))
    resolved_pm = pm.resolve()
    x.relative_to(resolved_pm)
""",
        ),
    ],
    ids=["bypass_max_wrapper", "bypass_ifexp", "bypass_lambda", "bypass_helper_arg"],
)
def test_static_check_flags_pattern_b_bypass_shapes(
    param_id: str, source: str
) -> None:
    """Each bypass shape must be flagged as a violation by the tightened
    Pattern B classifier. The tightened helper (_is_pattern_b_compliant)
    only accepts a direct Call to _persistent_project_dir or a BinOp whose
    .left chain terminates in such a Call — rejecting all four bypass shapes.
    """
    violations = _collect_derivation_violations(source, "hooks/receipts/fake.py")

    assert violations != [], (
        f"Expected at least one violation for bypass shape '{param_id}', "
        f"but the tightened classifier produced no violations. "
        f"This means the classifier incorrectly accepted a bypass shape as compliant."
    )


# ---------------------------------------------------------------------------
# Test 7 (AC-7, AC-8, AC-11): AnnAssign compliant roots — accepted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "param_id,source",
    [
        (
            "annassign_pattern_a_compliant",
            """\
def check(task_dir, other):
    pm: Path = Path(task_dir).resolve()
    x = something()
    x.relative_to(pm)
""",
        ),
        (
            "annassign_pattern_b_compliant",
            """\
def check(root, pm):
    safe_dir: Path = _persistent_project_dir(root) / "postmortems"
    resolved_safe = safe_dir.resolve()
    pm.relative_to(resolved_safe)
""",
        ),
    ],
    ids=["annassign_pattern_a_compliant", "annassign_pattern_b_compliant"],
)
def test_static_check_accepts_annassign_compliant_roots(
    param_id: str, source: str
) -> None:
    """AnnAssign-bound Pattern A and Pattern B roots must produce zero violations.

    Pattern A: pm: Path = Path(task_dir).resolve() — annotated assignment.
    Pattern B: safe_dir: Path = _persistent_project_dir(root) / "postmortems"
    followed by resolved_safe = safe_dir.resolve() (Step-5 intermediate peel).
    """
    violations = _collect_derivation_violations(source, "hooks/receipts/fake.py")

    assert violations == [], (
        f"Expected zero violations for AnnAssign compliant case '{param_id}', "
        f"but got: {violations}"
    )


# ---------------------------------------------------------------------------
# Test 8 (AC-9, AC-10, AC-12): AugAssign and AnnAssign unclassified — flagged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "param_id,source",
    [
        (
            "augassign_violation",
            """\
def check(root, x):
    pm = _persistent_project_dir(root)
    pm /= "postmortems"
    x.relative_to(pm)
""",
        ),
        (
            "annassign_unclassified_violation",
            """\
def check(some_config_path, x):
    bad_root: Path = some_config_path.resolve()
    x.relative_to(bad_root)
""",
        ),
    ],
    ids=["augassign_violation", "annassign_unclassified_violation"],
)
def test_static_check_flags_augassign_and_annassign_unclassified(
    param_id: str, source: str
) -> None:
    """AugAssign rebinding and AnnAssign with unclassified RHS must each produce
    at least one violation.

    augassign_violation: pm is initially Pattern B-compliant, then rebound via
    pm /= "postmortems". LAST-match-wins returns Constant("postmortems") as the
    RHS, which is neither Pattern A nor Pattern B. Violation fires.

    annassign_unclassified_violation: bad_root: Path = some_config_path.resolve()
    — the receiver of .resolve() is some_config_path (a Name with no assignment
    in scope), so classification falls through to unclassified. Violation fires.
    """
    violations = _collect_derivation_violations(source, "hooks/receipts/fake.py")

    assert violations != [], (
        f"Expected at least one violation for case '{param_id}', "
        f"but the checker produced no violations. "
        f"This means the checker incorrectly accepted an unsafe binding."
    )
