"""Tests for git-worktree-aware _persistent_project_dir normalization.

Before this fix: each git worktree got a distinct ~/.dynos/projects/{slug}/
dir because the slug was derived from the absolute filesystem path of the
checkout. Learning state (postmortems, prevention-rules, retrospectives,
learned-agents) partitioned per-worktree, invisible to main. This test file
locks in the fix: worktrees fold back to the main repo's slug.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))


@pytest.fixture(autouse=True)
def _clear_git_cache():
    """The resolver is lru_cached; clear between tests so they don't bleed."""
    from lib_core import _resolve_git_toplevel
    _resolve_git_toplevel.cache_clear()
    yield
    _resolve_git_toplevel.cache_clear()


def _git(cwd: Path, *args: str) -> None:
    """Run a git command, raising on failure."""
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            # Avoid GPG-signing config from host
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
        },
    )


class TestSlugNormalization:
    def test_main_repo_slug_matches_absolute_path(self, tmp_path: Path, monkeypatch):
        """A plain repo (no worktrees) uses its own toplevel as the slug source."""
        monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "home"))
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        (repo / "README.md").write_text("hi")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "init")

        from lib_core import _persistent_project_dir
        slug_dir = _persistent_project_dir(repo)
        expected = str(repo.resolve()).strip("/").replace("/", "-")
        assert slug_dir.name == expected, (
            f"main repo slug should match absolute path; got {slug_dir.name}, expected {expected}"
        )

    def test_worktree_folds_back_to_main_slug(self, tmp_path: Path, monkeypatch):
        """A git worktree must resolve to the MAIN repo's slug.
        This is the load-bearing assertion — the fix exists for this."""
        monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "home"))
        main_repo = tmp_path / "main"
        main_repo.mkdir()
        _git(main_repo, "init", "-b", "main")
        (main_repo / "README.md").write_text("hi")
        _git(main_repo, "add", ".")
        _git(main_repo, "commit", "-m", "init")

        wt_path = tmp_path / "worktree"
        _git(main_repo, "worktree", "add", str(wt_path), "-b", "feature")

        from lib_core import _persistent_project_dir, _resolve_git_toplevel
        _resolve_git_toplevel.cache_clear()

        main_slug = _persistent_project_dir(main_repo).name
        wt_slug = _persistent_project_dir(wt_path).name

        assert wt_slug == main_slug, (
            f"worktree ({wt_slug}) must fold to main ({main_slug}); "
            f"if they differ, learning state is partitioning per-worktree again"
        )

    def test_nested_subdir_of_worktree_also_folds(self, tmp_path: Path, monkeypatch):
        """A subdirectory inside a worktree still maps to main's slug."""
        monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "home"))
        main_repo = tmp_path / "main"
        main_repo.mkdir()
        _git(main_repo, "init", "-b", "main")
        (main_repo / "src").mkdir()
        (main_repo / "src" / "a.py").write_text("pass\n")
        _git(main_repo, "add", ".")
        _git(main_repo, "commit", "-m", "init")

        wt_path = tmp_path / "worktree"
        _git(main_repo, "worktree", "add", str(wt_path), "-b", "feature")

        from lib_core import _persistent_project_dir, _resolve_git_toplevel
        _resolve_git_toplevel.cache_clear()

        main_slug = _persistent_project_dir(main_repo).name
        nested_slug = _persistent_project_dir(wt_path / "src").name
        assert nested_slug == main_slug


class TestFallbackBehavior:
    def test_non_git_dir_falls_back_to_absolute_path_slug(self, tmp_path: Path, monkeypatch):
        """A tmp dir that is NOT a git repo must preserve the old behavior:
        slug = str(root.resolve()).strip('/').replace('/', '-'). Existing
        tests that use tmp_path without git init depend on this."""
        monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "home"))
        plain = tmp_path / "plain"
        plain.mkdir()

        from lib_core import _persistent_project_dir
        slug_dir = _persistent_project_dir(plain)
        expected = str(plain.resolve()).strip("/").replace("/", "-")
        assert slug_dir.name == expected

    def test_git_missing_falls_back(self, tmp_path: Path, monkeypatch):
        """If `git` isn't on PATH, fall back silently — do not raise."""
        monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("PATH", "/nonexistent-dir")
        from lib_core import _persistent_project_dir, _resolve_git_toplevel
        _resolve_git_toplevel.cache_clear()
        # Should not raise
        slug_dir = _persistent_project_dir(tmp_path)
        assert slug_dir.name  # got a non-empty slug
        assert "/" not in slug_dir.name  # slug is slugified

    def test_git_rev_parse_nonzero_falls_back(self, tmp_path: Path, monkeypatch):
        """A tmp dir that LOOKS like it might be a git repo but where
        `git rev-parse` returns non-zero must fall back."""
        monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "home"))
        plain = tmp_path / "plain"
        plain.mkdir()
        # NOT a git init — plain dir
        from lib_core import _persistent_project_dir
        slug_dir = _persistent_project_dir(plain)
        expected = str(plain.resolve()).strip("/").replace("/", "-")
        assert slug_dir.name == expected


class TestCaching:
    def test_repeated_calls_cache_git_lookup(self, tmp_path: Path, monkeypatch):
        """Per-process lru_cache must prevent repeated subprocess shell-outs."""
        monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "home"))
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-b", "main")

        from lib_core import _persistent_project_dir, _resolve_git_toplevel
        _resolve_git_toplevel.cache_clear()

        with mock.patch("subprocess.run", wraps=subprocess.run) as mock_run:
            _persistent_project_dir(repo)
            _persistent_project_dir(repo)
            _persistent_project_dir(repo)
        # First call shells out once. Subsequent calls hit cache.
        assert mock_run.call_count == 1, (
            f"expected 1 subprocess.run call (cached), got {mock_run.call_count}"
        )


class TestBackwardsCompat:
    def test_current_main_repo_slug_unchanged(self):
        """Verify the REAL main repo (this checkout) still resolves to the
        same slug it had before the fix. This test would fail if the fix
        accidentally moved the slug for the main repo, which would orphan
        everyone's existing learning state."""
        from lib_core import _persistent_project_dir, _resolve_git_toplevel
        _resolve_git_toplevel.cache_clear()
        # Derive using the pre-fix logic explicitly
        real_main = Path(__file__).resolve().parent.parent
        # Pre-fix logic: slug from absolute path of `real_main`
        # Post-fix: slug from git toplevel — should be identical for a non-worktree
        pre = str(real_main.resolve()).strip("/").replace("/", "-")
        post = _persistent_project_dir(real_main).name
        # If this test is run from a worktree, the post slug will differ
        # (legitimately — it folds to main). Skip the comparison in that case.
        git_dir_marker = real_main / ".git"
        if git_dir_marker.is_file():
            pytest.skip("running from a worktree — slug fold is expected to change")
        assert pre == post, (
            f"main repo slug changed post-fix: pre={pre} post={post}. "
            "This would orphan existing learning state for anyone who upgrades."
        )
