# Pre-migration collected count on main @ aac5b08: 832 tests
"""TDD verification meta-tests for pytest migration task-20260417-014.

Every test in this module is a pure structural assertion over repo file paths
and file source text. Each one is expected to FAIL on the pre-migration state
and PASS once the migration executor has completed its work. No fixtures,
no subprocess calls, no production imports.

Covers AC 15 (10 meta-tests) from spec.md.
"""

from pathlib import Path
import re

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

MIGRATED_FILES = [
    "tests/test_agent_generator.py",
    "tests/test_learning_runtime.py",
    "tests/test_policy_json.py",
    "tests/test_reward_scoring.py",
    "tests/test_ctl.py",
    "tests/test_planner.py",
    "tests/test_lib_templates.py",
]

# All migrated files except test_lib_templates.py, which preserves its
# existing pytest-style class groupings byte-for-byte per AC 10.
FLATTENED_FILES = [p for p in MIGRATED_FILES if p != "tests/test_lib_templates.py"]

# Allowed case-insensitive 'dyno' substring matches per AC 4(g).
ALLOWED_DYNO_TOKENS = {
    "dynos",
    ".dynos",
    "DYNOS_HOME",
    "dynos_home",
    "dynos-work",
    "dynos.fit",
}


def test_pytest_ini_present():
    """AC 1: pytest.ini exists at repo root and declares testpaths=tests."""
    ini_path = REPO_ROOT / "pytest.ini"
    assert ini_path.exists(), f"pytest.ini missing at {ini_path}"
    source = ini_path.read_text()
    assert "testpaths = tests" in source, (
        f"pytest.ini does not contain 'testpaths = tests'. Got:\n{source}"
    )


def test_conftest_present():
    """AC 2: tests/conftest.py exists and defines a @pytest.fixture dynos_home.

    The @pytest.fixture decorator must appear within 3 lines above the
    `def dynos_home(` line (multiline regex check).
    """
    conftest_path = REPO_ROOT / "tests" / "conftest.py"
    assert conftest_path.exists(), f"conftest.py missing at {conftest_path}"
    source = conftest_path.read_text()
    # Match @pytest.fixture (with optional args/newlines) within 3 lines
    # above a `def dynos_home(` definition.
    pattern = re.compile(
        r"@pytest\.fixture[^\n]*\n(?:[^\n]*\n){0,2}\s*def\s+dynos_home\s*\(",
        re.MULTILINE,
    )
    assert pattern.search(source), (
        "tests/conftest.py does not contain a @pytest.fixture decorator "
        "within 3 lines above 'def dynos_home(' definition."
    )


@pytest.mark.parametrize("rel_path", MIGRATED_FILES)
def test_no_unittest_testcase_in_migrated_files(rel_path):
    """AC 4(a),(b): no unittest TestCase base class and no bare unittest import.

    Permitted: `from unittest import mock` and `from unittest.mock import ...`.
    Forbidden: `import unittest`, `from unittest import TestCase`, or any
    `class X(...TestCase...)` declaration.
    """
    path = REPO_ROOT / rel_path
    assert path.exists(), f"Migrated file missing: {path}"
    source = path.read_text()

    testcase_re = re.compile(r"class\s+\w+\s*\([^)]*TestCase[^)]*\)")
    match = testcase_re.search(source)
    assert match is None, (
        f"{rel_path} still declares a TestCase subclass: {match.group(0)!r}"
    )

    import_unittest_re = re.compile(r"^import unittest$", re.MULTILINE)
    match = import_unittest_re.search(source)
    assert match is None, (
        f"{rel_path} still has a bare `import unittest` line."
    )

    # `from unittest import X` where X is not exactly `mock` must fail.
    from_unittest_re = re.compile(r"^from unittest import (?!mock\b)", re.MULTILINE)
    match = from_unittest_re.search(source)
    assert match is None, (
        f"{rel_path} still has a forbidden `from unittest import <non-mock>` "
        f"statement: {match.group(0)!r}"
    )


def test_renamed_files_exist_at_new_paths():
    """AC 8/9/10: the three renamed files exist at their new paths."""
    assert (REPO_ROOT / "tests" / "test_ctl.py").exists(), (
        "tests/test_ctl.py missing (rename from test_dynosctl.py not done)"
    )
    assert (REPO_ROOT / "tests" / "test_planner.py").exists(), (
        "tests/test_planner.py missing (rename from test_dynoplanner.py not done)"
    )
    assert (REPO_ROOT / "tests" / "test_lib_templates.py").exists(), (
        "tests/test_lib_templates.py missing "
        "(rename from test_dynoslib_templates.py not done)"
    )


def test_renamed_files_removed_at_old_paths():
    """AC 8/9/10: the three renamed files are gone from their old paths."""
    assert not (REPO_ROOT / "tests" / "test_dynosctl.py").exists(), (
        "tests/test_dynosctl.py still exists — rename to test_ctl.py incomplete"
    )
    assert not (REPO_ROOT / "tests" / "test_dynoplanner.py").exists(), (
        "tests/test_dynoplanner.py still exists — rename to test_planner.py incomplete"
    )
    assert not (REPO_ROOT / "tests" / "test_dynoslib_templates.py").exists(), (
        "tests/test_dynoslib_templates.py still exists — "
        "rename to test_lib_templates.py incomplete"
    )


def test_learning_runtime_no_calibrate_references():
    """AC 11: tests/test_learning_runtime.py has zero 'calibrate' references."""
    path = REPO_ROOT / "tests" / "test_learning_runtime.py"
    assert path.exists(), f"Missing: {path}"
    source = path.read_text()
    assert "calibrate" not in source, (
        "tests/test_learning_runtime.py still contains the substring 'calibrate' "
        "(should be replaced with 'agent_generator' per AC 11)."
    )


def test_learning_runtime_no_deleted_module_tests():
    """AC 12: the three deleted test names do not appear in test_learning_runtime.py."""
    path = REPO_ROOT / "tests" / "test_learning_runtime.py"
    assert path.exists(), f"Missing: {path}"
    source = path.read_text()
    deleted_names = [
        "test_structured_generation_registers_component_and_report_surfaces_it",
        "test_freshness_policy_blocks_stale_route",
        "test_route_resolution_prefers_allowed_highest_composite",
    ]
    for name in deleted_names:
        assert name not in source, (
            f"Deleted test name {name!r} still appears in "
            f"tests/test_learning_runtime.py (must be removed outright per AC 12)."
        )


def test_learning_runtime_no_deleted_script_invocations():
    """AC 12: tests/test_learning_runtime.py no longer invokes route.py or generate.py."""
    path = REPO_ROOT / "tests" / "test_learning_runtime.py"
    assert path.exists(), f"Missing: {path}"
    source = path.read_text()
    assert '"route.py"' not in source, (
        "tests/test_learning_runtime.py still references \"route.py\" "
        "as a subprocess script argument (AC 12 forbids this)."
    )
    assert '"generate.py"' not in source, (
        "tests/test_learning_runtime.py still references \"generate.py\" "
        "as a subprocess script argument (AC 12 forbids this)."
    )


@pytest.mark.parametrize("rel_path", FLATTENED_FILES)
def test_no_class_wrappers_in_flattened_files(rel_path):
    """AC 4(f): the 6 flattened files contain no module-level class definitions.

    tests/test_lib_templates.py is excluded (it preserves class groupings
    byte-for-byte per AC 10).
    """
    path = REPO_ROOT / rel_path
    assert path.exists(), f"Missing: {path}"
    source = path.read_text()
    match = re.search(r"^class\s+\w+", source, re.M)
    assert match is None, (
        f"{rel_path} still contains a module-level class definition: "
        f"{match.group(0)!r} (AC 4(f) requires all 6 flattened files to use "
        f"only module-level def test_* functions)."
    )


@pytest.mark.parametrize("rel_path", MIGRATED_FILES)
def test_no_stale_dyno_identifiers(rel_path):
    """AC 4(g): case-insensitive 'dyno*' matches must only be allowed tokens.

    Allowed: dynos, .dynos, DYNOS_HOME, dynos_home, dynos-work, dynos.fit.
    Forbidden: DYNOPLANNER, dynoplanner, DynosCtl, DynosCtlTests,
    TestDynoPlanner, test_dynoplanner_file_exists, etc.
    """
    path = REPO_ROOT / rel_path
    assert path.exists(), f"Missing: {path}"
    source = path.read_text()
    # Word-boundary, case-insensitive, capture the whole 'dyno...' token so
    # we can report the offender.
    token_re = re.compile(r"\bdyno\w*\b", re.IGNORECASE)
    offenders = []
    for match in token_re.finditer(source):
        token = match.group(0)
        if token not in ALLOWED_DYNO_TOKENS:
            # Compute line number for a useful error report.
            line_no = source.count("\n", 0, match.start()) + 1
            offenders.append((line_no, token))
    assert not offenders, (
        f"{rel_path} contains stale 'dyno*' identifiers not in the allowlist "
        f"{sorted(ALLOWED_DYNO_TOKENS)}:\n"
        + "\n".join(f"  line {ln}: {tok!r}" for ln, tok in offenders)
    )
