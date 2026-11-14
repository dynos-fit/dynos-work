"""Cursor harness tool-name aliases for pre_tool_use / agent_spawn_log."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = ROOT / "hooks"
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

import agent_spawn_log  # noqa: E402
import pre_tool_use  # noqa: E402


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("Bash", "Bash"),
        ("Shell", "Bash"),
        ("Write", "Write"),
        ("Edit", "Edit"),
        ("StrReplace", "Edit"),
        ("Task", "Agent"),
        ("Agent", "Agent"),
    ],
)
def test_canonical_tool_name(raw: str, canonical: str) -> None:
    assert pre_tool_use._canonical_tool_name(raw) == canonical


def test_resolve_hook_cwd_prefers_cursor_project_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    monkeypatch.setenv("CURSOR_PROJECT_DIR", str(project))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    resolved = pre_tool_use._resolve_hook_cwd({"cwd": ""})
    assert resolved == project.resolve()


def test_agent_spawn_log_accepts_task_tool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    task_dir = tmp_path / ".dynos" / "task-20261113-001"
    task_dir.mkdir(parents=True)
    (task_dir / "manifest.json").write_text(
        json.dumps({"stage": "EXECUTION"}), encoding="utf-8"
    )
    monkeypatch.setenv("CURSOR_PROJECT_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    payload = {
        "tool_name": "Task",
        "tool_input": {
            "subagent_type": "dynos-work:planning",
            "description": "plan",
            "prompt": "do planning",
        },
        "cwd": str(tmp_path),
    }
    monkeypatch.setattr(
        "sys.stdin",
        type("S", (), {"read": staticmethod(lambda: json.dumps(payload))})(),
    )
    rc = agent_spawn_log.main(["agent_spawn_log.py", "pre"])
    assert rc == 0
    log_path = task_dir / "spawn-log.jsonl"
    assert log_path.exists()
    entry = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert entry["tool"] == "Agent"
    assert entry["subagent_type"] == "dynos-work:planning"
    _ = capsys.readouterr()
