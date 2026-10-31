"""Bug 7 (7.6.0 field report): an auditor's role stamp must not outlive the
audit batch.

After the audit batch the `audit-security` stamp stayed in
`active-segment-role`; under a shared-session harness the orchestrator
adopted it and its next repo write was denied with "auditors write reports
only to ...audit-reports/". Only `ctl clear-role` (which the audit skill
never called) unblocked it. Now:

* the Agent spawn hook releases an `audit-*` stamp when that auditor's
  spawn returns (same lifetime as the planning stamp), and
* `audit-receipt`, `run-audit-findings-gate` and `run-audit-finish` drop
  every `audit-*` stamp whose spawn is no longer in flight.
"""
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

SESSION = "orch-session-7"


def _setup(tmp_path: Path, stage: str = "CHECKPOINT_AUDIT") -> Path:
    root = tmp_path / "proj"
    task_dir = root / ".dynos" / "task-20261031-007"
    task_dir.mkdir(parents=True)
    (task_dir / "manifest.json").write_text(json.dumps({"stage": stage}))
    actor_identity.pin_orchestrator(root, {"session_id": SESSION})
    return task_dir


def _env(task_dir: Path, tmp_path: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("DYNOS_ROLE", "DYNOS_TASK_DIR")}
    env["DYNOS_TASK_DIR"] = str(task_dir)
    env["DYNOS_HOME"] = str(tmp_path / "home")
    return env


def _spawn(phase: str, subagent_type: str, task_dir: Path, tmp_path: Path, prompt: str = "p"):
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
        [sys.executable, str(HOOKS / "ctl.py"), *args, str(task_dir)],
        capture_output=True, text=True, env=_env(task_dir, tmp_path),
    )


def _stamped(task_dir: Path) -> list[str]:
    f = task_dir / "active-segment-role"
    return [l.strip() for l in f.read_text().splitlines() if l.strip()] if f.exists() else []


def _rows(task_dir: Path) -> list[dict]:
    return [json.loads(l) for l in (task_dir / "spawn-log.jsonl").read_text().splitlines()]


def test_auditor_post_releases_its_stamp(tmp_path):
    task_dir = _setup(tmp_path)
    assert _spawn("pre", "dynos-work:security-auditor", task_dir, tmp_path).returncode == 0
    assert _stamped(task_dir) == ["audit-security"]
    post = _spawn("post", "dynos-work:security-auditor", task_dir, tmp_path)
    assert post.returncode == 0, post.stderr
    assert _stamped(task_dir) == []
    assert _rows(task_dir)[-1]["cleared_role"] == "audit-security"
    assert not [g for g in actor_identity.pending_grants(task_dir)
                if g.get("role") == "audit-security"]


def test_parallel_auditors_release_independently(tmp_path):
    task_dir = _setup(tmp_path)
    assert _spawn("pre", "dynos-work:security-auditor", task_dir, tmp_path, "s").returncode == 0
    assert _spawn("pre", "dynos-work:code-quality-auditor", task_dir, tmp_path, "q").returncode == 0
    assert set(_stamped(task_dir)) == {"audit-security", "audit-code-quality"}
    assert _spawn("post", "dynos-work:security-auditor", task_dir, tmp_path, "s").returncode == 0
    # The sibling auditor is still running: its stamp and grant survive.
    assert _stamped(task_dir) == ["audit-code-quality"]
    assert [g for g in actor_identity.pending_grants(task_dir)
            if g.get("role") == "audit-code-quality"]
    assert _spawn("post", "dynos-work:code-quality-auditor", task_dir, tmp_path, "q").returncode == 0
    assert _stamped(task_dir) == []


def test_ensemble_shards_of_one_auditor_clear_after_the_last(tmp_path):
    task_dir = _setup(tmp_path)
    assert _spawn("pre", "dynos-work:security-auditor", task_dir, tmp_path, "fast").returncode == 0
    assert _spawn("pre", "dynos-work:security-auditor", task_dir, tmp_path, "deep").returncode == 0
    assert _spawn("post", "dynos-work:security-auditor", task_dir, tmp_path, "fast").returncode == 0
    assert _stamped(task_dir) == ["audit-security"]
    assert _rows(task_dir)[-1].get("role_clear_deferred") == 1
    assert _spawn("post", "dynos-work:security-auditor", task_dir, tmp_path, "deep").returncode == 0
    assert _stamped(task_dir) == []


def test_executor_post_still_leaves_stamp_for_execute_skill(tmp_path):
    task_dir = _setup(tmp_path, stage="EXECUTION")
    assert _spawn("pre", "dynos-work:backend-executor", task_dir, tmp_path).returncode == 0
    assert _spawn("post", "dynos-work:backend-executor", task_dir, tmp_path).returncode == 0
    assert _stamped(task_dir) == ["backend-executor"]


def test_findings_gate_drops_stale_audit_stamp(tmp_path):
    # A stale stamp left behind (e.g. the post hook did not run).
    task_dir = _setup(tmp_path)
    (task_dir / "active-segment-role").write_text("audit-security")
    actor_identity.append_grant(task_dir, "audit-security")
    res = _ctl(task_dir, tmp_path, "run-audit-findings-gate")
    assert res.returncode == 0, res.stdout + res.stderr
    assert json.loads(res.stdout)["status"] == "clear"
    assert _stamped(task_dir) == []
    assert not actor_identity.pending_grants(task_dir)


def test_findings_gate_keeps_running_auditor_and_non_audit_roles(tmp_path):
    task_dir = _setup(tmp_path)
    assert _spawn("pre", "dynos-work:security-auditor", task_dir, tmp_path).returncode == 0
    (task_dir / "active-segment-role").write_text("backend-executor\naudit-security\naudit-ui")
    res = _ctl(task_dir, tmp_path, "run-audit-findings-gate")
    assert res.returncode == 0, res.stdout + res.stderr
    # audit-security is still in flight; audit-ui is stale; executors are
    # left to the execute skill's clear-role.
    assert _stamped(task_dir) == ["backend-executor", "audit-security"]


def test_stale_audit_stamp_no_longer_blocks_orchestrator_after_gate(tmp_path):
    task_dir = _setup(tmp_path)
    (task_dir / "active-segment-role").write_text("audit-security")
    payload = {
        "tool_name": "Write",
        "session_id": SESSION,
        "cwd": str(task_dir.parent.parent),
        "tool_input": {"file_path": str(task_dir / "execution-log.md"), "content": "x"},
    }

    def _write():
        return subprocess.run(
            [sys.executable, str(HOOKS / "pre_tool_use.py")],
            input=json.dumps(payload), capture_output=True, text=True,
            env=_env(task_dir, tmp_path),
        )

    before = _write()
    assert before.returncode == 2, before.stderr
    assert "audit-security" in before.stderr
    assert _ctl(task_dir, tmp_path, "run-audit-findings-gate").returncode == 0
    after = _write()
    assert after.returncode == 0, after.stderr


def test_audit_skill_clears_roles_after_batch_and_repair():
    text = (ROOT / "skills" / "audit" / "SKILL.md").read_text()
    gate = text.index("ctl run-audit-findings-gate")
    assert text.count('ctl clear-role .dynos/task-{id}') >= 2
    assert text.index('ctl clear-role .dynos/task-{id}') < gate
