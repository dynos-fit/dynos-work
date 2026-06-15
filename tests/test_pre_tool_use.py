"""TDD-first regression tests for findings #36 and #37 — MultiEdit watchdog branch
and backtick verb strip in _extract_bash_destinations.

AC 6: _watchdog_targets_artifact must return True when tool_name == "MultiEdit"
and the edits list contains a matching file_path.

AC 7: _extract_bash_destinations must detect the write destination when the
verb token has leading/trailing backticks (e.g. from `cp a /dst`).

References _watchdog_targets_artifact and _extract_bash_destinations INSIDE
function bodies so that import/collection never errors if those symbols exist
but are not yet implemented correctly.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))


# ---------------------------------------------------------------------------
# AC 6: MultiEdit watchdog branch
# ---------------------------------------------------------------------------


def test_watchdog_targets_artifact_multiedit_hit() -> None:
    """AC 6: _watchdog_targets_artifact returns True for MultiEdit with matching file_path.

    The new elif tool_name == "MultiEdit": branch (inserted after the Bash
    branch) must iterate edits and return True when any edit's file_path
    resolves to the artifact_abs.
    """
    import pre_tool_use as _ptu

    fn = getattr(_ptu, "_watchdog_targets_artifact", None)
    assert fn is not None, "_watchdog_targets_artifact must exist in pre_tool_use"

    artifact = "/path/to/artifact.json"
    tool_input = {"edits": [{"file_path": artifact}]}
    result = fn("MultiEdit", tool_input, artifact)
    assert result is True, (
        f"_watchdog_targets_artifact must return True for MultiEdit with matching file_path; "
        f"got {result!r}"
    )


def test_watchdog_targets_artifact_multiedit_miss() -> None:
    """AC 6: _watchdog_targets_artifact returns False for MultiEdit with non-matching file_path."""
    import pre_tool_use as _ptu

    fn = getattr(_ptu, "_watchdog_targets_artifact", None)
    assert fn is not None, "_watchdog_targets_artifact must exist in pre_tool_use"

    artifact = "/path/to/artifact.json"
    tool_input = {"edits": [{"file_path": "/path/to/other_file.json"}]}
    result = fn("MultiEdit", tool_input, artifact)
    assert result is False, (
        f"_watchdog_targets_artifact must return False for MultiEdit with non-matching file_path; "
        f"got {result!r}"
    )


# ---------------------------------------------------------------------------
# AC 7: Backtick verb strip in _extract_bash_destinations
# ---------------------------------------------------------------------------


def test_extract_bash_backtick_verb_detected() -> None:
    """AC 7: _extract_bash_destinations detects the destination from a backtick-wrapped verb.

    The command ``echo `cp a /task/b.json``` should yield /task/b.json because
    the token `cp is stripped of its leading backtick, revealing the 'cp' verb
    which is in _WRITE_VERB_DEST, and /task/b.json is its second argument.
    """
    import pre_tool_use as _ptu

    fn = getattr(_ptu, "_extract_bash_destinations", None)
    assert fn is not None, "_extract_bash_destinations must exist in pre_tool_use"

    command = "echo `cp a /task/b.json`"
    destinations = fn(command)
    assert isinstance(destinations, list), (
        f"_extract_bash_destinations must return a list; got {type(destinations)!r}"
    )
    assert "/task/b.json" in destinations, (
        f"_extract_bash_destinations must detect /task/b.json from backtick-wrapped cp; "
        f"got {destinations!r}"
    )
