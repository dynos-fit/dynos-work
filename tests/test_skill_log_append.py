"""Skills must route execution-log lines through `ctl log-append`.

Direct appends from the orchestrator fail as soon as a subagent role is
stamped (the orchestrator adopts the stamped role on hosts that share one
session id), so no skill may instruct a hand-written append.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILLS = sorted((ROOT / "skills").glob("*/SKILL.md"))

# Prose that tells the orchestrator to write the log file itself.
_HAND_APPEND_PATTERNS = [
    re.compile(r"(?i)append[^\n]{0,80}to the execution log:"),
    re.compile(r"(?i)append[^\n]{0,40}line[^\n]{0,60}to `execution-log\.md`"),
    re.compile(r">>\s*\.?dynos/task-\{id\}/execution-log\.md"),
    re.compile(r"(?m)^\{timestamp\} \[(GATE|SKIP|DONE|ADVANCE|BUDGET-PAUSE)\]"),
]


@pytest.mark.parametrize("skill", SKILLS, ids=lambda p: p.parent.name)
def test_no_hand_written_execution_log_appends(skill: Path) -> None:
    text = skill.read_text(encoding="utf-8")
    offenders = [p.pattern for p in _HAND_APPEND_PATTERNS if p.search(text)]
    assert not offenders, f"{skill.relative_to(ROOT)} instructs a direct execution-log append: {offenders}"


def test_start_skill_uses_log_append_for_its_gate_lines() -> None:
    text = (ROOT / "skills/start/SKILL.md").read_text(encoding="utf-8")
    assert text.count("ctl log-append") >= 3, "start skill should record SKIP/GATE/DONE lines via log-append"
    assert "[ADVANCE] PLAN_AUDIT" not in text, "transition_task writes the stage line; no manual [ADVANCE]"
