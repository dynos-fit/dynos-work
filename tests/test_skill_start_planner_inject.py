"""Planner receipts bind to the hook-observed Agent spawn, not prompt sidecars."""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

@pytest.fixture(scope="module")
def skill_text() -> str:
    return (ROOT / "skills/start/SKILL.md").read_text()

@pytest.fixture(scope="module")
def template_text() -> str:
    return (ROOT / "cli/assets/templates/base/start.md").read_text()

@pytest.mark.parametrize("text_fixture", ["skill_text", "template_text"])
def test_start_skill_uses_single_receipt_without_prompt_sidecar(text_fixture, request):
    text = request.getfixturevalue(text_fixture)
    if text_fixture == "skill_text":
        assert "planner-receipt" in text
    assert "planner-inject-prompt" not in text
    assert "--injected-prompt-sha256" not in text


def test_planner_receipt_receives_no_sidecar_flag(skill_text: str):
    assert "--injected-prompt-sha256" not in skill_text
