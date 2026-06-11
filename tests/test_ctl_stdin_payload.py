"""Tests for `--from -` stdin payloads on ctl write wrappers (D1).

The stdin form lets skills pipe an LLM-returned payload straight into the
wrapper via a heredoc instead of staging it at a raw filesystem path the
write policy would have to sanction. Validation, normalization, and the
atomic policy-checked write must be byte-identical to the file form.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

from test_ctl import _setup_task_dir  # noqa: E402


def _run_ctl_stdin(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(ROOT / "hooks" / "ctl.py"), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        input=stdin,
        env=os.environ.copy(),
        check=False,
    )


_CLASSIFICATION = {
    "type": "bugfix",
    "domains": ["backend", "security"],
    "risk_level": "low",
    "notes": "tighten auth path",
}


def test_write_classification_stdin_matches_file_form(tmp_path: Path) -> None:
    task_a = _setup_task_dir(tmp_path / "a")
    task_b = _setup_task_dir(tmp_path / "b")

    payload_path = tmp_path / "classification.json"
    payload_path.write_text(json.dumps(_CLASSIFICATION))
    via_file = _run_ctl_stdin("write-classification", str(task_a), "--from", str(payload_path))
    assert via_file.returncode == 0, via_file.stdout + via_file.stderr

    via_stdin = _run_ctl_stdin(
        "write-classification", str(task_b), "--from", "-",
        stdin=json.dumps(_CLASSIFICATION),
    )
    assert via_stdin.returncode == 0, via_stdin.stdout + via_stdin.stderr

    file_artifact = (task_a / "classification.json").read_text()
    stdin_artifact = (task_b / "classification.json").read_text()
    assert file_artifact == stdin_artifact


def test_write_classification_stdin_invalid_json_writes_nothing(tmp_path: Path) -> None:
    task_dir = _setup_task_dir(tmp_path)
    result = _run_ctl_stdin(
        "write-classification", str(task_dir), "--from", "-",
        stdin="{not json",
    )
    assert result.returncode != 0
    assert "not valid JSON" in (result.stderr + result.stdout)
    assert not (task_dir / "classification.json").exists()


def test_write_classification_stdin_still_validates_schema(tmp_path: Path) -> None:
    task_dir = _setup_task_dir(tmp_path)
    bad = dict(_CLASSIFICATION, domains=["backend", "unknown-domain"])
    result = _run_ctl_stdin(
        "write-classification", str(task_dir), "--from", "-",
        stdin=json.dumps(bad),
    )
    assert result.returncode == 1
    assert "classification domain invalid" in result.stderr
    assert not (task_dir / "classification.json").exists()


def test_write_execution_graph_stdin(tmp_path: Path) -> None:
    task_dir = _setup_task_dir(tmp_path)
    graph = {
        "segments": [
            {
                "id": "seg-1",
                "executor": "backend-executor",
                "files_expected": ["src/app.py"],
                "criteria_ids": [1],
                "depends_on": [],
            }
        ]
    }
    result = _run_ctl_stdin(
        "write-execution-graph", str(task_dir), "--from", "-",
        stdin=json.dumps(graph),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    persisted = json.loads((task_dir / "execution-graph.json").read_text())
    assert persisted["segments"][0]["id"] == "seg-1"
