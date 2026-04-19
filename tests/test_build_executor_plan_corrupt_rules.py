"""Tests for executor-plan CLI exit-code on corrupt rules (AC 19).

The router CLI dispatcher cmd_executor_plan catches the corrupt-rules
exception that propagates from build_executor_plan -> load_prevention_rules
and exits 2 with a JSON error on stderr. The corrupt event is also emitted.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "hooks" / "router.py"


def _setup(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Returns (root, graph_path, dynos_home)."""
    root = tmp_path / "project"
    (root / ".dynos").mkdir(parents=True)
    graph_path = root / ".dynos" / "execution-graph.json"
    graph_path.write_text(json.dumps({
        "segments": [
            {"id": "seg-1", "executor": "backend-executor"},
        ],
    }))
    home = tmp_path / "dynos-home"
    home.mkdir()
    return root, graph_path, home


def _run_executor_plan(root: Path, graph_path: Path, home: Path,
                       task_type: str = "backend") -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "DYNOS_HOME": str(home),
        "PYTHONPATH": str(ROOT / "hooks"),
    }
    return subprocess.run(
        [sys.executable, str(ROUTER), "executor-plan",
         "--root", str(root), "--task-type", task_type,
         "--graph", str(graph_path)],
        capture_output=True, text=True, check=False, env=env,
    )


def _persistent_dir_for(root: Path, home: Path) -> Path:
    """Compute the persistent project dir under `home` for `root`. Mirrors
    lib_core._persistent_project_dir but parameterised on home so the test
    process does not need to mutate its own DYNOS_HOME."""
    sys.path.insert(0, str(ROOT / "hooks"))
    # We need to run _persistent_project_dir under the same DYNOS_HOME
    # the subprocess will see, so do the resolution in a clean subprocess.
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, '" + str(ROOT / "hooks") + "'); "
         "from pathlib import Path; "
         "from lib_core import ensure_persistent_project_dir; "
         "p = ensure_persistent_project_dir(Path('" + str(root) + "')); "
         "print(p)"],
        env={**os.environ, "DYNOS_HOME": str(home)},
        capture_output=True, text=True, check=True,
    )
    return Path(proc.stdout.strip())


def test_corrupt_rules_makes_executor_plan_exit_2(tmp_path: Path):
    """AC 19: corrupt prevention-rules.json → cmd_executor_plan exit 2."""
    root, graph_path, home = _setup(tmp_path)
    pd = _persistent_dir_for(root, home)
    (pd / "prevention-rules.json").write_text("not json !!!")

    proc = _run_executor_plan(root, graph_path, home)
    assert proc.returncode == 2, f"expected exit 2, got {proc.returncode}: stdout={proc.stdout} stderr={proc.stderr}"
    # Stderr carries the JSON error
    assert "prevention-rules" in proc.stderr
    assert "corrupt" in proc.stderr.lower()


def test_corrupt_rules_emits_prevention_rules_corrupt_event(tmp_path: Path):
    """AC 19: the corrupt event is emitted as a side-effect of executor-plan."""
    root, graph_path, home = _setup(tmp_path)
    pd = _persistent_dir_for(root, home)
    (pd / "prevention-rules.json").write_text("malformed json {{{ ")

    _run_executor_plan(root, graph_path, home)

    events_path = root / ".dynos" / "events.jsonl"
    assert events_path.exists()
    events = [
        json.loads(line)
        for line in events_path.read_text().splitlines()
        if line.strip()
    ]
    corrupt = [e for e in events if e.get("event") == "prevention_rules_corrupt"]
    assert len(corrupt) >= 1


def test_valid_rules_makes_executor_plan_succeed(tmp_path: Path):
    """AC 19 control: valid rules → executor-plan returns 0."""
    root, graph_path, home = _setup(tmp_path)
    pd = _persistent_dir_for(root, home)
    (pd / "prevention-rules.json").write_text(json.dumps({"rules": []}))

    proc = _run_executor_plan(root, graph_path, home)
    assert proc.returncode == 0, f"expected 0, got {proc.returncode}: {proc.stderr}"
