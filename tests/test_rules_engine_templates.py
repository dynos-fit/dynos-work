"""AC 21: tests for each of the six rules-engine template handlers.

Coverage requirement: every template needs happy + violation + at least
one edge case. We construct minimal Python module fixtures + rules.json
under tmp_path and invoke the template handler functions directly.

We exercise:
- every_name_in_X_satisfies_Y: happy + violation + import-failure-warn-and-skip
- pattern_must_not_appear: happy + violation + context_required filtering
                           + syntax-error-warn-and-skip
- co_modification_required: happy + violation + mode="all" returns []
- signature_lock: happy + violation + import-failure warn-and-skip
- caller_count_required: happy + violation + sum-across-files
- import_constant_only: happy + violation + allowed_files glob exclusion
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

from rules_engine import (  # noqa: E402
    Rule,
    ScanScope,
    check_caller_count_required,
    check_co_modification_required,
    check_every_name_in_X_satisfies_Y,
    check_import_constant_only,
    check_pattern_must_not_appear,
    check_signature_lock,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_scope(root: Path, files: list[Path], mode: str = "all") -> ScanScope:
    return ScanScope(root=root.resolve(), files=tuple(sorted(files)), mode=mode)


def _isolate_module_path(monkeypatch, mod_dir: Path) -> None:
    """Insert *mod_dir* at sys.path[0] for the duration of the test."""
    monkeypatch.syspath_prepend(str(mod_dir))


# ---------------------------------------------------------------------------
# every_name_in_X_satisfies_Y (3 tests)
# ---------------------------------------------------------------------------


def test_every_name_in_X_satisfies_Y_happy_callable(tmp_path, monkeypatch):
    pkg = tmp_path / "fix_pkg_a"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "good_mod.py").write_text(
        "def alpha():\n    pass\n"
        "def beta():\n    pass\n"
        "__all__ = ['alpha', 'beta']\n"
    )
    _isolate_module_path(monkeypatch, tmp_path)
    rule = Rule(
        rule_id="r-good",
        template="every_name_in_X_satisfies_Y",
        params={
            "module": "fix_pkg_a.good_mod",
            "container": "__all__",
            "predicate": "callable",
        },
    )
    scope = _make_scope(tmp_path, [])
    out = check_every_name_in_X_satisfies_Y(rule, scope)
    assert out == [], (
        "happy path: every name in __all__ resolves to a callable; "
        "expected zero violations"
    )


def test_every_name_in_X_satisfies_Y_violation_non_callable(tmp_path, monkeypatch):
    pkg = tmp_path / "fix_pkg_b"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "bad_mod.py").write_text(
        "def alpha():\n    pass\n"
        "BETA = 'not-a-callable'\n"
        "__all__ = ['alpha', 'BETA']\n"
    )
    _isolate_module_path(monkeypatch, tmp_path)
    rule = Rule(
        rule_id="r-bad",
        template="every_name_in_X_satisfies_Y",
        params={
            "module": "fix_pkg_b.bad_mod",
            "container": "__all__",
            "predicate": "callable",
        },
    )
    out = check_every_name_in_X_satisfies_Y(rule, _make_scope(tmp_path, []))
    assert len(out) == 1, f"expected 1 violation for non-callable BETA, got {out!r}"
    v = out[0]
    assert v.rule_id == "r-bad"
    assert v.template == "every_name_in_X_satisfies_Y"
    assert "'BETA'" in v.message
    assert "fix_pkg_b.bad_mod.__all__" in v.message
    assert v.severity == "error"


def test_every_name_in_X_satisfies_Y_import_failure_warns_and_skips(
    tmp_path, monkeypatch, capsys
):
    # Module that does not exist on sys.path. Importer must warn-and-skip,
    # not raise, and return [].
    rule = Rule(
        rule_id="r-missing",
        template="every_name_in_X_satisfies_Y",
        params={
            "module": "nonexistent_pkg_xyz_12345.deeply_buried",
            "container": "__all__",
            "predicate": "callable",
        },
    )
    out = check_every_name_in_X_satisfies_Y(rule, _make_scope(tmp_path, []))
    assert out == [], "import failure must produce no violations"
    err = capsys.readouterr().err
    assert "WARN" in err
    assert "r-missing" in err
    assert "nonexistent_pkg_xyz_12345.deeply_buried" in err


# ---------------------------------------------------------------------------
# pattern_must_not_appear (4 tests)
# ---------------------------------------------------------------------------


def test_pattern_must_not_appear_happy_no_match(tmp_path):
    f = tmp_path / "clean.py"
    f.write_text("def foo():\n    return 1\n")
    rule = Rule(
        rule_id="r-pat-clean",
        template="pattern_must_not_appear",
        params={"regex": r"\btime\.time\(\)", "scope": "*.py"},
    )
    out = check_pattern_must_not_appear(rule, _make_scope(tmp_path, [f]))
    assert out == [], "happy path: regex does not match → zero violations"


def test_pattern_must_not_appear_violation(tmp_path):
    f = tmp_path / "dirty.py"
    f.write_text("import time\n\ndef now():\n    return time.time()\n")
    rule = Rule(
        rule_id="r-pat-dirty",
        template="pattern_must_not_appear",
        params={"regex": r"\btime\.time\(\)", "scope": "*.py"},
    )
    out = check_pattern_must_not_appear(rule, _make_scope(tmp_path, [f]))
    assert len(out) == 1, f"expected exactly 1 violation, got {out!r}"
    v = out[0]
    assert v.rule_id == "r-pat-dirty"
    assert v.line == 4
    assert "time.time()" in v.message
    assert v.file == "dirty.py"


def test_pattern_must_not_appear_context_required_filters(tmp_path):
    """When `context_required` is set, only matches whose smallest
    enclosing AST node (by line containment) unparses to source that
    matches the context regex are reported. The smallest-enclosing
    node here is a function-def or expression that spans the match
    line — we put the context on the same line as the match so the
    enclosing single-line node carries the context."""
    f = tmp_path / "mixed.py"
    # Two single-line statements at module scope. The second one's
    # smallest enclosing node spans both `lock_acquire()` and
    # `time.time()`, so unparsing it yields the context.
    f.write_text(
        "x = time.time()\n"
        "y = (lock_acquire(), time.time())\n"
    )
    rule = Rule(
        rule_id="r-pat-ctx",
        template="pattern_must_not_appear",
        params={
            "regex": r"\btime\.time\(\)",
            "scope": "*.py",
            "context_required": r"lock_acquire",
        },
    )
    out = check_pattern_must_not_appear(rule, _make_scope(tmp_path, [f]))
    assert len(out) == 1, (
        f"context filter should keep only the lock-context match, got {out!r}"
    )
    v = out[0]
    assert v.line == 2, f"expected line 2 (lock_acquire context), got {v.line}"


def test_pattern_must_not_appear_syntax_error_warns_and_skips(tmp_path, capsys):
    """When `context_required` is set and the file has a SyntaxError,
    the handler must emit a stderr WARN and produce no violation
    rather than raising."""
    f = tmp_path / "broken.py"
    f.write_text("def broken(:\n    return time.time()\n")
    rule = Rule(
        rule_id="r-pat-broken",
        template="pattern_must_not_appear",
        params={
            "regex": r"\btime\.time\(\)",
            "scope": "*.py",
            "context_required": r"broken",
        },
    )
    out = check_pattern_must_not_appear(rule, _make_scope(tmp_path, [f]))
    assert out == [], "syntax error must produce no violations"
    err = capsys.readouterr().err
    assert "WARN" in err
    assert "r-pat-broken" in err


# ---------------------------------------------------------------------------
# co_modification_required (3 tests)
# ---------------------------------------------------------------------------


def test_co_modification_required_happy_when_both_modified(tmp_path):
    trig = tmp_path / "lib_thing.py"
    trig.write_text("# trigger\n")
    accomp = tmp_path / "spec.md"
    accomp.write_text("# spec\n")
    rule = Rule(
        rule_id="r-co-ok",
        template="co_modification_required",
        params={
            "trigger_glob": "lib_*.py",
            "must_modify_glob": "*.md",
        },
    )
    out = check_co_modification_required(
        rule, _make_scope(tmp_path, [trig, accomp], mode="staged")
    )
    assert out == [], "trigger AND accompanying file present → no violation"


def test_co_modification_required_violation_when_only_trigger_modified(tmp_path):
    trig = tmp_path / "lib_alpha.py"
    trig.write_text("# trigger\n")
    irrelevant = tmp_path / "README.txt"
    irrelevant.write_text("hi\n")
    rule = Rule(
        rule_id="r-co-violate",
        template="co_modification_required",
        params={
            "trigger_glob": "lib_*.py",
            "must_modify_glob": "*.md",
        },
    )
    out = check_co_modification_required(
        rule, _make_scope(tmp_path, [trig, irrelevant], mode="staged")
    )
    assert len(out) == 1, f"trigger present but no *.md → expected 1 violation, got {out!r}"
    v = out[0]
    assert v.rule_id == "r-co-violate"
    assert v.line == 0
    assert "lib_alpha.py" in v.file
    assert "*.md" in v.message


def test_co_modification_required_returns_empty_in_all_mode(tmp_path, capsys):
    trig = tmp_path / "lib_beta.py"
    trig.write_text("# trigger\n")
    rule = Rule(
        rule_id="r-co-all",
        template="co_modification_required",
        params={
            "trigger_glob": "lib_*.py",
            "must_modify_glob": "*.md",
        },
    )
    # mode="all": handler must short-circuit and return [] regardless of
    # what files exist.
    out = check_co_modification_required(
        rule, _make_scope(tmp_path, [trig], mode="all")
    )
    assert out == [], (
        "co_modification_required must be a no-op in --all mode "
        "(staged-only template)"
    )
    err = capsys.readouterr().err
    assert "INFO" in err and "r-co-all" in err


# ---------------------------------------------------------------------------
# signature_lock (3 tests)
# ---------------------------------------------------------------------------


def test_signature_lock_happy(tmp_path, monkeypatch):
    pkg = tmp_path / "fix_pkg_sig"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "mod.py").write_text(
        "def my_fn(alpha, beta, gamma):\n    return None\n"
    )
    _isolate_module_path(monkeypatch, tmp_path)
    rule = Rule(
        rule_id="r-sig-ok",
        template="signature_lock",
        params={
            "module": "fix_pkg_sig.mod",
            "function": "my_fn",
            "expected_params": ["alpha", "beta", "gamma"],
        },
    )
    out = check_signature_lock(rule, _make_scope(tmp_path, []))
    assert out == [], f"signature matches expected; got violations {out!r}"


def test_signature_lock_violation_wrong_params(tmp_path, monkeypatch):
    pkg = tmp_path / "fix_pkg_sig2"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "mod.py").write_text(
        "def my_fn(alpha, beta):\n    return None\n"
    )
    _isolate_module_path(monkeypatch, tmp_path)
    rule = Rule(
        rule_id="r-sig-bad",
        template="signature_lock",
        params={
            "module": "fix_pkg_sig2.mod",
            "function": "my_fn",
            "expected_params": ["alpha", "beta", "gamma"],
        },
    )
    out = check_signature_lock(rule, _make_scope(tmp_path, []))
    assert len(out) == 1, f"expected 1 violation, got {out!r}"
    v = out[0]
    assert v.rule_id == "r-sig-bad"
    assert "['alpha', 'beta']" in v.message
    assert "['alpha', 'beta', 'gamma']" in v.message


def test_signature_lock_import_failure_warns_and_skips(tmp_path, capsys):
    rule = Rule(
        rule_id="r-sig-noimp",
        template="signature_lock",
        params={
            "module": "missing_pkg_zzz_98765.totally_absent",
            "function": "fn",
            "expected_params": ["a"],
        },
    )
    out = check_signature_lock(rule, _make_scope(tmp_path, []))
    assert out == [], "import failure must produce no violations"
    err = capsys.readouterr().err
    assert "WARN" in err
    assert "r-sig-noimp" in err
    assert "missing_pkg_zzz_98765" in err


# ---------------------------------------------------------------------------
# caller_count_required (3 tests)
# ---------------------------------------------------------------------------


def test_caller_count_required_happy_meets_min(tmp_path):
    f = tmp_path / "uses.py"
    f.write_text(
        "def x():\n"
        "    foo()\n"
        "    foo()\n"
    )
    rule = Rule(
        rule_id="r-call-ok",
        template="caller_count_required",
        params={"symbol": "foo", "scope": "*.py", "min_count": 2},
    )
    out = check_caller_count_required(rule, _make_scope(tmp_path, [f]))
    assert out == [], "2 calls meets min_count=2 → no violation"


def test_caller_count_required_violation_below_min(tmp_path):
    f = tmp_path / "uses.py"
    f.write_text("def x():\n    foo()\n")
    rule = Rule(
        rule_id="r-call-low",
        template="caller_count_required",
        params={"symbol": "foo", "scope": "*.py", "min_count": 3},
    )
    out = check_caller_count_required(rule, _make_scope(tmp_path, [f]))
    assert len(out) == 1, f"1 call < min_count=3 → expected 1 violation, got {out!r}"
    v = out[0]
    assert v.file == "(repo-wide)"
    assert v.line == 0
    assert "1 time" in v.message or "1 time(s)" in v.message
    assert ">= 3" in v.message


def test_caller_count_required_sums_across_files(tmp_path):
    a = tmp_path / "a.py"
    a.write_text("def _():\n    foo()\n    obj.foo()\n")
    b = tmp_path / "b.py"
    b.write_text("def _():\n    foo()\n")
    rule = Rule(
        rule_id="r-call-sum",
        template="caller_count_required",
        params={"symbol": "foo", "scope": "*.py", "min_count": 3},
    )
    out = check_caller_count_required(rule, _make_scope(tmp_path, [a, b]))
    # 3 total calls (2 in a.py via Name + Attribute, 1 in b.py).
    assert out == [], (
        f"3 calls summed across both files meets min_count=3; got {out!r}"
    )

    # Bump the threshold so the SAME files now violate — proves the sum
    # is what's being compared, not just "is symbol present in some file".
    rule_high = Rule(
        rule_id="r-call-sum-fail",
        template="caller_count_required",
        params={"symbol": "foo", "scope": "*.py", "min_count": 4},
    )
    out2 = check_caller_count_required(rule_high, _make_scope(tmp_path, [a, b]))
    assert len(out2) == 1
    assert "3 time" in out2[0].message


def test_caller_count_required_staged_caller_outside_diff_no_violation(
    tmp_path, monkeypatch
):
    """AC-6: staged-mode scope contains zero callers, but a file outside the
    staged diff has sufficient callers — the fix escalates to repo-wide so the
    rule does NOT false-fire."""
    import rules_engine  # noqa: PLC0415

    # File OUTSIDE the staged diff with 3 callers (meets min_count).
    outside = tmp_path / "library.py"
    outside.write_text("def _():\n    foo()\n    obj.foo()\n    foo()\n")

    # Staged file has zero callers and a different name (so glob matches both
    # but only the unstaged file has the symbol).
    staged = tmp_path / "unrelated.py"
    staged.write_text("def x():\n    pass\n")

    # Patch _all_tracked_files (object form) so the handler sees the union.
    monkeypatch.setattr(
        rules_engine, "_all_tracked_files", lambda root: [outside, staged]
    )

    rule = Rule(
        rule_id="r-call-staged-outside",
        template="caller_count_required",
        params={"symbol": "foo", "scope": "*.py", "min_count": 3},
    )
    # Staged scope only sees the unrelated file.
    scope = _make_scope(tmp_path, [staged], mode="staged")
    out = check_caller_count_required(rule, scope)
    assert out == [], (
        "staged-mode scope had 0 callers, but _all_tracked_files exposed a "
        f"file with 3 callers; expected no violation, got {out!r}"
    )


def test_caller_count_required_staged_union_across_staged_and_unstaged(
    tmp_path, monkeypatch
):
    """AC-7: callers are split across a staged file (1) and an unstaged file
    (2); the union (3) meets min_count and the rule does NOT fire."""
    import rules_engine  # noqa: PLC0415

    staged = tmp_path / "a.py"
    staged.write_text("def _():\n    foo()\n")
    unstaged = tmp_path / "b.py"
    unstaged.write_text("def _():\n    foo()\n    obj.foo()\n")

    monkeypatch.setattr(
        rules_engine, "_all_tracked_files", lambda root: [staged, unstaged]
    )

    rule = Rule(
        rule_id="r-call-staged-union",
        template="caller_count_required",
        params={"symbol": "foo", "scope": "*.py", "min_count": 3},
    )
    # Staged scope only sees `a.py` — but the handler must escalate to
    # repo-wide and find both files via _all_tracked_files.
    scope = _make_scope(tmp_path, [staged], mode="staged")
    out = check_caller_count_required(rule, scope)
    assert out == [], (
        f"3 calls across staged + unstaged meets min_count=3; got {out!r}"
    )


# ---------------------------------------------------------------------------
# import_constant_only (3 tests)
# ---------------------------------------------------------------------------


def test_import_constant_only_happy_only_in_allowed_file(tmp_path):
    allowed = tmp_path / "lib_constants.py"
    allowed.write_text(
        "RECEIPT_NAME = 'receipts/_injected-prompt'\n"
    )
    other = tmp_path / "consumer.py"
    other.write_text(
        "from lib_constants import RECEIPT_NAME\n"
        "def use():\n    return RECEIPT_NAME\n"
    )
    rule = Rule(
        rule_id="r-ico-ok",
        template="import_constant_only",
        params={
            "literal_pattern": r"receipts/_injected-prompt",
            "allowed_files": ["lib_constants.py"],
        },
    )
    out = check_import_constant_only(
        rule, _make_scope(tmp_path, [allowed, other])
    )
    assert out == [], (
        "literal only appears in the allowed file → no violation "
        f"(got {out!r})"
    )


def test_import_constant_only_violation_in_disallowed_file(tmp_path):
    allowed = tmp_path / "lib_constants.py"
    allowed.write_text("RECEIPT_NAME = 'receipts/_injected-prompt'\n")
    bad = tmp_path / "naughty.py"
    bad.write_text(
        "def f():\n"
        "    return 'receipts/_injected-prompt'  # inline literal!\n"
    )
    rule = Rule(
        rule_id="r-ico-bad",
        template="import_constant_only",
        params={
            "literal_pattern": r"receipts/_injected-prompt",
            "allowed_files": ["lib_constants.py"],
        },
    )
    out = check_import_constant_only(
        rule, _make_scope(tmp_path, [allowed, bad])
    )
    assert len(out) == 1, f"expected 1 violation, got {out!r}"
    v = out[0]
    assert v.rule_id == "r-ico-bad"
    assert v.file == "naughty.py"
    assert "receipts/_injected-prompt" in v.message
    assert "lib_constants.py" in v.message


def test_import_constant_only_allowed_files_glob_excludes_match(tmp_path):
    """Even when a literal exists in two files, if BOTH match an
    allowed_files glob then no violation is emitted. Demonstrates the
    glob (not just exact-name) exclusion semantics."""
    a = tmp_path / "lib_constants.py"
    a.write_text("X = 'receipts/_injected-prompt'\n")
    b = tmp_path / "lib_more.py"
    b.write_text("Y = 'receipts/_injected-prompt'\n")
    rule = Rule(
        rule_id="r-ico-glob",
        template="import_constant_only",
        params={
            "literal_pattern": r"receipts/_injected-prompt",
            "allowed_files": ["lib_*.py"],
        },
    )
    out = check_import_constant_only(rule, _make_scope(tmp_path, [a, b]))
    assert out == [], (
        f"both files match lib_*.py allowed glob → no violation; got {out!r}"
    )
