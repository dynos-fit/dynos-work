"""Doc test: skills/audit/SKILL.md states the D4 loop rule (AC10)."""
from __future__ import annotations

import re
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent / "skills" / "audit" / "SKILL.md"


def _text() -> str:
    return SKILL.read_text(encoding="utf-8")


def test_skill_states_blocking_finding_cascade():
    assert "blocking finding" in _text()
    assert "deep tier on any blocking finding" in _text()
    assert "deep tier on any finding" not in _text()


def test_skill_does_not_respawn_single_auditors_every_pass():
    text = _text()
    assert "spawn every auditor whose status is `spawn` or `single` (in parallel, at the returned `model`), write each receipt, then run" not in text
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        low = sentence.lower()
        if "single" in low and "every loop pass" in low and "spawn" in low:
            raise AssertionError(sentence)
    assert "never again while `receipts/audit-{name}.json` exists" in text
    assert "`complete` auditors are never re-spawned" in text
    assert "spawns only `status: spawn` auditors at the returned `model` and `shard_step_name`" in text or \
        "spawn only `status: spawn` auditors at the returned `model` and `shard_step_name`" in text


def test_skill_uses_canonical_report_path():
    text = _text()
    assert ".dynos/task-{id}/audit-reports/{auditor_name}-{model}-attempt-{n}.json" in text
    assert "{report_filename}" not in text
    assert "-checkpoint-{timestamp}" not in text
