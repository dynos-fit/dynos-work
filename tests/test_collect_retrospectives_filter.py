"""TDD-first tests for task-20260420-001 D6 part 1 — default filter.

Covers acceptance criterion 26:

    AC26 collect_retrospectives(root, include_unverified: bool = False):
         default False drops entries with _source == "persistent-unverified";
         include_unverified=True restores them. Cache key includes the
         include_unverified boolean (ADR-11).

TODAY these tests FAIL because collect_retrospectives has no
include_unverified parameter and the cache is keyed on root alone.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "hooks"))


def _persistent_dir_for(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point DYNOS_HOME at a tmp path so collect_retrospectives reads
    from our fixture persistent dir."""
    dynos_home = root.parent / "dynos-home"
    dynos_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DYNOS_HOME", str(dynos_home))
    slug = str(root.resolve()).strip("/").replace("/", "-")
    persistent = dynos_home / "projects" / slug / "retrospectives"
    persistent.mkdir(parents=True, exist_ok=True)
    return persistent


def _write_retro(path: Path, task_id: str, quality: float = 0.5) -> None:
    data = {"task_id": task_id, "quality_score": quality}
    path.write_text(json.dumps(data), encoding="utf-8")


def _setup_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "project"
    (root / ".dynos").mkdir(parents=True)
    persistent = _persistent_dir_for(root, monkeypatch)

    # Worktree retro (trusted; _source == "worktree")
    td = root / ".dynos" / "task-20260420-worktree"
    td.mkdir()
    _write_retro(td / "task-retrospective.json", "task-20260420-worktree", 0.7)

    # Persistent retro with NO matching retrospective_flushed event
    # → labelled "persistent-unverified" by the ingestion code.
    _write_retro(persistent / "task-legacy-001.json", "task-legacy-001", 0.3)

    # Clear cache to avoid state leaking between tests.
    from lib_core import _COLLECT_RETRO_CACHE  # noqa: PLC0415

    _COLLECT_RETRO_CACHE.clear()
    return root


# ---------------------------------------------------------------------------
# AC26 — default excludes persistent-unverified
# ---------------------------------------------------------------------------


def test_default_excludes_persistent_unverified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from lib_core import collect_retrospectives  # noqa: PLC0415

    root = _setup_fixture(tmp_path, monkeypatch)
    retros = collect_retrospectives(root)
    ids = {r.get("task_id") for r in retros}
    assert "task-20260420-worktree" in ids
    assert "task-legacy-001" not in ids, (
        "Default call must drop _source=='persistent-unverified' retros. "
        f"Got: {ids}"
    )


def test_include_flag_restores_persistent_unverified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from lib_core import collect_retrospectives  # noqa: PLC0415

    root = _setup_fixture(tmp_path, monkeypatch)
    retros = collect_retrospectives(root, include_unverified=True)
    ids = {r.get("task_id") for r in retros}
    assert "task-20260420-worktree" in ids
    assert "task-legacy-001" in ids


# ---------------------------------------------------------------------------
# AC26 / ADR-11 — cache key includes include_unverified
# ---------------------------------------------------------------------------


def test_cache_key_includes_include_unverified_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from lib_core import collect_retrospectives  # noqa: PLC0415

    root = _setup_fixture(tmp_path, monkeypatch)

    first = collect_retrospectives(root)  # default False
    second = collect_retrospectives(root, include_unverified=True)
    third = collect_retrospectives(root)  # should again be filtered

    first_ids = {r.get("task_id") for r in first}
    second_ids = {r.get("task_id") for r in second}
    third_ids = {r.get("task_id") for r in third}

    assert first_ids != second_ids, (
        "Default and include_unverified=True calls must return different lists "
        "— same cache entry for both flags is an ADR-11 regression"
    )
    assert first_ids == third_ids, (
        "Repeat default call must return the same filtered list — cache flipped"
    )


def test_include_unverified_is_keyword_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Spec: keyword-safe param; existing positional callers unchanged.
    Attempting to pass positionally must TypeError."""
    from lib_core import collect_retrospectives  # noqa: PLC0415

    root = _setup_fixture(tmp_path, monkeypatch)
    with pytest.raises(TypeError):
        collect_retrospectives(root, True)  # type: ignore[arg-type]
