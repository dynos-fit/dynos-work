"""Tests for the inline-vs-spawn decision table in skills/execute/SKILL.md
(CRITERION 2, Fix C).

Pins the decision table header, the four body rows, and the literal
FORBIDDEN sentence against regression. If a future edit softens or
removes this documentation, the test fails before it reaches main.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_PATH = REPO_ROOT / "skills" / "execute" / "SKILL.md"


@pytest.fixture(scope="module")
def skill_text() -> str:
    assert SKILL_PATH.exists(), f"missing: {SKILL_PATH}"
    return SKILL_PATH.read_text()


def test_decision_table_header_present(skill_text: str) -> None:
    """The literal five-column header the spec requires must be present."""
    pattern = (
        r"\|\s*fast_track\s*\|\s*n_segments\s*\|\s*route_mode\s*\|"
        r"\s*agent_path\s*\|\s*path_taken\s*\|"
    )
    assert re.search(pattern, skill_text), (
        "Decision table header row not found in skills/execute/SKILL.md. "
        "Expected columns: fast_track | n_segments | route_mode | agent_path | path_taken"
    )


def test_forbidden_sentence_present(skill_text: str) -> None:
    """The inline-branch FORBIDDEN sentence must appear verbatim."""
    sentence = (
        "If any segment has route_mode in {replace, alongside} with "
        "non-null agent_path, the inline branch is FORBIDDEN — take "
        "the spawn path."
    )
    assert sentence in skill_text, (
        "FORBIDDEN sentence missing or altered in skills/execute/SKILL.md.\n"
        f"Expected the literal substring:\n  {sentence}"
    )


def test_four_decision_rows_present(skill_text: str) -> None:
    """All four body rows covering the inline-vs-spawn cases must exist."""
    # Row 1: fast_track=true, n_segments=1, route_mode=generic, agent_path=null -> inline
    row1 = re.search(
        r"\|\s*true\s*\|\s*1\s*\|\s*generic\s*\|\s*null\s*\|\s*inline[^|]*\|",
        skill_text,
    )
    assert row1, "Row 1 (generic inline path) not found in decision table."

    # Row 2: fast_track=true, n_segments=1, route_mode=replace, agent_path=<any> -> spawn (inline FORBIDDEN)
    row2 = re.search(
        r"\|\s*true\s*\|\s*1\s*\|\s*replace\s*\|[^|]*\|\s*spawn[^|]*FORBIDDEN[^|]*\|",
        skill_text,
    )
    assert row2, "Row 2 (replace -> spawn with FORBIDDEN) not found in decision table."

    # Row 3: fast_track=true, n_segments=1, route_mode=alongside, agent_path=<any> -> spawn (inline FORBIDDEN)
    row3 = re.search(
        r"\|\s*true\s*\|\s*1\s*\|\s*alongside\s*\|[^|]*\|\s*spawn[^|]*FORBIDDEN[^|]*\|",
        skill_text,
    )
    assert row3, "Row 3 (alongside -> spawn with FORBIDDEN) not found in decision table."

    # Row 4: non-fast-track case -> spawn
    row4 = re.search(
        r"\|[^|]*(false|n_segments\s*>\s*1)[^|]*\|[^|]*\|[^|]*\|[^|]*\|\s*spawn\s*\|",
        skill_text,
    )
    assert row4, "Row 4 (non-fast-track -> spawn) not found in decision table."
