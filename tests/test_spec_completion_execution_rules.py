"""spec-completion-auditor must not accept unexecuted tests or unreachable
features (field report: e2e tests "written and analyzed but not run" and
modules flagged isBuilt but never registered both passed audit)."""
from pathlib import Path

PROMPT = (Path(__file__).resolve().parents[1] / "agents" / "spec-completion-auditor.md").read_text()


def test_written_but_unrun_tests_are_not_evidence() -> None:
    assert "A test that was written but never run proves nothing." in PROMPT
    assert "test-backed criterion never executed" in PROMPT


def test_user_visible_features_must_be_reachable() -> None:
    assert "must be **reachable**" in PROMPT
    assert "isBuilt: true" in PROMPT
