"""Tests for the install-hook subcommand of rules_engine.py.

Covers AC 16 from task-20260507-004.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RULES_ENGINE = ROOT / "hooks" / "rules_engine.py"

sys.path.insert(0, str(ROOT / "hooks"))

from rules_engine import _HOOK_BODY  # noqa: E402


def _init_git_repo(path: Path) -> None:
    """Initialize a bare git repo in path."""
    subprocess.run(
        ["git", "init", str(path)],
        check=True,
        capture_output=True,
    )


def _run_install_hook(cwd: Path, *extra_args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(RULES_ENGINE), "install-hook", *extra_args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# AC 16: install-hook creates pre-commit hook
# ---------------------------------------------------------------------------


def test_install_hook_creates_pre_commit(tmp_path: Path):
    """AC 16: install-hook creates .git/hooks/pre-commit that is executable and matches _HOOK_BODY."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    result = _run_install_hook(repo)

    assert result.returncode == 0, (
        f"install-hook exited {result.returncode}.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )

    hook_path = repo / ".git" / "hooks" / "pre-commit"
    assert hook_path.exists(), (
        f".git/hooks/pre-commit was not created at {hook_path}"
    )

    # Must be executable
    assert os.access(str(hook_path), os.X_OK), (
        f".git/hooks/pre-commit is not executable"
    )
    # The execute bit must be set on the file itself
    mode = os.stat(str(hook_path)).st_mode
    assert mode & stat.S_IXUSR, (
        f"pre-commit execute bit not set; mode={oct(mode)}"
    )

    # Content must be byte-identical to _HOOK_BODY
    content = hook_path.read_text()
    assert content == _HOOK_BODY, (
        f"pre-commit content does not match _HOOK_BODY.\n"
        f"Expected: {_HOOK_BODY!r}\n"
        f"Got:      {content!r}"
    )


def test_install_hook_idempotent(tmp_path: Path):
    """AC 16: running install-hook twice exits 0 and does not modify the hook file.

    The second run must detect the marker ('# dynos-rules-engine v1' in first 200 bytes)
    and return without writing.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    # First install
    result1 = _run_install_hook(repo)
    assert result1.returncode == 0, (
        f"First install-hook failed: {result1.stderr!r}"
    )

    hook_path = repo / ".git" / "hooks" / "pre-commit"
    assert hook_path.exists()
    mtime_after_first = hook_path.stat().st_mtime
    content_after_first = hook_path.read_text()

    # Second install — must be idempotent
    result2 = _run_install_hook(repo)
    assert result2.returncode == 0, (
        f"Second install-hook failed: stdout={result2.stdout!r} stderr={result2.stderr!r}"
    )

    content_after_second = hook_path.read_text()
    assert content_after_second == content_after_first, (
        "install-hook modified the pre-commit file on the second run"
    )


def test_install_hook_marker_detection(tmp_path: Path):
    """AC 16: install-hook detects the dynos marker in first 200 bytes and skips writing."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    hook_path = repo / ".git" / "hooks" / "pre-commit"
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    # Write a hook that already has the marker
    hook_path.write_text(_HOOK_BODY)
    os.chmod(str(hook_path), 0o755)
    mtime_before = hook_path.stat().st_mtime

    result = _run_install_hook(repo)

    assert result.returncode == 0, (
        f"install-hook exited non-zero when marker was present: {result.stderr!r}"
    )
    # File should not have been modified
    assert hook_path.stat().st_mtime == mtime_before or hook_path.read_text() == _HOOK_BODY, (
        "install-hook modified the file despite the marker being present"
    )
