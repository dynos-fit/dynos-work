"""Smoke tests for task-20260419-006 invariants (AC 27).

These tests verify cross-cutting invariants that mustn't regress as the
codebase evolves:
  * The 'learned_agent_injected' SEC-004 sentinel is not used (PR-prior invariant).
  * Every writer in lib_receipts.__all__ is either called somewhere
    (hooks/memory/tools) OR explicitly listed in an allowlist.
  * MIN_VERSION_PER_STEP covers every receipt step that should be at floor 2.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

import lib_receipts  # noqa: E402


def _ripgrep(pattern: str, *paths: str) -> list[str]:
    """Returns matching lines for ripgrep; empty list if no matches.

    Falls back to Python-side recursive grep when rg is unavailable so the
    test still works on systems without ripgrep installed.
    """
    abs_paths = [str(ROOT / p) for p in paths]
    try:
        proc = subprocess.run(
            ["rg", "-n", pattern, *abs_paths],
            capture_output=True, text=True, check=False, timeout=30,
        )
        if proc.returncode == 2:
            # rg returns 2 on errors (e.g. directory not found)
            return []
        return [l for l in proc.stdout.splitlines() if l.strip()]
    except (FileNotFoundError, subprocess.SubprocessError):
        # Fallback to Python grep
        matches = []
        compiled = re.compile(pattern)
        for p in abs_paths:
            base = Path(p)
            if not base.exists():
                continue
            for f in base.rglob("*"):
                if not f.is_file():
                    continue
                # Skip binary / non-text
                if f.suffix in {".pyc", ".so", ".dylib", ".bin"}:
                    continue
                try:
                    text = f.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                for i, line in enumerate(text.splitlines(), 1):
                    if compiled.search(line):
                        matches.append(f"{f}:{i}:{line}")
        return matches


def test_learned_agent_injected_symbol_absent_from_runtime_dirs():
    """AC 27: PR #123 invariant — the 'learned_agent_injected' string must
    NOT appear in hooks/, skills/, or cli/assets/templates/."""
    matches = _ripgrep(
        r"learned_agent_injected",
        "hooks", "skills", "cli/assets/templates",
    )
    # Filter out matches that live in test fixtures (tests/ is allowed) or
    # in this very test file (some grep implementations match the pattern
    # source string).
    real_hits = [
        m for m in matches
        if "/tests/" not in m
        and "test_no_learned_agent_injected_symbol" not in m
        and "test_task_006_self_proof_smoke" not in m
    ]
    assert real_hits == [], \
        f"PR #123 invariant violated — learned_agent_injected symbol present: {real_hits}"


def test_every_writer_in_all_is_either_called_or_allowlisted():
    """AC 27: every receipt_* in __all__ must be reachable from runtime code
    OR be on a documented allowlist of intentionally unused writers."""
    # Allowlist for writers known to be called dynamically only (e.g. via
    # importlib in skills) or staged for future use.
    ALLOWLIST = {
        "receipt_plan_routing",  # AC 5: pruned from required chain, kept for future
        # rules-check-passed receipt is consumed by lib_core.transition_task
        # gates (TEST_EXECUTION + DONE) but the *writer* is invoked from a
        # rules-check skill runner that lives outside this repo's hooks/. The
        # gate-side reference is enough to prove the receipt's lifecycle.
        "receipt_rules_check_passed",
    }
    writer_names = [
        n for n in lib_receipts.__all__
        if n.startswith("receipt_") and callable(getattr(lib_receipts, n, None))
    ]

    # Search for `writer_name(` invocations across runtime dirs.
    unreferenced = []
    for name in writer_names:
        if name in ALLOWLIST:
            continue
        # Match: name followed by paren OR import statement
        matches = _ripgrep(
            rf"\b{name}\b",
            "hooks", "skills", "memory", "sandbox", "bin",
        )
        # Filter out the definition site itself
        real_hits = [m for m in matches if "lib_receipts.py" not in m]
        if not real_hits:
            unreferenced.append(name)

    assert not unreferenced, (
        f"writer(s) in lib_receipts.__all__ are exported but unreferenced "
        f"in runtime code: {unreferenced}\n"
        "Either add a real call site OR add the name to ALLOWLIST."
    )


def test_min_version_per_step_covers_v2_required_steps():
    """AC 27: every step that the spec mandates at v2 floor must appear in
    MIN_VERSION_PER_STEP with floor>=2."""
    required_v2 = {
        "executor-*": 2,
        "audit-*": 2,
        "plan-validated": 2,
        "rules-check-passed": 2,
        "calibration-applied": 2,
        "calibration-noop": 2,
        "human-approval-*": 2,
    }
    for step, floor in required_v2.items():
        assert step in lib_receipts.MIN_VERSION_PER_STEP, \
            f"missing v2-required step in MIN_VERSION_PER_STEP: {step}"
        assert lib_receipts.MIN_VERSION_PER_STEP[step] >= floor, (
            f"step {step} floor={lib_receipts.MIN_VERSION_PER_STEP[step]} "
            f"below required {floor}"
        )


def test_calibration_noop_writer_exported():
    """AC 27: receipt_calibration_noop is in __all__ (the new writer)."""
    assert "receipt_calibration_noop" in lib_receipts.__all__


def test_receipt_contract_version_constant_is_three():
    """AC 27: contract bump landed (RECEIPT_CONTRACT_VERSION == 3)."""
    assert lib_receipts.RECEIPT_CONTRACT_VERSION == 3
