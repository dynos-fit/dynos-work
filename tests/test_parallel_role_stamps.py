"""Parallel segments share the role file: stamping or clearing one segment's
role must not revoke a sibling that is still running."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "hooks"
sys.path.insert(0, str(HOOKS))

import actor_identity  # noqa: E402
from lib_core import clear_stamped_role, read_stamped_roles, stamp_role  # noqa: E402

SESSION = "orch-session-1"


def _setup(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    task_dir = root / ".dynos" / "task-20260101-001"
    task_dir.mkdir(parents=True)
    (task_dir / "manifest.json").write_text(json.dumps({"stage": "EXECUTION"}))
    actor_identity.pin_orchestrator(root, {"session_id": SESSION})
    return task_dir


def _env(task_dir: Path, tmp_path: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("DYNOS_ROLE", "DYNOS_TASK_DIR")}
    env["DYNOS_TASK_DIR"] = str(task_dir)
    env["DYNOS_HOME"] = str(tmp_path / "home")
    return env


def _spawn(phase: str, subagent_type: str, task_dir: Path, tmp_path: Path, prompt: str):
    payload = {
        "tool_name": "Agent",
        "session_id": SESSION,
        "cwd": str(task_dir.parent.parent),
        "tool_input": {"subagent_type": subagent_type, "prompt": prompt, "description": "d"},
        "tool_response": {"content": "done"},
    }
    return subprocess.run(
        [sys.executable, str(HOOKS / "agent_spawn_log.py"), phase],
        input=json.dumps(payload), capture_output=True, text=True,
        env=_env(task_dir, tmp_path),
    )


def _ctl(task_dir: Path, tmp_path: Path, *args: str):
    return subprocess.run(
        [sys.executable, str(HOOKS / "ctl.py"), *args],
        capture_output=True, text=True, env=_env(task_dir, tmp_path),
    )


def _write(task_dir: Path, tmp_path: Path, path: Path):
    payload = {
        "tool_name": "Write",
        "session_id": SESSION,
        "cwd": str(task_dir.parent.parent),
        "tool_input": {"file_path": str(path), "content": "x"},
    }
    return subprocess.run(
        [sys.executable, str(HOOKS / "pre_tool_use.py")],
        input=json.dumps(payload), capture_output=True, text=True,
        env=_env(task_dir, tmp_path),
    )


def test_sequential_stamp_replaces_previous_role(tmp_path):
    stamp_role(tmp_path, "backend-executor")
    stamp_role(tmp_path, "ui-executor")
    assert read_stamped_roles(tmp_path) == ["ui-executor"]


def test_stamp_keeps_running_siblings(tmp_path):
    stamp_role(tmp_path, "backend-executor")
    stamp_role(tmp_path, "ui-executor", keep={"backend-executor"})
    assert read_stamped_roles(tmp_path) == ["backend-executor", "ui-executor"]


def test_clear_stamped_role_removes_one_line(tmp_path):
    stamp_role(tmp_path, "backend-executor")
    stamp_role(tmp_path, "planning", keep={"backend-executor"})
    assert clear_stamped_role(tmp_path, "planning") is True
    assert read_stamped_roles(tmp_path) == ["backend-executor"]
    assert clear_stamped_role(tmp_path, "backend-executor") is True
    assert not (tmp_path / "active-segment-role").exists()


def test_parallel_spawns_both_stamped(tmp_path):
    task_dir = _setup(tmp_path)
    assert _spawn("pre", "backend-executor", task_dir, tmp_path, "seg-1").returncode == 0
    assert _spawn("pre", "ui-executor", task_dir, tmp_path, "seg-2").returncode == 0
    assert read_stamped_roles(task_dir) == ["backend-executor", "ui-executor"]


def test_clear_role_keeps_sibling_still_running(tmp_path):
    task_dir = _setup(tmp_path)
    _spawn("pre", "backend-executor", task_dir, tmp_path, "seg-1")
    _spawn("pre", "ui-executor", task_dir, tmp_path, "seg-2")
    assert _spawn("post", "ui-executor", task_dir, tmp_path, "seg-2").returncode == 0

    res = _ctl(task_dir, tmp_path, "clear-role", str(task_dir))
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    assert out["roles_cleared"] == ["ui-executor"]
    assert out["roles_kept_in_flight"] == ["backend-executor"]
    assert read_stamped_roles(task_dir) == ["backend-executor"]
    assert any(g["role"] == "backend-executor" for g in actor_identity.pending_grants(task_dir))
    assert not any(g["role"] == "ui-executor" for g in actor_identity.pending_grants(task_dir))

    # The still-running executor's write resolves to its role, not orchestrator.
    target = task_dir.parent.parent / "backend" / "app.py"
    res = _write(task_dir, tmp_path, target)
    assert res.returncode == 0, res.stderr


def test_clear_role_after_whole_batch_clears_everything(tmp_path):
    task_dir = _setup(tmp_path)
    _spawn("pre", "backend-executor", task_dir, tmp_path, "seg-1")
    _spawn("pre", "ui-executor", task_dir, tmp_path, "seg-2")
    _spawn("post", "backend-executor", task_dir, tmp_path, "seg-1")
    _spawn("post", "ui-executor", task_dir, tmp_path, "seg-2")
    res = _ctl(task_dir, tmp_path, "clear-role", str(task_dir))
    assert res.returncode == 0, res.stderr
    assert not (task_dir / "active-segment-role").exists()
    assert actor_identity.pending_grants(task_dir) == []


def test_clear_role_flags(tmp_path):
    task_dir = _setup(tmp_path)
    _spawn("pre", "backend-executor", task_dir, tmp_path, "seg-1")
    _spawn("pre", "ui-executor", task_dir, tmp_path, "seg-2")

    res = _ctl(task_dir, tmp_path, "clear-role", str(task_dir), "--role", "ui-executor")
    assert res.returncode == 0, res.stderr
    assert read_stamped_roles(task_dir) == ["backend-executor"]
    assert {g["role"] for g in actor_identity.pending_grants(task_dir)} == {"backend-executor"}

    res = _ctl(task_dir, tmp_path, "clear-role", str(task_dir), "--all")
    assert res.returncode == 0, res.stderr
    assert not (task_dir / "active-segment-role").exists()
    assert actor_identity.pending_grants(task_dir) == []


def test_write_allowed_when_any_running_role_allows_it(tmp_path):
    task_dir = _setup(tmp_path)
    _spawn("pre", "backend-executor", task_dir, tmp_path, "seg-1")
    _spawn("pre", "audit-security", task_dir, tmp_path, "audit")
    # Newest role is the auditor, which may not write source files; the
    # backend executor still running may.
    target = task_dir.parent.parent / "backend" / "app.py"
    res = _write(task_dir, tmp_path, target)
    assert res.returncode == 0, res.stderr


def test_write_denied_once_no_role_is_running(tmp_path):
    task_dir = _setup(tmp_path)
    _spawn("pre", "backend-executor", task_dir, tmp_path, "seg-1")
    _ctl(task_dir, tmp_path, "clear-role", str(task_dir), "--all")
    target = task_dir.parent.parent / "backend" / "app.py"
    res = _write(task_dir, tmp_path, target)
    assert res.returncode == 2
    assert "orchestrator" in res.stderr
