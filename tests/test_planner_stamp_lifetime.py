"""AC13: the `planning` stamp lives exactly as long as the planner spawn."""
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

SESSION = "orch-session-1"
COMMAND = f"{HOOKS}/ctl planner-receipt /tmp/x"


def _setup(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    task_dir = root / ".dynos" / "task-20260101-001"
    task_dir.mkdir(parents=True)
    (task_dir / "manifest.json").write_text(json.dumps({"stage": "PLANNING"}))
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


def _bash(task_dir: Path, tmp_path: Path):
    payload = {
        "tool_name": "Bash",
        "session_id": SESSION,
        "cwd": str(task_dir.parent.parent),
        "tool_input": {"command": COMMAND},
    }
    return subprocess.run(
        [sys.executable, str(HOOKS / "pre_tool_use.py")],
        input=json.dumps(payload), capture_output=True, text=True,
        env=_env(task_dir, tmp_path),
    )


def _events(task_dir: Path) -> str:
    p = task_dir / "events.jsonl"
    root_p = task_dir.parent.parent / ".dynos" / "events.jsonl"
    return "".join(f.read_text() for f in (p, root_p) if f.exists())


def test_planner_post_clears_stamp_so_orchestrator_runs_planner_receipt(tmp_path):
    task_dir = _setup(tmp_path)
    assert _spawn("pre", "planning", task_dir, tmp_path).returncode == 0
    assert (task_dir / "active-segment-role").read_text().strip() == "planning"
    post = _spawn("post", "planning", task_dir, tmp_path)
    assert post.returncode == 0, post.stderr
    assert not (task_dir / "active-segment-role").exists()
    rows = [json.loads(l) for l in (task_dir / "spawn-log.jsonl").read_text().splitlines()]
    assert rows[-1]["phase"] == "post" and rows[-1]["cleared_role"] == "planning"
    res = _bash(task_dir, tmp_path)
    assert res.returncode == 0, res.stderr
    assert "planner_bash_denied" not in _events(task_dir)


def test_planner_pre_only_still_denies_planner_receipt(tmp_path):
    task_dir = _setup(tmp_path)
    assert _spawn("pre", "planning", task_dir, tmp_path).returncode == 0
    res = _bash(task_dir, tmp_path)
    assert res.returncode == 2
    assert "the planner does not run validators" in res.stderr


def test_non_planner_post_leaves_planning_stamp(tmp_path):
    task_dir = _setup(tmp_path)
    (task_dir / "active-segment-role").write_text("planning")
    post = _spawn("post", "backend-executor", task_dir, tmp_path)
    assert post.returncode == 0, post.stderr
    assert (task_dir / "active-segment-role").read_text() == "planning"
    rows = [json.loads(l) for l in (task_dir / "spawn-log.jsonl").read_text().splitlines()]
    assert "cleared_role" not in rows[-1]


# ---------------------------------------------------------------------------
# Audit repair (cycle 1): parallel worker planners, stale pre rows, failures
# ---------------------------------------------------------------------------

def _rows(task_dir: Path) -> list[dict]:
    return [json.loads(l) for l in (task_dir / "spawn-log.jsonl").read_text().splitlines()]


def test_parallel_planners_clear_only_after_the_last_returns(tmp_path):
    # arch-002: hierarchical worker planners share one stamp. The first worker
    # to return must not clear it while a sibling is still running.
    task_dir = _setup(tmp_path)
    assert _spawn("pre", "planning", task_dir, tmp_path, prompt="worker-a").returncode == 0
    assert _spawn("pre", "planning", task_dir, tmp_path, prompt="worker-b").returncode == 0
    first = _spawn("post", "planning", task_dir, tmp_path, prompt="worker-a")
    assert first.returncode == 0, first.stderr
    assert (task_dir / "active-segment-role").read_text().strip() == "planning"
    assert _rows(task_dir)[-1].get("planning_clear_deferred") == 1
    assert "cleared_role" not in _rows(task_dir)[-1]
    # The sibling is still in flight, so its own Bash validator call is denied.
    assert _bash(task_dir, tmp_path).returncode == 2
    second = _spawn("post", "planning", task_dir, tmp_path, prompt="worker-b")
    assert second.returncode == 0, second.stderr
    assert not (task_dir / "active-segment-role").exists()
    assert _rows(task_dir)[-1]["cleared_role"] == "planning"


def test_identical_parallel_prompts_pair_one_to_one(tmp_path):
    task_dir = _setup(tmp_path)
    assert _spawn("pre", "planning", task_dir, tmp_path).returncode == 0
    assert _spawn("pre", "planning", task_dir, tmp_path).returncode == 0
    assert _spawn("post", "planning", task_dir, tmp_path).returncode == 0
    assert (task_dir / "active-segment-role").exists()
    assert _spawn("post", "planning", task_dir, tmp_path).returncode == 0
    assert not (task_dir / "active-segment-role").exists()


def test_stale_unmatched_planner_pre_does_not_pin_the_stamp(tmp_path):
    # A crashed spawn from hours ago (pre without post) must not block the
    # clear for every later planner.
    task_dir = _setup(tmp_path)
    stale = {"phase": "pre", "tool": "Agent", "subagent_type": "planning",
             "prompt_sha256": "crashed", "timestamp": "2020-01-01T00:00:00Z"}
    (task_dir / "spawn-log.jsonl").write_text(json.dumps(stale) + "\n")
    assert _spawn("pre", "planning", task_dir, tmp_path).returncode == 0
    post = _spawn("post", "planning", task_dir, tmp_path)
    assert post.returncode == 0, post.stderr
    assert not (task_dir / "active-segment-role").exists()
    assert _rows(task_dir)[-1]["cleared_role"] == "planning"


def test_clear_failure_still_appends_row_and_exits_zero(tmp_path, monkeypatch, capsys):
    # TS-2: a failing clear prints to stderr, keeps the stamp, still logs the
    # post row, and never fails the Agent tool call.
    import io
    import agent_spawn_log
    import lib_core

    task_dir = _setup(tmp_path)
    (task_dir / "active-segment-role").write_text("planning")

    def _boom(*_a, **_k):
        raise OSError("disk says no")

    monkeypatch.setattr(lib_core, "clear_stamped_role", _boom)
    monkeypatch.setenv("DYNOS_TASK_DIR", str(task_dir))
    monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "home"))
    payload = {
        "tool_name": "Agent",
        "session_id": SESSION,
        "cwd": str(task_dir.parent.parent),
        "tool_input": {"subagent_type": "planning", "prompt": "p", "description": "d"},
        "tool_response": {"content": "done"},
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert agent_spawn_log.main(["agent_spawn_log.py", "post"]) == 0
    assert "role clear failed: disk says no" in capsys.readouterr().err
    assert (task_dir / "active-segment-role").read_text() == "planning"
    row = _rows(task_dir)[-1]
    assert row["phase"] == "post" and "cleared_role" not in row


# ---------------------------------------------------------------------------
# Background (async) planner spawns: post fires at launch, not at return
# ---------------------------------------------------------------------------

def _async_post(task_dir: Path, tmp_path: Path, prompt: str = "p", response=None):
    payload = {
        "tool_name": "Agent",
        "session_id": SESSION,
        "cwd": str(task_dir.parent.parent),
        "tool_input": {"subagent_type": "dynos-work:planning", "prompt": prompt, "description": "d"},
        "tool_response": response if response is not None else
            {"status": "async_launched", "agentId": "agent-a", "description": "d"},
    }
    return subprocess.run(
        [sys.executable, str(HOOKS / "agent_spawn_log.py"), "post"],
        input=json.dumps(payload), capture_output=True, text=True,
        env=_env(task_dir, tmp_path),
    )


def _subagent_stop(task_dir: Path, tmp_path: Path, agent_id: str = "agent-a"):
    transcript = tmp_path / f"{agent_id}.jsonl"
    transcript.write_text("".join(json.dumps(e) + "\n" for e in (
        {"type": "assistant", "agentId": agent_id, "timestamp": "2026-10-20T01:38:06.420Z",
         "message": {"model": "fable", "usage": {"input_tokens": 12, "output_tokens": 4}}},
        {"type": "assistant", "agentId": agent_id, "timestamp": "2026-10-20T01:43:54.855Z",
         "message": {"model": "fable", "usage": {"input_tokens": 3, "output_tokens": 1}}},
    )))
    payload = {"cwd": str(task_dir.parent.parent), "agent_transcript_path": str(transcript),
               "agent_type": "dynos-work:planning", "session_id": ""}
    return subprocess.run([str(HOOKS / "subagent-tokens")], input=json.dumps(payload),
                          text=True, capture_output=True, env=_env(task_dir, tmp_path))


def test_async_launch_post_keeps_stamp_until_subagent_stop(tmp_path):
    # task-20261020-001: the Agent call returned "Async agent launched" in the
    # same second as pre, the post cleared the stamp, and the still-running
    # planner was write-denied as the orchestrator.
    task_dir = _setup(tmp_path)
    assert _spawn("pre", "dynos-work:planning", task_dir, tmp_path).returncode == 0
    post = _async_post(task_dir, tmp_path)
    assert post.returncode == 0, post.stderr
    assert (task_dir / "active-segment-role").read_text().strip() == "planning"
    row = _rows(task_dir)[-1]
    assert row["async_launch"] is True and row["agent_id"] == "agent-a"
    assert "cleared_role" not in row
    # The planner is still running, so the stamp keeps denying validators.
    assert _bash(task_dir, tmp_path).returncode == 2

    stop = _subagent_stop(task_dir, tmp_path)
    assert stop.returncode == 0, stop.stderr
    assert _rows(task_dir)[-1]["phase"] == "stop"
    assert not (task_dir / "active-segment-role").exists()


def test_empty_response_post_is_treated_as_a_launch(tmp_path):
    task_dir = _setup(tmp_path)
    assert _spawn("pre", "planning", task_dir, tmp_path).returncode == 0
    assert _async_post(task_dir, tmp_path, response={}).returncode == 0
    assert (task_dir / "active-segment-role").read_text().strip() == "planning"
    assert _rows(task_dir)[-1]["async_launch"] is True


def test_parallel_async_planners_clear_after_the_last_stop(tmp_path):
    import agent_spawn_log

    task_dir = _setup(tmp_path)
    for prompt, agent_id in (("worker-a", "agent-a"), ("worker-b", "agent-b")):
        assert _spawn("pre", "planning", task_dir, tmp_path, prompt=prompt).returncode == 0
        launch = {"status": "async_launched", "agentId": agent_id}
        assert _async_post(task_dir, tmp_path, prompt=prompt, response=launch).returncode == 0
    assert _subagent_stop(task_dir, tmp_path, "agent-a").returncode == 0
    assert (task_dir / "active-segment-role").read_text().strip() == "planning"
    assert agent_spawn_log._planner_spawns_in_flight(task_dir / "spawn-log.jsonl") == 1
    assert _subagent_stop(task_dir, tmp_path, "agent-b").returncode == 0
    assert not (task_dir / "active-segment-role").exists()


def test_foreground_planner_stop_before_post_does_not_clear(tmp_path):
    # SubagentStop for a foreground planner can land before its post row; the
    # pre is still unmatched then, so the stop must leave the stamp alone.
    task_dir = _setup(tmp_path)
    assert _spawn("pre", "planning", task_dir, tmp_path).returncode == 0
    assert _subagent_stop(task_dir, tmp_path).returncode == 0
    assert (task_dir / "active-segment-role").read_text().strip() == "planning"
    assert _spawn("post", "planning", task_dir, tmp_path).returncode == 0
    assert not (task_dir / "active-segment-role").exists()
