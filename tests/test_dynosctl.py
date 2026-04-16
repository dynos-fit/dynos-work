#!/usr/bin/env python3

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DynosCtlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.task_dir = Path(self.tempdir.name) / ".dynos" / "task-20260403-001"
        self.task_dir.mkdir(parents=True)
        (self.task_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "task_id": "task-20260403-001",
                    "created_at": "2026-04-03T00:00:00Z",
                    "title": "Test task",
                    "raw_input": "Build a thing",
                    "stage": "PLAN_AUDIT",
                    "classification": {
                        "type": "feature",
                        "domains": ["backend"],
                        "risk_level": "medium",
                        "notes": "test",
                    },
                    "retry_counts": {},
                    "blocked_reason": None,
                    "completion_at": None,
                },
                indent=2,
            )
            + "\n"
        )
        (self.task_dir / "spec.md").write_text(
            "# Normalized Spec\n\n"
            "## Task Summary\nA.\n\n"
            "## User Context\nB.\n\n"
            "## Acceptance Criteria\n"
            "1. First criterion\n"
            "2. Second criterion\n\n"
            "## Implicit Requirements Surfaced\nC.\n\n"
            "## Out of Scope\nD.\n\n"
            "## Assumptions\nsafe assumption: none\n\n"
            "## Risk Notes\nE.\n"
        )
        (self.task_dir / "plan.md").write_text(
            "# Implementation Plan\n\n"
            "## Technical Approach\nA.\n\n"
            "## Reference Code\nB.\n\n"
            "## Components / Modules\nC.\n\n"
            "## API Contracts\nContracts.\n\n"
            "## Data Flow\nD.\n\n"
            "## Error Handling Strategy\nE.\n\n"
            "## Test Strategy\nF.\n\n"
            "## Dependency Graph\nG.\n\n"
            "## Open Questions\nH.\n"
        )
        (self.task_dir / "execution-graph.json").write_text(
            json.dumps(
                {
                    "task_id": "task-20260403-001",
                    "segments": [
                        {
                            "id": "seg-1",
                            "executor": "backend-executor",
                            "description": "Build backend",
                            "files_expected": ["src/a.py"],
                            "depends_on": [],
                            "parallelizable": True,
                            "criteria_ids": [1],
                        },
                        {
                            "id": "seg-2",
                            "executor": "testing-executor",
                            "description": "Build tests",
                            "files_expected": ["tests/test_a.py"],
                            "depends_on": ["seg-1"],
                            "parallelizable": True,
                            "criteria_ids": [2],
                        },
                    ],
                },
                indent=2,
            )
            + "\n"
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def run_ctl(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(ROOT / "hooks" / "ctl.py"), *args],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_validate_task_passes_for_valid_fixture(self) -> None:
        result = self.run_ctl("validate-task", str(self.task_dir), "--strict")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Validation passed", result.stdout)

    def test_validate_task_fails_for_invalid_criteria_mapping(self) -> None:
        graph_path = self.task_dir / "execution-graph.json"
        graph = json.loads(graph_path.read_text())
        graph["segments"][0]["criteria_ids"] = [3]
        graph_path.write_text(json.dumps(graph, indent=2) + "\n")
        result = self.run_ctl("validate-task", str(self.task_dir), "--strict")
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not exist in spec", result.stdout)

    def test_transition_rejects_illegal_stage_change(self) -> None:
        result = self.run_ctl("transition", str(self.task_dir), "EXECUTION")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Illegal stage transition", result.stderr)

    def test_transition_allows_legal_stage_change(self) -> None:
        result = self.run_ctl("transition", str(self.task_dir), "PRE_EXECUTION_SNAPSHOT")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        manifest = json.loads((self.task_dir / "manifest.json").read_text())
        self.assertEqual(manifest["stage"], "PRE_EXECUTION_SNAPSHOT")

    def test_check_ownership_fails_for_foreign_file(self) -> None:
        result = self.run_ctl(
            "check-ownership",
            str(self.task_dir),
            "seg-1",
            "src/a.py",
            "src/b.py",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("src/b.py", result.stdout)


if __name__ == "__main__":
    unittest.main()
