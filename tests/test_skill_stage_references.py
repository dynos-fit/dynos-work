"""Linter test: every stage name referenced in skills/*/SKILL.md must
exist in lib_core.STAGE_ORDER.

Caught the "REPAIR" bug at execute/SKILL.md:299 (the actual stage is
REPAIR_PLANNING; REPAIR has not existed since the state machine split).
Generally guards against skill prose drifting out of sync with the
state machine.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_core import STAGE_ORDER

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "skills"

# Patterns that indicate a stage name is being referenced as a transition target.
# These are the prose patterns the orchestrator follows literally.
_TRANSITION_PATTERNS = [
    # `python3 hooks/ctl.py transition .dynos/task-{id} STAGE_NAME`
    re.compile(r"hooks/ctl\.py\s+transition\s+\S+\s+([A-Z_][A-Z0-9_]+)"),
    # "Update manifest.json stage to STAGE_NAME" / "Update ... stage to `STAGE`"
    re.compile(r"Update\s+(?:manifest\.json\s+)?stage\s+to\s+`?([A-Z_][A-Z0-9_]+)`?"),
    # "transition_task(task_dir, "STAGE")" or "transition_task(.., 'STAGE')"
    re.compile(r"transition_task\([^)]*[\"']([A-Z_][A-Z0-9_]+)[\"']"),
]

# Names that look like stage references but are not actually stages
# (e.g., section headings, code-related identifiers).
_NOT_A_STAGE = {"STAGE", "STAGES", "TODO", "FIXME", "HACK", "NOTE", "PLUGIN_HOOKS",
                "PYTHONPATH", "EOF", "AGENT_JSON_OUTPUT", "EXECUTOR_PLAN_JSON"}


def _extract_stage_references(text: str) -> set[str]:
    """Return the set of stage-like tokens referenced via transition patterns."""
    found: set[str] = set()
    for pattern in _TRANSITION_PATTERNS:
        for match in pattern.finditer(text):
            token = match.group(1)
            if token in _NOT_A_STAGE:
                continue
            found.add(token)
    return found


def test_all_skill_stage_references_are_valid():
    """Every stage name referenced in any skill's prose must exist in STAGE_ORDER.

    If this fails, either the skill is wrong (most likely) or the state
    machine was renamed without updating the skills.
    """
    valid_stages = set(STAGE_ORDER)
    failures: list[str] = []

    for skill_md in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        text = skill_md.read_text()
        refs = _extract_stage_references(text)
        unknown = refs - valid_stages
        if unknown:
            relpath = skill_md.relative_to(REPO_ROOT)
            for stage in sorted(unknown):
                failures.append(f"{relpath}: references unknown stage {stage!r}")

    assert not failures, (
        "Skill prose references stages that are not in lib_core.STAGE_ORDER:\n"
        + "\n".join(f"  - {f}" for f in failures)
    )


def test_extractor_finds_known_patterns():
    """Sanity check that the regex patterns match the prose conventions
    the skills actually use."""
    sample = """
    Some prose. Then `python3 hooks/ctl.py transition .dynos/task-{id} EXECUTION` happens.
    Update manifest.json stage to `CHECKPOINT_AUDIT` here.
    The code calls transition_task(task_dir, "DONE") at the end.
    """
    refs = _extract_stage_references(sample)
    assert "EXECUTION" in refs
    assert "CHECKPOINT_AUDIT" in refs
    assert "DONE" in refs


def test_extractor_ignores_non_stage_uppercase_tokens():
    sample = """
    Reference $PYTHONPATH and ${PLUGIN_HOOKS} freely — these are not stage refs.
    A heading like ## STAGE is not a transition target either.
    """
    refs = _extract_stage_references(sample)
    assert refs == set(), f"expected no stage refs, got {refs}"
