"""TDD-first tests for AC21 + AC22 (task-20260419-009).

After AC21 lands, ``hooks/lib_core._ingest`` (called from
``collect_retrospectives``) tags persistent retros that have no
``retrospective_flushed`` event as ``_source == "persistent-unverified"``
and emits a single ``retrospective_trusted_without_flush_event`` event
(swallow log_event failures per D3 / AC21 spec language).

Tests:

  1. A persistent retro with NO flush event and a valid task_id →
     ``_source == "persistent-unverified"`` AND the event is emitted.
  2. A persistent retro with a matching flush event (matching content
     hash) → existing behavior: ``_source == "persistent"`` (no new
     emission, no downgrade).
  3. A worktree retro (``persistent=False`` path) → ``_source ==
     "worktree"``. No change to this branch.
  4. Fail-open (D3): if log_event raises during the emission, the
     persistent-unverified tagging must still be applied and
     collect_retrospectives must not propagate the exception.
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


def _clear_cache():
    """Some tests re-use the same root; the cache must be cleared so
    _ingest re-runs the AC21 branch deterministically."""
    import lib_core  # noqa: PLC0415
    lib_core._COLLECT_RETRO_CACHE.clear()


@pytest.fixture(autouse=True)
def _clear_retro_cache():
    _clear_cache()
    yield
    _clear_cache()


def _setup_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "dynos-home"
    home.mkdir()
    monkeypatch.setenv("DYNOS_HOME", str(home))

    root = tmp_path / "project"
    (root / ".dynos").mkdir(parents=True)
    return root


def _write_persistent_retro(root: Path, tid: str, payload: dict | None = None) -> Path:
    pdir = _persistent_project_dir(root) / "retrospectives"
    pdir.mkdir(parents=True, exist_ok=True)
    path = pdir / f"{tid}.json"
    payload = payload or {"task_id": tid, "quality_score": 0.9}
    path.write_text(json.dumps(payload, indent=2))
    return path


def _read_events(root: Path) -> list[dict]:
    events = root / ".dynos" / "events.jsonl"
    if not events.exists():
        return []
    return [
        json.loads(line)
        for line in events.read_text().splitlines()
        if line.strip()
    ]


# --- AC22 primary: no flush event => _source=="persistent-unverified" ----

def test_persistent_retro_without_flush_event_tagged_unverified(
    tmp_path, monkeypatch
):
    root = _setup_project(tmp_path, monkeypatch)
    tid = "task-20260419-UVF"
    _write_persistent_retro(root, tid)
    # Intentionally no retrospective_flushed event in .dynos/events.jsonl.

    retros = collect_retrospectives(root, include_unverified=True)

    matching = [r for r in retros if r.get("task_id") == tid]
    assert matching, f"retro for {tid} not collected; got {retros!r}"
    assert matching[0].get("_source") == "persistent-unverified", (
        f"_source must be 'persistent-unverified' when no flush event exists; "
        f"got {matching[0].get('_source')!r}"
    )


def test_persistent_unverified_emits_named_event(tmp_path, monkeypatch):
    """AC21: emit retrospective_trusted_without_flush_event with
    task=tid and path=<str of retro path>."""
    root = _setup_project(tmp_path, monkeypatch)
    tid = "task-20260419-EMIT"
    path = _write_persistent_retro(root, tid)

    collect_retrospectives(root)

    events = _read_events(root)
    matches = [
        e for e in events
        if e.get("event") == "retrospective_trusted_without_flush_event"
        and e.get("task") == tid
    ]
    assert matches, (
        f"expected exactly one retrospective_trusted_without_flush_event "
        f"for {tid}; got events: {[e.get('event') for e in events]}"
    )
    assert matches[0].get("path") == str(path), (
        f"emitted event must carry path=str(path); got {matches[0]!r}"
    )


# --- AC21 inverse: worktree retros get _source=="worktree" (unchanged) ---

def test_worktree_retro_keeps_source_worktree(tmp_path, monkeypatch):
    root = _setup_project(tmp_path, monkeypatch)
    tid = "task-20260419-WORKTREE"
    td = root / ".dynos" / tid
    td.mkdir()
    (td / "task-retrospective.json").write_text(
        json.dumps({"task_id": tid, "quality_score": 0.9})
    )

    retros = collect_retrospectives(root)
    matching = [r for r in retros if r.get("task_id") == tid]
    assert matching, f"worktree retro not collected: {retros!r}"
    assert matching[0].get("_source") == "worktree", (
        f"worktree retros must keep _source='worktree'; got "
        f"{matching[0].get('_source')!r}"
    )


# --- AC21 fail-open (D3): log_event failure does not propagate ------------

def test_fail_open_when_log_event_raises(tmp_path, monkeypatch):
    """If log_event raises during the AC21 emission path, collect_retrospectives
    must still return the retro tagged persistent-unverified without
    propagating the exception.
    """
    root = _setup_project(tmp_path, monkeypatch)
    tid = "task-20260419-FOPEN"
    _write_persistent_retro(root, tid)

    import lib_log

    def _explode(*a, **kw):
        raise Exception("forced log_event failure")

    monkeypatch.setattr(lib_log, "log_event", _explode)

    # Must not raise.
    retros = collect_retrospectives(root, include_unverified=True)
    matching = [r for r in retros if r.get("task_id") == tid]
    assert matching, "retro must still be collected when log_event explodes"
    assert matching[0].get("_source") == "persistent-unverified", (
        "tag must still be applied on fail-open path"
    )


# --- Negative control: matching flush event => _source stays "persistent" --

def test_persistent_retro_with_matching_flush_event_stays_persistent(
    tmp_path, monkeypatch,
):
    """When a retrospective_flushed event names this tid and the content
    sha256 matches, the existing SEC-003 path runs and _source should
    stay "persistent" (not downgrade to persistent-unverified).
    """
    root = _setup_project(tmp_path, monkeypatch)
    tid = "task-20260419-VF"
    retro_path = _write_persistent_retro(root, tid)

    # Compute sha and emit a flush event matching it.
    from lib_receipts import hash_file  # noqa: PLC0415
    sha = hash_file(retro_path)
    events = root / ".dynos" / "events.jsonl"
    events.write_text(
        json.dumps({
            "ts": "2026-04-19T00:00:00Z",
            "event": "retrospective_flushed",
            "task": tid,
            "task_id": tid,
            "sha256": sha,
        }) + "\n"
    )

    retros = collect_retrospectives(root)
    matching = [r for r in retros if r.get("task_id") == tid]
    assert matching, "retro must be collected"
    assert matching[0].get("_source") == "persistent", (
        f"matching flush event must keep _source='persistent'; got "
        f"{matching[0].get('_source')!r}"
    )
