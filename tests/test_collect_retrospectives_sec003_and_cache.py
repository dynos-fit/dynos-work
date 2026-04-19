"""Tests for SEC-003 (flush-event hash cross-check) and PERF-003
(per-process mtime-based memo) on `collect_retrospectives`.

SEC-003: a persistent retrospective at
`~/.dynos/projects/{slug}/retrospectives/{task_id}.json` whose content
sha256 does not match the sha256 recorded in `retrospective_flushed`
events in `events.jsonl` is SKIPPED by `collect_retrospectives`.
Rationale: the persistent dir is outside the worktree trust boundary;
post-flush tampering is invisible without the cross-check. Events are
append-only and co-located with the flush, so the sha256 recorded there
is the anchor.

PERF-003: `collect_retrospectives` memoizes by a stat fingerprint of
(worktree .dynos/, persistent retros dir, events.jsonl). Any mutation
of any input path invalidates the cache on the next call. Same-state
calls share one read pass.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_core import (  # noqa: E402
    _COLLECT_RETRO_CACHE,
    _flushed_sha_by_task_id,
    _persistent_project_dir,
    _retros_stat_fingerprint,
    collect_retrospectives,
)
from lib_receipts import hash_file  # noqa: E402


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / ".dynos").mkdir(parents=True)
    return root


def _emit_flush_event(root: Path, task_id: str, sha: str) -> None:
    line = json.dumps({
        "ts": "2026-04-19T00:00:00Z",
        "event": "retrospective_flushed",
        "task_id": task_id,
        "source": "x",
        "destination": "y",
        "sha256": sha,
    })
    ev = root / ".dynos" / "events.jsonl"
    with ev.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


# ---------------------------------------------------------------------------
# SEC-003: flush-event hash cross-check
# ---------------------------------------------------------------------------


def test_persistent_retro_with_matching_flush_event_accepted(tmp_path: Path) -> None:
    """Write a persistent retro AND a matching `retrospective_flushed`
    event with its sha256. collect_retrospectives returns the retro."""
    root = _project(tmp_path)
    pd = _persistent_project_dir(root) / "retrospectives"
    pd.mkdir(parents=True)
    retro = {"task_id": "task-OK", "quality_score": 0.9}
    path = pd / "task-OK.json"
    path.write_text(json.dumps(retro))
    _emit_flush_event(root, "task-OK", hash_file(path))

    _COLLECT_RETRO_CACHE.clear()
    result = collect_retrospectives(root)
    assert any(r.get("task_id") == "task-OK" for r in result)


def test_persistent_retro_with_mismatched_flush_event_skipped(tmp_path: Path) -> None:
    """Write a persistent retro AND a `retrospective_flushed` event with
    a DIFFERENT sha256 (simulates someone tampering with the persistent
    file after flush). collect_retrospectives must skip the tampered
    retro — the worktree copy (if present) stands alone."""
    root = _project(tmp_path)
    pd = _persistent_project_dir(root) / "retrospectives"
    pd.mkdir(parents=True)
    retro = {"task_id": "task-BAD", "quality_score": 0.9}
    path = pd / "task-BAD.json"
    path.write_text(json.dumps(retro))
    # Wrong sha — tamper detected.
    _emit_flush_event(root, "task-BAD", "0" * 64)

    _COLLECT_RETRO_CACHE.clear()
    result = collect_retrospectives(root)
    assert not any(r.get("task_id") == "task-BAD" for r in result), (
        f"tampered persistent retro must be skipped; got {result}"
    )


def test_persistent_retro_without_flush_event_trusted(tmp_path: Path) -> None:
    """Cold-start / pre-SEC-003 retros have no matching flush event in
    events.jsonl. Those are TRUSTED (not skipped) so existing
    retrospectives pre-dating this code continue to feed the EMA."""
    root = _project(tmp_path)
    pd = _persistent_project_dir(root) / "retrospectives"
    pd.mkdir(parents=True)
    retro = {"task_id": "task-COLD", "quality_score": 0.9}
    (pd / "task-COLD.json").write_text(json.dumps(retro))
    # events.jsonl does not exist — no flush record for task-COLD.

    _COLLECT_RETRO_CACHE.clear()
    result = collect_retrospectives(root)
    assert any(r.get("task_id") == "task-COLD" for r in result)


def test_tampered_persistent_falls_back_to_worktree(tmp_path: Path) -> None:
    """If the persistent copy is tampered AND a worktree copy for the
    same task_id exists, the worktree copy should be returned (not
    skipped along with the persistent)."""
    root = _project(tmp_path)
    pd = _persistent_project_dir(root) / "retrospectives"
    pd.mkdir(parents=True)
    # Worktree copy — legitimate.
    wt_task_dir = root / ".dynos" / "task-MIX"
    wt_task_dir.mkdir(parents=True)
    wt_retro = {"task_id": "task-MIX", "quality_score": 0.88, "source_marker": "worktree"}
    (wt_task_dir / "task-retrospective.json").write_text(json.dumps(wt_retro))
    # Persistent copy — tampered (claims wrong hash).
    ptd = pd / "task-MIX.json"
    ptd.write_text(json.dumps({"task_id": "task-MIX", "quality_score": 0.1, "source_marker": "persistent_tampered"}))
    _emit_flush_event(root, "task-MIX", "0" * 64)

    _COLLECT_RETRO_CACHE.clear()
    result = collect_retrospectives(root)
    mix = [r for r in result if r.get("task_id") == "task-MIX"]
    assert len(mix) == 1, f"expected exactly one task-MIX row, got {mix}"
    assert mix[0].get("source_marker") == "worktree", (
        f"tampered persistent should be skipped; worktree should stand: {mix[0]}"
    )


# ---------------------------------------------------------------------------
# PERF-003: per-process memo
# ---------------------------------------------------------------------------


def test_second_call_returns_cached_result(tmp_path: Path) -> None:
    """Two calls with no file changes return lists with identical
    contents (content equality) AND the fingerprint is computed from the
    filesystem — so a no-op second call is cheap."""
    root = _project(tmp_path)
    pd = _persistent_project_dir(root) / "retrospectives"
    pd.mkdir(parents=True)
    retro = {"task_id": "task-CACHE", "quality_score": 0.9}
    path = pd / "task-CACHE.json"
    path.write_text(json.dumps(retro))
    _emit_flush_event(root, "task-CACHE", hash_file(path))

    _COLLECT_RETRO_CACHE.clear()
    r1 = collect_retrospectives(root)
    # Without touching anything, second call should use cache.
    r2 = collect_retrospectives(root)
    assert r1 == r2
    assert root in _COLLECT_RETRO_CACHE


def test_cache_invalidates_on_new_persistent_retro(tmp_path: Path) -> None:
    """Adding a new persistent retro changes the stat fingerprint and
    invalidates the cache. The second call sees the new row."""
    root = _project(tmp_path)
    pd = _persistent_project_dir(root) / "retrospectives"
    pd.mkdir(parents=True)
    p1 = pd / "task-A.json"
    p1.write_text(json.dumps({"task_id": "task-A", "quality_score": 0.9}))
    _emit_flush_event(root, "task-A", hash_file(p1))

    _COLLECT_RETRO_CACHE.clear()
    r1 = collect_retrospectives(root)
    assert {r.get("task_id") for r in r1} == {"task-A"}

    # Add a new retro + matching event.
    p2 = pd / "task-B.json"
    p2.write_text(json.dumps({"task_id": "task-B", "quality_score": 0.9}))
    _emit_flush_event(root, "task-B", hash_file(p2))

    r2 = collect_retrospectives(root)
    assert {r.get("task_id") for r in r2} == {"task-A", "task-B"}


def test_cache_invalidates_on_worktree_retro_mutation(tmp_path: Path) -> None:
    """Mutating a worktree retro file must invalidate the cache even if
    no new files are added."""
    root = _project(tmp_path)
    wt = root / ".dynos" / "task-MUT"
    wt.mkdir(parents=True)
    retro_path = wt / "task-retrospective.json"
    retro_path.write_text(json.dumps({"task_id": "task-MUT", "quality_score": 0.5}))

    _COLLECT_RETRO_CACHE.clear()
    r1 = collect_retrospectives(root)
    assert r1 and r1[0].get("quality_score") == 0.5

    # Mutate in place. _retros_stat_fingerprint picks up mtime/size.
    import time
    time.sleep(0.01)
    retro_path.write_text(json.dumps({"task_id": "task-MUT", "quality_score": 0.9}))

    r2 = collect_retrospectives(root)
    assert r2 and r2[0].get("quality_score") == 0.9, (
        f"cache did not invalidate on mutation: {r2}"
    )


def test_fingerprint_tuple_structure(tmp_path: Path) -> None:
    """Regression: the fingerprint must capture worktree, persistent,
    and events — three distinct inputs. An attacker-edited events.jsonl
    must change the fingerprint."""
    root = _project(tmp_path)
    fp1 = _retros_stat_fingerprint(root)

    _emit_flush_event(root, "task-X", "a" * 64)
    fp2 = _retros_stat_fingerprint(root)

    assert fp1 != fp2, "events.jsonl change must alter the fingerprint"


def test_flushed_sha_by_task_id_last_event_wins(tmp_path: Path) -> None:
    """If a task is flushed multiple times (re-play), the last recorded
    sha256 is authoritative (matches the current persistent file)."""
    root = _project(tmp_path)
    _emit_flush_event(root, "task-R", "a" * 64)
    _emit_flush_event(root, "task-R", "b" * 64)
    result = _flushed_sha_by_task_id(root)
    assert result.get("task-R") == "b" * 64
