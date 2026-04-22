"""Tests for `python3 hooks/router.py inject-prompt` sidecar (AC 13)."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "hooks" / "router.py"


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "project"
    task_dir = project / ".dynos" / "task-20260418-IP"
    task_dir.mkdir(parents=True)
    graph_path = task_dir / "execution-graph.json"
    graph_path.write_text(json.dumps({
        "segments": [
            {"id": "seg-1", "executor": "backend-executor", "files_expected": ["a.py"]},
        ]
    }))
    return project, graph_path


def _run(project: Path, graph: Path, segment: str, stdin: str = "BASE PROMPT") -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "hooks"),
        "DYNOS_HOME": str(project / ".dynos-home"),
    }
    return subprocess.run(
        [sys.executable, str(ROUTER), "inject-prompt",
         "--root", str(project), "--task-type", "feature",
         "--graph", str(graph), "--segment-id", segment],
        input=stdin, text=True, capture_output=True, check=False,
        env=env, cwd=str(ROOT),
    )


def test_sidecar_matches_stdout_bytes(tmp_path: Path):
    project, graph = _setup(tmp_path)
    r = _run(project, graph, "seg-1")
    assert r.returncode == 0, r.stderr
    sidecar = graph.parent / "receipts" / "_injected-prompts" / "seg-1.sha256"
    assert sidecar.exists()
    on_disk = sidecar.read_text().strip()
    expected = hashlib.sha256(r.stdout.encode("utf-8")).hexdigest()
    assert on_disk == expected


def test_sidecar_txt_companion_exists(tmp_path: Path):
    project, graph = _setup(tmp_path)
    r = _run(project, graph, "seg-1")
    assert r.returncode == 0, r.stderr
    txt = graph.parent / "receipts" / "_injected-prompts" / "seg-1.txt"
    assert txt.exists()
    assert txt.read_bytes() == r.stdout.encode("utf-8")


def test_sidecar_has_no_trailing_newline(tmp_path: Path):
    project, graph = _setup(tmp_path)
    r = _run(project, graph, "seg-1")
    assert r.returncode == 0, r.stderr
    sidecar = graph.parent / "receipts" / "_injected-prompts" / "seg-1.sha256"
    raw = sidecar.read_bytes()
    assert len(raw) == 64
    assert not raw.endswith(b"\n")


def test_retry_overwrites_sidecar(tmp_path: Path):
    project, graph = _setup(tmp_path)
    r1 = _run(project, graph, "seg-1", stdin="FIRST PROMPT")
    assert r1.returncode == 0
    sidecar = graph.parent / "receipts" / "_injected-prompts" / "seg-1.sha256"
    first = sidecar.read_text().strip()

    r2 = _run(project, graph, "seg-1", stdin="SECOND DIFFERENT PROMPT")
    assert r2.returncode == 0
    second = sidecar.read_text().strip()
    assert first != second
    expected = hashlib.sha256(r2.stdout.encode("utf-8")).hexdigest()
    assert second == expected
    # Only one .sha256 + one .txt should exist (no orphans)
    files = sorted(p.name for p in (graph.parent / "receipts" / "_injected-prompts").iterdir())
    assert files == ["seg-1.sha256", "seg-1.txt"]
