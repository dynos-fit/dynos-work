"""Tests for `hash_file` per-process memo (PERF-001).

`hash_file` caches digests keyed by (absolute path, mtime_ns, size) so
repeated calls against the same unchanged file share one disk read.
Mutation of the file invalidates the entry automatically.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_receipts import _HASH_CACHE, hash_file  # noqa: E402


def test_second_call_on_same_file_hits_cache(tmp_path: Path) -> None:
    """Second hash of the same unchanged file must return the same digest
    AND populate the module-level cache."""
    f = tmp_path / "a.txt"
    f.write_bytes(b"hello")
    _HASH_CACHE.clear()
    d1 = hash_file(f)
    cache_size_after_first = len(_HASH_CACHE)
    d2 = hash_file(f)
    assert d1 == d2
    # Second call must not grow the cache (already cached).
    assert len(_HASH_CACHE) == cache_size_after_first
    assert cache_size_after_first == 1


def test_mutation_invalidates_cache(tmp_path: Path) -> None:
    """Re-writing the file changes mtime_ns / size; the cached digest for
    the old stat tuple stays in the dict but a new-stat read computes a
    fresh digest. Stale-digest-returned-for-mutated-file is ruled out."""
    f = tmp_path / "m.txt"
    f.write_bytes(b"v1")
    _HASH_CACHE.clear()
    d1 = hash_file(f)
    # Ensure mtime actually bumps (some FS have low resolution).
    time.sleep(0.01)
    os.utime(f, None)
    f.write_bytes(b"v2 content bigger")  # also different size
    d2 = hash_file(f)
    assert d1 != d2, "digest must differ after content mutation"


def test_cache_bounded(tmp_path: Path) -> None:
    """Cache cap is bounded (FIFO eviction). Stuffing more than the cap
    keeps the dict at/below the limit."""
    from lib_receipts import _HASH_CACHE_MAX
    _HASH_CACHE.clear()
    for i in range(_HASH_CACHE_MAX + 20):
        fp = tmp_path / f"f{i}.txt"
        fp.write_bytes(f"body-{i}".encode())
        hash_file(fp)
    assert len(_HASH_CACHE) <= _HASH_CACHE_MAX
