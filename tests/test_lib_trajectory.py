"""
Regression tests for sandbox/trajectory/lib_trajectory.py — AC 3, AC 4, AC 5
(task-20260616-002, finding #12).

make_trajectory_entry currently does a bare subscript `retrospective["task_id"]`,
so a retrospective with a missing or non-string task_id raises KeyError and
aborts the whole rebuild_trajectory_store pass. These tests encode the FIXED
behaviour: make_trajectory_entry returns None (no raise) for missing/non-string
task_id, and rebuild_trajectory_store filters those None entries out, keeping
only valid trajectories.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sandbox" / "trajectory"))
sys.path.insert(0, str(ROOT / "hooks"))


def _import_lib_trajectory():
    try:
        import lib_trajectory
        return lib_trajectory
    except ModuleNotFoundError as exc:  # pragma: no cover
        pytest.fail(f"lib_trajectory module not importable: {exc}")


# ---------------------------------------------------------------------------
# AC 3: missing task_id -> None, no raise.
# ---------------------------------------------------------------------------

def test_make_trajectory_entry_missing_task_id_returns_none():
    """make_trajectory_entry({}) returns None and does not raise. FAILS while
    the function does a bare retrospective['task_id'] subscript (KeyError)."""
    lt = _import_lib_trajectory()
    result = lt.make_trajectory_entry({})
    assert result is None, f"expected None for missing task_id, got {result!r}"


# ---------------------------------------------------------------------------
# AC 4: non-string task_id -> None, no raise.
# ---------------------------------------------------------------------------

def test_make_trajectory_entry_non_str_task_id_returns_none():
    """make_trajectory_entry with a non-string / None task_id returns None and
    does not raise."""
    lt = _import_lib_trajectory()
    assert lt.make_trajectory_entry({"task_id": 42}) is None
    assert lt.make_trajectory_entry({"task_id": None}) is None
    assert lt.make_trajectory_entry({"task_id": ""}) is None


def test_make_trajectory_entry_valid_task_id_returns_dict():
    """A valid string task_id still produces a trajectory entry (non-regression)."""
    lt = _import_lib_trajectory()
    result = lt.make_trajectory_entry({"task_id": "task-001"})
    assert isinstance(result, dict)
    assert result["trajectory_id"] == "task-001"
    assert result["source_task_id"] == "task-001"


# ---------------------------------------------------------------------------
# AC 5: rebuild_trajectory_store skips bad entries, keeps the valid one.
# ---------------------------------------------------------------------------

def test_rebuild_trajectory_store_skips_bad_task_id(monkeypatch, tmp_path):
    """rebuild over a store containing one entry with no task_id and one valid
    entry does not raise, and the resulting trajectory list contains exactly
    the one valid entry. FAILS while make_trajectory_entry raises KeyError."""
    lt = _import_lib_trajectory()

    retros = [
        {"task_id": "task-good", "task_type": "feature"},
        {"task_type": "feature"},  # missing task_id -> must be skipped, not crash
    ]

    monkeypatch.setattr(lt, "collect_retrospectives", lambda root: list(retros))
    monkeypatch.setattr(
        lt, "ensure_trajectory_store",
        lambda root: {"version": 1, "updated_at": "x", "trajectories": []},
    )
    captured = {}
    monkeypatch.setattr(lt, "trajectories_store_path", lambda root: tmp_path / "store.json")
    monkeypatch.setattr(lt, "write_json", lambda path, data: captured.update({"data": data}))

    store = lt.rebuild_trajectory_store(tmp_path)

    trajectories = store["trajectories"]
    assert len(trajectories) == 1, (
        f"expected exactly one valid trajectory, got {len(trajectories)}: {trajectories!r}"
    )
    assert trajectories[0]["trajectory_id"] == "task-good"
    # No None entry leaked into the persisted store.
    assert all(t is not None for t in trajectories)
