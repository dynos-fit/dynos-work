"""Tests for AC 6: LOC-weighted budget in compute_segment_budget.

These tests are RED by design until seg-5 lands the `files_loc` kwarg
and LOC_BASE / LOC_SLOPE constants in hooks/lib_tool_budget.py.

All tests import compute_segment_budget directly from hooks.lib_tool_budget.
Calibration values are pinned by spec AC 6 and must not be changed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

from lib_tool_budget import compute_segment_budget  # noqa: E402


# ---------------------------------------------------------------------------
# AC 6 pinned calibration: 4 files where raw = 4*3+5 = 17
# LOC_BASE=400, LOC_SLOPE=400
# surcharge per file = max(0, loc - 400) // 400
# ---------------------------------------------------------------------------


def test_loc_weighted_budget_large_file() -> None:
    """4 files with one at 6000 LOC → budget=31, no advisory trip.

    Pinned calibration:
        raw=17, surcharge=(6000-400)//400=14, weighted_raw=31
        31 < TOOL_BUDGET_ADVISORY(35) → no advisory
    """
    result = compute_segment_budget(4, "haiku", files_loc=[6000, 100, 100, 100])  # noqa: model-literal
    assert result == 31, (
        f"Expected budget=31 for files_loc=[6000,100,100,100], got {result}"
    )


def test_loc_weighted_budget_advisory_trips() -> None:
    """4 files with one at 7600 LOC → budget=35, advisory fires.

    Pinned calibration:
        raw=17, surcharge=(7600-400)//400=18, weighted_raw=35
        35 >= TOOL_BUDGET_ADVISORY(35) → advisory fires
        budget=35
    """
    result = compute_segment_budget(4, "haiku", files_loc=[7600, 100, 100, 100])  # noqa: model-literal
    assert result == 35, (
        f"Expected budget=35 for files_loc=[7600,100,100,100], got {result}"
    )


def test_loc_weighted_budget_small_files() -> None:
    """4 files all at 100 LOC → budget=17, byte-identical to no-kwarg call.

    Pinned calibration:
        raw=17, surcharge=0 (100 < 400), weighted_raw=17
        budget=17
    """
    result = compute_segment_budget(4, "haiku", files_loc=[100, 100, 100, 100])  # noqa: model-literal
    expected = compute_segment_budget(4, "haiku")  # noqa: model-literal
    assert result == 17, f"Expected budget=17, got {result}"
    assert result == expected, (
        f"files_loc=[100]*4 must produce same result as no-kwarg call: "
        f"{result} != {expected}"
    )


def test_loc_weighted_budget_none_kwarg() -> None:
    """files_loc=None returns the same integer as calling with no files_loc kwarg.

    Verifies backward-compatibility: callers that omit files_loc get identical
    results to callers that explicitly pass files_loc=None.
    """
    with_none = compute_segment_budget(4, "haiku", files_loc=None)  # noqa: model-literal
    without = compute_segment_budget(4, "haiku")  # noqa: model-literal
    assert with_none == without, (
        f"files_loc=None must equal no-kwarg call: {with_none} != {without}"
    )
    # Verify for multiple file counts to ensure the equality holds generically
    for n in (1, 5, 10):
        a = compute_segment_budget(n, None, files_loc=None)
        b = compute_segment_budget(n, None)
        assert a == b, (
            f"files_loc=None must equal no-kwarg for n={n}: {a} != {b}"
        )


def test_two_phase_flag_set_at_ceiling() -> None:
    """4 files all at 10000 LOC → budget=40 (ceiling clamp).

    Pinned calibration:
        raw=17, surcharge per file=(10000-400)//400=24, total_surcharge=96
        weighted_raw=17+96=113, clamped to TOOL_BUDGET_CEILING=40
        budget=40 → two_phase: true must be set by caller
    """
    from lib_tool_budget import TOOL_BUDGET_CEILING

    result = compute_segment_budget(4, "haiku", files_loc=[10000, 10000, 10000, 10000])  # noqa: model-literal
    assert result == 40, f"Expected budget=40 (ceiling), got {result}"
    assert result == TOOL_BUDGET_CEILING, (
        f"Result must equal TOOL_BUDGET_CEILING={TOOL_BUDGET_CEILING}"
    )


def test_loc_constants_exist() -> None:
    """LOC_BASE and LOC_SLOPE constants must exist and be pinned at 400."""
    from lib_tool_budget import LOC_BASE, LOC_SLOPE  # type: ignore[attr-defined]

    assert LOC_BASE == 400, f"LOC_BASE must be 400, got {LOC_BASE}"
    assert LOC_SLOPE == 400, f"LOC_SLOPE must be 400, got {LOC_SLOPE}"


def test_new_file_contributes_zero_surcharge() -> None:
    """New/nonexistent file (loc=0) contributes zero surcharge.

    spec: 'New files (not yet on disk) contribute loc=0, producing zero
    surcharge: max(0, 0 - 400) // 400 = 0.'
    """
    result = compute_segment_budget(4, "haiku", files_loc=[0, 0, 0, 0])  # noqa: model-literal
    baseline = compute_segment_budget(4, "haiku")  # noqa: model-literal
    assert result == baseline, (
        f"files_loc=[0,0,0,0] must equal no-kwarg baseline {baseline}, got {result}"
    )


def test_boundary_at_loc_base() -> None:
    """LOC exactly at LOC_BASE (400) contributes zero surcharge.

    max(0, 400 - 400) // 400 = 0
    LOC_BASE+1=401 contributes zero surcharge too (399//400=0).
    LOC_BASE+400=800 contributes 1 surcharge unit.
    """
    result_at_base = compute_segment_budget(4, "haiku", files_loc=[400, 100, 100, 100])  # noqa: model-literal
    baseline = compute_segment_budget(4, "haiku")  # noqa: model-literal
    assert result_at_base == baseline, (
        f"loc=400 (at base) should contribute zero surcharge, got {result_at_base}"
    )
    result_at_800 = compute_segment_budget(4, "haiku", files_loc=[800, 100, 100, 100])  # noqa: model-literal
    assert result_at_800 == baseline + 1, (
        f"loc=800 should add exactly 1 surcharge unit, expected {baseline + 1}, got {result_at_800}"
    )
