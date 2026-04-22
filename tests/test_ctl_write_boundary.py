#!/usr/bin/env python3

from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _task_dir(tmp_path: Path) -> Path:
    task_dir = tmp_path / ".dynos" / "task-20260421-002"
    task_dir.mkdir(parents=True)
    (task_dir / "manifest.json").write_text(
        json.dumps(
            {
                "task_id": task_dir.name,
                "created_at": "2026-04-21T00:00:00Z",
                "raw_input": "Implement write boundary wrappers",
                "stage": "PLAN_AUDIT",
                "classification": {
                    "type": "feature",
                    "domains": ["backend"],
                    "risk_level": "medium",
                },
            },
            indent=2,
        )
        + "\n"
    )
    (task_dir / "spec.md").write_text(
        "# Normalized Spec\n\n"
        "## Task Summary\nA\n\n"
        "## User Context\nB\n\n"
        "## Acceptance Criteria\n1. One\n2. Two\n\n"
        "## Implicit Requirements Surfaced\nC\n\n"
        "## Out of Scope\nD\n\n"
        "## Assumptions\nE\n\n"
        "## Risk Notes\nF\n"
    )
    (task_dir / "plan.md").write_text(
        "# Implementation Plan\n\n"
        "## Technical Approach\nA\n\n"
        "## Reference Code\nB\n\n"
        "## Components / Modules\nC\n\n"
        "## API Contracts\nD\n\n"
        "## Data Flow\nE\n\n"
        "## Error Handling Strategy\nF\n\n"
        "## Test Strategy\nG\n\n"
        "## Dependency Graph\nH\n\n"
        "## Open Questions\nI\n"
    )
    return task_dir


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(ROOT / "hooks" / "ctl.py"), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_write_execution_graph_normalizes_and_persists(tmp_path: Path) -> None:
    task_dir = _task_dir(tmp_path)
    payload_path = tmp_path / "graph.json"
    payload_path.write_text(
        json.dumps(
            {
                "task_id": "wrong-task-id",
                "segments": [
                    {
                        "id": "seg-1",
                        "executor": "backend-executor",
                        "description": "Do work",
                        "files_expected": ["src/a.py", "src/a.py"],
                        "depends_on": [],
                        "parallelizable": True,
                        "criteria_ids": ["1", 2, 2],
                    }
                ],
            }
        )
    )
    result = _run("write-execution-graph", str(task_dir), "--from", str(payload_path))
    assert result.returncode == 0, result.stdout + result.stderr
    graph = json.loads((task_dir / "execution-graph.json").read_text())
    assert graph["task_id"] == task_dir.name
    assert graph["segments"][0]["files_expected"] == ["src/a.py"]
    assert graph["segments"][0]["criteria_ids"] == [1, 2]


def test_write_repair_log_normalizes_affected_files(tmp_path: Path) -> None:
    task_dir = _task_dir(tmp_path)
    reports = task_dir / "audit-reports"
    reports.mkdir()
    (reports / "security-auditor.json").write_text(
        json.dumps(
            {
                "auditor_name": "security-auditor",
                "findings": [
                    {"id": "SEC-1", "blocking": True, "severity": "high"},
                ],
            }
        )
    )
    payload_path = tmp_path / "repair.json"
    payload_path.write_text(
        json.dumps(
            {
                "repair_cycle": 1,
                "batches": [
                    {
                        "batch_id": "batch-1",
                        "parallel": False,
                        "tasks": [
                            {
                                "finding_id": "SEC-1",
                                "auditor": "security-auditor",
                                "severity": "high",
                                "instruction": "Fix it",
                                "assigned_executor": "backend-executor",
                                "files_to_modify": ["src/a.py", "src/a.py"],
                            }
                        ],
                    }
                ],
            }
        )
    )
    result = _run("write-repair-log", str(task_dir), "--from", str(payload_path))
    assert result.returncode == 0, result.stdout + result.stderr
    repair = json.loads((task_dir / "repair-log.json").read_text())
    task = repair["batches"][0]["tasks"][0]
    assert repair["task_id"] == task_dir.name
    assert task["affected_files"] == ["src/a.py"]
    assert task["retry_count"] == 0
    assert task["max_retries"] == 3
    assert task["status"] == "pending"
