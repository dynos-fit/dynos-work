"""Tests for hooks/worktree.py migrate-id subcommand — RED state.

These tests verify the migrate-id subcommand, marker file, conflict
resolution, embedded-path rewriting, v1→v2 registry upgrade, and
byte-identical file preservation.

Imports from lib_project_id will fail until production code exists —
that is the expected RED state.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

# worktree.py already exists.
import worktree

# lib_project_id is new — RED import.
from lib_project_id import is_uuid_id, resolve_project_id


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


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _populate_slug_dir(slug_dir: Path) -> dict[str, str]:
    """Create a representative set of state files and return {rel_path: sha256}."""
    slug_dir.mkdir(parents=True, exist_ok=True)
    (slug_dir / "prevention-rules.json").write_text(
        json.dumps({"rules": [{"id": "r1", "text": "never do bad things"}]}),
        encoding="utf-8",
    )
    (slug_dir / "postmortems").mkdir(exist_ok=True)
    (slug_dir / "postmortems" / "2026-05-01.md").write_text(
        "# Postmortem\nWhat happened.", encoding="utf-8"
    )
    (slug_dir / "learned-agents").mkdir(exist_ok=True)
    agents_registry = {
        "agents": [
            {
                "name": "backend-executor",
                "role": "backend",
                "path": str(slug_dir / "learned-agents" / "backend-executor"),
                "fixture_path": str(slug_dir / "learned-agents" / "fixtures" / "be.json"),
            }
        ]
    }
    (slug_dir / "learned-agents" / "registry.json").write_text(
        json.dumps(agents_registry), encoding="utf-8"
    )
    (slug_dir / "benchmarks").mkdir(exist_ok=True)
    (slug_dir / "benchmarks" / "history.json").write_text(
        json.dumps({"runs": [{"run_id": "abc", "score": 0.9}]}),
        encoding="utf-8",
    )
    # Return {rel: sha256} for non-schema files. learned-agents/registry.json is
    # exempt because AC 29 explicitly requires its embedded path/fixture_path
    # fields to be rewritten from the source slug to the destination UUID, which
    # is mutually exclusive with byte-identity. The byte-identical check (AC 28,
    # 33, 35) applies to "non-schema files" per the spec qualifier; this fixture
    # implements that filter.
    _SCHEMA_EXEMPT = {"learned-agents/registry.json"}
    checksums: dict[str, str] = {}
    for p in slug_dir.rglob("*"):
        if p.is_file():
            rel = str(p.relative_to(slug_dir))
            if rel in _SCHEMA_EXEMPT:
                continue
            checksums[rel] = _sha256(p)
    return checksums


def _fake_args(**kwargs):
    return SimpleNamespace(**kwargs)


# ---------------------------------------------------------------------------
# AC 26 — migrate-id consolidates old slug into UUID dir
# ---------------------------------------------------------------------------


def test_migrate_id_consolidates_old_slug_into_uuid(tmp_path: Path, monkeypatch):
    """AC 26 — cmd_migrate_id looks up the slug, resolves UUID, and plans the move.
    Dry-run by default; --execute performs the actual move.
    """
    dynos_home = tmp_path / "dynos-home"
    dynos_home.mkdir()
    projects_dir = dynos_home / "projects"
    projects_dir.mkdir()
    monkeypatch.setenv("DYNOS_HOME", str(dynos_home))

    repo = _make_git_repo(tmp_path / "myrepo")
    uuid = resolve_project_id(repo)
    assert is_uuid_id(uuid)

    old_slug = "-tmp-myrepo"
    old_slug_dir = projects_dir / old_slug
    checksums = _populate_slug_dir(old_slug_dir)

    # Build a minimal registry pointing the slug at the repo.
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

    # Dry-run — nothing should be moved.
    args = _fake_args(slug=old_slug, all=False, execute=False)
    worktree.cmd_migrate_id(args)
    assert old_slug_dir.exists(), "Dry-run must not delete source dir"
    assert not (projects_dir / uuid).exists(), "Dry-run must not create UUID dir"

    # Execute — state files should move.
    args = _fake_args(slug=old_slug, all=False, execute=True)
    worktree.cmd_migrate_id(args)
    uuid_dir = projects_dir / uuid
    assert uuid_dir.exists(), "UUID dir was not created after --execute"
    assert not old_slug_dir.exists(), "Old slug dir was not removed after --execute"

    # Verify file count matches. learned-agents/registry.json is exempt from
    # this comparison because AC 29's path-rewrite contract changes its content
    # post-migration; the fixture's `checksums` dict already excludes it.
    _SCHEMA_EXEMPT = {"learned-agents/registry.json"}
    moved_files = {
        str(p.relative_to(uuid_dir)): _sha256(p)
        for p in uuid_dir.rglob("*")
        if p.is_file() and str(p.relative_to(uuid_dir)) not in _SCHEMA_EXEMPT
    }
    assert set(moved_files.keys()) == set(checksums.keys()), (
        "File set differs after migration"
    )


# ---------------------------------------------------------------------------
# AC 27 — migrate-id --all writes marker file on completion
# ---------------------------------------------------------------------------


def test_migrate_id_all_writes_marker_on_completion(tmp_path: Path, monkeypatch):
    """AC 27 — dynos worktree migrate-id --all writes .migrated-v2 marker on success."""
    dynos_home = tmp_path / "dynos-home"
    (dynos_home / "projects").mkdir(parents=True)
    monkeypatch.setenv("DYNOS_HOME", str(dynos_home))

    reg_file = dynos_home / "registry.json"
    reg_file.write_text(json.dumps({"version": 1, "projects": []}), encoding="utf-8")

    args = _fake_args(all=True, execute=True, slug=None)
    worktree.cmd_migrate_id(args)

    marker = dynos_home / "projects" / ".migrated-v2"
    assert marker.exists(), ".migrated-v2 marker file was not written after --all"


# ---------------------------------------------------------------------------
# AC 28 — execute moves all state files with byte-identical content
# ---------------------------------------------------------------------------


def test_migrate_id_execute_moves_all_state_files(tmp_path: Path, monkeypatch):
    """AC 28 — cmd_migrate_id migrates all state files with sha256 matching before/after."""
    dynos_home = tmp_path / "dynos-home"
    (dynos_home / "projects").mkdir(parents=True)
    monkeypatch.setenv("DYNOS_HOME", str(dynos_home))

    repo = _make_git_repo(tmp_path / "testrepo")
    uuid = resolve_project_id(repo)

    old_slug = "-tmp-testrepo"
    old_slug_dir = dynos_home / "projects" / old_slug
    checksums = _populate_slug_dir(old_slug_dir)

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

    args = _fake_args(slug=old_slug, all=False, execute=True)
    worktree.cmd_migrate_id(args)

    uuid_dir = dynos_home / "projects" / uuid
    for rel, original_sha in checksums.items():
        dest = uuid_dir / rel
        assert dest.exists(), f"Migrated file missing: {rel}"
        assert _sha256(dest) == original_sha, (
            f"Content mismatch for {rel} after migration"
        )


# ---------------------------------------------------------------------------
# AC 29 — rewrites embedded paths in learned-agents/registry.json
# ---------------------------------------------------------------------------


def test_migrate_id_rewrites_embedded_fixture_paths(tmp_path: Path, monkeypatch):
    """AC 29 — cmd_migrate_id rewrites 'path' and 'fixture_path' fields in
    learned-agents/registry.json from the old slug string to the new UUID string.
    """
    dynos_home = tmp_path / "dynos-home"
    (dynos_home / "projects").mkdir(parents=True)
    monkeypatch.setenv("DYNOS_HOME", str(dynos_home))

    repo = _make_git_repo(tmp_path / "fixturerepo")
    uuid = resolve_project_id(repo)

    old_slug = "-tmp-fixturerepo"
    old_slug_dir = dynos_home / "projects" / old_slug
    (old_slug_dir / "learned-agents").mkdir(parents=True)

    agents_data = {
        "agents": [
            {
                "name": "testing-executor",
                "path": str(dynos_home / "projects" / old_slug / "learned-agents" / "testing-executor"),
                "fixture_path": str(dynos_home / "projects" / old_slug / "fixtures" / "t.json"),
            }
        ]
    }
    (old_slug_dir / "learned-agents" / "registry.json").write_text(
        json.dumps(agents_data), encoding="utf-8"
    )

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

    args = _fake_args(slug=old_slug, all=False, execute=True)
    worktree.cmd_migrate_id(args)

    uuid_dir = dynos_home / "projects" / uuid
    rewritten = json.loads(
        (uuid_dir / "learned-agents" / "registry.json").read_text(encoding="utf-8")
    )
    for agent in rewritten["agents"]:
        assert old_slug not in agent.get("path", ""), (
            f"Old slug still present in path: {agent['path']}"
        )
        assert old_slug not in agent.get("fixture_path", ""), (
            f"Old slug still present in fixture_path: {agent['fixture_path']}"
        )
        assert uuid in agent.get("path", ""), (
            f"UUID not found in rewritten path: {agent['path']}"
        )


# ---------------------------------------------------------------------------
# AC 30 — migrate-id upgrades registry v1 → v2
# ---------------------------------------------------------------------------


def test_migrate_id_upgrades_registry_v1_to_v2(tmp_path: Path, monkeypatch):
    """AC 30 — cmd_migrate_id upgrades ~/.dynos/registry.json from v1 to v2
    schema as part of the migration.
    """
    dynos_home = tmp_path / "dynos-home"
    (dynos_home / "projects").mkdir(parents=True)
    monkeypatch.setenv("DYNOS_HOME", str(dynos_home))

    repo = _make_git_repo(tmp_path / "upgraderepo")
    old_slug = "-tmp-upgraderepo"
    _populate_slug_dir(dynos_home / "projects" / old_slug)

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

    args = _fake_args(slug=old_slug, all=False, execute=True)
    worktree.cmd_migrate_id(args)

    on_disk = json.loads(reg_file.read_text(encoding="utf-8"))
    assert on_disk.get("schema_version") == 2, (
        f"Expected schema_version=2, got {on_disk.get('schema_version')}"
    )


# ---------------------------------------------------------------------------
# AC 31 — archives non-mappable slug dirs instead of deleting them
# ---------------------------------------------------------------------------


def test_migrate_id_archives_unmappable_slugs(tmp_path: Path, monkeypatch):
    """AC 31 — cmd_migrate_id archives slug dirs whose repo no longer exists to .archive/."""
    dynos_home = tmp_path / "dynos-home"
    (dynos_home / "projects").mkdir(parents=True)
    monkeypatch.setenv("DYNOS_HOME", str(dynos_home))

    # Slug that points to a non-existent repo.
    ghost_slug = "-nonexistent-repo"
    ghost_dir = dynos_home / "projects" / ghost_slug
    ghost_dir.mkdir()
    (ghost_dir / "prevention-rules.json").write_text("{}", encoding="utf-8")

    # The registry entry points to a path that does not exist.
    reg_file = dynos_home / "registry.json"
    reg_file.write_text(
        json.dumps({
            "version": 1,
            "projects": [
                {
                    "path": "/nonexistent/repo/that/was/deleted",
                    "registered_at": "2026-01-01T00:00:00Z",
                    "last_active_at": "2026-01-02T00:00:00Z",
                    "status": "active",
                }
            ],
        }),
        encoding="utf-8",
    )

    args = _fake_args(all=True, execute=True, slug=None)
    worktree.cmd_migrate_id(args)

    archive_dir = dynos_home / "projects" / ".archive" / ghost_slug
    assert archive_dir.exists(), f"Unmappable slug dir was not archived to {archive_dir}"
    assert not ghost_dir.exists(), "Unmappable slug dir was not moved out of projects/"


# ---------------------------------------------------------------------------
# AC 32 — list-orphans suggests migrate-id for path-slug dirs
# ---------------------------------------------------------------------------


def test_list_orphans_suggests_migrate_id_for_path_slug_dirs(
    tmp_path: Path, monkeypatch, capsys
):
    """AC 32 — list-orphans output includes 'dynos worktree migrate-id <slug>'
    suggestion for each path-slug dir whose repo resolves to a UUID.
    """
    dynos_home = tmp_path / "dynos-home"
    (dynos_home / "projects").mkdir(parents=True)
    monkeypatch.setenv("DYNOS_HOME", str(dynos_home))

    repo = _make_git_repo(tmp_path / "orphanrepo")
    old_slug = "-tmp-orphanrepo"
    (dynos_home / "projects" / old_slug).mkdir()

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

    args = _fake_args()
    worktree.cmd_list_orphans(args)

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "migrate-id" in output, (
        "list-orphans output does not mention 'migrate-id'"
    )
    assert old_slug in output, (
        f"list-orphans output does not mention the old slug {old_slug!r}"
    )


# ---------------------------------------------------------------------------
# AC 33 — byte-identical preservation (fixture mirror of user state)
# ---------------------------------------------------------------------------


def test_migrate_id_preserves_all_files_byte_for_byte(tmp_path: Path, monkeypatch):
    """AC 33 — migration of a synthesized state dir preserves every file
    byte-for-byte (sha256 matching for all non-schema files).
    """
    dynos_home = tmp_path / "dynos-home"
    (dynos_home / "projects").mkdir(parents=True)
    monkeypatch.setenv("DYNOS_HOME", str(dynos_home))

    repo = _make_git_repo(tmp_path / "userrepo")
    uuid = resolve_project_id(repo)

    old_slug = "-tmp-userrepo"
    old_slug_dir = dynos_home / "projects" / old_slug
    checksums = _populate_slug_dir(old_slug_dir)

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

    args = _fake_args(slug=old_slug, all=False, execute=True)
    worktree.cmd_migrate_id(args)

    uuid_dir = dynos_home / "projects" / uuid
    for rel, original_sha in checksums.items():
        dest = uuid_dir / rel
        assert dest.exists(), f"File missing after migration: {rel}"
        assert _sha256(dest) == original_sha, (
            f"File {rel!r} content changed after migration"
        )


# ---------------------------------------------------------------------------
# AC 34 — backup of registry.json written before v1→v2 swap
# ---------------------------------------------------------------------------


def test_registry_migration_writes_backup(tmp_path: Path, monkeypatch):
    """AC 34 — before the v1→v2 atomic swap, a backup registry.json.bak-{ts} is created."""
    dynos_home = tmp_path / "dynos-home"
    (dynos_home / "projects").mkdir(parents=True)
    monkeypatch.setenv("DYNOS_HOME", str(dynos_home))

    repo = _make_git_repo(tmp_path / "backrepo")
    old_slug = "-tmp-backrepo"
    _populate_slug_dir(dynos_home / "projects" / old_slug)

    reg_file = dynos_home / "registry.json"
    original_content = json.dumps({
        "version": 2,
        "projects": [
            {
                "path": str(repo),
                "registered_at": "2026-01-01T00:00:00Z",
                "last_active_at": "2026-01-02T00:00:00Z",
                "status": "active",
            }
        ],
    })
    reg_file.write_text(original_content, encoding="utf-8")

    args = _fake_args(slug=old_slug, all=False, execute=True)
    worktree.cmd_migrate_id(args)

    # A backup file matching registry.json.bak-* must exist.
    backups = list(dynos_home.glob("registry.json.bak-*"))
    assert backups, "No registry.json.bak-* backup file was created"
    # Backup must contain valid JSON.
    backup_data = json.loads(backups[0].read_text(encoding="utf-8"))
    assert isinstance(backup_data, dict), "Backup is not valid JSON"


# ---------------------------------------------------------------------------
# AC 35 — migration is idempotent after interrupt
# ---------------------------------------------------------------------------


def test_migration_rerun_after_interrupt_is_idempotent(tmp_path: Path, monkeypatch):
    """AC 35 — if migration is interrupted (partial UUID dir, old slug dir still present),
    rerunning it completes correctly without data loss.
    """
    dynos_home = tmp_path / "dynos-home"
    (dynos_home / "projects").mkdir(parents=True)
    monkeypatch.setenv("DYNOS_HOME", str(dynos_home))

    repo = _make_git_repo(tmp_path / "idempotentrepo")
    uuid = resolve_project_id(repo)

    old_slug = "-tmp-idempotentrepo"
    old_slug_dir = dynos_home / "projects" / old_slug
    checksums = _populate_slug_dir(old_slug_dir)

    # Simulate interrupted migration: partial UUID dir exists.
    uuid_dir = dynos_home / "projects" / uuid
    uuid_dir.mkdir()
    (uuid_dir / "partial.json").write_text("{}", encoding="utf-8")

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

    # Rerun must complete without raising.
    args = _fake_args(slug=old_slug, all=False, execute=True)
    worktree.cmd_migrate_id(args)

    # All original files must be present in UUID dir.
    for rel, original_sha in checksums.items():
        dest = uuid_dir / rel
        assert dest.exists(), f"File missing after idempotent rerun: {rel}"
        assert _sha256(dest) == original_sha, (
            f"File {rel!r} content changed during idempotent rerun"
        )
