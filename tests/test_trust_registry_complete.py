"""TDD-first tests for task-20260420-001 D4 — trust registry.

Covers acceptance criteria 19, 20, 21, 22:

    AC19 .dynos/trust-registry.json exists with schema:
           {version: 1, entries: [{id, file, function, field, type,
                                  justification (>=20 chars), added_in_task}]}
    AC20 Registry includes all 5 bootstrap entries for pre-existing
         caller-supplied fields (from task-009 + inherits).
    AC21 AST-walking lint asserts every caller-supplied-string field in
         receipt_* functions + transition_task is registered.
    AC22 Registry-complete check is a merge blocker: the test file is
         discoverable under default pytest, no module-level skips.

TODAY these tests FAIL because .dynos/trust-registry.json does not exist.
"""

from __future__ import annotations

import ast
import json
import re
import sys
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = REPO_ROOT / ".dynos" / "trust-registry.json"

sys.path.insert(0, str(REPO_ROOT / "hooks"))


# ---------------------------------------------------------------------------
# AC19 — schema
# ---------------------------------------------------------------------------


def _load_registry() -> dict:
    assert REGISTRY_PATH.exists(), (
        f"{REGISTRY_PATH.relative_to(REPO_ROOT)} must exist and be committed. "
        f"Missing means D4 has not been shipped."
    )
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def test_registry_file_exists_with_schema():
    data = _load_registry()
    assert data.get("version") == 1
    entries = data.get("entries")
    assert isinstance(entries, list)
    assert len(entries) >= 5
    required_keys = {
        "id",
        "file",
        "function",
        "field",
        "type",
        "justification",
        "added_in_task",
    }
    for i, entry in enumerate(entries):
        assert isinstance(entry, dict), f"entry {i} must be a dict"
        missing = required_keys - set(entry.keys())
        assert not missing, f"entry {i} missing keys: {missing}"
        for k in required_keys:
            v = entry[k]
            assert isinstance(v, str) and v, (
                f"entry {i}.{k} must be a non-empty string (got {v!r})"
            )
        assert len(entry["justification"]) >= 20, (
            f"entry {entry['id']!r} justification must be >=20 chars, "
            f"got {len(entry['justification'])}"
        )


# ---------------------------------------------------------------------------
# AC20 — bootstrap entries
# ---------------------------------------------------------------------------


_BOOTSTRAP_IDS = {
    "receipt_force_override.reason",
    "receipt_force_override.approver",
    "receipt_human_approval.approver",
    "transition_task.force_reason",
    "transition_task.force_approver",
}


def test_registry_has_five_bootstrap_entries():
    data = _load_registry()
    ids = {e["id"] for e in data["entries"]}
    missing = _BOOTSTRAP_IDS - ids
    assert not missing, f"bootstrap entries missing: {missing}"


def test_bootstrap_entries_point_to_correct_files():
    data = _load_registry()
    by_id = {e["id"]: e for e in data["entries"]}
    for fid in {
        "receipt_force_override.reason",
        "receipt_force_override.approver",
        "receipt_human_approval.approver",
    }:
        assert by_id[fid]["file"] == "hooks/lib_receipts.py", (
            f"{fid} must point to hooks/lib_receipts.py"
        )
    for fid in {"transition_task.force_reason", "transition_task.force_approver"}:
        assert by_id[fid]["file"] == "hooks/lib_core.py", (
            f"{fid} must point to hooks/lib_core.py"
        )


# ---------------------------------------------------------------------------
# AC21 — AST lint enforces completeness
# ---------------------------------------------------------------------------


class _CallerSuppliedCollector(ast.NodeVisitor):
    """Identify string-typed parameters of receipt_* / transition_task
    functions whose only in-body usage is `write_receipt(..., field=param)`
    or `require_*(param, ...)` — the AC21 heuristic."""

    TARGET_PREFIXES = ("receipt_",)
    TARGET_EXACT = frozenset({"transition_task"})

    def __init__(self) -> None:
        self.candidates: list[tuple[str, str]] = []  # (function, field)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        is_target = node.name.startswith(self.TARGET_PREFIXES) or (
            node.name in self.TARGET_EXACT
        )
        if not is_target:
            self.generic_visit(node)
            return
        params = self._string_param_names(node)
        used_in_writer: set[str] = set()
        derived_from_disk: set[str] = set()

        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                # field=param passthroughs into write_receipt
                if self._is_write_receipt_call(sub):
                    for kw in sub.keywords or []:
                        if (
                            kw.arg
                            and isinstance(kw.value, ast.Name)
                            and kw.value.id in params
                        ):
                            used_in_writer.add(kw.value.id)
                # disk-derived reads: hash_file/read_receipt/load_json/...
                if self._is_disk_reader_call(sub):
                    for a in sub.args or []:
                        if isinstance(a, ast.Name) and a.id in params:
                            derived_from_disk.add(a.id)

        caller_supplied = (used_in_writer - derived_from_disk)
        for field in sorted(caller_supplied):
            self.candidates.append((node.name, field))

        self.generic_visit(node)

    @staticmethod
    def _string_param_names(fn: ast.FunctionDef) -> set[str]:
        """Return parameters annotated as str (or optional str) — the
        heuristic. Un-annotated params are accepted too if their default
        is a string literal or None."""
        names: set[str] = set()
        all_args = list(fn.args.args) + list(fn.args.kwonlyargs)
        for a in all_args:
            ann = a.annotation
            is_stringy = False
            if ann is None:
                is_stringy = True  # un-annotated is a candidate
            elif isinstance(ann, ast.Name) and ann.id == "str":
                is_stringy = True
            elif isinstance(ann, ast.Subscript):
                # Optional[str] / Union[str, None] / str | None shapes
                text = ast.unparse(ann) if hasattr(ast, "unparse") else ""
                if "str" in text:
                    is_stringy = True
            elif isinstance(ann, ast.BinOp):
                text = ast.unparse(ann) if hasattr(ast, "unparse") else ""
                if "str" in text:
                    is_stringy = True
            if is_stringy:
                names.add(a.arg)
        return names

    @staticmethod
    def _is_write_receipt_call(call: ast.Call) -> bool:
        if isinstance(call.func, ast.Name) and call.func.id == "write_receipt":
            return True
        if isinstance(call.func, ast.Attribute) and call.func.attr == "write_receipt":
            return True
        return False

    @staticmethod
    def _is_disk_reader_call(call: ast.Call) -> bool:
        disk_readers = {
            "hash_file",
            "read_receipt",
            "load_json",
        }
        if isinstance(call.func, ast.Name) and call.func.id in disk_readers:
            return True
        if (
            isinstance(call.func, ast.Attribute)
            and call.func.attr in disk_readers
        ):
            return True
        return False


def _collect_candidates(path: Path) -> list[tuple[str, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    v = _CallerSuppliedCollector()
    v.visit(tree)
    return v.candidates


def test_registry_ast_lint_finds_zero_unregistered_fields():
    data = _load_registry()
    registered = {e["id"] for e in data["entries"]}

    unregistered: list[str] = []
    for rel in ("hooks/lib_receipts.py", "hooks/lib_core.py"):
        p = REPO_ROOT / rel
        for (fn, field) in _collect_candidates(p):
            eid = f"{fn}.{field}"
            if eid not in registered:
                unregistered.append(f"{rel}: {eid}")
    assert not unregistered, (
        "Every caller-supplied-string field in receipt_* / transition_task "
        "MUST be registered in trust-registry.json with non-empty "
        "justification. Unregistered:\n  " + "\n  ".join(unregistered)
    )


def test_lint_fires_on_synthetic_unregistered_field(tmp_path: Path):
    """Inject a tmp module with an unregistered caller-supplied field and
    prove the AST collector finds it."""
    fake = tmp_path / "fake_writer.py"
    fake.write_text(
        textwrap.dedent(
            '''
            def receipt_new_thing(task_dir, *, new_field: str):
                """New writer with an unregistered caller-supplied field."""
                return write_receipt(task_dir, "new-thing", new_field=new_field)
            '''
        ).strip()
        + "\n"
    )
    cands = _collect_candidates(fake)
    assert ("receipt_new_thing", "new_field") in cands, (
        f"lint must catch the synthetic unregistered field. Got: {cands}"
    )


# ---------------------------------------------------------------------------
# AC22 — merge blocker: no module-level skips in this test file
# ---------------------------------------------------------------------------


def test_this_test_file_has_no_module_level_skip():
    text = Path(__file__).read_text(encoding="utf-8")
    assert not re.search(r"^pytest\.skip", text, flags=re.M), (
        "AC22: this test must be a merge blocker; no pytest.skip at module level"
    )
    assert not re.search(r"pytestmark\s*=\s*pytest\.mark\.skip", text), (
        "AC22: no module-level skip markers"
    )
