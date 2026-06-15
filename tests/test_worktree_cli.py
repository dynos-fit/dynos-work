"""Tests for the `dynos worktree` CLI.

Covers (against the production UUID-identity scheme in lib_project_id):
- migrate-id dry-run prints plan without mutating anything
- migrate-id --execute consolidates a legacy path-slug dir into the repo's
  UUID-anchored dir and backs up the source
- list-orphans flags a legacy path-slug remnant but NOT the canonical UUID dir
- migrate (legacy worktree-slug path) refuses same source/target slug
- migrate with missing source errors cleanly
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))


def _seed_registry(home: Path, *paths: Path):
    """Seed the global registry so worktree CLI can resolve slug → original path."""
    home.mkdir(parents=True, exist_ok=True)
    reg_path = home / "registry.json"
    reg = {"version": 1, "projects": [
        {"path": str(p.resolve()), "registered_at": "2026-01-01T00:00:00Z",
         "last_active_at": "2026-01-01T00:00:00Z", "status": "active"}
        for p in paths
    ], "checksum": ""}
    # Compute checksum like registry.py does
    import hashlib
    copy = dict(reg)
    copy.pop("checksum", None)
    blob = json.dumps(copy, sort_keys=True, separators=(",", ":"))
    reg["checksum"] = hashlib.sha256(blob.encode()).hexdigest()
    reg_path.write_text(json.dumps(reg))


def _git(cwd: Path, *args: str):
    subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
        env={**os.environ, "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t"},
    )


def _make_git_repo_and_worktree(tmp_path: Path):
    """Create a real git repo + worktree so the CLI can git-resolve the slug."""
    main_repo = tmp_path / "my-repo"
    main_repo.mkdir()
    _git(main_repo, "init", "-b", "main")
    (main_repo / "README.md").write_text("hi")
    _git(main_repo, "add", ".")
    _git(main_repo, "commit", "-m", "init")
    wt_path = tmp_path / "my-repo-feature"
    _git(main_repo, "worktree", "add", str(wt_path), "-b", "feature")
    return main_repo, wt_path


def _make_git_repo(path: Path) -> Path:
    """Create a real git repo with one commit."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    (path / "README.md").write_text("hi")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "init")
    return path


def _resolve_uuid(repo: Path) -> str:
    """Resolve the production UUID identity for a git repo (writes the
    dynos-project-id file the CLI subprocess will read)."""
    from lib_project_id import resolve_project_id
    return resolve_project_id(repo)


def _seed_state(slug_dir: Path) -> None:
    """Populate a legacy path-slug persistent dir with migratable state."""
    slug_dir.mkdir(parents=True, exist_ok=True)
    (slug_dir / "postmortems").mkdir(exist_ok=True)
    (slug_dir / "postmortems" / "task-B.json").write_text('{"task_id": "task-B"}')
    (slug_dir / "prevention-rules.json").write_text(json.dumps({
        "rules": [{"executor": "all", "category": "sec", "rule": "wt-rule-1",
                   "source_task": "task-B", "source_finding": "fB1"}],
    }))


def _run_cli(tmp_path: Path, *args: str):
    """Invoke hooks/worktree.py with the given args; return (exit, stdout, stderr)."""
    hooks_dir = Path(__file__).resolve().parent.parent / "hooks"
    result = subprocess.run(
        ["python3", str(hooks_dir / "worktree.py"), *args],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "DYNOS_HOME": str(tmp_path / "home")},
    )
    return result.returncode, result.stdout, result.stderr


class TestMigrateId:
    """migrate-id consolidates a legacy path-slug dir into the repo's UUID dir."""

    def test_dry_run_does_not_mutate(self, tmp_path: Path):
        home = tmp_path / "home"
        (home / "projects").mkdir(parents=True)
        repo = _make_git_repo(tmp_path / "my-repo")
        uuid = _resolve_uuid(repo)
        old_slug = "legacy-path-slug"
        old_dir = home / "projects" / old_slug
        _seed_state(old_dir)
        _seed_registry(home, repo)

        exit_code, stdout, stderr = _run_cli(tmp_path, "migrate-id", old_slug)
        assert exit_code == 0, f"stderr: {stderr}"
        payload = json.loads(stdout)
        assert payload["dry_run"] is True
        # Dry run must not move/create anything.
        assert old_dir.exists()
        assert not (home / "projects" / uuid).exists()

    def test_execute_moves_state_and_backs_up(self, tmp_path: Path):
        home = tmp_path / "home"
        (home / "projects").mkdir(parents=True)
        repo = _make_git_repo(tmp_path / "my-repo")
        uuid = _resolve_uuid(repo)
        old_slug = "legacy-path-slug"
        old_dir = home / "projects" / old_slug
        _seed_state(old_dir)
        _seed_registry(home, repo)

        exit_code, stdout, stderr = _run_cli(tmp_path, "migrate-id", old_slug, "--execute")
        assert exit_code == 0, f"stderr: {stderr}"
        payload = json.loads(stdout)
        assert payload["ok"] is True
        uuid_dir = home / "projects" / uuid
        # State migrated into the UUID-anchored dir.
        assert (uuid_dir / "postmortems" / "task-B.json").exists()
        # Source slug dir renamed to a timestamped backup, gone from old path.
        assert not old_dir.exists()
        backups = list((home / "projects").glob(f"{old_slug}.bak-*"))
        assert len(backups) == 1


class TestMigrate:
    def test_same_source_and_target_refused(self, tmp_path: Path):
        """If source slug == target slug (e.g., user accidentally runs on main's dir),
        refuse."""
        main_repo, _ = _make_git_repo_and_worktree(tmp_path)
        home = tmp_path / "home"
        main_slug = str(main_repo.resolve()).strip("/").replace("/", "-")
        projects = home / "projects"
        projects.mkdir(parents=True)
        (projects / main_slug).mkdir()
        _seed_registry(home, main_repo)

        exit_code, stdout, stderr = _run_cli(tmp_path, "migrate", str(projects / main_slug))
        assert exit_code == 1
        result = json.loads(stdout)
        assert "same slug" in result["error"]

    def test_missing_source_errors(self, tmp_path: Path):
        exit_code, stdout, stderr = _run_cli(tmp_path, "migrate", str(tmp_path / "nonexistent"))
        assert exit_code == 2
        result = json.loads(stdout)
        assert "does not exist" in result["error"]


class TestListOrphans:
    def test_detects_path_slug_remnant_not_uuid_dir(self, tmp_path: Path):
        """A legacy path-slug dir whose repo now resolves to a UUID is flagged
        as an orphan; the canonical UUID-named dir is NOT."""
        home = tmp_path / "home"
        (home / "projects").mkdir(parents=True)
        repo = _make_git_repo(tmp_path / "my-repo")
        uuid = _resolve_uuid(repo)
        (home / "projects" / uuid).mkdir()
        old_slug = "legacy-path-slug"
        (home / "projects" / old_slug).mkdir()
        _seed_registry(home, repo)

        exit_code, stdout, stderr = _run_cli(tmp_path, "list-orphans")
        assert exit_code == 0, f"stderr: {stderr}"
        result = json.loads(stdout)
        orphan_slugs = [o["slug"] for o in result["orphans"]]
        assert old_slug in orphan_slugs
        assert uuid not in orphan_slugs  # canonical UUID dir must NOT be flagged

    def test_empty_when_no_projects_dir(self, tmp_path: Path):
        exit_code, stdout, stderr = _run_cli(tmp_path, "list-orphans")
        assert exit_code == 0, f"stderr: {stderr}"
        result = json.loads(stdout)
        assert result["orphans"] == []

    def test_ignores_backup_dirs(self, tmp_path: Path):
        """Dirs with .bak-{timestamp} suffix should not be flagged as orphans."""
        home = tmp_path / "home"
        (home / "projects" / "something.bak-20260101-120000").mkdir(parents=True)
        exit_code, stdout, stderr = _run_cli(tmp_path, "list-orphans")
        assert exit_code == 0, f"stderr: {stderr}"
        result = json.loads(stdout)
        assert result["orphans"] == []
