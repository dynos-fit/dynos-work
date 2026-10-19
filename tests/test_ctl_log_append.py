"""`ctl log-append` is the sanctioned way for skills to write execution-log lines."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CTL = ROOT / "hooks" / "ctl.py"


def _task(tmp_path: Path) -> Path:
    task = tmp_path / ".dynos" / "task-20261019-001"
    task.mkdir(parents=True)
    (task / "manifest.json").write_text(json.dumps({"task_id": task.name, "stage": "PLANNING"}))
    (task / "execution-log.md").write_text("2026-10-19T00:00:00Z [INIT] created\n")
    return task


def _run(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CTL), *argv],
        text=True,
        capture_output=True,
        check=False,
    )


def test_log_append_writes_timestamped_tagged_line(tmp_path: Path) -> None:
    task = _task(tmp_path)
    result = _run("log-append", str(task), "--tag", "gate", "--message", "arch-design — no open questions")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "log_appended"
    lines = (task / "execution-log.md").read_text().splitlines()
    assert len(lines) == 2
    assert lines[-1].endswith("[GATE] arch-design — no open questions")
    assert lines[-1].split(" ", 1)[0].endswith("Z")  # ISO timestamp prefix


def test_log_append_refuses_stage_tag(tmp_path: Path) -> None:
    task = _task(tmp_path)
    result = _run("log-append", str(task), "--tag", "STAGE", "--message", "→ PLANNING")
    assert result.returncode == 1
    assert "transition_task" in result.stderr
    assert (task / "execution-log.md").read_text().count("\n") == 1


def test_log_append_refuses_unknown_tag_and_multiline(tmp_path: Path) -> None:
    task = _task(tmp_path)
    bad_tag = _run("log-append", str(task), "--tag", "WHATEVER", "--message", "x")
    assert bad_tag.returncode == 1 and "unknown tag" in bad_tag.stderr
    multiline = _run("log-append", str(task), "--tag", "NOTE", "--message", "line one\nline two")
    assert multiline.returncode == 1 and "single line" in multiline.stderr
    empty = _run("log-append", str(task), "--tag", "NOTE", "--message", "   ")
    assert empty.returncode == 1
    assert (task / "execution-log.md").read_text().count("\n") == 1


def test_log_append_requires_task_directory(tmp_path: Path) -> None:
    not_a_task = tmp_path / "elsewhere"
    not_a_task.mkdir()
    result = _run("log-append", str(not_a_task), "--tag", "NOTE", "--message", "x")
    assert result.returncode == 1
    assert not (not_a_task / "execution-log.md").exists()


def test_external_solution_gate_logs_its_own_line(tmp_path: Path) -> None:
    task = _task(tmp_path)
    (task / "manifest.json").write_text(json.dumps({
        "task_id": task.name,
        "stage": "SPEC_NORMALIZATION",
        "raw_input": "rename a helper in utils.py",
        "classification": {"type": "refactor", "domains": ["backend"], "risk_level": "low"},
    }))
    (task / "raw-input.md").write_text("rename a helper in utils.py\n")
    result = _run("run-external-solution-gate", str(task))
    assert result.returncode == 0, result.stderr
    log = (task / "execution-log.md").read_text()
    assert "[GATE] external-solution — recommended: false" in log
