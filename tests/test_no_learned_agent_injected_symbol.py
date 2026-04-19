"""Regression: no `learned_agent_injected` literal anywhere in caller code (AC 14).

Scope: Python caller sites only. The string may legitimately appear in Markdown
documentation (e.g. `skills/audit/SKILL.md`) as the name of an emitted event
type consumed by `memory/policy_engine.py::_extract_quads`. What this test
rules out is Python code still passing/reading the removed receipt boolean.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_no_literal_in_hooks_or_skills_or_templates_py():
    """Use grep to confirm zero matches for `learned_agent_injected` in Python
    code under hooks/, skills/, and cli/assets/templates/. Markdown is allowed
    (see module docstring)."""
    targets = [ROOT / "hooks", ROOT / "skills", ROOT / "cli" / "assets" / "templates"]
    targets = [str(p) for p in targets if p.exists()]
    assert targets, "expected at least one target dir to exist"
    proc = subprocess.run(
        ["grep", "-rn", "--include=*.py",
         "learned_agent_injected", *targets],
        capture_output=True, text=True, check=False,
    )
    # grep returns 1 when no matches; 0 when matches found.
    assert proc.returncode == 1, (
        f"unexpected matches for learned_agent_injected in .py:\n{proc.stdout}"
    )
