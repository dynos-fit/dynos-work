"""AC 22: rules-engine dispatcher integration test.

Builds a synthetic prevention-rules.json with one rule per template
(seven entries — six enforced + one advisory) and asserts that
`run_checks` dispatches each rule exactly once and produces violations
of the expected shape and count.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

import rules_engine  # noqa: E402
from rules_engine import run_checks  # noqa: E402


def _project_modules(workspace: Path) -> None:
    """Create importable Python packages used by enforced rules."""
    pkg = workspace / "fix_dispatch_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    # every_name_in_X_satisfies_Y target — has a non-callable in __all__.
    (pkg / "names.py").write_text(
        "def alpha():\n    pass\n"
        "BETA = 'not-a-callable'\n"
        "__all__ = ['alpha', 'BETA']\n"
    )
    # signature_lock target — wrong signature for r-sig.
    (pkg / "sig.py").write_text(
        "def my_fn(only_one):\n    return None\n"
    )


def _project_files(workspace: Path) -> None:
    """Create files that template handlers will scan during run_checks."""
    # pattern_must_not_appear target — file with bad pattern.
    (workspace / "uses_time.py").write_text(
        "import time\n"
        "def now():\n"
        "    return time.time()\n"
    )
    # caller_count_required: target symbol called only once but rule
    # demands min_count >= 5.
    (workspace / "callers.py").write_text(
        "def x():\n    foo()\n"
    )
    # import_constant_only target — literal in disallowed file.
    (workspace / "leaks_literal.py").write_text(
        "x = 'receipts/_injected-prompt'\n"
    )
    # co_modification_required target — staged trigger without companion.
    (workspace / "lib_alpha.py").write_text("# trigger\n")


def test_run_checks_dispatches_all_templates_once(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    _project_modules(workspace)
    _project_files(workspace)

    monkeypatch.syspath_prepend(str(workspace))

    rules_dir = tmp_path / "persistent"
    rules_dir.mkdir()
    rules = {
        "rules": [
            {
                "rule_id": "r-every",
                "template": "every_name_in_X_satisfies_Y",
                "params": {
                    "module": "fix_dispatch_pkg.names",
                    "container": "__all__",
                    "predicate": "callable",
                },
                "severity": "error",
            },
            {
                "rule_id": "r-pat",
                "template": "pattern_must_not_appear",
                "params": {"regex": r"\btime\.time\(\)", "scope": "*.py"},
                "severity": "error",
            },
            {
                "rule_id": "r-co",
                "template": "co_modification_required",
                "params": {
                    "trigger_glob": "lib_*.py",
                    "must_modify_glob": "*.md",
                },
                "severity": "error",
            },
            {
                "rule_id": "r-sig",
                "template": "signature_lock",
                "params": {
                    "module": "fix_dispatch_pkg.sig",
                    "function": "my_fn",
                    "expected_params": ["a", "b"],
                },
                "severity": "error",
            },
            {
                "rule_id": "r-call",
                "template": "caller_count_required",
                "params": {"symbol": "foo", "scope": "*.py", "min_count": 5},
                "severity": "error",
            },
            {
                "rule_id": "r-ico",
                "template": "import_constant_only",
                "params": {
                    "literal_pattern": r"receipts/_injected-prompt",
                    "allowed_files": ["lib_constants.py"],
                },
                "severity": "error",
            },
            {
                "rule_id": "r-adv",
                "template": "advisory",
                "params": {},
                "severity": "warn",
            },
        ]
    }
    (rules_dir / "prevention-rules.json").write_text(json.dumps(rules))

    # Point _persistent_project_dir at our rules dir for any root. The
    # engine imported the symbol into its OWN module namespace, so we
    # must patch the engine's reference, not lib_core's.
    monkeypatch.setattr(
        rules_engine, "_persistent_project_dir", lambda root: rules_dir
    )

    violations = run_checks(workspace, "all")

    # Advisory contributes 0; co_modification_required is no-op in --all
    # mode and contributes 0. The other five contribute exactly 1 each
    # (each rule is constructed to produce exactly one violation against
    # the fixtures).
    assert len(violations) == 5, (
        f"expected 5 violations from 5 enforced rules in --all mode, got "
        f"{len(violations)}: {[(v.rule_id, v.message) for v in violations]}"
    )

    # Every violation has the rule_id of one of the enforced templates.
    seen = {v.rule_id for v in violations}
    assert seen == {"r-every", "r-pat", "r-sig", "r-call", "r-ico"}, (
        f"unexpected rule_id set in violations: {sorted(seen)!r}"
    )

    # Sorted determinism guarantee (file, line, rule_id).
    keys = [(v.file, v.line, v.rule_id) for v in violations]
    assert keys == sorted(keys), (
        f"violations not sorted by (file, line, rule_id): {keys!r}"
    )

    # Each violation has the contract shape from to_dict.
    for v in violations:
        d = v.to_dict()
        for key in ("rule_id", "template", "file", "line", "message", "severity"):
            assert key in d, f"violation dict missing {key!r}: {d!r}"
        assert isinstance(d["line"], int)
        assert d["severity"] in ("error", "warn")

    # The advisory rule is silently a no-op (no stderr WARN/INFO about
    # template name, no violation emitted under r-adv).
    err = capsys.readouterr().err
    assert "unknown template" not in err


def test_run_checks_with_stats_returns_loaded_skipped(tmp_path: Path) -> None:
    """run_checks_with_stats returns (violations, loaded, skipped) — added
    in task-20260507-004 to surface load-time skip counts to the
    rules-check-passed receipt without a separate file read."""
    from rules_engine import run_checks_with_stats  # noqa: PLC0415

    persistent = tmp_path / ".dynos" / "projects" / "p"
    persistent.mkdir(parents=True)
    rules_file = persistent / "prevention-rules.json"
    rules_file.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "rule_id": "test-1",
                        "template": "advisory",
                        "params": {},
                        "rule": "advisory rule body",
                    },
                    {"rule": "no rule_id, gets skipped"},
                ]
            }
        )
    )
    import rules_engine as _re  # noqa: PLC0415

    orig = _re._persistent_project_dir
    _re._persistent_project_dir = lambda root: persistent  # type: ignore[assignment]
    try:
        result = run_checks_with_stats(tmp_path, mode="all")
    finally:
        _re._persistent_project_dir = orig  # type: ignore[assignment]

    assert isinstance(result, tuple) and len(result) == 3
    _violations, loaded, skipped = result
    assert isinstance(loaded, int) and isinstance(skipped, int)
    assert loaded == 1 and skipped == 1
