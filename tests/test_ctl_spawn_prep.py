"""TDD-first regression tests for finding #6 — spawn-prep grant allowlist enforcement.

AC 5: cmd_spawn_prep must call _validated_grant_role before any artifact
parsing or grant-append logic, returning exit code 1 (with a stderr message
containing "not in the role allowlist") for any role absent from
_STAMP_ROLE_ALLOWLIST.

These tests use the same subprocess pattern established in tests/test_spawn_prep.py.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CTL_PY = ROOT / "hooks" / "ctl.py"


def _make_task_dir(tmp_path: Path, task_id: str = "task-20260615-002-ctl-test") -> Path:
    """Create a minimal task directory with role-grants.json."""
    project = tmp_path / "project"
    task_dir = project / ".dynos" / task_id
    task_dir.mkdir(parents=True)
    manifest = {
        "task_id": task_id,
        "stage": "CHECKPOINT_AUDIT",
        "fast_track": False,
        "created_at": "2026-06-15T00:00:00Z",
        "raw_input": "allowlist enforcement test",
    }
    (task_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (task_dir / "role-grants.json").write_text(
        json.dumps({"grants": []}), encoding="utf-8"
    )
    (task_dir / "audit-reports").mkdir(exist_ok=True)
    return task_dir


def _run_spawn_prep(
    task_dir: Path,
    role: str,
    artifact_name: str = "audit-security-sonnet-attempt-1.json",
) -> subprocess.CompletedProcess:
    """Invoke `python3 ctl.py spawn-prep <task_dir> --role <role> --artifact <path>`."""
    artifact_path = str(task_dir / artifact_name)
    cmd = [
        sys.executable,
        str(CTL_PY),
        "spawn-prep",
        str(task_dir),
        "--role", role,
        "--artifact", artifact_path,
    ]
    return subprocess.run(
        cmd,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# AC 5: spawn-prep rejects non-allowlisted roles
# ---------------------------------------------------------------------------


def test_spawn_prep_rejects_unknown_role(tmp_path: Path) -> None:
    """AC 5: spawn-prep with a non-allowlisted role exits 1 and prints allowlist error.

    The role 'super-admin' is guaranteed absent from _STAMP_ROLE_ALLOWLIST.
    After the fix, cmd_spawn_prep calls _validated_grant_role immediately after
    the task_dir.is_dir() check, so NO entry is appended to role-grants.json.
    """
    task_dir = _make_task_dir(tmp_path)
    grants_before = json.loads((task_dir / "role-grants.json").read_text())["grants"]

    result = _run_spawn_prep(task_dir, role="super-admin")

    assert result.returncode == 1, (
        f"spawn-prep with non-allowlisted role must exit 1; got {result.returncode}.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    assert "not in the role allowlist" in result.stderr, (
        f"stderr must contain 'not in the role allowlist'; got: {result.stderr!r}"
    )

    # Confirm NO grant was appended
    grants_after = json.loads((task_dir / "role-grants.json").read_text())["grants"]
    assert len(grants_after) == len(grants_before), (
        f"role-grants.json must not be modified on rejection; "
        f"before={len(grants_before)} after={len(grants_after)}"
    )


def test_spawn_prep_accepts_allowlisted_role(tmp_path: Path) -> None:
    """AC 5: spawn-prep with a role in _STAMP_ROLE_ALLOWLIST exits 0.

    Uses 'audit-security' which is confirmed present in _STAMP_ROLE_ALLOWLIST
    (ctl.py line ~2273).
    """
    task_dir = _make_task_dir(tmp_path)

    result = _run_spawn_prep(task_dir, role="audit-security")

    assert result.returncode == 0, (
        f"spawn-prep with allowlisted role 'audit-security' must exit 0; "
        f"got {result.returncode}.\nstdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
