"""Tests for hooks/lib_project_id.py — RED state (module does not exist yet).

All imports from lib_project_id will fail until production code is written.
That is the expected RED state.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Production imports — these WILL fail until lib_project_id is implemented.
# That is the expected RED state.
# ---------------------------------------------------------------------------
from lib_project_id import (
    ProjectIdSecurityError,
    _GIT_ENV_BLOCKLIST,
    _UUID4_RE,
    _assert_safe_common_dir,
    _git_common_dir,
    _read_or_generate_id,
    _safe_git_env,
    is_path_fallback_id,
    is_uuid_id,
    resolve_project_id,
    sanitize_path_for_slug,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

UUID4_REGEX = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def _make_git_repo(path: Path) -> Path:
    """Initialise a minimal git repo at *path* and return it."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    return path


# ---------------------------------------------------------------------------
# AC 1 — module exports expected names
# ---------------------------------------------------------------------------


def test_lib_project_id_exports_expected_names():
    """AC 1 — lib_project_id must export exactly the listed public names."""
    import lib_project_id as m

    expected = [
        "resolve_project_id",
        "is_uuid_id",
        "is_path_fallback_id",
        "sanitize_path_for_slug",
        "ProjectIdSecurityError",
        "_git_common_dir",
        "_read_or_generate_id",
        "_assert_safe_common_dir",
        "_safe_git_env",
        "_GIT_ENV_BLOCKLIST",
        "_UUID4_RE",
    ]
    for name in expected:
        assert hasattr(m, name), f"lib_project_id is missing export: {name!r}"


# ---------------------------------------------------------------------------
# AC 2 — generates UUID4 in fresh repo
# ---------------------------------------------------------------------------


def test_resolve_project_id_generates_uuid_in_fresh_repo(tmp_path: Path):
    """AC 2 — resolve_project_id on a fresh git repo generates a UUID4 id file."""
    repo = _make_git_repo(tmp_path / "repo")
    result = resolve_project_id(repo)
    assert UUID4_REGEX.match(result), f"Expected UUID4, got {result!r}"
    # The id file must exist inside .git
    id_file = repo / ".git" / "dynos-project-id"
    assert id_file.exists(), "dynos-project-id file was not created"
    assert id_file.read_text(encoding="utf-8").strip() == result


# ---------------------------------------------------------------------------
# AC 3 — returns same UUID on second call
# ---------------------------------------------------------------------------


def test_resolve_project_id_returns_same_uuid_on_second_call(tmp_path: Path):
    """AC 3 — calling resolve_project_id twice returns the identical UUID."""
    repo = _make_git_repo(tmp_path / "repo")
    first = resolve_project_id(repo)
    second = resolve_project_id(repo)
    assert first == second, "UUID changed between calls"
    # Confirm the file was not overwritten (mtime unchanged or content identical)
    id_file = repo / ".git" / "dynos-project-id"
    assert id_file.read_text(encoding="utf-8").strip() == first


# ---------------------------------------------------------------------------
# AC 4 — same UUID across worktrees
# ---------------------------------------------------------------------------


def test_resolve_project_id_same_across_worktrees(tmp_path: Path):
    """AC 4 — main worktree and a linked worktree of the same clone return the same UUID."""
    main = _make_git_repo(tmp_path / "main")
    # Create an initial commit so worktree creation succeeds.
    dummy = main / "README.md"
    dummy.write_text("init")
    subprocess.run(["git", "-C", str(main), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(main), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", str(linked)],
        check=True,
        capture_output=True,
    )
    main_id = resolve_project_id(main)
    linked_id = resolve_project_id(linked)
    assert main_id == linked_id, "UUID differs between main worktree and linked worktree"


# ---------------------------------------------------------------------------
# AC 5 — concurrent first call does not double-write
# ---------------------------------------------------------------------------


def test_resolve_project_id_concurrent_first_call_does_not_double_write(
    tmp_path: Path,
):
    """AC 5 — two concurrent threads on a fresh repo produce exactly one UUID file."""
    repo = _make_git_repo(tmp_path / "repo")
    results: list[str] = []
    errors: list[Exception] = []

    def _call():
        try:
            results.append(resolve_project_id(repo))
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=_call)
    t2 = threading.Thread(target=_call)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors, f"Unexpected errors in threads: {errors}"
    assert len(results) == 2
    # Both observers must see the same UUID.
    assert results[0] == results[1], "Threads returned different UUIDs"
    # Exactly one file.
    id_file = repo / ".git" / "dynos-project-id"
    assert id_file.exists(), "dynos-project-id file missing"
    # No leftover .tmp files.
    tmp_files = list((repo / ".git").glob("dynos-project-id.*.tmp"))
    assert not tmp_files, f"Leftover tmp files: {tmp_files}"


# ---------------------------------------------------------------------------
# AC 6 — path fallback outside git
# ---------------------------------------------------------------------------


def test_resolve_project_id_falls_back_to_path_outside_git(tmp_path: Path, monkeypatch):
    """AC 6 — non-git directory falls back to a path- prefixed slug."""
    non_git = tmp_path / "non_git_dir"
    non_git.mkdir()
    result = resolve_project_id(non_git)
    assert result.startswith("path-"), f"Expected path- prefix, got {result!r}"
    assert re.match(r"^path-[a-zA-Z0-9._-]{1,200}$", result), (
        f"Path fallback slug {result!r} does not match expected pattern"
    )


# ---------------------------------------------------------------------------
# AC 7 — does not write into working tree
# ---------------------------------------------------------------------------


def test_resolve_project_id_does_not_write_into_working_tree(tmp_path: Path):
    """AC 7 — resolve_project_id must not create any file in the working tree (outside .git)."""
    repo = _make_git_repo(tmp_path / "repo")
    # Snapshot working tree files (excluding .git) before call.
    before = set(
        p for p in repo.rglob("*") if ".git" not in p.parts and p.is_file()
    )
    resolve_project_id(repo)
    after = set(
        p for p in repo.rglob("*") if ".git" not in p.parts and p.is_file()
    )
    new_files = after - before
    assert not new_files, f"resolve_project_id created unexpected files: {new_files}"


# ---------------------------------------------------------------------------
# AC 8 — existing id file is respected
# ---------------------------------------------------------------------------


def test_existing_dynos_project_id_file_is_respected_not_overwritten(tmp_path: Path):
    """AC 8 — an existing UUID4 in dynos-project-id is read verbatim, not overwritten."""
    repo = _make_git_repo(tmp_path / "repo")
    preset_uuid = "a1b2c3d4-e5f6-4a7b-8c9d-e0f1a2b3c4d5"
    id_file = repo / ".git" / "dynos-project-id"
    id_file.write_text(preset_uuid + "\n", encoding="utf-8")
    result = resolve_project_id(repo)
    assert result == preset_uuid, f"Expected preset UUID {preset_uuid!r}, got {result!r}"
    # File must not have been overwritten.
    assert id_file.read_text(encoding="utf-8").strip() == preset_uuid


# ---------------------------------------------------------------------------
# AC 9 — _safe_git_env strips GIT_EXEC_PATH and GIT_CONFIG_ prefix
# ---------------------------------------------------------------------------


def test_safe_git_env_strips_GIT_EXEC_PATH(monkeypatch):
    """AC 9 — _safe_git_env must not include GIT_EXEC_PATH."""
    monkeypatch.setenv("GIT_EXEC_PATH", "/malicious/git")
    env = _safe_git_env()
    assert "GIT_EXEC_PATH" not in env


def test_safe_git_env_strips_GIT_CONFIG_prefix(monkeypatch):
    """AC 9 — _safe_git_env strips any key matching the GIT_CONFIG_ prefix."""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    env = _safe_git_env()
    for key in list(env.keys()):
        assert not key.startswith("GIT_CONFIG_"), (
            f"GIT_CONFIG_* key {key!r} was not stripped from env"
        )


def test_safe_git_env_strips_blocklist_entries(monkeypatch):
    """AC 9 — _safe_git_env strips all keys in _GIT_ENV_BLOCKLIST."""
    for key in _GIT_ENV_BLOCKLIST:
        monkeypatch.setenv(key, "/attacker")
    env = _safe_git_env()
    for key in _GIT_ENV_BLOCKLIST:
        assert key not in env, f"Blocklisted key {key!r} was not stripped"


# ---------------------------------------------------------------------------
# AC 10 — sanitize_path_for_slug security rejections
# ---------------------------------------------------------------------------


def test_sanitize_path_for_slug_rejects_traversal():
    """AC 10 — sanitize_path_for_slug raises ProjectIdSecurityError for paths containing '..'."""
    with pytest.raises(ProjectIdSecurityError):
        sanitize_path_for_slug("/Users/hassam/../etc/passwd")


def test_sanitize_path_for_slug_rejects_control_characters():
    """AC 10 — sanitize_path_for_slug raises ProjectIdSecurityError for control chars."""
    with pytest.raises(ProjectIdSecurityError):
        sanitize_path_for_slug("/Users/hassam\x01/evil")


def test_sanitize_path_for_slug_rejects_null_byte():
    """AC 10 — sanitize_path_for_slug raises ProjectIdSecurityError for null bytes."""
    with pytest.raises(ProjectIdSecurityError):
        sanitize_path_for_slug("/Users/hassam\x00/evil")


def test_sanitize_path_for_slug_rejects_backslash():
    """AC 10 — sanitize_path_for_slug raises ProjectIdSecurityError for backslash."""
    with pytest.raises(ProjectIdSecurityError):
        sanitize_path_for_slug("/Users/hassam\\evil")


def test_sanitize_path_for_slug_rejects_non_ascii():
    """AC 10 — sanitize_path_for_slug raises ProjectIdSecurityError for non-ASCII chars."""
    with pytest.raises(ProjectIdSecurityError):
        sanitize_path_for_slug("/Users/hassam/évil")


def test_sanitize_path_for_slug_rejects_too_long():
    """AC 10 — sanitize_path_for_slug raises ProjectIdSecurityError for paths exceeding 200 chars post-sanitization."""
    long_path = "/Users/" + "a" * 201
    with pytest.raises(ProjectIdSecurityError):
        sanitize_path_for_slug(long_path)


def test_sanitize_path_for_slug_returns_valid_slug_for_well_formed_path():
    """AC 10 — sanitize_path_for_slug returns a valid slug for a well-formed ASCII absolute path."""
    result = sanitize_path_for_slug("/Users/hassam/Documents/dynos-work")
    # Result must match ^[a-zA-Z0-9._-]{1,200}$ (without path- prefix; caller adds it)
    assert re.match(r"^[a-zA-Z0-9._-]{1,200}$", result), (
        f"Slug {result!r} does not match expected pattern"
    )
    # Must not contain path separators.
    assert "/" not in result


def test_sanitize_path_for_slug_rejects_del_char():
    """AC 10 — sanitize_path_for_slug raises ProjectIdSecurityError for DEL char (0x7f)."""
    with pytest.raises(ProjectIdSecurityError):
        sanitize_path_for_slug("/Users/hassam\x7f/evil")


# ---------------------------------------------------------------------------
# Regression — is_uuid_id and is_path_fallback_id helpers
# ---------------------------------------------------------------------------


def test_is_uuid_id_true_for_valid_uuid():
    """AC 1 — is_uuid_id returns True for a valid UUID4 string."""
    assert is_uuid_id("a1b2c3d4-e5f6-4a7b-8c9d-e0f1a2b3c4d5")


def test_is_uuid_id_false_for_path_slug():
    """AC 1 — is_uuid_id returns False for a path-derived slug."""
    assert not is_uuid_id("-Users-hassam-Documents-dynos-work")


def test_is_path_fallback_id_true_for_path_slug():
    """AC 1 — is_path_fallback_id returns True for path- prefixed slug."""
    assert is_path_fallback_id("path--Users-hassam-Documents-dynos-work")


def test_is_path_fallback_id_false_for_uuid():
    """AC 1 — is_path_fallback_id returns False for a UUID4 slug."""
    assert not is_path_fallback_id("a1b2c3d4-e5f6-4a7b-8c9d-e0f1a2b3c4d5")
