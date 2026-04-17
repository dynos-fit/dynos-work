#!/usr/bin/env python3

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DreamRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        (self.root / ".dynos").mkdir()
        task_dir = self.root / ".dynos" / "task-20260401-001"
        task_dir.mkdir()
        (task_dir / "task-retrospective.json").write_text(
            json.dumps(
                {
                    "task_id": "task-20260401-001",
                    "task_outcome": "DONE",
                    "task_type": "feature",
                    "task_domains": "backend,security",
                    "task_risk_level": "high",
                    "findings_by_auditor": {"security-auditor": 1},
                    "findings_by_category": {"sec": 1},
                    "executor_repair_frequency": {"backend-executor": 1},
                    "spec_review_iterations": 1,
                    "repair_cycle_count": 1,
                    "subagent_spawn_count": 9,
                    "wasted_spawns": 1,
                    "auditor_zero_finding_streaks": {"security-auditor": 0},
                    "executor_zero_repair_streak": 0,
                    "quality_score": 0.88,
                    "cost_score": 0.77,
                    "efficiency_score": 0.81,
                },
                indent=2,
            )
            + "\n"
        )
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text(
            "import os\n\n"
            "def handler(x):\n"
            "    if x:\n"
            "        return x\n"
            "    return None\n"
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def run_py(self, script: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(ROOT / "hooks" / script), *args],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_state_encoder_outputs_signature(self) -> None:
        result = subprocess.run(
            ["python3", str(ROOT / "sandbox" / "state.py"), "--root", str(self.root), "--target", str(self.root / "src")],
            cwd=ROOT, text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["version"], 1)
        self.assertGreaterEqual(payload["file_count"], 1)
        self.assertIn(".py", payload["dominant_languages"])

    def test_dream_runner_emits_design_certificates(self) -> None:
        subprocess.run(
            ["python3", str(ROOT / "hooks" / "trajectory.py"), "rebuild", "--root", str(self.root)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        options_path = self.root / "options.json"
        options_path.write_text(
            json.dumps(
                {
                    "task_id": "task-20260401-099",
                    "subtask": "authentication-layer",
                    "task_type": "feature",
                    "task_domains": ["backend", "security"],
                    "task_risk_level": "high",
                    "options": [
                        {
                            "id": "option-a",
                            "description": "Use a small auth middleware with minimal file changes.",
                            "files": ["src/auth.py", "src/app.py"],
                            "complexity": "medium",
                            "risk": "medium",
                        },
                        {
                            "id": "option-b",
                            "description": "Introduce a database migration and OAuth security gateway.",
                            "files": ["src/auth.py", "src/app.py", "src/db.py", "migrations/001.sql"],
                            "complexity": "hard",
                            "risk": "high",
                        },
                    ],
                }
            )
        )
        result = self.run_py("dream.py", str(options_path), "--root", str(self.root))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["search_strategy"]["algorithm"], "mcts-lite")
        self.assertEqual(len(payload["design_certificates"]), 2)
        scores = [item["score"] for item in payload["design_certificates"]]
        self.assertGreaterEqual(scores[0], scores[1])


if __name__ == "__main__":
    unittest.main()
