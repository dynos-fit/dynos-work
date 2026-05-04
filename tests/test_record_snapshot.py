"""Tests for ctl record-snapshot subcommand (task-20260504-001).

Pure subprocess tests against ctl.py. Each test creates a tmp_path-isolated
.dynos/task-X/ directory with a minimal manifest, then invokes
`python3 hooks/ctl.py record-snapshot ...` and asserts the subprocess
returncode plus the on-disk manifest mutation (or absence of mutation).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
CTL = REPO_ROOT / "hooks" / "ctl.py"


def _make_task_dir(tmp_path: Path, *, task_id: str = "task-20260504-001",
                    stage: str = "PRE_EXECUTION_SNAPSHOT",
                    extra: dict | None = None) -> Path:
    """Build a tmp_path-isolated .dynos/task-X/ with a minimal manifest."""
    task_dir = tmp_path / ".dynos" / task_id
    task_dir.mkdir(parents=True)
    manifest = {
        "task_id": task_id,
        "created_at": "2026-05-04T00:00:00Z",
        "title": "test",
        "raw_input": "test",
        "input_type": "text",
        "stage": stage,
        "classification": None,
        "retry_counts": {},
        "blocked_reason": None,
        "completed_at": None,
    }
    if extra:
        manifest.update(extra)
    (task_dir / "manifest.json").write_text(json.dumps(manifest))
    return task_dir


def _ctl(*args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(CTL), *args],
        capture_output=True, text=True, timeout=30, env=env,
    )


def _init_git(repo_dir: Path, *, sha: str | None = None,
              branch: str = "main", detached: bool = False) -> str:
    """Initialize a git repo in repo_dir and return the head SHA."""
    subprocess.run(["git", "init", "-q", "-b", branch], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo_dir, check=True)
    (repo_dir / "stub.txt").write_text("hello")
    subprocess.run(["git", "add", "stub.txt"], cwd=repo_dir, check=True)
    subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"], cwd=repo_dir, check=True)
    real_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True, text=True, check=True,
    ).stdout.strip()
    if detached:
        subprocess.run(["git", "checkout", "-q", "--detach"], cwd=repo_dir, check=True)
    return real_sha


def test_happy_path_auto_detect(tmp_path):
    """Auto-detect from git: no --head-sha, no --branch."""
    real_sha = _init_git(tmp_path)
    task_dir = _make_task_dir(tmp_path, task_id="task-auto")
    r = _ctl("record-snapshot", str(task_dir))
    assert r.returncode == 0, f"stderr={r.stderr}"
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["status"] == "recorded"
    manifest = json.loads((task_dir / "manifest.json").read_text())
    assert manifest["snapshot"]["head_sha"] == real_sha
    assert manifest["snapshot"]["branch"] == "main"
    assert "recorded_at" in manifest["snapshot"]


def test_explicit_head_sha_and_branch(tmp_path):
    """Explicit --head-sha + --branch bypasses git auto-detect."""
    _init_git(tmp_path)
    task_dir = _make_task_dir(tmp_path, task_id="task-explicit")
    explicit_sha = "a" * 40
    r = _ctl("record-snapshot", str(task_dir),
             "--head-sha", explicit_sha, "--branch", "feature-x")
    assert r.returncode == 0, f"stderr={r.stderr}"
    manifest = json.loads((task_dir / "manifest.json").read_text())
    assert manifest["snapshot"]["head_sha"] == explicit_sha
    assert manifest["snapshot"]["branch"] == "feature-x"


def test_idempotent_same_sha(tmp_path):
    """Re-running with the same SHA is a no-op."""
    _init_git(tmp_path)
    task_dir = _make_task_dir(tmp_path, task_id="task-idem")
    explicit_sha = "b" * 40
    r1 = _ctl("record-snapshot", str(task_dir), "--head-sha", explicit_sha, "--branch", "x")
    assert r1.returncode == 0
    first_manifest = (task_dir / "manifest.json").read_bytes()

    r2 = _ctl("record-snapshot", str(task_dir), "--head-sha", explicit_sha, "--branch", "x")
    assert r2.returncode == 0
    out = json.loads(r2.stdout.strip().splitlines()[-1])
    assert out["status"] == "already_recorded"
    # Byte-identical manifest after second call (no mutation, including recorded_at)
    assert (task_dir / "manifest.json").read_bytes() == first_manifest


def test_refusal_different_sha(tmp_path):
    """Re-running with a different SHA is refused; manifest unchanged."""
    _init_git(tmp_path)
    task_dir = _make_task_dir(tmp_path, task_id="task-diff")
    r1 = _ctl("record-snapshot", str(task_dir), "--head-sha", "c" * 40, "--branch", "x")
    assert r1.returncode == 0
    before = (task_dir / "manifest.json").read_bytes()

    r2 = _ctl("record-snapshot", str(task_dir), "--head-sha", "d" * 40, "--branch", "x")
    assert r2.returncode == 1
    assert "already recorded" in r2.stderr.lower()
    # Manifest must NOT have been mutated
    assert (task_dir / "manifest.json").read_bytes() == before


def test_stage_gate_refused_at_execution(tmp_path):
    """Stage gate refuses when manifest.stage == EXECUTION."""
    _init_git(tmp_path)
    task_dir = _make_task_dir(tmp_path, task_id="task-exec", stage="EXECUTION")
    r = _ctl("record-snapshot", str(task_dir), "--head-sha", "e" * 40, "--branch", "x")
    assert r.returncode == 1
    assert "EXECUTION" in r.stderr
    # Manifest's stage stays EXECUTION; no snapshot key added
    manifest = json.loads((task_dir / "manifest.json").read_text())
    assert manifest["stage"] == "EXECUTION"
    assert "snapshot" not in manifest or manifest.get("snapshot") is None


def test_stage_gate_refused_at_done(tmp_path):
    """Stage gate refuses when manifest.stage == DONE."""
    _init_git(tmp_path)
    task_dir = _make_task_dir(tmp_path, task_id="task-done", stage="DONE")
    r = _ctl("record-snapshot", str(task_dir), "--head-sha", "f" * 40, "--branch", "x")
    assert r.returncode == 1
    assert "DONE" in r.stderr


def test_stage_gate_allowed_at_pre_execution_snapshot(tmp_path):
    """Stage gate allows when manifest.stage == PRE_EXECUTION_SNAPSHOT."""
    _init_git(tmp_path)
    task_dir = _make_task_dir(tmp_path, task_id="task-pre", stage="PRE_EXECUTION_SNAPSHOT")
    r = _ctl("record-snapshot", str(task_dir), "--head-sha", "0" * 40, "--branch", "x")
    assert r.returncode == 0


def test_invalid_head_sha_format(tmp_path):
    """head_sha format validation rejects uppercase, short, long."""
    _init_git(tmp_path)
    task_dir = _make_task_dir(tmp_path, task_id="task-bad")
    # Uppercase
    r1 = _ctl("record-snapshot", str(task_dir), "--head-sha", "A" * 40, "--branch", "x")
    assert r1.returncode == 1
    assert "head_sha" in r1.stderr.lower()
    # Too short
    r2 = _ctl("record-snapshot", str(task_dir), "--head-sha", "a" * 39, "--branch", "x")
    assert r2.returncode == 1
    # Too long
    r3 = _ctl("record-snapshot", str(task_dir), "--head-sha", "a" * 41, "--branch", "x")
    assert r3.returncode == 1


def test_no_git_no_flag_hard_errors(tmp_path):
    """When --head-sha omitted and no git repo, command exits 1."""
    # Don't init git in tmp_path
    task_dir = _make_task_dir(tmp_path, task_id="task-nogit")
    r = _ctl("record-snapshot", str(task_dir))
    assert r.returncode == 1
    # Stderr must hint at the remedy
    assert "head-sha" in r.stderr.lower() or "git" in r.stderr.lower()


def test_detached_head_branch_fallback(tmp_path):
    """Detached HEAD with no --branch falls back to dynos/{task_id}-snapshot."""
    real_sha = _init_git(tmp_path, detached=True)
    task_dir = _make_task_dir(tmp_path, task_id="task-detached")
    r = _ctl("record-snapshot", str(task_dir))
    assert r.returncode == 0, f"stderr={r.stderr}"
    manifest = json.loads((task_dir / "manifest.json").read_text())
    assert manifest["snapshot"]["head_sha"] == real_sha
    assert manifest["snapshot"]["branch"] == "dynos/task-detached-snapshot"


def test_invalid_branch_empty(tmp_path):
    """Empty branch (after strip) is rejected."""
    _init_git(tmp_path)
    task_dir = _make_task_dir(tmp_path, task_id="task-emptybranch")
    r = _ctl("record-snapshot", str(task_dir), "--head-sha", "0" * 40, "--branch", "   ")
    assert r.returncode == 1
    assert "branch" in r.stderr.lower()
