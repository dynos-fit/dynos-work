"""Doc test: the execute skill's ctl calls run in an order the stage gates accept.

- `run-execution-finish` refuses without the `rules-check-passed` receipt, so
  the skill must run `run-rules-check` first, in `--mode all` (execution never
  stages files, so the default `staged` mode would check nothing).
- `run-execution-verify-evidence` accepts only EXECUTION / TEST_EXECUTION and
  `run-tests` advances the task to CHECKPOINT_AUDIT, so the verifier must run
  before `run-tests`.
"""
from __future__ import annotations

from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent / "skills" / "execute" / "SKILL.md"


def _text() -> str:
    return SKILL.read_text(encoding="utf-8")


def _first(text: str, needle: str) -> int:
    index = text.find(needle)
    assert index >= 0, f"execute skill no longer mentions {needle!r}"
    return index


def test_rules_check_runs_in_all_mode_before_finish():
    text = _text()
    rules = _first(text, "ctl run-rules-check .dynos/task-{id} --mode all")
    finish = _first(text, "ctl run-execution-finish .dynos/task-{id}")
    assert rules < finish


def test_evidence_verifier_runs_before_run_tests():
    text = _text()
    verify = _first(text, "ctl run-execution-verify-evidence .dynos/task-{id}")
    finish = _first(text, "ctl run-execution-finish .dynos/task-{id}")
    run_tests = _first(text, "ctl run-tests .dynos/task-{id}")
    assert finish < verify < run_tests


def test_inline_path_finishes_execution_before_tests():
    text = _text()
    assert "proceed to Step 4." not in text
    assert "`run-tests` refuses a task still at `EXECUTION`" in text
