from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))
ROUTER = ROOT / "hooks" / "router.py"

from lib_validate import check_segment_ownership  # noqa: E402


def _task_dir(tmp_path: Path) -> Path:
    task_dir = tmp_path / ".dynos" / "task-20260421-003"
    task_dir.mkdir(parents=True)
    (task_dir / "manifest.json").write_text(
        json.dumps(
            {
                "task_id": task_dir.name,
                "created_at": "2026-04-21T00:00:00Z",
                "raw_input": "test task",
                "stage": "EXECUTION",
                "classification": {
                    "type": "feature",
                    "domains": ["backend"],
                    "risk_level": "medium",
                },
            }
        )
        + "\n"
    )
    (task_dir / "execution-graph.json").write_text(
        json.dumps(
            {
                "task_id": task_dir.name,
                "segments": [
                    {
                        "id": "seg-1",
                        "executor": "backend-executor",
                        "description": "Implement",
                        "files_expected": ["src/a.py"],
                        "depends_on": [],
                        "criteria_ids": [1],
                    }
                ],
            }
        )
        + "\n"
    )
    return task_dir


def test_check_segment_ownership_denies_manifest_write(tmp_path: Path) -> None:
    task_dir = _task_dir(tmp_path)
    violations = check_segment_ownership(task_dir, "seg-1", ["manifest.json"])
    assert any("manifest.json" in v and "control-plane" in v for v in violations)


def test_check_segment_ownership_denies_receipt_write(tmp_path: Path) -> None:
    task_dir = _task_dir(tmp_path)
    violations = check_segment_ownership(task_dir, "seg-1", ["receipts/audit.json"])
    assert any("receipts/audit.json" in v and "control-plane" in v for v in violations)


def test_check_segment_ownership_allows_evidence_file(tmp_path: Path) -> None:
    task_dir = _task_dir(tmp_path)
    violations = check_segment_ownership(task_dir, "seg-1", ["evidence/seg-1.md"])
    assert violations == []


def test_router_sidecar_write_still_succeeds_under_policy(tmp_path: Path) -> None:
    project = tmp_path / "project"
    task_dir = project / ".dynos" / "task-20260421-004"
    task_dir.mkdir(parents=True)
    graph_path = task_dir / "execution-graph.json"
    graph_path.write_text(
        json.dumps(
            {
                "segments": [
                    {"id": "seg-1", "executor": "backend-executor", "files_expected": ["a.py"]},
                ]
            }
        )
    )
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "hooks"),
        "DYNOS_HOME": str(project / ".dynos-home"),
    }
    result = subprocess.run(
        [
            sys.executable,
            str(ROUTER),
            "inject-prompt",
            "--root",
            str(project),
            "--task-type",
            "feature",
            "--graph",
            str(graph_path),
            "--segment-id",
            "seg-1",
        ],
        input="BASE PROMPT",
        text=True,
        capture_output=True,
        check=False,
        env=env,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stderr
    assert (task_dir / "receipts" / "_injected-prompts" / "seg-1.sha256").exists()
