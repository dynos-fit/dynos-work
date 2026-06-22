"""Regression coverage for ``lib_core.allocate_task_id``.

Background: task ids used to be ``task-{YYYYMMDD}-{seq:03d}`` where ``seq`` was
derived by globbing the *local* ``.dynos/``. Each worktree has its own
``.dynos/``, so concurrent worktrees that branched from origin/main at once all
globbed an empty set, all landed on ``seq=1``, and all produced
``task-20260622-001``. The persistent project store
(``~/.dynos/projects/{uuid}/``) is shared across worktrees and keyed by
task_id, so last-writer-wins clobbered every retrospective but one.

The fix appends 32 bits of CSPRNG entropy to the id, making it
collision-resistant regardless of which ``.dynos`` root allocated it, while
keeping a human-readable (best-effort, local) sequence prefix. These tests pin
the format, the local monotonic seq, and — the actual bug — uniqueness across
both same-root and separate-root (worktree) concurrent allocation.
"""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

from lib_core import allocate_task_id, is_safe_task_id  # noqa: E402

_FIXED_NOW = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)


def test_format_and_safe_slug(tmp_path: Path) -> None:
    task_id, task_dir = allocate_task_id(tmp_path / ".dynos", now=_FIXED_NOW)
    assert task_id.startswith("task-20260622-001-")
    # date(8) + seq(3) + entropy(8 hex)
    _, date, seq, ent = task_id.split("-")
    assert date == "20260622"
    assert seq == "001"
    assert len(ent) == 8 and all(c in "0123456789abcdef" for c in ent)
    # Must pass the path-traversal slug gate that guards every task_id->path join.
    assert is_safe_task_id(task_id) is True
    # The dir is claimed atomically as part of allocation.
    assert task_dir.is_dir() and task_dir.name == task_id


def test_local_seq_is_monotonic(tmp_path: Path) -> None:
    dynos = tmp_path / ".dynos"
    ids = [allocate_task_id(dynos, now=_FIXED_NOW)[0] for _ in range(3)]
    seqs = [i.split("-")[2] for i in ids]
    assert seqs == ["001", "002", "003"]
    assert len(set(ids)) == 3


def test_reads_legacy_unsuffixed_dirs_for_seq(tmp_path: Path) -> None:
    """A pre-fix ``task-YYYYMMDD-NNN`` dir (no entropy suffix) must still raise
    the local high-water mark so a new allocation does not reuse its seq."""
    dynos = tmp_path / ".dynos"
    (dynos / "task-20260622-007").mkdir(parents=True)
    task_id, _ = allocate_task_id(dynos, now=_FIXED_NOW)
    assert task_id.split("-")[2] == "008"


def test_retries_and_rerolls_on_entropy_collision(tmp_path: Path) -> None:
    """The glob/mkdir TOCTOU: another allocator created the candidate dir after
    we computed seq but before our mkdir. allocation must re-roll the entropy
    suffix rather than crash or clobber. We reproduce the race deterministically
    by having the first ``rand()`` plant the colliding dir as a side effect."""
    dynos = tmp_path / ".dynos"
    dynos.mkdir(parents=True)
    state = {"raced": False}

    def rand() -> str:
        if not state["raced"]:
            # Simulate a concurrent allocator winning the seq=001 slot in the
            # window between our glob (already done) and our mkdir.
            (dynos / "task-20260622-001-deadbeef").mkdir()
            state["raced"] = True
            return "deadbeef"
        return "feedface"

    task_id, task_dir = allocate_task_id(dynos, now=_FIXED_NOW, rand=rand)
    assert task_id == "task-20260622-001-feedface"
    assert task_dir.is_dir()


def test_concurrent_same_dynos_unique(tmp_path: Path) -> None:
    dynos = tmp_path / ".dynos"
    dynos.mkdir(parents=True)
    with ThreadPoolExecutor(max_workers=16) as pool:
        ids = list(pool.map(lambda _: allocate_task_id(dynos)[0], range(64)))
    assert len(set(ids)) == 64
    for tid in ids:
        assert (dynos / tid).is_dir()


def test_concurrent_separate_dynos_unique(tmp_path: Path) -> None:
    """The reported bug: concurrent allocations in *separate* .dynos roots
    (each simulating a worktree) must not collide on the same id. The seq
    prefix WILL repeat (each root globs only itself) — entropy is what keeps
    the full id unique across roots, exactly as the shared persistent store
    requires."""
    def alloc(i: int) -> str:
        return allocate_task_id(tmp_path / f"wt{i}" / ".dynos", now=_FIXED_NOW)[0]

    with ThreadPoolExecutor(max_workers=16) as pool:
        ids = list(pool.map(alloc, range(32)))

    # Every seq prefix is 001 (the old bug's collision point)...
    assert all(i.split("-")[2] == "001" for i in ids)
    # ...yet every full id is distinct because of the entropy suffix.
    assert len(set(ids)) == 32
