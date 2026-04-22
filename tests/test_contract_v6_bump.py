"""TDD-first tests for task-20260420-001 cross-cutting (AC29, AC30).

Covers acceptance criteria 29 and 30:

    AC29 RECEIPT_CONTRACT_VERSION == 6 in hooks/lib_receipts.py (was 5).
         Docstring block above it names v5 -> v6 rationale and references
         "trust-registry" AND "signed events" as the new guarantees.
    AC30 MIN_VERSION_PER_STEP floors preserved (per Open Question #4 the
         new receipt type is conditional — existing floors must not regress).

TODAY these tests FAIL because RECEIPT_CONTRACT_VERSION is still 5 and the
docstring does not mention v5 -> v6 / trust-registry / signed events.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "hooks"))


# ---------------------------------------------------------------------------
# AC29 — contract version bumped to 6
# ---------------------------------------------------------------------------


def test_receipt_contract_version_is_six():
    import lib_receipts  # noqa: PLC0415

    assert lib_receipts.RECEIPT_CONTRACT_VERSION == 6, (
        f"RECEIPT_CONTRACT_VERSION must be 6, got "
        f"{lib_receipts.RECEIPT_CONTRACT_VERSION}"
    )


def test_bump_rationale_docstring_present():
    """Per AC29, the bump rationale prose above the constant must name
    (a) v5 -> v6, (b) trust-registry, (c) signed events."""
    text = (REPO_ROOT / "hooks" / "lib_receipts.py").read_text(encoding="utf-8")
    # Find the line that declares RECEIPT_CONTRACT_VERSION = 6 and take
    # the preceding 60 lines as the rationale block.
    lines = text.splitlines()
    decl_idx: int | None = None
    for i, ln in enumerate(lines):
        if ln.strip().startswith("RECEIPT_CONTRACT_VERSION") and "6" in ln:
            decl_idx = i
            break
    assert decl_idx is not None, (
        "RECEIPT_CONTRACT_VERSION = 6 line not found; contract bump missing"
    )
    rationale = "\n".join(lines[max(0, decl_idx - 60) : decl_idx])
    assert "v5 -> v6" in rationale, (
        f"Rationale must contain literal 'v5 -> v6'; got:\n{rationale}"
    )
    assert "trust-registry" in rationale, (
        f"Rationale must reference 'trust-registry' as a v6 guarantee"
    )
    assert "signed events" in rationale, (
        f"Rationale must reference 'signed events' as a v6 guarantee"
    )


# ---------------------------------------------------------------------------
# AC30 — MIN_VERSION_PER_STEP floors preserved / extended
# ---------------------------------------------------------------------------


def test_min_version_per_step_preserves_v5_floors():
    """Existing v5 floors MUST NOT regress. Per Open Question #4 the
    new "event-signed" key is conditional — if present, its value is 6."""
    from lib_receipts import MIN_VERSION_PER_STEP  # noqa: PLC0415

    expected_preserved = {
        "executor-*": 2,
        "audit-*": 2,
        "plan-validated": 2,
        "rules-check-passed": 2,
        "calibration-applied": 2,
        "calibration-noop": 2,
        "human-approval-*": 2,
        "force-override-*": 5,
    }
    for key, floor in expected_preserved.items():
        assert key in MIN_VERSION_PER_STEP, (
            f"MIN_VERSION_PER_STEP regressed: key {key!r} removed"
        )
        assert MIN_VERSION_PER_STEP[key] == floor, (
            f"MIN_VERSION_PER_STEP[{key!r}] regressed: expected {floor}, "
            f"got {MIN_VERSION_PER_STEP[key]}"
        )


def test_min_version_per_step_new_event_signed_entry_if_present():
    """If the executor decided to add the `event-signed` floor (Open
    Question #4), it MUST be set to 6. If absent, existing floors
    preserved is enough. This test only fails if the key is present
    with a wrong value."""
    from lib_receipts import MIN_VERSION_PER_STEP  # noqa: PLC0415

    if "event-signed" in MIN_VERSION_PER_STEP:
        assert MIN_VERSION_PER_STEP["event-signed"] == 6, (
            f"If `event-signed` is added, its floor must be 6; got "
            f"{MIN_VERSION_PER_STEP['event-signed']}"
        )
