"""Tests for the planner-inject-prompt + receipt_planner_spawn wiring in
skills/start/SKILL.md and cli/assets/templates/base/start.md (CRITERION 7).

Both files must document:
  - Three planner-inject-prompt invocations (one per phase: discovery,
    spec, plan).
  - Every `receipt_planner_spawn(` block must pass
    `injected_prompt_sha256=` through as a kwarg in the same call.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_PATH = REPO_ROOT / "skills" / "start" / "SKILL.md"
TEMPLATE_PATH = REPO_ROOT / "cli" / "assets" / "templates" / "base" / "start.md"

# `receipt_planner_spawn(` may appear with either `hooks/router.py` or the
# template-rendered `dynorouter.py`; both forms are acceptable for the
# inject-prompt call as long as the subcommand and --phase appear.
_INJECT_PATTERN = (
    r"planner-inject-prompt\s+--task-id\s+\S+\s+--phase\s+(discovery|spec|plan)"
)

# A `receipt_planner_spawn(` call may span multiple lines in these files.
# We match a full call to its closing `)` and then assert the kwarg appears
# inside.
_RPS_CALL_PATTERN = re.compile(
    r"receipt_planner_spawn\s*\((.*?)\)",
    re.DOTALL,
)


def _find_all_rps_calls(text: str) -> list[str]:
    """Return the argument block of every receipt_planner_spawn(...) call.

    Uses a stack-based parser so nested parens inside string args don't
    confuse the match (safer than regex .*? on multi-line calls).
    """
    results: list[str] = []
    needle = "receipt_planner_spawn("
    idx = 0
    while True:
        start = text.find(needle, idx)
        if start < 0:
            return results
        # Scan forward balancing parens from the char after `(`.
        depth = 1
        i = start + len(needle)
        while i < len(text) and depth > 0:
            ch = text[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            i += 1
        if depth != 0:
            # Malformed call — stop parsing (test will fail on count below).
            return results
        results.append(text[start + len(needle) : i - 1])
        idx = i


@pytest.fixture(scope="module")
def skill_text() -> str:
    assert SKILL_PATH.exists(), f"missing: {SKILL_PATH}"
    return SKILL_PATH.read_text()


@pytest.fixture(scope="module")
def template_text() -> str:
    assert TEMPLATE_PATH.exists(), f"missing: {TEMPLATE_PATH}"
    return TEMPLATE_PATH.read_text()


def test_start_skill_has_three_planner_inject_prompt_calls(skill_text: str):
    """skills/start/SKILL.md must document at least three planner-inject-prompt
    invocations — one per phase."""
    matches = re.findall(_INJECT_PATTERN, skill_text)
    assert len(matches) >= 3, (
        f"expected >=3 planner-inject-prompt invocations in {SKILL_PATH}; "
        f"found {len(matches)}: {matches}"
    )
    # All three phases must be represented at least once.
    phases = set(matches)
    assert {"discovery", "spec", "plan"}.issubset(phases), (
        f"missing phases in {SKILL_PATH}; found phases={phases}"
    )


def test_skill_receipt_planner_spawn_calls_include_injected_prompt_sha256(
    skill_text: str,
):
    """skills/start/SKILL.md must preserve planner injection proof.

    Accept either:
    - direct `receipt_planner_spawn(..., injected_prompt_sha256=...)` calls
    - deterministic `hooks/ctl.py planner-receipt ... --injected-prompt-sha256`
    """
    calls = _find_all_rps_calls(skill_text)
    if calls:
        for idx, args in enumerate(calls):
            assert "injected_prompt_sha256=" in args, (
                f"receipt_planner_spawn() call #{idx + 1} in {SKILL_PATH} is "
                f"missing the injected_prompt_sha256 kwarg. Args block:\n{args}"
            )
        return

    assert "--injected-prompt-sha256" in skill_text and "planner-receipt" in skill_text, (
        f"{SKILL_PATH} must document either direct receipt_planner_spawn(..., "
        "injected_prompt_sha256=...) calls or ctl.py planner-receipt with "
        "--injected-prompt-sha256"
    )


def test_template_has_three_planner_inject_prompt_calls(template_text: str):
    """cli/assets/templates/base/start.md must mirror the three invocations."""
    matches = re.findall(_INJECT_PATTERN, template_text)
    assert len(matches) >= 3, (
        f"expected >=3 planner-inject-prompt invocations in {TEMPLATE_PATH}; "
        f"found {len(matches)}: {matches}"
    )
    phases = set(matches)
    assert {"discovery", "spec", "plan"}.issubset(phases), (
        f"missing phases in {TEMPLATE_PATH}; found phases={phases}"
    )


def test_template_receipt_planner_spawn_calls_include_injected_prompt_sha256(
    template_text: str,
):
    """Every receipt_planner_spawn(...) block in the base template must
    carry `injected_prompt_sha256=` within the same call."""
    calls = _find_all_rps_calls(template_text)
    assert calls, f"no receipt_planner_spawn(...) calls found in {TEMPLATE_PATH}"
    for idx, args in enumerate(calls):
        assert "injected_prompt_sha256=" in args, (
            f"receipt_planner_spawn() call #{idx + 1} in {TEMPLATE_PATH} is "
            f"missing the injected_prompt_sha256 kwarg. Args block:\n{args}"
        )
