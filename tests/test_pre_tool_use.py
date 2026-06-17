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
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

import pre_tool_use as _ptu  # noqa: E402


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


def _write_manifest(task_dir: Path, stage: str) -> None:
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "manifest.json").write_text(json.dumps({"task_id": task_dir.name, "stage": stage}))


def test_find_task_dir_from_ancestors_pointer_null_returns_none(tmp_path: Path) -> None:
    project = tmp_path / "project"
    dynos = project / ".dynos"
    dynos.mkdir(parents=True)
    (dynos / "active-task.json").write_text(json.dumps({"task_id": None}))

    assert _ptu._find_task_dir_from_ancestors(project) is None


def test_find_task_dir_from_ancestors_pointer_calibrated_returns_none(tmp_path: Path) -> None:
    project = tmp_path / "project"
    dynos = project / ".dynos"
    task_dir = dynos / "task-20260617-999"
    _write_manifest(task_dir, "CALIBRATED")
    (dynos / "active-task.json").write_text(
        json.dumps({"task_id": task_dir.name, "task_dir": str(task_dir)})
    )

    assert _ptu._find_task_dir_from_ancestors(project) is None


def test_find_task_dir_from_ancestors_fallback_skips_calibrated(tmp_path: Path) -> None:
    project = tmp_path / "project"
    newest = project / ".dynos" / "task-99999999-999"
    active = project / ".dynos" / "task-20260617-001"
    _write_manifest(newest, "CALIBRATED")
    _write_manifest(active, "EXECUTION")

    assert _ptu._find_task_dir_from_ancestors(project / "src") == active


def test_find_task_dir_from_ancestors_returns_none_without_dynos(tmp_path: Path) -> None:
    assert _ptu._find_task_dir_from_ancestors(tmp_path) is None
