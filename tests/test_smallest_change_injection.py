"""Executors that write product code get the shared smallest-change rules.

`build_executor_prompt` appends `agents/_shared/smallest-change.md` for every
executor except docs-executor (prose) and testing-executor (its output is the
tests), so the rule set lives in one file instead of thirteen prompts.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

import router  # noqa: E402
from router import build_executor_prompt  # noqa: E402

HEADING = "## Smallest Complete Change (injected)"


def _prompt(executor: str, plan_entry: dict | None = None) -> str:
    segment = {"id": "seg-1", "executor": executor, "files_expected": ["src/a.py"]}
    return build_executor_prompt(ROOT, segment, plan_entry or {}, "BASE PROMPT")


@pytest.mark.parametrize("executor", sorted(
    p.stem for p in (ROOT / "agents").glob("*-executor.md")
    if p.stem not in {"docs-executor", "testing-executor"}
))
def test_code_writing_executors_get_the_rules(executor: str) -> None:
    prompt = _prompt(executor)
    assert prompt.startswith("BASE PROMPT")
    assert prompt.count(HEADING) == 1
    assert "Never add a dependency for a few lines" in prompt
    assert "Never cut to make it smaller" in prompt
    assert "## Skipped and Not Checked" in prompt
    # The attribution comment is for people reading the file, not the agent.
    assert "<!--" not in prompt and "THIRD_PARTY_NOTICES" not in prompt


@pytest.mark.parametrize("executor", ["docs-executor", "testing-executor", "planning", ""])
def test_other_roles_do_not(executor: str) -> None:
    assert HEADING not in _prompt(executor)


def test_rules_come_before_prevention_rules_and_budget() -> None:
    prompt = _prompt("backend-executor", {"prevention_rules": ["Keep it typed."], "tool_budget": 12})
    assert prompt.index(HEADING) < prompt.index("## Prevention Rules") < prompt.index("## Tool-Use Budget")


def test_missing_rules_file_is_not_fatal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(router, "_SMALLEST_CHANGE_PATH", tmp_path / "gone.md")
    assert _prompt("backend-executor") == "BASE PROMPT"
