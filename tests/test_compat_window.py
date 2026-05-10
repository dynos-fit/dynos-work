"""Tests for hooks/lib_compat_legacy_slug.py — RED state.

These tests verify the dual-read compatibility window: when a UUID dir is
empty and a legacy slug dir exists, the legacy dir is used and a warning
event is emitted once per process. The .migrated-v2 marker short-circuits
the scan on steady state.

Imports from lib_compat_legacy_slug will fail until production code exists —
that is the expected RED state.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

# lib_compat_legacy_slug is a new module — RED import.
from lib_compat_legacy_slug import check_dual_read

# lib_project_id is also new — RED import.
from lib_project_id import resolve_project_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "t@t.com"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "T"],
        check=True, capture_output=True,
    )
    return path


def _legacy_slug_for(root: Path) -> str:
    """Mirror of the old slug derivation logic."""
    return str(root.resolve()).strip("/").replace("/", "-")


# ---------------------------------------------------------------------------
# AC 36 — dual-read returns legacy dir when UUID dir is empty
# ---------------------------------------------------------------------------


def test_dual_read_returns_legacy_dir_when_uuid_dir_empty(
    tmp_path: Path, monkeypatch
):
    """AC 36 — check_dual_read returns the legacy slug dir when the UUID dir is
    empty/absent and the legacy slug dir is non-empty.
    """
    dynos_home = tmp_path / "dynos-home"
    projects_dir = dynos_home / "projects"
    projects_dir.mkdir(parents=True)
    monkeypatch.setenv("DYNOS_HOME", str(dynos_home))

    repo = _make_git_repo(tmp_path / "repo")
    uuid = resolve_project_id(repo)

    # UUID dir does not exist yet.
    uuid_dir = projects_dir / uuid

    # Legacy slug dir is non-empty.
    legacy_slug = _legacy_slug_for(repo)
    legacy_dir = projects_dir / legacy_slug
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "prevention-rules.json").write_text("{}", encoding="utf-8")

    result = check_dual_read(repo, uuid_dir, dynos_home)
    assert result is not None, "check_dual_read should return legacy dir"
    assert result == legacy_dir, (
        f"Expected {legacy_dir}, got {result}"
    )


def test_dual_read_returns_none_when_uuid_dir_has_content(
    tmp_path: Path, monkeypatch
):
    """AC 36 — check_dual_read returns None when the UUID dir already has content
    (migration is done; use UUID dir, not legacy).
    """
    dynos_home = tmp_path / "dynos-home"
    projects_dir = dynos_home / "projects"
    projects_dir.mkdir(parents=True)
    monkeypatch.setenv("DYNOS_HOME", str(dynos_home))

    repo = _make_git_repo(tmp_path / "repo2")
    uuid = resolve_project_id(repo)

    uuid_dir = projects_dir / uuid
    uuid_dir.mkdir(parents=True)
    (uuid_dir / "prevention-rules.json").write_text("{}", encoding="utf-8")

    legacy_slug = _legacy_slug_for(repo)
    legacy_dir = projects_dir / legacy_slug
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "prevention-rules.json").write_text("{}", encoding="utf-8")

    result = check_dual_read(repo, uuid_dir, dynos_home)
    assert result is None, "check_dual_read should return None when UUID dir has content"


# ---------------------------------------------------------------------------
# AC 36 — dual-read emits warning event once per process
# ---------------------------------------------------------------------------


def test_dual_read_emits_warning_event_once_per_process(
    tmp_path: Path, monkeypatch
):
    """AC 36 — check_dual_read emits identity_legacy_slug_in_use event exactly once
    per process; subsequent calls do not re-emit.
    """
    dynos_home = tmp_path / "dynos-home"
    projects_dir = dynos_home / "projects"
    projects_dir.mkdir(parents=True)
    monkeypatch.setenv("DYNOS_HOME", str(dynos_home))

    repo = _make_git_repo(tmp_path / "repo3")
    uuid = resolve_project_id(repo)
    uuid_dir = projects_dir / uuid  # does not exist

    legacy_slug = _legacy_slug_for(repo)
    legacy_dir = projects_dir / legacy_slug
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "prevention-rules.json").write_text("{}", encoding="utf-8")

    emitted_events: list[str] = []

    def _mock_log_event(root, event_name, **kwargs):
        emitted_events.append(event_name)

    # Monkeypatch the event emission in lib_compat_legacy_slug.
    import lib_compat_legacy_slug as compat_mod
    monkeypatch.setattr(compat_mod, "log_event", _mock_log_event, raising=False)

    # Also clear the per-process dedup set so this test is hermetic.
    if hasattr(compat_mod, "_LEGACY_WARNED_FOR"):
        monkeypatch.setattr(compat_mod, "_LEGACY_WARNED_FOR", set())

    check_dual_read(repo, uuid_dir, dynos_home)
    check_dual_read(repo, uuid_dir, dynos_home)
    check_dual_read(repo, uuid_dir, dynos_home)

    legacy_events = [e for e in emitted_events if "legacy" in e]
    assert len(legacy_events) == 1, (
        f"Expected exactly 1 legacy event, got {len(legacy_events)}: {legacy_events}"
    )


# ---------------------------------------------------------------------------
# AC 37 — marker file short-circuits check on steady state
# ---------------------------------------------------------------------------


def test_migrate_marker_short_circuits_check_on_steady_state(
    tmp_path: Path, monkeypatch
):
    """AC 37 — when .migrated-v2 marker exists and no legacy dir is present,
    check_dual_read skips the legacy-dir scan entirely (returns None immediately).
    """
    dynos_home = tmp_path / "dynos-home"
    projects_dir = dynos_home / "projects"
    projects_dir.mkdir(parents=True)
    monkeypatch.setenv("DYNOS_HOME", str(dynos_home))

    # Write marker.
    (projects_dir / ".migrated-v2").touch()

    repo = _make_git_repo(tmp_path / "repo4")
    uuid = resolve_project_id(repo)
    uuid_dir = projects_dir / uuid
    uuid_dir.mkdir(parents=True)
    (uuid_dir / "prevention-rules.json").write_text("{}", encoding="utf-8")

    # No legacy dir exists. With marker present, check_dual_read must return None.
    result = check_dual_read(repo, uuid_dir, dynos_home)
    assert result is None, (
        "check_dual_read returned non-None even though marker exists and no legacy dir is present"
    )


# ---------------------------------------------------------------------------
# AC 38 — marker does not unconditionally skip when fresh legacy dir appears
# ---------------------------------------------------------------------------


def test_marker_does_not_unconditionally_skip_when_legacy_dir_appears(
    tmp_path: Path, monkeypatch
):
    """AC 38 — when marker exists but a fresh legacy dir appears (post-marker),
    check_dual_read emits identity_legacy_slug_in_use_post_marker and continues
    with dual-read rather than returning None.
    """
    dynos_home = tmp_path / "dynos-home"
    projects_dir = dynos_home / "projects"
    projects_dir.mkdir(parents=True)
    monkeypatch.setenv("DYNOS_HOME", str(dynos_home))

    # Write marker.
    (projects_dir / ".migrated-v2").touch()

    repo = _make_git_repo(tmp_path / "repo5")
    uuid = resolve_project_id(repo)
    uuid_dir = projects_dir / uuid  # UUID dir absent (empty).

    # Fresh legacy dir created post-marker.
    legacy_slug = _legacy_slug_for(repo)
    legacy_dir = projects_dir / legacy_slug
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "prevention-rules.json").write_text("{}", encoding="utf-8")

    post_marker_events: list[str] = []

    def _mock_log_event(root, event_name, **kwargs):
        post_marker_events.append(event_name)

    import lib_compat_legacy_slug as compat_mod
    monkeypatch.setattr(compat_mod, "log_event", _mock_log_event, raising=False)
    if hasattr(compat_mod, "_LEGACY_WARNED_FOR"):
        monkeypatch.setattr(compat_mod, "_LEGACY_WARNED_FOR", set())

    result = check_dual_read(repo, uuid_dir, dynos_home)

    # Must have returned the legacy dir (dual-read active despite marker).
    assert result == legacy_dir, (
        f"Expected legacy dir {legacy_dir}, got {result}"
    )
    # Must have emitted the post-marker event.
    assert any("post_marker" in e for e in post_marker_events), (
        f"identity_legacy_slug_in_use_post_marker event not emitted; got {post_marker_events}"
    )


# ---------------------------------------------------------------------------
# AC 39 — UUID generation emits identity_uuid_generated event
# ---------------------------------------------------------------------------


def test_uuid_generation_emits_event(tmp_path: Path, monkeypatch):
    """AC 39 — first-time UUID generation emits identity_uuid_generated event
    observable via the event bus.
    """
    import lib_project_id as pid_mod

    emitted: list[str] = []

    def _mock_log_event(root, event_name, **kwargs):
        emitted.append(event_name)

    monkeypatch.setattr(pid_mod, "log_event", _mock_log_event, raising=False)

    repo = _make_git_repo(tmp_path / "evtrepo")
    pid_mod.resolve_project_id(repo)

    assert any("uuid_generated" in e for e in emitted), (
        f"identity_uuid_generated event not emitted; got {emitted}"
    )


# ---------------------------------------------------------------------------
# AC 39 — path fallback emits identity_fell_back_to_path once per process
# ---------------------------------------------------------------------------


def test_path_fallback_emits_event_once_per_process(tmp_path: Path, monkeypatch):
    """AC 39 — path fallback emits identity_fell_back_to_path exactly once per process;
    subsequent calls do not re-emit.
    """
    import lib_project_id as pid_mod

    emitted: list[str] = []

    def _mock_log_event(root, event_name, **kwargs):
        emitted.append(event_name)

    monkeypatch.setattr(pid_mod, "log_event", _mock_log_event, raising=False)
    # Clear the per-process dedup set.
    if hasattr(pid_mod, "_PATH_FALLBACK_EMITTED_FOR"):
        monkeypatch.setattr(pid_mod, "_PATH_FALLBACK_EMITTED_FOR", set())

    non_git = tmp_path / "not_a_repo"
    non_git.mkdir()
    pid_mod.resolve_project_id(non_git)
    pid_mod.resolve_project_id(non_git)
    pid_mod.resolve_project_id(non_git)

    fallback_events = [e for e in emitted if "fell_back_to_path" in e]
    assert len(fallback_events) == 1, (
        f"Expected exactly 1 fallback event, got {len(fallback_events)}: {fallback_events}"
    )
