"""Tests for `collect_retrospectives` reading both worktree and
persistent sources (F11, CRITERION 11).

`collect_retrospectives(root)` now unions entries from:
  - `root/.dynos/task-*/task-retrospective.json` (the worktree copy)
  - `_persistent_project_dir(root)/retrospectives/*.json` (the
    copy-on-DONE flush destination)

Deduplication is keyed by `retro["task_id"]`. When the same task
appears in both, the PERSISTENT copy wins (it is the hash-verified
final state at DONE time; the worktree copy may have been edited
post-DONE).

Missing persistent dir is treated as empty (cold-start for new
projects). Malformed JSON is skipped silently on either side.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_core import (  # noqa: E402
    _persistent_project_dir,
    collect_retrospectives,
)


def _setup_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a project tmp dir and point DYNOS_HOME at a sibling dir
    so `_persistent_project_dir(root)` resolves inside the test tree."""
    home = tmp_path / "dynos-home"
    home.mkdir()
    monkeypatch.setenv("DYNOS_HOME", str(home))
    project = tmp_path / "project"
    (project / ".dynos").mkdir(parents=True)
    return project


def _write_worktree_retro(root: Path, task_id: str, *, extra: dict | None = None) -> None:
    """Write a task-retrospective.json under `.dynos/{task_id}/`."""
    td = root / ".dynos" / task_id
    td.mkdir(parents=True, exist_ok=True)
    payload: dict = {"task_id": task_id, "quality_score": 0.5, "_origin": "worktree"}
    if extra:
        payload.update(extra)
    (td / "task-retrospective.json").write_text(json.dumps(payload))


def _write_persistent_retro(root: Path, task_id: str, *, extra: dict | None = None) -> None:
    """Write a retrospective into the persistent dir (the copy-on-DONE
    flush destination)."""
    pdir = _persistent_project_dir(root) / "retrospectives"
    pdir.mkdir(parents=True, exist_ok=True)
    payload: dict = {"task_id": task_id, "quality_score": 0.9, "_origin": "persistent"}
    if extra:
        payload.update(extra)
    (pdir / f"{task_id}.json").write_text(json.dumps(payload))


def _tids(retros: list[dict]) -> set[str]:
    return {r.get("task_id") for r in retros if isinstance(r.get("task_id"), str)}


# ---------------------------------------------------------------------------
# Union behavior
# ---------------------------------------------------------------------------


def test_merges_both_sources(tmp_path: Path,
                             monkeypatch: pytest.MonkeyPatch) -> None:
    """A worktree-only task and a persistent-only task must both appear
    in the result — the union of the two sources."""
    root = _setup_project(tmp_path, monkeypatch)
    _write_worktree_retro(root, "task-A")
    _write_persistent_retro(root, "task-B")

    retros = collect_retrospectives(root, include_unverified=True)
    assert _tids(retros) == {"task-A", "task-B"}


def test_persistent_wins_on_conflict(tmp_path: Path,
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """Same task_id present in BOTH sources with different content.
    The result must reflect the persistent-dir row, not the worktree
    row. Keyed by `_origin` which we set to "worktree" / "persistent"."""
    root = _setup_project(tmp_path, monkeypatch)
    _write_worktree_retro(root, "task-X",
                          extra={"quality_score": 0.01})
    _write_persistent_retro(root, "task-X",
                            extra={"quality_score": 0.99})

    retros = collect_retrospectives(root, include_unverified=True)
    rows = [r for r in retros if r.get("task_id") == "task-X"]
    assert len(rows) == 1, f"expected exactly one row for task-X — got {rows!r}"
    row = rows[0]
    # Persistent wins — row shows persistent's _origin + its quality_score.
    assert row.get("_origin") == "persistent", (
        f"persistent must win on conflict — got _origin={row.get('_origin')!r}"
    )
    assert row.get("quality_score") == 0.99


def test_works_when_persistent_dir_missing(tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh project with no persistent retrospectives dir returns
    only worktree rows without raising. Cold-start scenario."""
    root = _setup_project(tmp_path, monkeypatch)
    _write_worktree_retro(root, "task-solo")
    # _persistent_project_dir(root)/retrospectives is absent.
    assert not (_persistent_project_dir(root) / "retrospectives").exists()

    retros = collect_retrospectives(root, include_unverified=True)
    assert _tids(retros) == {"task-solo"}


def test_works_when_worktree_is_empty(tmp_path: Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """Only persistent retrospectives present; `.dynos/` has no
    task-*/ directories. Result returns the persistent rows."""
    root = _setup_project(tmp_path, monkeypatch)
    _write_persistent_retro(root, "task-persistent-only")
    # No worktree retros.
    retros = collect_retrospectives(root, include_unverified=True)
    assert _tids(retros) == {"task-persistent-only"}


def test_skips_malformed_json(tmp_path: Path,
                               monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed retro file (invalid JSON) in the persistent dir must
    not crash `collect_retrospectives`. Valid rows around it are still
    returned."""
    root = _setup_project(tmp_path, monkeypatch)
    pdir = _persistent_project_dir(root) / "retrospectives"
    pdir.mkdir(parents=True, exist_ok=True)
    # Good entry.
    _write_persistent_retro(root, "task-good")
    # Malformed entry.
    (pdir / "task-bad.json").write_text("{not valid json")

    retros = collect_retrospectives(root, include_unverified=True)
    # No crash. The good entry is in the result; the malformed one
    # is either skipped or kept under a synthetic key — either way it
    # must not cause an exception and must not shadow task-good.
    assert "task-good" in _tids(retros)


def test_malformed_worktree_entry_does_not_drop_persistent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive regression: a malformed worktree retro in the same
    task_id namespace must not shadow a valid persistent copy. The
    impl keys synthetic-task-id entries under a `__no_task_id__::...`
    slot so a malformed entry can never collapse a legitimate row
    sharing its task_id."""
    root = _setup_project(tmp_path, monkeypatch)
    # Malformed worktree file.
    td = root / ".dynos" / "task-gremlin"
    td.mkdir(parents=True)
    (td / "task-retrospective.json").write_text("{broken")
    # Valid persistent row for the same task_id — MUST survive.
    _write_persistent_retro(root, "task-gremlin", extra={"quality_score": 0.88})

    retros = collect_retrospectives(root, include_unverified=True)
    rows = [r for r in retros if r.get("task_id") == "task-gremlin"]
    assert len(rows) == 1, f"lost persistent row for task-gremlin: {retros!r}"
    assert rows[0]["quality_score"] == 0.88
