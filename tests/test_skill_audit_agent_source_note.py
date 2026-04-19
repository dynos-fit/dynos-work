"""Test for the agent_source cross-check note in skills/audit/SKILL.md
(CRITERION 8).

The audit skill must document the `agent_source_reclassified` event
name so auditors and reviewers understand that retrospective `learned:X`
claims are cross-checked against `.dynos/events.jsonl` and unmatched
claims are downgraded to `"generic"` with an audit-trail event.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AUDIT_SKILL = REPO_ROOT / "skills" / "audit" / "SKILL.md"


def test_audit_skill_mentions_agent_source_reclassified():
    """skills/audit/SKILL.md must contain the literal event name."""
    assert AUDIT_SKILL.exists(), f"missing: {AUDIT_SKILL}"
    text = AUDIT_SKILL.read_text()
    assert "agent_source_reclassified" in text, (
        f"expected the literal substring 'agent_source_reclassified' in "
        f"{AUDIT_SKILL}; the audit skill must document the cross-check "
        f"event so reviewers can trace downgrades."
    )
