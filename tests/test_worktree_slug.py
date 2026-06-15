"""Tests for project-identity-aware _persistent_project_dir.

Production migrated from a path-derived slug to a UUID4 anchored in the git
common dir (lib_project_id.resolve_project_id). All worktrees of one repo
share ONE UUID, so learning state no longer partitions per-worktree. A
non-git dir falls back to a sanitised ``path-`` slug. These tests lock in
that contract.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))


def _git(cwd: Path, *args: str) -> None:
    """Run a git command, raising on failure."""
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
        },
    )


class TestGitRepoIdentity:
    def test_git_repo_slug_is_uuid(self, tmp_path: Path, monkeypatch):
        """A git repo resolves to a UUID4 slug (stored in the git common dir)."""
        monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "home"))
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-b", "main")

        from lib_core import _persistent_project_dir
        from lib_project_id import is_uuid_id

        slug = _persistent_project_dir(repo).name
        assert is_uuid_id(slug), f"git repo slug should be a UUID4, got {slug!r}"

    def test_worktree_shares_main_uuid(self, tmp_path: Path, monkeypatch):
        """The load-bearing assertion: a worktree folds to the MAIN repo's
        identity. Under the UUID scheme this is even stronger — both read the
        SAME dynos-project-id from the shared git common dir."""
        monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "home"))
        main_repo = tmp_path / "main"
        main_repo.mkdir()
        _git(main_repo, "init", "-b", "main")
        (main_repo / "README.md").write_text("hi")
        _git(main_repo, "add", ".")
        _git(main_repo, "commit", "-m", "init")

        wt_path = tmp_path / "worktree"
        _git(main_repo, "worktree", "add", str(wt_path), "-b", "feature")

        from lib_core import _persistent_project_dir
        from lib_project_id import is_uuid_id

        main_slug = _persistent_project_dir(main_repo).name
        wt_slug = _persistent_project_dir(wt_path).name
        assert is_uuid_id(main_slug)
        assert wt_slug == main_slug, (
            f"worktree ({wt_slug}) must share main's UUID ({main_slug}); "
            "if they differ, learning state is partitioning per-worktree again"
        )

    def test_uuid_is_stable_and_persisted(self, tmp_path: Path, monkeypatch):
        """The UUID is persisted in <git-common-dir>/dynos-project-id, so
        repeated resolution returns the same identity."""
        monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "home"))
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-b", "main")

        from lib_core import _persistent_project_dir

        first = _persistent_project_dir(repo).name
        second = _persistent_project_dir(repo).name
        assert first == second
        assert (repo / ".git" / "dynos-project-id").exists()


class TestFallbackBehavior:
    def test_non_git_dir_falls_back_to_path_slug(self, tmp_path: Path, monkeypatch):
        """A tmp dir that is NOT a git repo gets a sanitised ``path-`` slug."""
        monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "home"))
        plain = tmp_path / "plain"
        plain.mkdir()

        from lib_core import _persistent_project_dir
        from lib_project_id import is_path_fallback_id, is_uuid_id

        slug = _persistent_project_dir(plain).name
        assert is_path_fallback_id(slug), (
            f"non-git dir should yield a 'path-' slug, got {slug!r}"
        )
        assert not is_uuid_id(slug)
        assert "/" not in slug  # slug is slugified

    def test_git_missing_falls_back(self, tmp_path: Path, monkeypatch):
        """If `git` isn't on PATH, fall back to a path- slug — do not raise."""
        monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("PATH", "/nonexistent-dir")

        from lib_core import _persistent_project_dir
        from lib_project_id import is_path_fallback_id

        slug = _persistent_project_dir(tmp_path).name
        assert is_path_fallback_id(slug)
        assert "/" not in slug
