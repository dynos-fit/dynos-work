"""CI CHECK-2 (Class B — self-verify parity lint).

Closes the "writer trusts caller's numeric/hash" class by asserting
every ``receipt_*`` writer in ``hooks/lib_receipts.py`` that accepts a
hash/path parameter AND a numeric/list parameter the receipt payload
will be trusted for ALSO has a self-verify cross-check: the writer
either opens the hashed artifact and re-derives the trusted value, OR
refuses caller-supplied values via ``TypeError`` on legacy kwargs.

This is task-007's prevention for Class B self-report forgery. The
test parses ``lib_receipts.py`` with ``ast`` and checks each writer's
body for one of the approved self-verify patterns:

  * ``if _legacy: raise TypeError(...)`` — caller refuses path (the
    pattern used by the self-compute writers introduced in task-007
    B-001/B-002/B-003/B-004/B-006).
  * ``if <caller_param> != <computed_value>: raise ValueError(...)`` —
    cross-check path (the pattern used by ``receipt_audit_done``
    with ``actual_finding_count``).
  * Opening the hashed artifact via ``hash_file`` / ``Path.open`` /
    ``json.load`` — direct re-derivation path.

A writer exempt from this rule (no numeric/list payload beyond the
hash itself) is allowed. The allowlist below is the set of writers
for which no caller-supplied payload is trusted beyond what the
receipt's own hashes cover.

No wildcard escape — every exemption is named.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS = REPO_ROOT / "hooks" / "lib_receipts.py"
sys.path.insert(0, str(REPO_ROOT / "hooks"))

import lib_receipts  # noqa: E402


# Writers exempt from self-verify because they carry no trusted payload
# beyond artifact hashes (or their payload IS the hash itself, which is
# the thing being verified). Every name here is reviewed and named —
# there is no wildcard.
_SELF_VERIFY_EXEMPT = {
    "receipt_human_approval",       # payload: stage + user-approval hash; hash IS the artifact
    "receipt_executor_routing",     # payload: routed segments list from planner
    "receipt_audit_routing",        # payload: routed auditors list from planner
    "receipt_postmortem_skipped",   # payload: skip reason + skip-rule hash
    "receipt_calibration_applied",  # payload: before/after retros_consumed — hashed
    "receipt_calibration_noop",     # payload: reason + noop-rule hash
    "receipt_postmortem_analysis",  # payload: analysis hash + finding_count; self-compute would require LLM output parsing
    "receipt_tdd_tests",            # payload: file list + evidence hash; hash IS the artifact
    "receipt_planner_spawn",        # payload: phase + tokens_used; tokens_used fundamentally unverifiable (B-005 acknowledges this)
    "receipt_rules_check_passed",   # payload: error_violations + rules_file_sha256; self-verify against rules_engine at gate time (B-008)
    "receipt_force_override",       # payload: bypassed_gates observability snapshot supplied by caller; no authoritative on-disk artifact to self-verify against
    "receipt_scheduler_refused",    # payload: missing_proofs observability snapshot from compute_next_stage; no authoritative on-disk artifact to self-verify against (parallel to receipt_force_override)
    "receipt_post_completion",      # payload: handlers_run list; self-verified via verify_signed_events (HMAC-checked events log); AST checker cannot see through the cross-module call boundary
}


def _parse_receipts_module() -> ast.Module:
    src = HOOKS.read_text(encoding="utf-8")
    return ast.parse(src, filename=str(HOOKS))


def _iter_receipt_writers(mod: ast.Module):
    for node in ast.walk(mod):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("receipt_"):
            yield node


def _has_legacy_typeerror_refusal(fn: ast.FunctionDef) -> bool:
    """Approved pattern A: ``if _legacy: raise TypeError(...)``.

    Matches any early refusal that raises TypeError keyed off a
    ``**_legacy`` / ``**_deprecated`` kwarg bucket. This is the
    self-compute writer pattern (task-007 B-001..B-004, B-006).
    """
    for stmt in ast.walk(fn):
        if isinstance(stmt, ast.If):
            # Condition references an identifier likely named *_legacy*.
            names = [n.id for n in ast.walk(stmt.test) if isinstance(n, ast.Name)]
            if not any(n.startswith("_legacy") or n == "_legacy" or "_deprecated" in n
                       for n in names):
                continue
            # Body raises TypeError.
            for sub in ast.walk(stmt):
                if (isinstance(sub, ast.Raise) and isinstance(sub.exc, ast.Call)
                        and isinstance(sub.exc.func, ast.Name)
                        and sub.exc.func.id == "TypeError"):
                    return True
    return False


def _has_cross_check_valueerror(fn: ast.FunctionDef) -> bool:
    """Approved pattern B: a caller-supplied value is compared against a
    locally-derived ``actual_*`` / ``computed_*`` variable, and mismatch
    raises ValueError.

    Heuristic: look for any ``ast.Compare`` whose comparators reference
    a name starting with ``actual_`` or ``computed_`` or ``expected_``
    inside a function body that raises ValueError downstream.
    """
    has_computed_compare = False
    has_value_error = False
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Compare):
            names = [n.id for n in ast.walk(sub) if isinstance(n, ast.Name)]
            if any(n.startswith(("actual_", "computed_", "expected_")) for n in names):
                has_computed_compare = True
        if (isinstance(sub, ast.Raise) and isinstance(sub.exc, ast.Call)
                and isinstance(sub.exc.func, ast.Name)
                and sub.exc.func.id == "ValueError"):
            has_value_error = True
    return has_computed_compare and has_value_error


def _has_hash_artifact_open(fn: ast.FunctionDef) -> bool:
    """Approved pattern C: the writer opens an artifact (``Path.open``
    or ``json.load`` or ``hash_file``) — evidence of direct re-derivation."""
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
            if sub.func.attr in {"open", "read_text", "read_bytes"}:
                return True
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
            if sub.func.id in {"hash_file", "parse_acceptance_criteria"}:
                return True
    return False


def _has_numeric_or_list_param(fn: ast.FunctionDef) -> bool:
    """Does the writer accept a numeric or list parameter (beyond the
    hash/path/string inputs)?"""
    for arg in fn.args.args + fn.args.kwonlyargs:
        annotation = ast.unparse(arg.annotation) if arg.annotation else ""
        if any(t in annotation for t in ("int", "list", "dict", "float", "bool")):
            return True
    return False


def test_every_receipt_writer_has_self_verify_or_is_exempt():
    """task-007 CHECK-2: every caller-supplied payload is either
    self-computed, cross-checked, or the writer is explicitly exempt.

    Any writer that accepts a numeric/list parameter AND doesn't open
    an artifact, refuse legacy kwargs, or cross-check caller values
    fails this test. The fix is to migrate the writer to self-compute
    (preferred) or document why no self-verify is possible (extend the
    exempt list with a one-line justification)."""
    mod = _parse_receipts_module()
    missing: list[str] = []
    for fn in _iter_receipt_writers(mod):
        if fn.name in _SELF_VERIFY_EXEMPT:
            continue
        if not _has_numeric_or_list_param(fn):
            continue
        ok = (
            _has_legacy_typeerror_refusal(fn)
            or _has_cross_check_valueerror(fn)
            or _has_hash_artifact_open(fn)
        )
        if not ok:
            missing.append(f"{fn.name} (line {fn.lineno})")
    assert not missing, (
        "writer(s) accept caller-supplied numeric/list payload but show no "
        "self-verify pattern (_legacy TypeError refusal, computed_* "
        "ValueError cross-check, or artifact open). Either self-compute, "
        "add a cross-check, or document the exemption in "
        f"_SELF_VERIFY_EXEMPT: {missing}"
    )


def test_self_compute_writers_refuse_legacy_kwargs():
    """Spot-check: the 6 writers migrated by task-007 B-001..B-006 must
    all raise TypeError on any legacy caller-supplied kwarg."""
    migrated = [
        "receipt_postmortem_generated",  # B-001
        "receipt_retrospective",         # B-002
        "receipt_spec_validated",        # B-003
        "receipt_plan_validated",        # B-004
        "receipt_plan_audit",            # B-006
    ]
    mod = _parse_receipts_module()
    fns = {fn.name: fn for fn in _iter_receipt_writers(mod)}
    not_refusing = [
        name for name in migrated
        if name in fns and not _has_legacy_typeerror_refusal(fns[name])
    ]
    assert not not_refusing, (
        "task-007 self-compute writer(s) missing the "
        "'if _legacy: raise TypeError(...)' refusal pattern: "
        f"{not_refusing}"
    )
