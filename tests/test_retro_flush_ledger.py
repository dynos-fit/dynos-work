"""Persistent retrospective flush ledger.

A task run in a git worktree flushed its retrospective to the shared
persistent dir, but the ``retrospective_flushed`` event went to the
worktree's ``.dynos/events.jsonl`` and was deleted with the worktree. Every
persistent retro then had no flush record, was tagged
``persistent-unverified`` and dropped, and calibration saw 0 retrospectives.

The flush now also appends to a ledger in the persistent project dir, the
SEC-003 verifier reads it, and ``backfill_retro_flush_ledger`` records
entries for retros flushed before the ledger existed.
"""
from __future__ import annotations

import argparse
import io
import json
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

import lib_core  # noqa: E402
from lib_core import (  # noqa: E402
    _flush_retrospective_on_done,
    _persistent_project_dir,
    _retro_flush_ledger_path,
    backfill_retro_flush_ledger,
    collect_retrospectives,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    home = tmp_path / "dynos-home"
    home.mkdir()
    monkeypatch.setenv("DYNOS_HOME", str(home))
    lib_core._COLLECT_RETRO_CACHE.clear()
    yield
    lib_core._COLLECT_RETRO_CACHE.clear()


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, check=True, capture_output=True,
    )


def _make_repo_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q")
    (main / "README").write_text("x")
    _git(main, "add", "README")
    _git(main, "commit", "-qm", "init")
    (main / ".dynos").mkdir()
    wt = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", str(wt))
    return main, wt


def _make_task(root: Path, tid: str, quality: float = 0.7) -> Path:
    td = root / ".dynos" / tid
    td.mkdir(parents=True)
    (td / "task-retrospective.json").write_text(
        json.dumps({"task_id": tid, "quality_score": quality}, indent=2)
    )
    return td


def _write_persistent_retro(root: Path, tid: str) -> Path:
    pdir = _persistent_project_dir(root) / "retrospectives"
    pdir.mkdir(parents=True, exist_ok=True)
    path = pdir / f"{tid}.json"
    path.write_text(json.dumps({"task_id": tid, "quality_score": 0.5}, indent=2))
    return path


def _ledger_lines(root: Path) -> list[dict]:
    path = _retro_flush_ledger_path(root)
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def test_flush_from_removed_worktree_stays_verified(tmp_path):
    main, wt = _make_repo_with_worktree(tmp_path)
    tid = "task-20261031-WT"
    td = _make_task(wt, tid)
    _flush_retrospective_on_done(task_dir=td, manifest={"task_id": tid})

    # The worktree's events.jsonl holds the flush event; main's does not.
    assert not (main / ".dynos" / "events.jsonl").exists() or "retrospective_flushed" not in (
        main / ".dynos" / "events.jsonl"
    ).read_text()
    _git(main, "worktree", "remove", "--force", str(wt))
    assert not wt.exists()

    retros = collect_retrospectives(main)
    assert [r["task_id"] for r in retros] == [tid]
    assert retros[0]["_source"] == "persistent"


def test_flush_appends_ledger_record(tmp_path):
    root = tmp_path / "project"
    td = _make_task(root, "task-20261031-LG")
    _flush_retrospective_on_done(task_dir=td, manifest={"task_id": td.name})
    lines = _ledger_lines(root)
    assert len(lines) == 1
    assert lines[0]["event"] == "retrospective_flushed"
    assert lines[0]["task_id"] == td.name
    assert lines[0]["origin"] == "flush"
    from lib_receipts import hash_file
    assert lines[0]["sha256"] == hash_file(
        _persistent_project_dir(root) / "retrospectives" / f"{td.name}.json"
    )


def test_ledger_mismatch_still_rejects_tampered_retro(tmp_path):
    root = tmp_path / "project"
    (root / ".dynos").mkdir(parents=True)
    td = _make_task(root, "task-20261031-TP")
    _flush_retrospective_on_done(task_dir=td, manifest={"task_id": td.name})
    shutil.rmtree(td)
    (root / ".dynos" / "events.jsonl").unlink()  # only the ledger remains
    persistent = _persistent_project_dir(root) / "retrospectives" / f"{td.name}.json"
    persistent.write_text(json.dumps({"task_id": td.name, "quality_score": 1.0}))
    assert collect_retrospectives(root, include_unverified=True) == []


def test_backfill_verifies_orphaned_retros(tmp_path):
    root = tmp_path / "project"
    (root / ".dynos").mkdir(parents=True)
    tids = [f"task-20261031-B{i}" for i in range(3)]
    for tid in tids:
        _write_persistent_retro(root, tid)
    assert collect_retrospectives(root) == []

    dry = backfill_retro_flush_ledger(root, dry_run=True)
    assert dry["backfilled"] == tids
    assert _ledger_lines(root) == []

    res = backfill_retro_flush_ledger(root)
    assert res["backfilled"] == tids
    assert {l["origin"] for l in _ledger_lines(root)} == {"backfill"}
    retros = collect_retrospectives(root)
    assert sorted(r["task_id"] for r in retros) == tids
    assert {r["_source"] for r in retros} == {"persistent"}

    again = backfill_retro_flush_ledger(root)
    assert again["backfilled"] == []
    assert again["already_verified"] == tids


def test_backfill_leaves_hash_mismatch_alone(tmp_path):
    root = tmp_path / "project"
    (root / ".dynos").mkdir(parents=True)
    path = _write_persistent_retro(root, "task-20261031-MM")
    backfill_retro_flush_ledger(root)
    path.write_text(json.dumps({"task_id": "task-20261031-MM", "quality_score": 1.0}))
    before = _ledger_lines(root)

    res = backfill_retro_flush_ledger(root)
    assert res["hash_mismatch"] == ["task-20261031-MM"]
    assert res["backfilled"] == []
    assert _ledger_lines(root) == before


def test_backfill_skips_invalid_entries(tmp_path):
    root = tmp_path / "project"
    (root / ".dynos").mkdir(parents=True)
    pdir = _persistent_project_dir(root) / "retrospectives"
    pdir.mkdir(parents=True)
    (pdir / "task-20261031-X.json").write_text(json.dumps({"task_id": "task-20261031-Y"}))
    (pdir / "task-20261031-Z.json").write_text("not json")
    res = backfill_retro_flush_ledger(root)
    assert res["backfilled"] == []
    assert len(res["invalid"]) == 2
    assert _ledger_lines(root) == []


def test_cmd_auto_reports_unverified_count(tmp_path):
    from agent_generator import cmd_auto

    root = tmp_path / "project"
    (root / ".dynos").mkdir(parents=True)
    for i in range(2):
        _write_persistent_retro(root, f"task-20261031-U{i}")
    buf = io.StringIO()
    with redirect_stdout(buf):
        cmd_auto(argparse.Namespace(root=str(root), min_tasks=5))
    out = json.loads(buf.getvalue())
    assert out["unverified_retrospectives"] == 2
    assert out["skipped_reasons"][0].startswith("insufficient retrospectives: 0 < 5 (2 persistent")


# --- Slot discovery from retrospectives (calibration on main) -------------

def _write_verified_retro(root: Path, tid: str, roles: list[str], task_type: str = "feature") -> None:
    pdir = _persistent_project_dir(root) / "retrospectives"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / f"{tid}.json").write_text(json.dumps({
        "task_id": tid,
        "task_type": task_type,
        "quality_score": 0.6,
        "cost_score": 0.6,
        "efficiency_score": 0.6,
        "model_used_by_agent": {r: "claude-sonnet-5-5" for r in roles},
    }, indent=2))


def _run_auto(root: Path, min_tasks: int = 3) -> dict:
    from agent_generator import cmd_auto

    buf = io.StringIO()
    with redirect_stdout(buf):
        cmd_auto(argparse.Namespace(root=str(root), min_tasks=min_tasks))
    return json.loads(buf.getvalue())


def test_cmd_auto_discovers_slots_from_persistent_retros(tmp_path):
    """On main there are no task dirs or execution graphs; slots come from
    the retrospectives' model_used_by_agent."""
    root = tmp_path / "project"
    (root / ".dynos").mkdir(parents=True)
    for i in range(3):
        _write_verified_retro(root, f"task-20261031-S{i}", ["backend-executor", "security-auditor"])
    _write_verified_retro(root, "task-20261031-S9", ["ui-executor"])
    backfill_retro_flush_ledger(root)

    out = _run_auto(root)
    generated = {(g["role"], g["task_type"]) for g in out["generated"]}
    assert generated == {("backend-executor", "feature")}  # ui ran once; auditors are not slots


def test_cmd_auto_counts_tasks_not_segments(tmp_path):
    """One task with three backend segments is one sample, not three."""
    root = tmp_path / "project"
    td = root / ".dynos" / "task-20261031-G1"
    td.mkdir(parents=True)
    (td / "task-retrospective.json").write_text(json.dumps({
        "task_id": td.name, "task_type": "feature",
        "quality_score": 0.6, "cost_score": 0.6, "efficiency_score": 0.6,
    }))
    (td / "execution-graph.json").write_text(json.dumps({
        "segments": [{"id": f"seg-{i}", "executor": "backend-executor"} for i in range(3)],
    }))
    for i in range(2):
        _write_verified_retro(root, f"task-20261031-H{i}", ["ui-executor"])
    backfill_retro_flush_ledger(root)

    out = _run_auto(root)
    assert out["generated"] == []
