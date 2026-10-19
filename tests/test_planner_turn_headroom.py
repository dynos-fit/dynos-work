"""Planner turns must cover largest enforced read phase, writes, and buffer."""
import re
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def test_planner_turn_headroom_covers_enforced_read_budget():
    from sys import path
    path.insert(0,str(ROOT/"hooks"))
    from lib_tool_budget import PLANNER_PHASE_READ_ALLOWANCE, PLANNER_WRITE_TURNS
    text=(ROOT/"agents/planning.md").read_text()
    turns=int(re.search(r"^maxTurns:\s*(\d+)$",text,re.M).group(1))
    assert turns >= max(PLANNER_PHASE_READ_ALLOWANCE.values())+PLANNER_WRITE_TURNS+10
