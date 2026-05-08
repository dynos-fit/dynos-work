"""AC 12: skills/start/SKILL.md Step 2c must instruct providing the new
search-receipt fields when search_recommended is true.

This test is a string assertion against the skill prose — it exists so the
amendment to Step 2c is a contract the harness can verify, not just a
human-review item.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = ROOT / "skills" / "start" / "SKILL.md"


@pytest.fixture(scope="module")
def skill_text() -> str:
    return SKILL_PATH.read_text(encoding="utf-8")


def _section_text(text: str, heading: str, next_heading_re: str = r"^## ") -> str:
    """Extract text between `heading` and the next top-level heading."""
    import re

    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == heading.strip():
            start = i + 1
            break
    if start is None:
        return ""
    end = len(lines)
    for j in range(start, len(lines)):
        if re.match(next_heading_re, lines[j]):
            end = j
            break
    return "\n".join(lines[start:end])


def test_skill_md_step_2c_mentions_urls_consulted_flag(skill_text: str) -> None:
    """Step 2c prose must reference the --urls-consulted flag so the
    orchestrator knows to provide it."""
    section = _section_text(skill_text, "## Step 2c — External Solution Gate (always runs)")
    assert section, "could not locate Step 2c section in SKILL.md"
    assert "--urls-consulted" in section, (
        "Step 2c must mention --urls-consulted; AC 12 of task-005 requires "
        "the prose to instruct providing this flag when search_recommended=true"
    )


def test_skill_md_step_2c_mentions_findings_summary_flag(skill_text: str) -> None:
    section = _section_text(skill_text, "## Step 2c — External Solution Gate (always runs)")
    assert section
    assert "--findings-summary" in section, (
        "Step 2c must mention --findings-summary; AC 12 of task-005"
    )


def test_skill_md_step_2c_includes_example_invocation(skill_text: str) -> None:
    """The amendment must include at least a one-line example showing the
    new flags in use, so the orchestrator has a concrete shape to copy."""
    section = _section_text(skill_text, "## Step 2c — External Solution Gate (always runs)")
    assert section
    # Look for an example block that mentions both new flags together
    # within the same code block / line vicinity.
    has_combined_example = (
        "--urls-consulted" in section
        and "--findings-summary" in section
        and "write-search-receipt" in section
    )
    assert has_combined_example, (
        "Step 2c must include a write-search-receipt example invocation "
        "that uses both new flags"
    )
