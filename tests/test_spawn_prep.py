"""Tests for AC 4, 9: ctl spawn-prep subcommand.

These tests are RED by design until seg-2 adds cmd_spawn_prep to hooks/ctl.py.

Invoke the real `ctl spawn-prep` command via subprocess (same pattern as
test_actor_identity.py which uses python3 hooks/ctl.py grant-role ...).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CTL_PY = ROOT / "hooks" / "ctl.py"


def _create_task_dir(tmp_path: Path, task_id: str = "task-20260612-spawn-prep") -> Path:
    """Create a minimal task dir with role-grants.json."""
    project = tmp_path / "project"
    task_dir = project / ".dynos" / task_id
    task_dir.mkdir(parents=True)
    manifest = {
        "task_id": task_id,
        "stage": "CHECKPOINT_AUDIT",
        "fast_track": False,
        "created_at": "2026-06-12T00:00:00Z",
        "raw_input": "test spawn-prep",
    }
    (task_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    # Initialize empty grants ledger
    (task_dir / "role-grants.json").write_text(
        json.dumps({"grants": []}), encoding="utf-8"
    )
    (task_dir / "audit-reports").mkdir(exist_ok=True)
    return task_dir


def _run_spawn_prep(
    task_dir: Path,
    role: str,
    artifact_path: str,
    model: str | None = None,
) -> subprocess.CompletedProcess:
    """Run `python3 ctl.py spawn-prep <task_dir> --role <role> --artifact <path>`."""
    cmd = [
        sys.executable,
        str(CTL_PY),
        "spawn-prep",
        str(task_dir),
        "--role", role,
        "--artifact", artifact_path,
    ]
    if model is not None:
        cmd.extend(["--model", model])
    result = subprocess.run(
        cmd,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    return result


# ---------------------------------------------------------------------------
# AC 4: Two consecutive spawn-prep calls produce attempt=1 and attempt=2
# ---------------------------------------------------------------------------


def test_spawn_prep_attempt_monotone(tmp_path: Path) -> None:
    """Two spawn-prep calls for the same (auditor, model) produce attempt=1 and attempt=2.

    AC 4: Two consecutive spawn-prep calls produce:
    - Distinct file paths (attempt=1, attempt=2)
    - Neither overwrites the other
    - The files are both present on disk after both calls
    """
    task_dir = _create_task_dir(tmp_path)
    artifact1 = str(task_dir / "audit-reports" / "security-haiku-attempt-1.json")  # noqa: model-literal
    artifact2 = str(task_dir / "audit-reports" / "security-haiku-attempt-2.json")  # noqa: model-literal

    # First call — should create attempt=1
    r1 = _run_spawn_prep(task_dir, role="audit-security", artifact_path=artifact1, model="haiku")  # noqa: model-literal
    assert r1.returncode == 0, (
        f"First spawn-prep must succeed. stderr: {r1.stderr!r}, stdout: {r1.stdout!r}"
    )
    out1 = json.loads(r1.stdout)
    assert out1.get("attempt") == 1, f"First call must produce attempt=1, got {out1}"
    assert Path(artifact1).exists(), f"Attempt-1 skeleton must exist on disk at {artifact1}"

    # Second call for the same (auditor, model) — should create attempt=2
    r2 = _run_spawn_prep(task_dir, role="audit-security", artifact_path=artifact2, model="haiku")  # noqa: model-literal
    assert r2.returncode == 0, (
        f"Second spawn-prep must succeed. stderr: {r2.stderr!r}, stdout: {r2.stdout!r}"
    )
    out2 = json.loads(r2.stdout)
    assert out2.get("attempt") == 2, f"Second call must produce attempt=2, got {out2}"
    assert Path(artifact2).exists(), f"Attempt-2 skeleton must exist on disk at {artifact2}"

    # Both files present (neither was overwritten)
    assert Path(artifact1).exists(), "Attempt-1 must still exist after second spawn-prep"
    assert Path(artifact2).exists(), "Attempt-2 must exist"
    assert artifact1 != artifact2, "Attempt-1 and attempt-2 must be distinct paths"

    # attempt-1 file content must still be a skeleton (not overwritten by attempt-2)
    content1 = json.loads(Path(artifact1).read_text())
    assert content1.get("status") == "in_progress", (
        f"Attempt-1 skeleton must retain status=in_progress after attempt-2 created: {content1}"
    )

    # Both attempt numbers should be in the grants ledger
    ledger = json.loads((task_dir / "role-grants.json").read_text())
    grants = ledger.get("grants", [])
    attempts_in_grants = [g.get("attempt") for g in grants]
    assert 1 in attempts_in_grants, f"Attempt=1 must be in grants ledger: {grants}"
    assert 2 in attempts_in_grants, f"Attempt=2 must be in grants ledger: {grants}"


def test_spawn_prep_returns_budget(tmp_path: Path) -> None:
    """spawn-prep returns a non-None budget in its JSON output.

    AC 4: The returned JSON includes 'budget' field (pre-computed weighted budget).
    """
    task_dir = _create_task_dir(tmp_path)
    artifact = str(task_dir / "audit-reports" / "security-haiku-attempt-1.json")  # noqa: model-literal
    r = _run_spawn_prep(task_dir, role="audit-security", artifact_path=artifact, model="haiku")  # noqa: model-literal
    assert r.returncode == 0, f"spawn-prep must succeed. stderr: {r.stderr!r}"
    out = json.loads(r.stdout)
    assert "budget" in out, f"spawn-prep output must include 'budget', got keys: {list(out.keys())}"
    assert isinstance(out["budget"], int), f"budget must be an int, got {type(out['budget'])}"
    assert out["budget"] >= 1, f"budget must be positive, got {out['budget']}"


def test_spawn_prep_artifact_path_in_output(tmp_path: Path) -> None:
    """spawn-prep returns artifact_path in its JSON output."""
    task_dir = _create_task_dir(tmp_path)
    artifact = str(task_dir / "audit-reports" / "security-haiku-attempt-1.json")  # noqa: model-literal
    r = _run_spawn_prep(task_dir, role="audit-security", artifact_path=artifact, model="haiku")  # noqa: model-literal
    assert r.returncode == 0, f"spawn-prep must succeed. stderr: {r.stderr!r}"
    out = json.loads(r.stdout)
    assert "artifact_path" in out, f"Output must include artifact_path, got {list(out.keys())}"


def test_spawn_prep_skeleton_has_in_progress_status(tmp_path: Path) -> None:
    """The skeleton written by spawn-prep has status='in_progress'."""
    task_dir = _create_task_dir(tmp_path)
    artifact_path = task_dir / "audit-reports" / "security-haiku-attempt-1.json"  # noqa: model-literal
    r = _run_spawn_prep(task_dir, role="audit-security", artifact_path=str(artifact_path), model="haiku")  # noqa: model-literal
    assert r.returncode == 0, f"spawn-prep must succeed. stderr: {r.stderr!r}"
    assert artifact_path.exists(), f"Skeleton must be written to {artifact_path}"
    content = json.loads(artifact_path.read_text())
    assert content.get("status") == "in_progress", (
        f"Skeleton must have status=in_progress, got {content.get('status')}"
    )
    assert "started_at" in content, f"Skeleton must have started_at timestamp, got {content}"


def test_spawn_prep_bad_artifact_name_errors(tmp_path: Path) -> None:
    """spawn-prep with artifact basename not matching {a}-{m}-attempt-{n}.json returns error.

    Per seg-2 spec: basename doesn't match pattern → exit 1 with error JSON.
    """
    task_dir = _create_task_dir(tmp_path)
    bad_artifact = str(task_dir / "audit-reports" / "security-checkpoint-20260101.json")
    r = _run_spawn_prep(task_dir, role="audit-security", artifact_path=bad_artifact)
    assert r.returncode != 0, (
        f"spawn-prep with legacy filename must fail. stdout: {r.stdout!r}"
    )


# ---------------------------------------------------------------------------
# AC 9: Continuation spawn — partial artifact → continuation=True, no overwrite
# ---------------------------------------------------------------------------


def test_resume_spawn_prep_output(tmp_path: Path) -> None:
    """spawn-prep on partial artifact returns continuation=True, no new skeleton.

    AC 9: Given a fixture task dir where audit-reports/sc-haiku-attempt-1.json
    exists with status='partial' and attempt=1 grant recorded in role-grants.json,
    calling spawn-prep returns {"attempt":1, "continuation":true, ...} without
    creating a new skeleton. The artifact file is not overwritten.
    """
    task_dir = _create_task_dir(tmp_path)
    artifact_path = task_dir / "audit-reports" / "sc-haiku-attempt-1.json"  # noqa: model-literal

    # Write a partial artifact with known content
    partial_content = {
        "status": "partial",
        "owner_model": "haiku",  # noqa: model-literal
        "attempt": 1,
        "findings": [{"id": "PARTIAL-001", "file": "foo.py", "line": 1, "category": "bug"}],
        "## Progress Ledger": "### Done\n- scanned module A\n### In-Flight\n- module B\n### Next\n- module C",
    }
    artifact_path.write_text(json.dumps(partial_content), encoding="utf-8")
    original_mtime = artifact_path.stat().st_mtime

    # Write role-grants.json with an existing attempt=1 grant for this (auditor, model)
    now = time.time()
    ledger = {
        "grants": [
            {
                "role": "audit-security",
                "granted_at": now - 100,
                "expires_at": now + 3500,
                "consumed_by": "session-sub-001",
                "consumed_at": now - 100,
                "expected_artifact": str(artifact_path),
                "attempt": 1,
                "budget": 20,
            }
        ]
    }
    (task_dir / "role-grants.json").write_text(json.dumps(ledger), encoding="utf-8")

    # Call spawn-prep — should detect status=partial and return continuation=True.
    # Role must be allowlisted; validation now runs before artifact parsing (AC 5).
    r = _run_spawn_prep(
        task_dir,
        role="audit-security",
        artifact_path=str(artifact_path),
        model="haiku",  # noqa: model-literal
    )
    assert r.returncode == 0, (
        f"spawn-prep on partial artifact must succeed. "
        f"stderr: {r.stderr!r}, stdout: {r.stdout!r}"
    )
    out = json.loads(r.stdout)
    assert out.get("continuation") is True, (
        f"spawn-prep on partial artifact must return continuation=true. Got: {out}"
    )
    assert out.get("attempt") == 1, (
        f"Continuation must return same attempt number (1). Got: {out}"
    )

    # Verify the artifact was NOT overwritten
    assert artifact_path.exists(), "Partial artifact must still exist"
    current_content = json.loads(artifact_path.read_text())
    assert current_content.get("status") == "partial", (
        f"Partial artifact must retain status=partial after spawn-prep. "
        f"Got: {current_content.get('status')}"
    )
    # Content must be byte-identical (no overwrite)
    assert current_content.get("findings") == partial_content["findings"], (
        f"Partial artifact findings must be preserved (not overwritten)"
    )
