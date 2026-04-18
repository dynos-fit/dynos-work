"""Tests for the `dynos worktree` CLI.

Covers:
- migrate dry-run prints plan without mutating anything
- migrate --execute performs the migration, creates backups
- list-orphans detects worktree-remnant slugs
- migrate with same source/target slug refuses
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


def _seed_split_state(home: Path, main_slug: str, wt_slug: str):
    """Create a fake worktree-slug persistent dir with some state to migrate."""
    projects = home / "projects"
    projects.mkdir(parents=True, exist_ok=True)

    main = projects / main_slug
    main.mkdir(parents=True, exist_ok=True)
    (main / "postmortems").mkdir()
    (main / "postmortems" / "task-A.json").write_text('{"task_id": "task-A"}')
    (main / "prevention-rules.json").write_text(json.dumps({
        "rules": [{"executor": "all", "category": "sec", "rule": "existing-main-rule",
                   "source_task": "task-A", "source_finding": "fA"}],
        "updated_at": "2026-01-01T00:00:00Z",
    }))

    wt = projects / wt_slug
    wt.mkdir(parents=True, exist_ok=True)
    (wt / "postmortems").mkdir()
    (wt / "postmortems" / "task-B.json").write_text('{"task_id": "task-B"}')
    (wt / "postmortems" / "task-B.md").write_text("# task-B postmortem")
    (wt / "prevention-rules.json").write_text(json.dumps({
        "rules": [
            {"executor": "all", "category": "sec", "rule": "wt-rule-1",
             "source_task": "task-B", "source_finding": "fB1"},
            {"executor": "all", "category": "sec", "rule": "wt-rule-2",
             "source_task": "task-B", "source_finding": "fB2"},
        ],
    }))
    (wt / "policy.json").write_text(json.dumps({"freshness_task_window": 1}))
    return main, wt


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


def _run_cli(tmp_path: Path, *args: str):
    """Invoke hooks/worktree.py with the given args; return (exit, stdout, stderr)."""
    hooks_dir = Path(__file__).resolve().parent.parent / "hooks"
    result = subprocess.run(
        ["python3", str(hooks_dir / "worktree.py"), *args],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "DYNOS_HOME": str(tmp_path / "home")},
    )
    return result.returncode, result.stdout, result.stderr


class TestMigrate:
    def test_dry_run_prints_plan_without_mutating(self, tmp_path: Path):
        main_repo, wt_path = _make_git_repo_and_worktree(tmp_path)
        home = tmp_path / "home"
        main_slug = str(main_repo.resolve()).strip("/").replace("/", "-")
        wt_slug = str(wt_path.resolve()).strip("/").replace("/", "-")
        main_dir, wt_dir = _seed_split_state(home, main_slug, wt_slug)
        _seed_registry(home, main_repo, wt_path)

        wt_marker_before = list((wt_dir / "postmortems").iterdir())
        main_marker_before = list((main_dir / "postmortems").iterdir())

        exit_code, stdout, stderr = _run_cli(tmp_path, "migrate", str(wt_dir))
        assert exit_code == 0, f"stderr: {stderr}"
        plan = json.loads(stdout)
        assert plan.get("dry_run") is True
        assert "task-B.json" in plan["postmortems_to_copy"]
        assert "wt-rule-1" in plan["prevention_rules_new"]
        assert "wt-rule-2" in plan["prevention_rules_new"]
        assert "policy.json" in plan["not_migrated"]

        # Dry run must not have touched anything
        assert list((wt_dir / "postmortems").iterdir()) == wt_marker_before
        assert list((main_dir / "postmortems").iterdir()) == main_marker_before

    def test_execute_performs_migration_and_creates_backups(self, tmp_path: Path):
        main_repo, wt_path = _make_git_repo_and_worktree(tmp_path)
        home = tmp_path / "home"
        main_slug = str(main_repo.resolve()).strip("/").replace("/", "-")
        wt_slug = str(wt_path.resolve()).strip("/").replace("/", "-")
        main_dir, wt_dir = _seed_split_state(home, main_slug, wt_slug)
        _seed_registry(home, main_repo, wt_path)

        exit_code, stdout, stderr = _run_cli(tmp_path, "migrate", str(wt_dir), "--execute")
        assert exit_code == 0, f"stderr: {stderr}"
        result = json.loads(stdout)
        assert result["ok"] is True
        assert result["applied"]["postmortems_copied"] == 2  # task-B.json + task-B.md
        assert result["applied"]["prevention_rules_added"] == 2

        # Target now has both original and new postmortems
        assert (main_dir / "postmortems" / "task-A.json").exists()
        assert (main_dir / "postmortems" / "task-B.json").exists()
        assert (main_dir / "postmortems" / "task-B.md").exists()

        # Prevention rules merged
        rules = json.loads((main_dir / "prevention-rules.json").read_text())
        texts = {r["rule"] for r in rules["rules"]}
        assert texts == {"existing-main-rule", "wt-rule-1", "wt-rule-2"}

        # Source dir removed
        assert not wt_dir.exists()

        # Backups exist
        bak_dirs = sorted((home / "projects").glob(f"{wt_slug}.bak-*"))
        assert len(bak_dirs) == 1
        bak_main = sorted((home / "projects").glob(f"{main_slug}.bak-*"))
        assert len(bak_main) == 1

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
    def test_detects_worktree_remnant(self, tmp_path: Path):
        """A persistent dir whose slug corresponds to a git worktree of an
        existing main repo should be flagged as an orphan."""
        main_repo, wt_path = _make_git_repo_and_worktree(tmp_path)
        home = tmp_path / "home"
        main_slug = str(main_repo.resolve()).strip("/").replace("/", "-")
        wt_slug = str(wt_path.resolve()).strip("/").replace("/", "-")
        _seed_split_state(home, main_slug, wt_slug)
        _seed_registry(home, main_repo, wt_path)

        exit_code, stdout, stderr = _run_cli(tmp_path, "list-orphans")
        assert exit_code == 0, f"stderr: {stderr}"
        result = json.loads(stdout)
        orphan_slugs = [o["slug"] for o in result["orphans"]]
        assert wt_slug in orphan_slugs
        assert main_slug not in orphan_slugs  # main should NOT be flagged

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
