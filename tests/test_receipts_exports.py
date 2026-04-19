"""Tests for lib_receipts.__all__ exports (AC 17)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

import lib_receipts  # noqa: E402


def test_every_export_resolves_to_module_attribute():
    """AC 17 strict: every name in __all__ must resolve to a module attribute.

    No skip-on-missing escape hatch. Any name listed in __all__ that lacks a
    corresponding definition fails the test with the exact message AC 17
    specifies.
    """
    for name in lib_receipts.__all__:
        assert hasattr(lib_receipts, name), f"{name} in __all__ but not defined"


def test_every_receipt_name_is_callable():
    """Every receipt_* writer (and the core helpers) must be callable."""
    for name in lib_receipts.__all__:
        attr = getattr(lib_receipts, name)
        if name.startswith("receipt_") or name in {
            "write_receipt", "read_receipt", "require_receipt",
            "validate_chain", "hash_file", "plan_validated_receipt_matches",
        }:
            assert callable(attr), f"{name} should be callable"


def test_required_writers_present():
    required = {
        "receipt_human_approval",
        "receipt_spec_validated",
        "receipt_tdd_tests",
        "receipt_postmortem_generated",
        "receipt_postmortem_analysis",
        "receipt_postmortem_skipped",
        "receipt_calibration_applied",
    }
    missing = required - set(lib_receipts.__all__)
    assert not missing, f"missing writers in __all__: {missing}"


def test_constants_exported():
    assert "RECEIPT_CONTRACT_VERSION" in lib_receipts.__all__
    assert "CALIBRATION_POLICY_FILES" in lib_receipts.__all__
    assert lib_receipts.RECEIPT_CONTRACT_VERSION == 3
