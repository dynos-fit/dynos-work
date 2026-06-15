"""Adaptive per-spawn tool-budget arithmetic (leaf module).

Pure functions and constants used by the router/validator to compute and
enforce a per-segment tool-call budget for spawned executor agents.

Formula (spec AC-2):
    budget = min(
        TOOL_BUDGET_CEILING,
        max(N * PER_FILE_COST + FIXED_OVERHEAD, floor),
    )

    The floor is resolved from STATIC_CAPS_BY_MODEL (when model is provided),
    STATIC_CAPS_BY_TIER (when model is None and tier is provided), or 15
    (haiku-equivalent default when both are absent).  # noqa: model-literal

Overflow predicate (spec AC-3):
    overflow = (N * PER_FILE_COST + FIXED_OVERHEAD) > TOOL_BUDGET_CEILING

This module is a leaf: it MUST NOT import from any other dynos-work hooks/
module. Constants are byte-exact and load-bearing — the router, validator,
and prompt-builder all import these names directly.
"""

from __future__ import annotations

# Per-file marginal cost in tool calls (read + edit + verify).
PER_FILE_COST: int = 3

# Fixed per-segment overhead in tool calls (initial reads, evidence write,
# sanity-check shell calls, etc.).
FIXED_OVERHEAD: int = 5

# Hard ceiling: no segment may be granted more than this many tool calls.
# A segment whose raw budget exceeds this must be decomposed at planning time.
TOOL_BUDGET_CEILING: int = 40

# Soft ceiling: advisory threshold used by self-pacing prompt instruction.
TOOL_BUDGET_ADVISORY: int = 35

# Static per-model floor (minimum budget) keyed by model family name.
# An unknown model falls back to 15 (the haiku floor) — see compute_segment_budget.  # noqa: model-literal
STATIC_CAPS_BY_MODEL: dict[str, int] = {"haiku": 15, "sonnet": 20, "opus": 25}  # noqa: model-literal

# Static per-tier floor (minimum budget) keyed by tier name.
# Maps tier names (fast/balanced/deep) to their respective floor values.
# Used by compute_segment_budget when model=None and a tier is supplied.
STATIC_CAPS_BY_TIER: dict[str, int] = {"fast": 15, "balanced": 20, "deep": 25}

# LOC below this per file are free (no surcharge added to raw budget).
LOC_BASE: int = 400

# LOC per +1 additional tool-call surcharge above LOC_BASE.
LOC_SLOPE: int = 400


def compute_segment_budget(
    files_expected_count: int,
    model: "str | None",
    *,
    tier: "str | None" = None,
    files_loc: "list[int] | None" = None,
) -> int:
    """Return the tool-call budget for a segment.

    Spec AC-2:
        min(TOOL_BUDGET_CEILING,
            max(N * PER_FILE_COST + FIXED_OVERHEAD,
                floor))

    The floor is resolved as follows:
      1. If *model* is a non-None string, use STATIC_CAPS_BY_MODEL.get(model, 15)
         (original behavior — backward-compatible with all existing callers).
      2. If *model* is None and *tier* is a known tier name, use
         STATIC_CAPS_BY_TIER[tier].
      3. Otherwise (model is None and tier is None or unknown), default to 15
         (haiku-equivalent — preserves pre-task behavior when model is null).  # noqa: model-literal

    When *files_loc* is provided (non-None), the raw budget is augmented by:
        loc_surcharge = sum(max(0, loc - LOC_BASE) // LOC_SLOPE for loc in files_loc)
    The surcharge is added to raw BEFORE the min(ceiling, max(raw, floor)) clamp.
    Passing files_loc=None is byte-identical to calling without the kwarg.

    Args:
        files_expected_count: Number of files in the segment's files_expected.
        model: Model family name ("haiku" | "sonnet" | "opus" | other), or None.  # noqa: model-literal
        tier: Optional tier name ("fast" | "balanced" | "deep").  Keyword-only.
              Only consulted when model is None.
        files_loc: Optional list of on-disk line counts for each expected file.
                   Use 0 for files that do not yet exist.  Keyword-only.

    Returns:
        Integer tool-call budget, always 1 <= budget <= TOOL_BUDGET_CEILING.
    """
    raw = files_expected_count * PER_FILE_COST + FIXED_OVERHEAD
    if files_loc is not None:
        for loc in files_loc:
            raw += max(0, loc - LOC_BASE) // LOC_SLOPE
    if model is not None:
        floor = STATIC_CAPS_BY_MODEL.get(model, 15)
    elif tier is not None:
        floor = STATIC_CAPS_BY_TIER.get(tier, 15)
    else:
        floor = 15
    return min(TOOL_BUDGET_CEILING, max(raw, floor))


def would_overflow(files_expected_count: int) -> bool:
    """Return True if the raw (pre-floor) budget would exceed the hard ceiling.

    Spec AC-3:
        (N * PER_FILE_COST + FIXED_OVERHEAD) > TOOL_BUDGET_CEILING

    Boundary: 11 files -> 38 (False); 12 files -> 41 (True).

    Args:
        files_expected_count: Number of files in the segment's files_expected.

    Returns:
        True iff the segment exceeds the 11-file ceiling and must be decomposed.
    """
    return (files_expected_count * PER_FILE_COST + FIXED_OVERHEAD) > TOOL_BUDGET_CEILING
