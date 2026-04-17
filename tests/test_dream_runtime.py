#!/usr/bin/env python3

from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _setup_dream_env(tmp_path: Path) -> Path:
    root = tmp_path
    (root / ".dynos").mkdir()
    task_dir = root / ".dynos" / "task-20260401-001"
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
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text(
        "import os\n\n"
        "def handler(x):\n"
        "    if x:\n"
        "        return x\n"
        "    return None\n"
    )
    return root


def run_py(script: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(ROOT / "hooks" / script), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


class TestDreamRuntime:
    def test_state_encoder_outputs_signature(self, tmp_path: Path) -> None:
        root = _setup_dream_env(tmp_path)
        result = subprocess.run(
            ["python3", str(ROOT / "sandbox" / "state.py"), "--root", str(root), "--target", str(root / "src")],
            cwd=ROOT, text=True, capture_output=True, check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        payload = json.loads(result.stdout)
        assert payload["version"] == 1
        assert payload["file_count"] >= 1
        assert ".py" in payload["dominant_languages"]

    def test_dream_runner_emits_design_certificates(self, tmp_path: Path) -> None:
        root = _setup_dream_env(tmp_path)
        subprocess.run(
            ["python3", str(ROOT / "hooks" / "trajectory.py"), "rebuild", "--root", str(root)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        options_path = root / "options.json"
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
        result = run_py("dream.py", str(options_path), "--root", str(root))
        assert result.returncode == 0, result.stdout + result.stderr
        payload = json.loads(result.stdout)
        assert payload["search_strategy"]["algorithm"] == "mcts-lite"
        assert len(payload["design_certificates"]) == 2
        scores = [item["score"] for item in payload["design_certificates"]]
        assert scores[0] >= scores[1]
