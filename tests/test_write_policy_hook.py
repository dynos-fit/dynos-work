"""Tests for task-20260423-001 AC20: PreToolUse write-policy hook.

These tests spawn `hooks/pre-tool-use` as a subprocess with a JSON payload
piped on stdin. They assert:

  (a) An executor writing to an allowed repo path exits 0.
  (b) An executor writing to ``.dynos/task-X/manifest.json`` is denied
      (exit 2, stderr prefixed with ``write-policy: ``).
  (c) A Bash command that appends to ``.dynos/task-X/events.jsonl`` under
      role ``execute-inline`` is denied via the Bash pre-filter (exit 2,
      stderr prefixed with ``write-policy: ``).
  (d) A Bash command ``ls`` under any role passes (exit 0, no policy check).
  (e) Missing ``DYNOS_ROLE`` env: the hook writes a
      ``pre_tool_use_role_missing`` event and proceeds with a fallback
      role; the write succeeds or denies based on resolved path.

Tests tolerate the hook not being implemented yet: if
``hooks/pre-tool-use`` is missing or not executable, each test is skipped
with a reason — the regression suite will pick them up once the hook
lands.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = ROOT / "hooks" / "pre-tool-use"


def _hook_available() -> bool:
    if not HOOK_PATH.exists():
        return False
    try:
        mode = HOOK_PATH.stat().st_mode
    except OSError:
        return False
    # Executable by owner, group, or other.
    return bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))


pytestmark = pytest.mark.skipif(
    not _hook_available(),
    reason="hooks/pre-tool-use not present or not executable yet (TDD-first)",
)


def _make_task_dir(tmp_path: Path) -> tuple[Path, Path]:
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / ".dynos").mkdir()
    task_dir = project_root / ".dynos" / "task-20260423-999"
    task_dir.mkdir()
    # A minimal manifest is enough for the ancestor-resolution fallback.
    (task_dir / "manifest.json").write_text(json.dumps({"task_id": task_dir.name}))
    return project_root, task_dir


def _invoke_hook(
    *,
    payload: dict,
    env_extra: dict[str, str] | None = None,
    cwd: Path | None = None,
    drop_keys: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ}
    for key in drop_keys:
        env.pop(key, None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(HOOK_PATH)],
        cwd=str(cwd or ROOT),
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


# ---------------------------------------------------------------------------
# (a) executor writing to an allowed repo path passes (exit 0)
# ---------------------------------------------------------------------------
def test_executor_writing_repo_path_passes(tmp_path: Path) -> None:
    project_root, task_dir = _make_task_dir(tmp_path)
    repo_file = project_root / "src" / "widget.py"
    repo_file.parent.mkdir(parents=True)

    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(repo_file)},
        "cwd": str(project_root),
    }
    env = {
        "DYNOS_ROLE": "backend-executor",
        "DYNOS_TASK_DIR": str(task_dir),
    }

    result = _invoke_hook(payload=payload, env_extra=env, cwd=project_root)

    assert result.returncode == 0, (
        f"expected exit 0 for allowed repo path, got {result.returncode}; "
        f"stderr={result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# (b) executor writing to .dynos/task-X/manifest.json is denied (exit 2,
#     stderr contains "write-policy: ")
# ---------------------------------------------------------------------------
def test_executor_writing_manifest_is_denied(tmp_path: Path) -> None:
    project_root, task_dir = _make_task_dir(tmp_path)
    manifest_path = task_dir / "manifest.json"

    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(manifest_path)},
        "cwd": str(project_root),
    }
    env = {
        "DYNOS_ROLE": "backend-executor",
        "DYNOS_TASK_DIR": str(task_dir),
    }

    result = _invoke_hook(payload=payload, env_extra=env, cwd=project_root)

    assert result.returncode == 2, (
        f"expected exit 2 (deny) for manifest write by executor, got "
        f"{result.returncode}; stderr={result.stderr!r}"
    )
    assert "write-policy:" in result.stderr, (
        f"expected stderr to carry 'write-policy:' prefix on deny; "
        f"stderr={result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# (c) Bash `echo '{}' >> .dynos/task-X/events.jsonl` under execute-inline
#     is denied via Bash pre-filter (exit 2)
# ---------------------------------------------------------------------------
def test_bash_append_to_events_jsonl_is_denied(tmp_path: Path) -> None:
    project_root, task_dir = _make_task_dir(tmp_path)
    events_path = task_dir / "events.jsonl"

    command = f"echo '{{}}' >> {events_path}"
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(project_root),
    }
    env = {
        "DYNOS_ROLE": "execute-inline",
        "DYNOS_TASK_DIR": str(task_dir),
    }

    result = _invoke_hook(payload=payload, env_extra=env, cwd=project_root)

    assert result.returncode == 2, (
        f"expected exit 2 (deny) for Bash append into events.jsonl, got "
        f"{result.returncode}; stderr={result.stderr!r}"
    )
    assert "write-policy:" in result.stderr, (
        f"expected stderr to carry 'write-policy:' prefix on deny; "
        f"stderr={result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# (d) Bash `ls` passes (exit 0, no policy check)
# ---------------------------------------------------------------------------
def test_bash_ls_passes_without_policy_check(tmp_path: Path) -> None:
    project_root, task_dir = _make_task_dir(tmp_path)

    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "cwd": str(project_root),
    }
    env = {
        "DYNOS_ROLE": "execute-inline",
        "DYNOS_TASK_DIR": str(task_dir),
    }

    result = _invoke_hook(payload=payload, env_extra=env, cwd=project_root)

    assert result.returncode == 0, (
        f"expected exit 0 for harmless `ls`, got {result.returncode}; "
        f"stderr={result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# (e) Missing DYNOS_ROLE: fallback role is logged; write proceeds or denies
#     based on resolved path. Assert that (i) the hook does not exit 0
#     silently on an internal error, (ii) when the path is a clean repo
#     artifact the fallback role is sufficient to allow, and (iii) a
#     ``pre_tool_use_role_missing`` event is appended to the events log.
# ---------------------------------------------------------------------------
def test_missing_dynos_role_logs_fallback_event_and_resolves(tmp_path: Path) -> None:
    project_root, task_dir = _make_task_dir(tmp_path)
    repo_file = project_root / "src" / "fallback.py"
    repo_file.parent.mkdir(parents=True)

    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(repo_file)},
        "cwd": str(project_root),
    }

    result = _invoke_hook(
        payload=payload,
        env_extra={"DYNOS_TASK_DIR": str(task_dir)},
        cwd=project_root,
        drop_keys=("DYNOS_ROLE",),
    )

    # The fallback role is ``execute-inline``; writing a repo source file
    # is explicitly allowed by decide_write for that role, so we expect
    # exit 0. Crucially, the hook must NOT exit 0 through an internal
    # error swallow — the test below checks that a role-missing event is
    # recorded on disk, which proves the hook actually executed the
    # fallback branch.
    assert result.returncode == 0, (
        f"expected exit 0 for repo write under fallback role, got "
        f"{result.returncode}; stderr={result.stderr!r}"
    )

    events_path = task_dir / "events.jsonl"
    assert events_path.exists(), (
        "expected task events.jsonl to exist after the hook logged the "
        f"role-missing event; looked at {events_path}"
    )

    events_text = events_path.read_text()
    assert "pre_tool_use_role_missing" in events_text, (
        "expected a pre_tool_use_role_missing event to be appended when "
        f"DYNOS_ROLE is unset; events.jsonl content: {events_text!r}"
    )


# ---------------------------------------------------------------------------
# Regression guard: the hook must NEVER exit 0 on an internal error
# (malformed stdin, missing fields). This is the silent-allow-all failure
# mode flagged in the spec (AC3, AC5). A weak implementation that wraps
# its body in a bare `try/except: pass` would pass every other test and
# regress this one.
# ---------------------------------------------------------------------------
def test_malformed_stdin_never_silently_allows(tmp_path: Path) -> None:
    project_root, _ = _make_task_dir(tmp_path)
    env = {"DYNOS_ROLE": "backend-executor"}
    result = subprocess.run(
        ["bash", str(HOOK_PATH)],
        cwd=str(project_root),
        input="this is not valid json",
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, **env},
    )
    assert result.returncode != 0, (
        "expected a non-zero exit on malformed stdin (hook must not "
        f"silently allow); got exit {result.returncode}, stderr="
        f"{result.stderr!r}"
    )
