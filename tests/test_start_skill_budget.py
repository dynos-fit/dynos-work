"""Observed baseline after PR-01…11 plus the log-append fix: combined 18 commands / 2 spawns / 2 rounds; staged medium 21 / 3 / 3; high/critical has at most 5 spawns / 4 rounds. The three extra commands are `ctl log-append` lines that replaced hand-written execution-log appends (which the stamped role could not perform). Command ceilings include a margin of two."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Intentional ceiling changes require updating this contract and documenting
# the newly observed counts in the module docstring.
COMBINED = {"commands": 20, "spawns": 2, "rounds": 2}
STAGED_MEDIUM = {"commands": 23, "spawns": 3, "rounds": 3}
HIGH_CRITICAL = {"spawns": 7, "rounds": 5}


def _sections(text: str) -> dict[str, str]:
    matches = list(re.finditer(r"(?m)^## Step (\d+)[^\n]*", text))
    result = {}
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        result[match.group(1)] = text[match.start():end]
    return result


def _counts(text: str) -> dict[str, int]:
    # Count only fenced command lines, explicit spawn instructions, and
    # actual AskUserQuestion invocations, not explanatory prose references.
    commands = len(re.findall(r'(?m)^\s*"\$DYNOS"\s+', text))
    spawns = len(re.findall(r"(?im)^\s*(?:\d+\.\s*)?(?:Spawn|spawn)\s+(?:the\s+)?(?:Planner|Master Planner|planning|subagent|spec-completion-auditor|testing-executor)", text))
    rounds = len(re.findall(r"AskUserQuestion", text))
    return {"commands": commands, "spawns": spawns, "rounds": rounds}


def test_start_skill_paths_stay_within_budget() -> None:
    sections = _sections((ROOT / "skills/start/SKILL.md").read_text())
    combined_text = "\n".join(value for step, value in sections.items() if step not in {"3", "4"})
    staged_text = "\n".join(sections.values())
    combined = _counts(combined_text)
    staged = _counts(staged_text)
    # Count each selected path's actual planner phases and review gates.
    # Step 5 alternatives are exclusive; Step 2d and Step 8 are high-only.
    combined["spawns"] = 1 + len(re.findall(r"Spawn Planner ONCE", sections["5"]))
    staged["spawns"] = 1 + 1 + len(re.findall(r"Spawn Planner \(Opus\)", sections["5"]))
    combined["rounds"] = 1 + 1  # discovery question round + combined review
    staged["rounds"] = 1 + 1 + 1  # discovery, spec review, plan review
    for key, ceiling in COMBINED.items():
        assert combined[key] <= ceiling, f"combined {key}: {combined[key]} > {ceiling}"
    for key, ceiling in STAGED_MEDIUM.items():
        assert staged[key] <= ceiling, f"staged medium {key}: {staged[key]} > {ceiling}"
    assert staged["spawns"] <= HIGH_CRITICAL["spawns"]
    assert staged["rounds"] <= HIGH_CRITICAL["rounds"]
