"""TDD-first tests for the Agent-tool spawn-log hook (task-20260430-003).

The hook is a PreToolUse + PostToolUse interceptor for the `Agent` tool. Its
job is to capture harness-level evidence of every subagent spawn and its
return, so the audit-receipt write step can mechanically reconcile claimed
spawns against actual spawns. Without this hook, the orchestrator can claim
`model: "haiku"` for an auditor that never spawned and the receipt chain has
no way to detect the lie (this is the audit-chain forgery incident from
2026-04-30: 7/8 ensemble auditors were synthesized by the orchestrator
itself after one truncation).

The hook MUST:

  (pre)  Append a JSONL line of the form
         ``{"phase": "pre", "tool": "Agent", "subagent_type": ..., ...,
           "prompt_sha256": ..., "timestamp": ...}``
         to ``.dynos/task-{id}/spawn-log.jsonl`` BEFORE the spawn runs.
  (post) Append a matching ``{"phase": "post", ...,
           "result_sha256": ..., "result_excerpt": ..., "stop_reason": ...}``
         entry AFTER the spawn returns.
  (idle) Exit 0 silently when no active task dir is discoverable
         (Agent calls outside dynos-work tasks must not error).
  (deny) Refuse direct orchestrator writes to ``spawn-log.jsonl`` via
         write_policy — only the hook subprocess can append to it.

These tests TDD-fail until ``hooks/agent-spawn-log`` (bash entry) and
``hooks/agent_spawn_log.py`` (Python impl) land.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = ROOT / "hooks" / "agent-spawn-log"
WRITE_POLICY_HOOK = ROOT / "hooks" / "pre-tool-use"


def _hook_available(p: Path) -> bool:
    if not p.exists():
        return False
    try:
        mode = p.stat().st_mode
    except OSError:
        return False
    return bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))


def _make_task_dir(tmp_path: Path) -> tuple[Path, Path]:
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / ".dynos").mkdir()
    task_dir = project_root / ".dynos" / "task-20260430-999"
    task_dir.mkdir()
    (task_dir / "manifest.json").write_text(json.dumps({"task_id": task_dir.name}))
    return project_root, task_dir


def _invoke(
    payload: dict,
    *,
    phase: str,
    cwd: Path,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(HOOK_PATH), phase],
        cwd=str(cwd),
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


@pytest.mark.skipif(
    not _hook_available(HOOK_PATH),
    reason="hooks/agent-spawn-log not present or not executable yet (TDD-first)",
)
def test_pre_phase_appends_spawn_log_entry(tmp_path: Path):
    project_root, task_dir = _make_task_dir(tmp_path)
    payload = {
        "tool_name": "Agent",
        "tool_input": {
            "subagent_type": "audit-spec-completion",
            "description": "Verify ACs",
            "prompt": "You are the spec-completion auditor...\nRead the spec.\n",
        },
        "cwd": str(project_root),
    }
    proc = _invoke(payload, phase="pre", cwd=project_root)
    assert proc.returncode == 0, f"hook failed: rc={proc.returncode} stderr={proc.stderr}"

    log_path = task_dir / "spawn-log.jsonl"
    assert log_path.is_file(), "spawn-log.jsonl was not created"
    lines = [json.loads(ln) for ln in log_path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected 1 entry, got {len(lines)}"
    entry = lines[0]
    assert entry["phase"] == "pre"
    assert entry["tool"] == "Agent"
    assert entry["subagent_type"] == "audit-spec-completion"
    assert "prompt_sha256" in entry and len(entry["prompt_sha256"]) == 64
    assert "timestamp" in entry


@pytest.mark.skipif(
    not _hook_available(HOOK_PATH),
    reason="hooks/agent-spawn-log not present or not executable yet (TDD-first)",
)
def test_post_phase_appends_result_entry_with_sha_and_excerpt(tmp_path: Path):
    project_root, task_dir = _make_task_dir(tmp_path)
    pre_payload = {
        "tool_name": "Agent",
        "tool_input": {
            "subagent_type": "audit-security",
            "description": "Adversarial review",
            "prompt": "You are the security auditor.\n",
        },
        "cwd": str(project_root),
    }
    rc1 = _invoke(pre_payload, phase="pre", cwd=project_root)
    assert rc1.returncode == 0, rc1.stderr

    post_payload = {
        **pre_payload,
        "tool_response": {
            "content": "FINDINGS: none.\nReport written to audit-reports/security.json",
            "stop_reason": "end_turn",
        },
    }
    rc2 = _invoke(post_payload, phase="post", cwd=project_root)
    assert rc2.returncode == 0, rc2.stderr

    log_path = task_dir / "spawn-log.jsonl"
    lines = [json.loads(ln) for ln in log_path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 2, f"expected 2 entries (pre+post), got {len(lines)}"
    post = lines[1]
    assert post["phase"] == "post"
    assert post["subagent_type"] == "audit-security"
    assert "result_sha256" in post and len(post["result_sha256"]) == 64
    assert "result_excerpt" in post and "FINDINGS" in post["result_excerpt"]
    assert post.get("stop_reason") == "end_turn"


@pytest.mark.skipif(
    not _hook_available(HOOK_PATH),
    reason="hooks/agent-spawn-log not present or not executable yet (TDD-first)",
)
def test_post_phase_marks_truncated_when_stop_reason_max_tokens(tmp_path: Path):
    project_root, task_dir = _make_task_dir(tmp_path)
    pre_payload = {
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "audit-security", "prompt": "go"},
        "cwd": str(project_root),
    }
    _invoke(pre_payload, phase="pre", cwd=project_root)
    post_payload = {
        **pre_payload,
        "tool_response": {"content": "partial...", "stop_reason": "max_tokens"},
    }
    rc = _invoke(post_payload, phase="post", cwd=project_root)
    assert rc.returncode == 0
    lines = [json.loads(ln) for ln in (task_dir / "spawn-log.jsonl").read_text().splitlines() if ln.strip()]
    post = lines[-1]
    assert post["stop_reason"] == "max_tokens"
    assert post.get("truncated") is True, "max_tokens stop_reason must set truncated=true"


@pytest.mark.skipif(
    not _hook_available(HOOK_PATH),
    reason="hooks/agent-spawn-log not present or not executable yet (TDD-first)",
)
def test_no_active_task_dir_exits_zero_silently(tmp_path: Path):
    """Agent calls outside a dynos-work task must not error — exit 0 silently."""
    bare = tmp_path / "bare"
    bare.mkdir()
    payload = {
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "general-purpose", "prompt": "hi"},
        "cwd": str(bare),
    }
    rc = _invoke(payload, phase="pre", cwd=bare)
    assert rc.returncode == 0, f"expected silent exit-0; rc={rc.returncode} stderr={rc.stderr}"


@pytest.mark.skipif(
    not _hook_available(HOOK_PATH),
    reason="hooks/agent-spawn-log not present or not executable yet (TDD-first)",
)
def test_unknown_phase_argument_exits_one(tmp_path: Path):
    project_root, _ = _make_task_dir(tmp_path)
    payload = {"tool_name": "Agent", "tool_input": {"subagent_type": "x"}, "cwd": str(project_root)}
    rc = _invoke(payload, phase="garbage", cwd=project_root)
    assert rc.returncode == 1, "unknown phase must be a hook internal error"


@pytest.mark.skipif(
    not _hook_available(WRITE_POLICY_HOOK),
    reason="hooks/pre-tool-use not present (write-policy not enforced)",
)
def test_orchestrator_cannot_write_spawn_log_directly(tmp_path: Path):
    """spawn-log.jsonl is hook-owned. Direct orchestrator Writes must be denied."""
    project_root, task_dir = _make_task_dir(tmp_path)
    target = task_dir / "spawn-log.jsonl"
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(target), "content": "{\"forged\": true}\n"},
        "cwd": str(project_root),
    }
    env = {**os.environ, "DYNOS_TASK_DIR": str(task_dir)}
    env.pop("DYNOS_ROLE", None)
    proc = subprocess.run(
        ["bash", str(WRITE_POLICY_HOOK)],
        cwd=str(project_root),
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 2, (
        f"orchestrator Write to spawn-log.jsonl must be denied (exit 2); "
        f"got rc={proc.returncode} stderr={proc.stderr}"
    )
    assert "write-policy:" in proc.stderr
