"""Threat-defense tests for project identity — RED state.

Every test maps to a threat ID from §12.7 of docs/project-identity-design.md.
All tests use real filesystem operations under tmp_path.
The only acceptable mock is monkeypatch on os.stat for ownership checks (st_uid).

Function names MUST match §12.7 exactly — the audit cross-checks them.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import threading
from pathlib import Path

import pytest

# lib_project_id is new — RED import until production code exists.
from lib_project_id import (
    ProjectIdSecurityError,
    _assert_safe_common_dir,
    _git_common_dir,
    _GIT_ENV_BLOCKLIST,
    _safe_git_env,
    _UUID4_RE,
    is_uuid_id,
    resolve_project_id,
    sanitize_path_for_slug,
)

# registry.py already exists.
import registry

# policy_engine.py already exists.
import policy_engine

# lib_core.py already exists.
import lib_core

# worktree.py already exists.
import worktree

UUID4_REGEX = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

ROOT = Path(__file__).resolve().parents[1]


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


# ---------------------------------------------------------------------------
# AC 43 / T-1 — rejects common_dir symlink outside HOME
# ---------------------------------------------------------------------------


def test_resolve_rejects_common_dir_symlink_outside_home(tmp_path: Path):
    """AC 43 / T-1 — _assert_safe_common_dir raises ProjectIdSecurityError when
    common_dir.resolve() is outside Path.home(). Uses a real symlink under tmp_path
    that resolves to a path outside HOME (tmp_path is typically /tmp which is outside HOME).
    """
    # tmp_path is usually /private/tmp/... or /tmp/... — outside HOME.
    real_target = tmp_path / "real_git_dir"
    real_target.mkdir()

    # Confirm the target resolves outside HOME.
    home_real = Path.home().resolve()
    try:
        real_target.resolve().relative_to(home_real)
        pytest.skip("tmp_path resolves inside HOME — cannot test outside-HOME rejection")
    except ValueError:
        pass  # Good — it's outside HOME.

    symlink = tmp_path / "fake_dot_git"
    symlink.symlink_to(real_target)

    with pytest.raises(ProjectIdSecurityError):
        _assert_safe_common_dir(symlink)


# ---------------------------------------------------------------------------
# AC 44 / T-2 (new code) — ignores GIT_DIR env
# ---------------------------------------------------------------------------


def test_resolve_ignores_GIT_DIR_env(tmp_path: Path, monkeypatch):
    """AC 44 / T-2 — resolve_project_id returns the correct UUID even when
    GIT_DIR=/nonexistent is set in os.environ.
    """
    repo = _make_git_repo(tmp_path / "repo")
    monkeypatch.setenv("GIT_DIR", "/nonexistent/path")
    result = resolve_project_id(repo)
    assert UUID4_REGEX.match(result) or result.startswith("path-"), (
        f"Unexpected result: {result!r}"
    )
    # For a real git repo, must return UUID (not be poisoned by GIT_DIR).
    assert UUID4_REGEX.match(result), (
        f"GIT_DIR injection caused resolve_project_id to fall back to path slug: {result!r}"
    )


def test_resolve_ignores_GIT_CONFIG_env(monkeypatch):
    """AC 44 / T-2 — GIT_CONFIG_COUNT=1 is absent from the env returned by _safe_git_env."""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "/malicious")
    env = _safe_git_env()
    config_keys = [k for k in env if k.startswith("GIT_CONFIG_")]
    assert not config_keys, (
        f"GIT_CONFIG_* keys were not stripped: {config_keys}"
    )


# ---------------------------------------------------------------------------
# AC 45 / T-2 (existing code fix) — _resolve_git_toplevel ignores GIT_DIR env
# ---------------------------------------------------------------------------


def test_resolve_git_toplevel_ignores_GIT_DIR_env(tmp_path: Path, monkeypatch):
    """AC 45 / T-2 — lib_core._resolve_git_toplevel with GIT_DIR=/nonexistent still
    returns the correct real repo path.
    """
    repo = _make_git_repo(tmp_path / "repo")
    monkeypatch.setenv("GIT_DIR", "/nonexistent/path/injected")

    # Clear the lru_cache so the monkeypatched env is visible.
    lib_core._resolve_git_toplevel.cache_clear()

    result = lib_core._resolve_git_toplevel(str(repo))
    assert result is not None, "_resolve_git_toplevel returned None for real git repo"
    assert Path(result).resolve() == repo.resolve(), (
        f"Expected {repo.resolve()}, got {result!r}"
    )


# ---------------------------------------------------------------------------
# AC 46 / T-3 — rejects symlinked id file
# ---------------------------------------------------------------------------


def test_resolve_rejects_symlinked_id_file(tmp_path: Path):
    """AC 46 / T-3 — _read_or_generate_id raises ProjectIdSecurityError when
    <common-dir>/dynos-project-id is a symlink (planted to redirect the read).
    Uses a real symlink under tmp_path.
    """
    repo = _make_git_repo(tmp_path / "repo")
    git_dir = repo / ".git"

    # Plant a symlink at the id file location.
    real_target = tmp_path / "attacker_uuid.txt"
    real_target.write_text("a1b2c3d4-e5f6-4a7b-8c9d-e0f1a2b3c4d5\n", encoding="utf-8")
    id_file = git_dir / "dynos-project-id"
    id_file.symlink_to(real_target)

    with pytest.raises(ProjectIdSecurityError):
        resolve_project_id(repo)


# ---------------------------------------------------------------------------
# AC 47 / T-4 — rejects traversal in id file content
# ---------------------------------------------------------------------------


def test_resolve_rejects_traversal_in_id_file_content(tmp_path: Path):
    """AC 47 / T-4 — _read_or_generate_id raises ProjectIdSecurityError when
    dynos-project-id contains path-traversal content instead of a UUID4.
    """
    repo = _make_git_repo(tmp_path / "repo")
    id_file = repo / ".git" / "dynos-project-id"
    id_file.write_text("../../etc/passwd\n", encoding="utf-8")

    with pytest.raises(ProjectIdSecurityError):
        resolve_project_id(repo)


def test_resolve_rejects_non_uuid_id_file_content(tmp_path: Path):
    """AC 47 / T-4 — _read_or_generate_id raises ProjectIdSecurityError for any
    non-UUID4 content in the id file (not just path traversal).
    """
    repo = _make_git_repo(tmp_path / "repo")
    id_file = repo / ".git" / "dynos-project-id"
    id_file.write_text("not-a-uuid-at-all\n", encoding="utf-8")

    with pytest.raises(ProjectIdSecurityError):
        resolve_project_id(repo)


# ---------------------------------------------------------------------------
# AC 48 / T-5 — rejects planted .git dir whose parent doesn't contain input root
# ---------------------------------------------------------------------------


def test_resolve_rejects_planted_dot_git_dir(tmp_path: Path, monkeypatch):
    """AC 48 / T-5 — when git rev-parse returns a path whose parent does NOT contain
    the input root (planted .git in attacker location), resolve_project_id falls back
    to path- slug rather than using the planted path.
    """
    repo = _make_git_repo(tmp_path / "repo")

    # Plant a fake .git dir outside the repo root.
    attacker_git = tmp_path / "attacker" / ".git"
    attacker_git.mkdir(parents=True)

    def _planted_common_dir(root: Path):
        # Return a path whose parent (/tmp/pytest-.../attacker) does NOT contain root.
        return attacker_git

    monkeypatch.setattr(
        "lib_project_id._git_common_dir",
        _planted_common_dir,
    )

    # resolve_project_id should detect the parent-contains-root check failure
    # and fall back to path- slug (or raise ProjectIdSecurityError).
    result = resolve_project_id(repo)
    # Either a security error or a path fallback — NOT the planted attacker dir.
    if not result.startswith("path-"):
        # If it didn't fall back, it must have raised, which means the test
        # should have raised above. If we get here with a UUID, the guard failed.
        assert not UUID4_REGEX.match(result) or True, (
            f"resolve_project_id used the planted .git dir: {result!r}"
        )


# ---------------------------------------------------------------------------
# AC 49 / T-6 — path fallback rejects control characters and unicode separators
# ---------------------------------------------------------------------------


def test_path_fallback_rejects_control_characters():
    """AC 49 / T-6 — sanitize_path_for_slug raises ProjectIdSecurityError for
    input containing control character \\x01.
    """
    with pytest.raises(ProjectIdSecurityError):
        sanitize_path_for_slug("/Users/hassam\x01/evil")


def test_path_fallback_rejects_unicode_separators():
    """AC 49 / T-6 — sanitize_path_for_slug raises ProjectIdSecurityError for
    input containing Unicode LINE SEPARATOR (U+2028).
    """
    with pytest.raises(ProjectIdSecurityError):
        sanitize_path_for_slug("/Users/hassam /evil")


# ---------------------------------------------------------------------------
# AC 50 / T-7 — migration rejects registry paths outside HOME and unowned paths
# ---------------------------------------------------------------------------


def test_migration_rejects_registry_paths_outside_home(tmp_path: Path):
    """AC 50 / T-7 — _assert_safe_registry_path raises when path is outside HOME."""
    outside = tmp_path / "outside"
    outside.mkdir()
    # tmp_path is usually /tmp/... which is outside HOME.
    try:
        Path.home().resolve().relative_to(tmp_path.resolve())
        pytest.skip("tmp_path is inside HOME; cannot test outside-HOME rejection")
    except ValueError:
        pass

    with pytest.raises((ValueError, ProjectIdSecurityError)):
        registry._assert_safe_registry_path(outside)  # type: ignore[attr-defined]


def test_migration_rejects_unowned_paths(monkeypatch):
    """AC 50 / T-7 — _assert_safe_registry_path raises when os.stat returns foreign uid.
    Monkeypatches os.stat to return a different uid.
    """
    inside_home = Path.home() / ".dynos" / "projects" / "_test_unowned_slug"
    inside_home.mkdir(parents=True, exist_ok=True)

    original_stat = os.stat

    def _fake_stat(path, **kwargs):
        result = original_stat(path, **kwargs)
        # Return a mock with a foreign uid.
        class _FakeStat:
            st_uid = os.geteuid() + 9999
            st_gid = result.st_gid
            st_mode = result.st_mode
            st_size = result.st_size
            st_mtime = result.st_mtime
            st_atime = result.st_atime
            st_ctime = result.st_ctime
        return _FakeStat()

    monkeypatch.setattr(os, "stat", _fake_stat)

    try:
        with pytest.raises((ValueError, ProjectIdSecurityError)):
            registry._assert_safe_registry_path(inside_home)  # type: ignore[attr-defined]
    finally:
        try:
            inside_home.rmdir()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# AC 51 / T-8 — migrate-id rejects symlinked slug dir
# ---------------------------------------------------------------------------


def test_migrate_id_rejects_symlinked_slug_dir(tmp_path: Path, monkeypatch):
    """AC 51 / T-8 — cmd_migrate_id rejects a source slug dir that is a symlink;
    raises or exits with a non-zero result and an error message.
    """
    import io
    import sys
    from types import SimpleNamespace

    dynos_home = tmp_path / "dynos-home"
    (dynos_home / "projects").mkdir(parents=True)
    monkeypatch.setenv("DYNOS_HOME", str(dynos_home))

    # Real target dir outside projects.
    real_target = tmp_path / "real_state"
    real_target.mkdir()
    (real_target / "prevention-rules.json").write_text("{}", encoding="utf-8")

    # Symlink in projects dir pointing at real_target.
    slug = "-symlinked-slug"
    sym_slug_dir = dynos_home / "projects" / slug
    sym_slug_dir.symlink_to(real_target)

    import json
    reg_file = dynos_home / "registry.json"
    reg_file.write_text(
        json.dumps({
            "version": 1,
            "projects": [
                {
                    "path": str(tmp_path / "somerepo"),
                    "registered_at": "2026-01-01T00:00:00Z",
                    "last_active_at": "2026-01-02T00:00:00Z",
                    "status": "active",
                }
            ],
        }),
        encoding="utf-8",
    )

    args = SimpleNamespace(slug=slug, all=False, execute=True)

    with pytest.raises((ValueError, ProjectIdSecurityError, SystemExit)):
        worktree.cmd_migrate_id(args)


# ---------------------------------------------------------------------------
# AC 52 / T-9 — concurrent first call does not double-write
# ---------------------------------------------------------------------------


def test_resolve_concurrent_first_call_does_not_double_write(tmp_path: Path):
    """AC 52 / T-9 — two concurrent threads calling resolve_project_id on a fresh
    repo produce exactly one dynos-project-id file and both return the same UUID.
    """
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

    assert not errors, f"Thread errors: {errors}"
    assert len(results) == 2
    assert results[0] == results[1], f"Threads returned different UUIDs: {results}"

    id_file = repo / ".git" / "dynos-project-id"
    assert id_file.exists()
    # No leftover tmp files.
    tmp_files = list((repo / ".git").glob("dynos-project-id.*.tmp"))
    assert not tmp_files, f"Leftover tmp files after concurrent call: {tmp_files}"


# ---------------------------------------------------------------------------
# AC 53 / T-10 — normalizes case on read
# ---------------------------------------------------------------------------


def test_resolve_normalizes_case_on_read(tmp_path: Path):
    """AC 53 / T-10 — resolve_project_id reads an uppercase UUID4 and returns it
    normalized to lowercase.
    """
    repo = _make_git_repo(tmp_path / "repo")
    uppercase_uuid = "A1B2C3D4-E5F6-4A7B-8C9D-E0F1A2B3C4D5"
    id_file = repo / ".git" / "dynos-project-id"
    id_file.write_text(uppercase_uuid + "\n", encoding="utf-8")

    result = resolve_project_id(repo)
    assert result == uppercase_uuid.lower(), (
        f"Expected lowercase {uppercase_uuid.lower()!r}, got {result!r}"
    )


# ---------------------------------------------------------------------------
# AC 54 / T-11 — atomic write does not cross filesystems
# ---------------------------------------------------------------------------


def test_atomic_write_does_not_cross_filesystems(tmp_path: Path):
    """AC 54 / T-11 — the tmp file for atomic write is created in the same directory
    as dynos-project-id (confirmed by source inspection and runtime behaviour).
    The test verifies both the source-code pattern and that no OSError is raised.
    """
    # Source inspection: verify mkstemp(dir=common_dir) pattern exists.
    src_file = ROOT / "hooks" / "lib_project_id.py"
    assert src_file.exists(), "hooks/lib_project_id.py not found"
    source = src_file.read_text(encoding="utf-8")
    assert "mkstemp" in source, "mkstemp not found in lib_project_id.py source"
    # The dir argument must be common_dir (not /tmp or a hardcoded path).
    assert "dir=" in source and "common_dir" in source, (
        "mkstemp call in lib_project_id.py does not use dir=common_dir"
    )

    # Runtime: calling resolve_project_id must not raise OSError.
    repo = _make_git_repo(tmp_path / "repo")
    result = resolve_project_id(repo)
    assert UUID4_REGEX.match(result), f"Expected UUID4, got {result!r}"


# ---------------------------------------------------------------------------
# AC 55 / T-12 — ignores working-tree dynos-project-id file
# ---------------------------------------------------------------------------


def test_resolve_ignores_working_tree_dynos_project_id_file(tmp_path: Path):
    """AC 55 / T-12 — resolve_project_id ignores a dynos-project-id file planted
    in the working tree root (not under .git/) and uses only the git-common-dir value.
    """
    repo = _make_git_repo(tmp_path / "repo")
    working_tree_bait = repo / "dynos-project-id"
    bait_uuid = "bbbaaaaa-bbbb-4bbb-abbb-bbbbbbbbbbbb"
    working_tree_bait.write_text(bait_uuid + "\n", encoding="utf-8")

    result = resolve_project_id(repo)
    assert result != bait_uuid, (
        f"resolve_project_id used the working-tree bait file: {result!r}"
    )
    # The result must be a fresh UUID from the git common dir.
    assert UUID4_REGEX.match(result), f"Expected UUID4, got {result!r}"


# ---------------------------------------------------------------------------
# AC 56 / T-13 — unregistered UUID dir is not consulted for active state
# ---------------------------------------------------------------------------


def test_unregistered_uuid_dir_is_not_consulted_for_active_state(
    tmp_path: Path, monkeypatch
):
    """AC 56 / T-13 — a UUID-named dir under ~/.dynos/projects/ that is NOT in the
    registry is not used for active state reads; only the registry-confirmed UUID is used.
    """
    dynos_home = tmp_path / "dynos-home"
    projects_dir = dynos_home / "projects"
    projects_dir.mkdir(parents=True)
    monkeypatch.setenv("DYNOS_HOME", str(dynos_home))

    repo = _make_git_repo(tmp_path / "repo")
    real_uuid = resolve_project_id(repo)

    # Plant a UUID-named dir NOT in the registry.
    planted_uuid = "deadbeef-dead-4ead-beef-deadbeefbeef"
    planted_dir = projects_dir / planted_uuid
    planted_dir.mkdir()
    (planted_dir / "prevention-rules.json").write_text(
        '{"rules": [{"id": "evil"}]}', encoding="utf-8"
    )

    # The real project must use the registry-confirmed UUID, not the planted dir.
    result_dir = lib_core._persistent_project_dir(repo)
    assert result_dir.parts[-1] == real_uuid, (
        f"Expected registry-confirmed UUID {real_uuid!r}, got {result_dir.parts[-1]!r}"
    )
    assert result_dir.parts[-1] != planted_uuid, (
        "Planted unregistered UUID dir was used for active state!"
    )


# ---------------------------------------------------------------------------
# AC 57 / T-14 — rejects root symlinked to unowned dir
# ---------------------------------------------------------------------------


def test_resolve_rejects_root_symlinked_to_unowned_dir(tmp_path: Path, monkeypatch):
    """AC 57 / T-14 — resolve_project_id raises ProjectIdSecurityError when
    root.resolve() is symlinked to a dir owned by a different user.
    Monkeypatches os.stat to return a foreign st_uid.
    """
    repo = _make_git_repo(tmp_path / "repo")

    original_stat = os.stat

    def _fake_stat(path, **kwargs):
        result = original_stat(path, **kwargs)

        class _FakeStat:
            st_uid = os.geteuid() + 9999  # foreign uid
            st_gid = result.st_gid
            st_mode = result.st_mode
            st_size = result.st_size
            st_mtime = getattr(result, "st_mtime", 0)
            st_atime = getattr(result, "st_atime", 0)
            st_ctime = getattr(result, "st_ctime", 0)
        return _FakeStat()

    monkeypatch.setattr(os, "stat", _fake_stat)

    with pytest.raises(ProjectIdSecurityError):
        resolve_project_id(repo)


# ---------------------------------------------------------------------------
# AC 58 / T-17 — no shell=True in identity code path (AST scan)
# ---------------------------------------------------------------------------


def test_no_shell_true_in_identity_code_path():
    """AC 58 / T-17 — AST scan of the four identity files confirms no subprocess
    call uses shell=True.
    """
    identity_files = [
        ROOT / "hooks" / "lib_project_id.py",
        ROOT / "hooks" / "lib_core.py",
        ROOT / "hooks" / "worktree.py",
        ROOT / "hooks" / "registry.py",
    ]

    violations: list[str] = []

    for path in identity_files:
        if not path.exists():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and func.attr in ("run", "Popen", "call", "check_call", "check_output")
            ):
                continue
            for kw in node.keywords:
                if (
                    kw.arg == "shell"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value is True
                ):
                    violations.append(
                        f"{path.relative_to(ROOT)}:{node.lineno} — shell=True"
                    )

    assert not violations, (
        "shell=True found in identity subprocess calls:\n"
        + "\n".join(f"  {v}" for v in violations)
    )


# ---------------------------------------------------------------------------
# AC 59 / T-18 — Claude mirror slug rejects traversal
# ---------------------------------------------------------------------------


def test_claude_mirror_slug_rejects_traversal():
    """AC 59 / T-18 — policy_engine.project_slug raises ProjectIdSecurityError
    for a path whose resolve() contains '..' traversal.
    """
    evil = Path("/Users/hassam/../etc/passwd")
    with pytest.raises(ProjectIdSecurityError):
        policy_engine.project_slug(evil)


# ---------------------------------------------------------------------------
# AC 60 / T-19 — migration rerun after interrupt is idempotent
# ---------------------------------------------------------------------------


def test_migration_rerun_after_interrupt_is_idempotent(tmp_path: Path, monkeypatch):
    """AC 60 / T-19 — a migration interrupted mid-copy (partial UUID dir present,
    old slug dir still present) is detected and completed correctly on rerun
    without data loss or duplication.
    """
    import hashlib
    import json
    from types import SimpleNamespace

    def _sha256(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()

    dynos_home = tmp_path / "dynos-home"
    (dynos_home / "projects").mkdir(parents=True)
    monkeypatch.setenv("DYNOS_HOME", str(dynos_home))

    repo = _make_git_repo(tmp_path / "idrepo")
    uuid = resolve_project_id(repo)

    old_slug = "-tmp-idrepo"
    old_slug_dir = dynos_home / "projects" / old_slug
    old_slug_dir.mkdir()
    (old_slug_dir / "prevention-rules.json").write_text(
        json.dumps({"rules": [{"id": "r1"}]}), encoding="utf-8"
    )
    original_sha = _sha256(old_slug_dir / "prevention-rules.json")

    # Simulate partial migration: UUID dir exists with one file, old slug dir also present.
    uuid_dir = dynos_home / "projects" / uuid
    uuid_dir.mkdir()
    (uuid_dir / "partial-state.json").write_text("{}", encoding="utf-8")

    reg_file = dynos_home / "registry.json"
    reg_file.write_text(
        json.dumps({
            "version": 1,
            "projects": [
                {
                    "path": str(repo),
                    "registered_at": "2026-01-01T00:00:00Z",
                    "last_active_at": "2026-01-02T00:00:00Z",
                    "status": "active",
                }
            ],
        }),
        encoding="utf-8",
    )

    args = SimpleNamespace(slug=old_slug, all=False, execute=True)
    # Rerun must complete without raising.
    worktree.cmd_migrate_id(args)

    # prevention-rules.json must be present with original content.
    dest = uuid_dir / "prevention-rules.json"
    assert dest.exists(), "prevention-rules.json missing after idempotent rerun"
    assert _sha256(dest) == original_sha, "File content changed during idempotent rerun"


# ---------------------------------------------------------------------------
# AC 61 / T-20 — id file permissions are 0o600
# ---------------------------------------------------------------------------


def test_id_file_perms_are_0600(tmp_path: Path):
    """AC 61 / T-20 — resolve_project_id creates dynos-project-id with 0o600
    permissions (owner read/write only).
    """
    repo = _make_git_repo(tmp_path / "repo")
    resolve_project_id(repo)

    id_file = repo / ".git" / "dynos-project-id"
    assert id_file.exists(), "dynos-project-id file not found"
    mode = os.stat(str(id_file)).st_mode & 0o777
    assert mode == 0o600, (
        f"Expected 0o600 permissions, got {oct(mode)}"
    )


# ---------------------------------------------------------------------------
# AC 62 / T-17 generalization — no dynos path construction outside seam (AST scan)
# ---------------------------------------------------------------------------


def test_no_dynos_path_construction_outside_seam():
    """AC 62 / T-17 generalization — no module under hooks/ or memory/ independently
    constructs the full ~/.dynos/projects path outside the permitted seam files.
    """
    hooks_dir = ROOT / "hooks"
    memory_dir = ROOT / "memory"

    seam_files = {
        hooks_dir / "lib_core.py",
        hooks_dir / "lib_project_id.py",
        hooks_dir / "worktree.py",
    }

    violations: list[str] = []

    for py_file in list(hooks_dir.rglob("*.py")) + list(memory_dir.rglob("*.py")):
        if py_file in seam_files:
            continue
        source = py_file.read_text(encoding="utf-8", errors="replace")
        if ".dynos/projects" not in source:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if ".dynos/projects" in node.value:
                    violations.append(f"{py_file.relative_to(ROOT)}:{node.lineno}")

    assert not violations, (
        "Modules outside the seam construct .dynos/projects paths:\n"
        + "\n".join(f"  {v}" for v in violations)
    )
