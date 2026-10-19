"""Regression: forbidden literals must not reappear in skill prose (AC 26-29)."""
from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"


FORBIDDEN_HUMAN_APPROVAL_LINES = [
    "[HUMAN] SPEC_REVIEW \u2014 approved",
    "[HUMAN] PLAN_REVIEW \u2014 approved",
    "[HUMAN] TDD_REVIEW \u2014 approved",
    "[HUMAN] SPEC_REVIEW - approved",
    "[HUMAN] PLAN_REVIEW - approved",
    "[HUMAN] TDD_REVIEW - approved",
]


def _all_skill_md_files() -> list[Path]:
    return sorted(SKILLS.glob("*/SKILL.md"))


@pytest.mark.parametrize("forbidden", FORBIDDEN_HUMAN_APPROVAL_LINES)
def test_no_legacy_human_approval_log_line(forbidden: str):
    offenders: list[str] = []
    for f in _all_skill_md_files():
        text = f.read_text()
        if forbidden in text:
            offenders.append(str(f.relative_to(ROOT)))
    assert not offenders, (
        f"forbidden literal {forbidden!r} found in: {offenders}"
    )


def test_no_learned_agent_injected_field_in_skills():
    """The renamed event-type literal must not appear as a payload field."""
    offenders: list[str] = []
    for f in _all_skill_md_files():
        text = f.read_text()
        if "learned_agent_injected=" in text:
            offenders.append(str(f.relative_to(ROOT)))
    assert not offenders, f"`learned_agent_injected=` found in: {offenders}"


def test_at_least_one_skill_uses_approve_stage():
    """Sanity check: the new approval pattern must replace the forbidden one."""
    found_any = False
    for f in _all_skill_md_files():
        text = f.read_text()
        if "approve-stage" in text:
            found_any = True
            break
    assert found_any, "no skill references `approve-stage` — replacement missing"


def test_token_records_are_hook_owned():
    for skill in ("start", "execute", "audit"):
        text = (SKILLS / skill / "SKILL.md").read_text()
        assert "lib_tokens record" not in text
        assert "Token usage is recorded automatically by the SubagentStop hook" in text
