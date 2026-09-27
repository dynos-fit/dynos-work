"""Runaway-defense seal tests.

This test file encodes the RUNAWAY-DEFENSE SEAL:
  - compute_segment_budget NEVER returns > 40 for any input.
  - would_overflow is True for any n >= 12, False for any n <= 11.
  - every executor's frontmatter maxTurns sits ABOVE that ceiling, by enough
    turns to write a progress ledger and exit cleanly.

These tests CANNOT be marked xfail or skipped. They are red-line tests.
If any test in this file is failing, production code is broken — do not
paper over it with marks. Fix the code.

Coverage:
  - AC-19: tool_budget never exceeds 40 for any (n_files, model) combination
  - AC-3:  would_overflow boundary is correct at 11/12
  - headroom: executor maxTurns > TOOL_BUDGET_CEILING with wrap-up margin
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "hooks") not in sys.path:
    sys.path.insert(0, str(ROOT / "hooks"))


# ---------------------------------------------------------------------------
# Parametrized ceiling seal
# ---------------------------------------------------------------------------


FILE_COUNTS = [0, 1, 8, 11, 12, 100, 1000]
MODELS = ["haiku", "sonnet", "opus"]


@pytest.mark.parametrize("n_files", FILE_COUNTS)
@pytest.mark.parametrize("model", MODELS)
def test_compute_segment_budget_never_exceeds_ceiling(n_files: int, model: str):
    """compute_segment_budget must NEVER return > 40 for any (n_files, model).

    This is the runaway-defense seal. The ceiling is 40. No code path may
    produce an unbounded budget. Violating this invariant means a runaway
    executor spawn can consume unlimited tool calls.
    """
    from lib_tool_budget import compute_segment_budget, TOOL_BUDGET_CEILING
    result = compute_segment_budget(n_files, model)
    assert result <= TOOL_BUDGET_CEILING, (
        f"RUNAWAY DEFENSE VIOLATED: compute_segment_budget({n_files}, {model!r}) "
        f"returned {result} which exceeds ceiling {TOOL_BUDGET_CEILING}. "
        f"No executor spawn may receive an unbounded budget."
    )


@pytest.mark.parametrize("n_files", FILE_COUNTS)
@pytest.mark.parametrize("model", MODELS)
def test_compute_segment_budget_always_positive(n_files: int, model: str):
    """compute_segment_budget must return a positive integer >= 1 for any input."""
    from lib_tool_budget import compute_segment_budget
    result = compute_segment_budget(n_files, model)
    assert isinstance(result, int), (
        f"compute_segment_budget({n_files}, {model!r}) returned {type(result).__name__}, expected int"
    )
    assert result >= 1, (
        f"compute_segment_budget({n_files}, {model!r}) returned {result}, must be >= 1"
    )


# ---------------------------------------------------------------------------
# would_overflow boundary seal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n_files", [0, 1, 2, 5, 8, 10, 11])
def test_would_overflow_false_for_n_lte_11(n_files: int):
    """would_overflow must be False for any n <= 11 (raw budget <= 40)."""
    from lib_tool_budget import would_overflow
    assert would_overflow(n_files) is False, (
        f"would_overflow({n_files}) returned True but must be False for n <= 11. "
        f"This breaks the runaway-defense seal: valid segments would be falsely rejected."
    )


@pytest.mark.parametrize("n_files", [12, 13, 15, 20, 50, 100, 1000])
def test_would_overflow_true_for_n_gte_12(n_files: int):
    """would_overflow must be True for any n >= 12 (raw budget > 40)."""
    from lib_tool_budget import would_overflow
    assert would_overflow(n_files) is True, (
        f"would_overflow({n_files}) returned False but must be True for n >= 12. "
        f"This breaks the runaway-defense seal: oversized segments would bypass the guard."
    )


def test_would_overflow_boundary_11_is_the_max_safe_count():
    """11 is the maximum file count that does not overflow — one more causes overflow."""
    from lib_tool_budget import would_overflow
    # 11 is safe
    assert would_overflow(11) is False, "11-file segment must not overflow (38 <= 40)"
    # 12 overflows
    assert would_overflow(12) is True, "12-file segment must overflow (41 > 40)"


# ---------------------------------------------------------------------------
# Ceiling constant integrity
# ---------------------------------------------------------------------------


def test_ceiling_constant_is_exactly_40():
    """TOOL_BUDGET_CEILING must be exactly 40. Any other value breaks the seal."""
    from lib_tool_budget import TOOL_BUDGET_CEILING
    assert TOOL_BUDGET_CEILING == 40, (
        f"TOOL_BUDGET_CEILING is {TOOL_BUDGET_CEILING}, must be exactly 40. "
        f"The validator ceiling and the max computed budget must agree on 40, and "
        f"every executor's frontmatter maxTurns must sit above it — see "
        f"test_executor_max_turns_leaves_wrapup_headroom."
    )


# ---------------------------------------------------------------------------
# Headroom seal: the operating ceiling sits BELOW the runaway backstop
#
# These three numbers used to be the same value (40/40/40), which is what made
# the seal bite the wrong thing: an executor handed a 40-call estimate had zero
# turns left to write the progress ledger that `ctl next-continuation` resumes
# from, so it was killed mid-edit instead of exiting cleanly. The ceiling is
# still 40 — the backstop moved up, so wrap-up fits underneath it.
# ---------------------------------------------------------------------------


# Turns an executor needs, after exhausting its tool-call estimate, to write
# its evidence file plus a progress ledger and return a summary.
MIN_WRAPUP_HEADROOM: int = 10

# The plugin's own executors: in-tree, authoritative, and what the pipeline
# actually spawns. These must each declare a cap with headroom.
_EXECUTOR_AGENTS = sorted((ROOT / "agents").glob("*-executor.md"))

# The copies the CLI scaffolds into new projects. Their final frontmatter is
# produced outside this repo (note the {{MODEL}} placeholder — nothing in-tree
# substitutes it), so a missing maxTurns here may be filled in by the packaged
# CLI and is NOT asserted. What is asserted: a template that does declare a cap
# must carry the same headroom, which catches a silent drift back to 40.
_EXECUTOR_TEMPLATES = sorted(
    (ROOT / "cli" / "assets" / "templates" / "base" / "agents").glob("*-executor.md")
)


def _frontmatter_max_turns(path: Path) -> "int | None":
    """Return the maxTurns value from an agent file's frontmatter, or None."""
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            return None  # frontmatter closed without declaring maxTurns
        if line.startswith("maxTurns:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                return None
    return None


def test_executor_agent_files_are_discoverable():
    """The headroom seal is only meaningful if it actually sees the agents."""
    assert len(_EXECUTOR_AGENTS) >= 10, (
        f"expected the executor agent files under agents/, found "
        f"{len(_EXECUTOR_AGENTS)}: {[p.name for p in _EXECUTOR_AGENTS]}"
    )
    assert _EXECUTOR_TEMPLATES, "expected executor templates under cli/assets/templates/base/agents/"


@pytest.mark.parametrize("agent_path", _EXECUTOR_AGENTS, ids=lambda p: str(p.relative_to(ROOT)))
def test_executor_declares_max_turns(agent_path: Path):
    """Every executor must declare maxTurns — an undeclared cap is unbounded."""
    assert _frontmatter_max_turns(agent_path) is not None, (
        f"{agent_path.relative_to(ROOT)} declares no parseable maxTurns in its frontmatter; "
        f"the runaway backstop would be whatever the harness defaults to."
    )


@pytest.mark.parametrize("agent_path", _EXECUTOR_AGENTS, ids=lambda p: str(p.relative_to(ROOT)))
def test_executor_max_turns_leaves_wrapup_headroom(agent_path: Path):
    """maxTurns must exceed TOOL_BUDGET_CEILING by at least MIN_WRAPUP_HEADROOM.

    Equal values mean an executor granted the maximum budget is killed at the
    exact call where it would have recorded its progress, so the continuation
    executor has nothing on disk to resume from.
    """
    from lib_tool_budget import TOOL_BUDGET_CEILING
    max_turns = _frontmatter_max_turns(agent_path)
    assert max_turns is not None, f"{agent_path.relative_to(ROOT)} declares no maxTurns"
    headroom = max_turns - TOOL_BUDGET_CEILING
    assert headroom >= MIN_WRAPUP_HEADROOM, (
        f"RUNAWAY DEFENSE HEADROOM VIOLATED: {agent_path.relative_to(ROOT)} has maxTurns={max_turns} "
        f"against TOOL_BUDGET_CEILING={TOOL_BUDGET_CEILING}, leaving {headroom} turns for "
        f"wrap-up (minimum {MIN_WRAPUP_HEADROOM}). An executor that exhausts its estimate "
        f"must still be able to write its evidence file and progress ledger; otherwise it "
        f"is killed mid-edit and the continuation loop has nothing to resume from."
    )


@pytest.mark.parametrize("agent_path", _EXECUTOR_AGENTS, ids=lambda p: str(p.relative_to(ROOT)))
def test_executor_prompt_does_not_contradict_injected_budget_block(agent_path: Path):
    """An executor must not be told to stop at its budget.

    router.py's injected `## Tool-Use Budget` block states the figure is an
    estimate and NOT a cap. An agent file that also says "stop within N tool
    uses of that budget" puts two opposite instructions in one context window.
    """
    text = agent_path.read_text(encoding="utf-8")
    assert "within 3 tool uses of that budget" not in text, (
        f"{agent_path.relative_to(ROOT)} still carries the superseded self-pacing instruction. "
        f"It contradicts the injected Tool-Use Budget block, which says the budget is "
        f"an estimate and not a cap."
    )
    assert "maxTurns: 40` is the runaway backstop" not in text, (
        f"{agent_path.relative_to(ROOT)} names maxTurns: 40 as its backstop, which no longer matches "
        f"its frontmatter."
    )


def test_ceiling_consistent_with_would_overflow_boundary():
    """The overflow boundary (11/12) must be consistent with PER_FILE_COST,
    FIXED_OVERHEAD, and TOOL_BUDGET_CEILING."""
    from lib_tool_budget import PER_FILE_COST, FIXED_OVERHEAD, TOOL_BUDGET_CEILING
    # 11 * 3 + 5 = 38 <= 40 → no overflow
    assert 11 * PER_FILE_COST + FIXED_OVERHEAD <= TOOL_BUDGET_CEILING
    # 12 * 3 + 5 = 41 > 40 → overflow
    assert 12 * PER_FILE_COST + FIXED_OVERHEAD > TOOL_BUDGET_CEILING


@pytest.mark.parametrize(
    "template_path", _EXECUTOR_TEMPLATES, ids=lambda p: p.name
)
def test_executor_template_cap_if_declared_has_headroom(template_path: Path):
    """A CLI executor template that declares maxTurns must leave wrap-up headroom.

    Templates whose frontmatter omits maxTurns are skipped deliberately: their
    final form is assembled by the packaged CLI, which is not in this repo, so
    this file cannot tell an omission from a value injected at scaffold time.
    A declared value, though, ships as written — and must not sit at the
    ceiling.
    """
    from lib_tool_budget import TOOL_BUDGET_CEILING
    max_turns = _frontmatter_max_turns(template_path)
    if max_turns is None:
        pytest.skip(f"{template_path.name} declares no maxTurns; the CLI supplies it")
    assert max_turns - TOOL_BUDGET_CEILING >= MIN_WRAPUP_HEADROOM, (
        f"template {template_path.name} declares maxTurns={max_turns} against ceiling "
        f"{TOOL_BUDGET_CEILING}; every project scaffolded from it would inherit an "
        f"executor with no room to write its progress ledger."
    )
