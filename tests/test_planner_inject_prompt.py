"""Tests for `python3 hooks/router.py planner-inject-prompt` (CRITERION 5,
Fix F subcommand).

The subcommand writes atomic sidecar files at
`.dynos/task-{id}/receipts/_injected-planner-prompts/{phase}.sha256` and
`.txt` from stdin bytes and prints the sha256 digest to stdout.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ROUTER = REPO_ROOT / "hooks" / "router.py"


def _run(project: Path, task_id: str, phase: str, stdin: bytes) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT / "hooks"),
        "DYNOS_HOME": str(project / ".dynos-home"),
    }
    return subprocess.run(
        [
            sys.executable,
            str(ROUTER),
            "planner-inject-prompt",
            "--root",
            str(project),
            "--task-id",
            task_id,
            "--phase",
            phase,
        ],
        input=stdin,
        capture_output=True,
        check=False,
        env=env,
        cwd=str(REPO_ROOT),
    )


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".dynos").mkdir()
    return project


def test_planner_inject_prompt_writes_sidecar(tmp_path: Path):
    """Subcommand writes matching .sha256 and .txt sidecars for phase=spec."""
    project = _project(tmp_path)
    task_id = "task-T"
    body = b"test prompt body"

    result = _run(project, task_id, "spec", body)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")

    sidecar_dir = (
        project / ".dynos" / task_id / "receipts" / "_injected-planner-prompts"
    )
    sha_path = sidecar_dir / "spec.sha256"
    txt_path = sidecar_dir / "spec.txt"
    assert sha_path.exists(), f"missing: {sha_path}"
    assert txt_path.exists(), f"missing: {txt_path}"

    expected = hashlib.sha256(body).hexdigest()
    assert sha_path.read_text().strip() == expected
    assert txt_path.read_bytes() == body


def test_planner_inject_prompt_prints_digest(tmp_path: Path):
    """stdout is a single line equal to the sha256 hex digest of stdin."""
    project = _project(tmp_path)
    body = b"body-2"
    expected = hashlib.sha256(body).hexdigest()

    result = _run(project, "task-X", "discovery", body)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert result.stdout.decode("utf-8").strip() == expected


def test_planner_inject_prompt_phase_choices(tmp_path: Path):
    """`--phase invalid` must cause argparse to reject the call (non-zero exit)."""
    project = _project(tmp_path)
    result = _run(project, "task-Y", "invalid", b"x")
    assert result.returncode != 0, (
        "argparse should reject a phase outside {discovery, spec, plan}; "
        f"got exit 0 with stdout={result.stdout!r}"
    )


def test_planner_inject_prompt_rejects_path_traversal_task_id(tmp_path: Path):
    """Hardening against SEC-001: `--task-id ../evil` must be rejected
    before any filesystem write. The subcommand must exit non-zero and
    produce no sidecar file anywhere. Re-verified for an absolute-path
    value as well."""
    project = _project(tmp_path)
    for bad in ("../evil", "..", "foo/bar", "/etc/victim", "task-/evil", ".task-x", "task- "):
        result = _run(project, bad, "spec", b"x")
        assert result.returncode != 0, (
            f"expected non-zero exit for task-id={bad!r}; got 0"
        )
        # No sidecar written anywhere under the project for this attempt.
        assert not list((project / ".dynos").rglob("*.sha256")), (
            f"sidecar leaked for task-id={bad!r}: {list((project / '.dynos').rglob('*'))}"
        )


def test_planner_inject_prompt_writes_per_phase_independently(tmp_path: Path):
    """Separate phases write to separate sidecar files (one per phase)."""
    project = _project(tmp_path)
    task_id = "task-M"
    for phase, body in [
        ("discovery", b"D-body"),
        ("spec", b"S-body"),
        ("plan", b"P-body"),
    ]:
        r = _run(project, task_id, phase, body)
        assert r.returncode == 0, r.stderr
        sha_path = (
            project
            / ".dynos"
            / task_id
            / "receipts"
            / "_injected-planner-prompts"
            / f"{phase}.sha256"
        )
        assert sha_path.exists()
        assert sha_path.read_text().strip() == hashlib.sha256(body).hexdigest()
