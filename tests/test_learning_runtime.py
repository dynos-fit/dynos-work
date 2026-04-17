#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LearningRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        (self.root / ".dynos").mkdir()
        # Redirect persistent state to temp dir
        self.dynos_home = self.root / ".dynos-home"
        self.dynos_home.mkdir()
        self._orig_dynos_home = os.environ.get("DYNOS_HOME")
        os.environ["DYNOS_HOME"] = str(self.dynos_home)
        self.make_task(
            "task-20260401-001",
            {
                "task_id": "task-20260401-001",
                "task_outcome": "DONE",
                "task_type": "feature",
                "task_domains": "backend",
                "task_risk_level": "high",
                "findings_by_auditor": {"security-auditor": 1},
                "findings_by_category": {"sec": 1},
                "executor_repair_frequency": {"backend-executor": 1},
                "spec_review_iterations": 1,
                "repair_cycle_count": 1,
                "subagent_spawn_count": 10,
                "wasted_spawns": 2,
                "auditor_zero_finding_streaks": {"security-auditor": 0},
                "executor_zero_repair_streak": 0,
                "quality_score": 0.8,
                "cost_score": 0.7,
                "efficiency_score": 0.6,
            },
        )
        self.make_task(
            "task-20260401-002",
            {
                "task_id": "task-20260401-002",
                "task_outcome": "DONE",
                "task_type": "refactor",
                "task_domains": "backend",
                "task_risk_level": "medium",
                "findings_by_auditor": {"code-quality-auditor": 2},
                "findings_by_category": {"cq": 2},
                "executor_repair_frequency": {"refactor-executor": 1},
                "spec_review_iterations": 1,
                "repair_cycle_count": 0,
                "subagent_spawn_count": 8,
                "wasted_spawns": 1,
                "auditor_zero_finding_streaks": {"code-quality-auditor": 0},
                "executor_zero_repair_streak": 0,
                "quality_score": 0.9,
                "cost_score": 0.8,
                "efficiency_score": 0.9,
            },
        )

    def tearDown(self) -> None:
        if self._orig_dynos_home is None:
            os.environ.pop("DYNOS_HOME", None)
        else:
            os.environ["DYNOS_HOME"] = self._orig_dynos_home
        self.tempdir.cleanup()

    def make_task(self, task_id: str, retrospective: dict) -> None:
        task_dir = self.root / ".dynos" / task_id
        task_dir.mkdir(parents=True)
        (task_dir / "task-retrospective.json").write_text(json.dumps(retrospective, indent=2) + "\n")

    @property
    def persistent_dir(self) -> Path:
        """The persistent project dir under DYNOS_HOME for the temp root."""
        slug = str(self.root.resolve()).strip("/").replace("/", "-")
        return self.dynos_home / "projects" / slug

    def run_py(self, script: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(ROOT / "hooks" / script), *args],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            env={**os.environ, "DYNOS_HOME": str(self.dynos_home)},
        )

    def test_rebuild_trajectory_store_and_search(self) -> None:
        rebuild = self.run_py("trajectory.py", "rebuild", "--root", str(self.root))
        self.assertEqual(rebuild.returncode, 0, rebuild.stdout + rebuild.stderr)
        query_path = self.root / "query.json"
        query_path.write_text(
            json.dumps(
                {
                    "task_type": "feature",
                    "task_domains": ["backend"],
                    "task_risk_level": "high",
                    "repair_cycle_count": 1,
                    "subagent_spawn_count": 10,
                    "wasted_spawns": 2,
                    "spec_review_iterations": 1,
                }
            )
        )
        search = self.run_py("trajectory.py", "search", str(query_path), "--root", str(self.root), "--limit", "1")
        self.assertEqual(search.returncode, 0, search.stdout + search.stderr)
        results = json.loads(search.stdout)
        self.assertEqual(results[0]["trajectory"]["source_task_id"], "task-20260401-001")

    def test_register_and_promote_learned_agent(self) -> None:
        init = self.run_py("calibrate.py", "init-registry", "--root", str(self.root))
        self.assertEqual(init.returncode, 0)
        register = self.run_py(
            "calibrate.py",
            "register-agent",
            "backend-sharp",
            "backend-executor",
            "feature",
            ".dynos/learned-agents/executors/backend-sharp.md",
            "task-20260401-002",
            "--root",
            str(self.root),
        )
        self.assertEqual(register.returncode, 0, register.stdout + register.stderr)

        candidate_path = self.root / "candidate.json"
        baseline_path = self.root / "baseline.json"
        candidate_path.write_text(
            json.dumps(
                [
                    {"quality_score": 0.95, "cost_score": 0.75, "efficiency_score": 0.9},
                    {"quality_score": 0.93, "cost_score": 0.8, "efficiency_score": 0.88},
                    {"quality_score": 0.94, "cost_score": 0.78, "efficiency_score": 0.9},
                ]
            )
        )
        baseline_path.write_text(
            json.dumps(
                [
                    {"quality_score": 0.88, "cost_score": 0.8, "efficiency_score": 0.82},
                    {"quality_score": 0.87, "cost_score": 0.82, "efficiency_score": 0.81},
                    {"quality_score": 0.89, "cost_score": 0.79, "efficiency_score": 0.83},
                ]
            )
        )

        promote = self.run_py(
            "eval.py",
            "promote",
            "backend-sharp",
            "backend-executor",
            "feature",
            str(candidate_path),
            str(baseline_path),
            "--root",
            str(self.root),
        )
        self.assertEqual(promote.returncode, 0, promote.stdout + promote.stderr)
        output = json.loads(promote.stdout)
        self.assertEqual(output["target_mode"], "replace")

        registry = json.loads((self.persistent_dir / "learned-agents" / "registry.json").read_text())
        agent = registry["agents"][0]
        self.assertEqual(agent["mode"], "replace")
        self.assertEqual(agent["last_evaluation"]["recommendation"], "promote_replace")

    def test_skill_shadow_mode_and_benchmark_runner(self) -> None:
        self.run_py("calibrate.py", "init-registry", "--root", str(self.root))
        register = self.run_py(
            "calibrate.py",
            "register-agent",
            "plan-tightener",
            "plan-skill",
            "feature",
            ".dynos/learned-agents/skills/plan-tightener.md",
            "task-20260401-002",
            "--item-kind",
            "skill",
            "--root",
            str(self.root),
        )
        self.assertEqual(register.returncode, 0, register.stdout + register.stderr)
        fixture_path = self.root / "fixture.json"
        fixture_path.write_text(
            json.dumps(
                {
                    "fixture_id": "skill-case",
                    "item_kind": "skill",
                    "target_name": "plan-tightener",
                    "role": "plan-skill",
                    "task_type": "feature",
                    "cases": [
                        {
                            "case_id": "1",
                            "baseline": {
                                "tests_passed": 8,
                                "tests_total": 10,
                                "findings": 2,
                                "files_touched": 5,
                                "duration_seconds": 120,
                                "tokens_used": 26000,
                            },
                            "candidate": {
                                "tests_passed": 10,
                                "tests_total": 10,
                                "findings": 1,
                                "files_touched": 4,
                                "duration_seconds": 100,
                                "tokens_used": 21000,
                            },
                        },
                        {
                            "case_id": "2",
                            "baseline": {
                                "tests_passed": 7,
                                "tests_total": 10,
                                "findings": 3,
                                "files_touched": 6,
                                "duration_seconds": 150,
                                "tokens_used": 31000,
                            },
                            "candidate": {
                                "tests_passed": 10,
                                "tests_total": 10,
                                "findings": 1,
                                "files_touched": 5,
                                "duration_seconds": 120,
                                "tokens_used": 24000,
                            },
                        },
                        {
                            "case_id": "3",
                            "baseline": {
                                "tests_passed": 8,
                                "tests_total": 10,
                                "findings": 2,
                                "files_touched": 5,
                                "duration_seconds": 130,
                                "tokens_used": 28000,
                            },
                            "candidate": {
                                "tests_passed": 10,
                                "tests_total": 10,
                                "findings": 1,
                                "files_touched": 4,
                                "duration_seconds": 110,
                                "tokens_used": 22000,
                            },
                        },
                    ],
                }
            )
        )
        bench = self.run_py("bench.py", "run", str(fixture_path), "--root", str(self.root), "--update-registry")
        self.assertEqual(bench.returncode, 0, bench.stdout + bench.stderr)
        payload = json.loads(bench.stdout)
        self.assertEqual(payload["item_kind"], "skill")
        self.assertEqual(payload["evaluation"]["target_mode"], "replace")
        registry = json.loads((self.persistent_dir / "learned-agents" / "registry.json").read_text())
        skill = registry["agents"][0]
        self.assertEqual(skill["item_kind"], "skill")
        self.assertEqual(skill["mode"], "replace")
        history = json.loads((self.persistent_dir / "benchmarks" / "history.json").read_text())
        self.assertEqual(len(history["runs"]), 1)

    def test_sandbox_benchmark_runner_executes_commands(self) -> None:
        fixture_path = self.root / "sandbox-fixture.json"
        fixture_path.write_text(
            json.dumps(
                {
                    "fixture_id": "sandbox-case",
                    "item_kind": "agent",
                    "target_name": "backend-sharp",
                    "role": "backend-executor",
                    "task_type": "feature",
                    "cases": [
                        {
                            "case_id": "sandbox-1",
                            "sandbox": {
                                "files": {
                                    "runner.py": (
                                        "import json, pathlib, sys\n"
                                        "mode = sys.argv[1]\n"
                                        "path = pathlib.Path('state.txt')\n"
                                        "if mode == 'baseline':\n"
                                        "    path.write_text('baseline')\n"
                                        "    print(json.dumps({'tests_passed': 8, 'tests_total': 10, 'findings': 2, 'tokens_used': 32000}))\n"
                                        "else:\n"
                                        "    path.write_text('candidate')\n"
                                        "    print(json.dumps({'tests_passed': 10, 'tests_total': 10, 'findings': 0, 'tokens_used': 21000}))\n"
                                    )
                                }
                            },
                            "baseline": {"command": ["python3", "runner.py", "baseline"]},
                            "candidate": {"command": ["python3", "runner.py", "candidate"]}
                        }
                    ]
                }
            )
        )
        bench = self.run_py("bench.py", "run", str(fixture_path), "--root", str(self.root))
        self.assertEqual(bench.returncode, 0, bench.stdout + bench.stderr)
        payload = json.loads(bench.stdout)
        self.assertEqual(payload["cases"][0]["execution_mode"], "sandbox")
        self.assertEqual(payload["cases"][0]["candidate_observed"]["tests_passed"], 10)
        self.assertGreaterEqual(payload["cases"][0]["candidate_observed"]["files_touched"], 1)

    def test_task_fixture_runner_supports_command_sequences(self) -> None:
        fixture_path = self.root / "task-fixture.json"
        fixture_path.write_text(
            json.dumps(
                {
                    "fixture_id": "task-fixture",
                    "item_kind": "agent",
                    "target_name": "backend-sharp",
                    "role": "backend-executor",
                    "task_type": "feature",
                    "cases": [
                        {
                            "case_id": "task-1",
                            "sandbox": {
                                "files": {
                                    "app.py": "def compute(x):\n    raise NotImplementedError()\n",
                                    "run_variant.py": (
                                        "import pathlib, sys\n"
                                        "variant = sys.argv[1]\n"
                                        "path = pathlib.Path('app.py')\n"
                                        "if variant == 'baseline':\n"
                                        "    path.write_text(\"def compute(x):\\n    return x\\n\")\n"
                                        "else:\n"
                                        "    path.write_text(\"def compute(x):\\n    return x * 2\\n\")\n"
                                    ),
                                    "verify.py": (
                                        "import json\n"
                                        "from app import compute\n"
                                        "value = compute(2)\n"
                                        "if value == 4:\n"
                                        "    print(json.dumps({'tests_passed': 3, 'tests_total': 3, 'findings': 0, 'tokens_used': 18000}))\n"
                                        "else:\n"
                                        "    print(json.dumps({'tests_passed': 1, 'tests_total': 3, 'findings': 2, 'tokens_used': 18000}))\n"
                                    ),
                                }
                            },
                            "baseline": {
                                "commands": [
                                    ["python3", "run_variant.py", "baseline"],
                                    ["python3", "verify.py"]
                                ]
                            },
                            "candidate": {
                                "commands": [
                                    ["python3", "run_variant.py", "candidate"],
                                    ["python3", "verify.py"]
                                ]
                            }
                        }
                    ]
                }
            )
        )
        bench = self.run_py("bench.py", "run", str(fixture_path), "--root", str(self.root))
        self.assertEqual(bench.returncode, 0, bench.stdout + bench.stderr)
        payload = json.loads(bench.stdout)
        self.assertEqual(payload["cases"][0]["candidate_observed"]["tests_passed"], 3)
        self.assertEqual(payload["cases"][0]["baseline_observed"]["tests_passed"], 1)
        self.assertEqual(payload["cases"][0]["winner"], "candidate")

    def test_repo_rollout_harness_copies_repo_paths(self) -> None:
        (self.root / "README.md").write_text("Repo fixture\n")
        fixture_path = self.root / "rollout-fixture.json"
        fixture_path.write_text(
            json.dumps(
                {
                    "fixture_id": "repo-rollout",
                    "item_kind": "agent",
                    "target_name": "backend-sharp",
                    "role": "backend-executor",
                    "task_type": "feature",
                    "cases": [
                        {
                            "case_id": "rollout-1",
                            "sandbox": {
                                "copy_repo_paths": ["README.md"],
                                "files": {
                                    "mutate.py": (
                                        "import pathlib, sys\n"
                                        "mode = sys.argv[1]\n"
                                        "path = pathlib.Path('README.md')\n"
                                        "text = path.read_text()\n"
                                        "if mode == 'baseline':\n"
                                        "    path.write_text(text + '\\nBaseline marker\\n')\n"
                                        "else:\n"
                                        "    path.write_text(text + '\\nCandidate marker\\n')\n"
                                    ),
                                    "verify.py": (
                                        "import json, pathlib\n"
                                        "text = pathlib.Path('README.md').read_text()\n"
                                        "if 'Candidate marker' in text:\n"
                                        "    print(json.dumps({'tests_passed': 2, 'tests_total': 2, 'findings': 0, 'tokens_used': 15000}))\n"
                                        "elif 'Baseline marker' in text:\n"
                                        "    print(json.dumps({'tests_passed': 1, 'tests_total': 2, 'findings': 1, 'tokens_used': 15000}))\n"
                                        "else:\n"
                                        "    print(json.dumps({'tests_passed': 0, 'tests_total': 2, 'findings': 2, 'tokens_used': 15000}))\n"
                                    ),
                                }
                            },
                            "baseline": {
                                "commands": [
                                    ["python3", "mutate.py", "baseline"],
                                    ["python3", "verify.py"]
                                ]
                            },
                            "candidate": {
                                "commands": [
                                    ["python3", "mutate.py", "candidate"],
                                    ["python3", "verify.py"]
                                ]
                            }
                        }
                    ]
                }
            )
        )
        rollout = self.run_py("rollout.py", str(fixture_path), "--root", str(self.root))
        self.assertEqual(rollout.returncode, 0, rollout.stdout + rollout.stderr)
        payload = json.loads(rollout.stdout)
        self.assertEqual(payload["execution_harness"], "rollout")
        self.assertEqual(payload["cases"][0]["candidate_observed"]["tests_passed"], 2)
        self.assertEqual(payload["cases"][0]["baseline_observed"]["tests_passed"], 1)

    def test_must_pass_category_blocks_promotion_and_demotes_active_component(self) -> None:
        self.run_py("calibrate.py", "init-registry", "--root", str(self.root))
        register = self.run_py(
            "calibrate.py",
            "register-agent",
            "security-hardener",
            "security-auditor",
            "feature",
            ".dynos/learned-agents/auditors/security-hardener.md",
            "task-20260401-002",
            "--root",
            str(self.root),
        )
        self.assertEqual(register.returncode, 0, register.stdout + register.stderr)

        candidate_path = self.root / "candidate-regress.json"
        baseline_path = self.root / "baseline-regress.json"
        candidate_path.write_text(
            json.dumps(
                [
                    {"category": "security", "quality_score": 0.7, "cost_score": 0.9, "efficiency_score": 0.9},
                    {"category": "quality", "quality_score": 0.98, "cost_score": 0.9, "efficiency_score": 0.95},
                    {"category": "quality", "quality_score": 0.97, "cost_score": 0.9, "efficiency_score": 0.94}
                ]
            )
        )
        baseline_path.write_text(
            json.dumps(
                [
                    {"category": "security", "quality_score": 0.9, "cost_score": 0.8, "efficiency_score": 0.85},
                    {"category": "quality", "quality_score": 0.85, "cost_score": 0.8, "efficiency_score": 0.82},
                    {"category": "quality", "quality_score": 0.84, "cost_score": 0.8, "efficiency_score": 0.81}
                ]
            )
        )

        promote = self.run_py(
            "eval.py",
            "promote",
            "security-hardener",
            "security-auditor",
            "feature",
            str(candidate_path),
            str(baseline_path),
            "--root",
            str(self.root),
        )
        self.assertEqual(promote.returncode, 0, promote.stdout + promote.stderr)
        registry = json.loads((self.persistent_dir / "learned-agents" / "registry.json").read_text())
        agent = registry["agents"][0]
        self.assertIn(agent["mode"], {"alongside", "replace"})
        self.assertTrue(agent["route_allowed"])

        fixture_path = self.root / "must-pass-fixture.json"
        fixture_path.write_text(
            json.dumps(
                {
                    "fixture_id": "must-pass-case",
                    "item_kind": "agent",
                    "target_name": "security-hardener",
                    "role": "security-auditor",
                    "task_type": "feature",
                    "policy": {
                        "min_samples": 3,
                        "min_quality_delta": 0.03,
                        "min_composite_delta": 0.02,
                        "must_pass_categories": ["security"]
                    },
                    "cases": [
                        {
                            "case_id": "sec-1",
                            "category": "security",
                            "baseline": {
                                "tests_passed": 10, "tests_total": 10, "findings": 0, "files_touched": 3, "duration_seconds": 100, "tokens_used": 22000
                            },
                            "candidate": {
                                "tests_passed": 7, "tests_total": 10, "findings": 1, "files_touched": 3, "duration_seconds": 90, "tokens_used": 21000
                            }
                        },
                        {
                            "case_id": "qual-1",
                            "category": "quality",
                            "baseline": {
                                "tests_passed": 7, "tests_total": 10, "findings": 2, "files_touched": 5, "duration_seconds": 160, "tokens_used": 32000
                            },
                            "candidate": {
                                "tests_passed": 10, "tests_total": 10, "findings": 0, "files_touched": 4, "duration_seconds": 120, "tokens_used": 23000
                            }
                        },
                        {
                            "case_id": "qual-2",
                            "category": "quality",
                            "baseline": {
                                "tests_passed": 7, "tests_total": 10, "findings": 2, "files_touched": 5, "duration_seconds": 170, "tokens_used": 33000
                            },
                            "candidate": {
                                "tests_passed": 10, "tests_total": 10, "findings": 0, "files_touched": 4, "duration_seconds": 125, "tokens_used": 24000
                            }
                        }
                    ]
                }
            )
        )
        bench = self.run_py("bench.py", "run", str(fixture_path), "--root", str(self.root), "--update-registry")
        self.assertEqual(bench.returncode, 0, bench.stdout + bench.stderr)
        payload = json.loads(bench.stdout)
        self.assertTrue(payload["evaluation"]["blocked_by_category"])
        self.assertEqual(payload["evaluation"]["recommendation"], "reject")
        registry = json.loads((self.persistent_dir / "learned-agents" / "registry.json").read_text())
        agent = registry["agents"][0]
        self.assertEqual(agent["status"], "demoted_on_regression")
        self.assertFalse(agent["route_allowed"])

    def test_route_resolution_prefers_allowed_highest_composite(self) -> None:
        self.run_py("calibrate.py", "init-registry", "--root", str(self.root))
        self.run_py(
            "calibrate.py",
            "register-agent",
            "backend-sharp",
            "backend-executor",
            "feature",
            ".dynos/learned-agents/executors/backend-sharp.md",
            "task-20260401-001",
            "--root",
            str(self.root),
        )
        self.run_py(
            "calibrate.py",
            "register-agent",
            "backend-steady",
            "backend-executor",
            "feature",
            ".dynos/learned-agents/executors/backend-steady.md",
            "task-20260401-002",
            "--root",
            str(self.root),
        )
        low_candidate = self.root / "low.json"
        high_candidate = self.root / "high.json"
        baseline = self.root / "baseline.json"
        low_candidate.write_text(
            json.dumps(
                [
                    {"quality_score": 0.90, "cost_score": 0.8, "efficiency_score": 0.85},
                    {"quality_score": 0.91, "cost_score": 0.8, "efficiency_score": 0.84},
                    {"quality_score": 0.90, "cost_score": 0.79, "efficiency_score": 0.85},
                ]
            )
        )
        high_candidate.write_text(
            json.dumps(
                [
                    {"quality_score": 0.95, "cost_score": 0.82, "efficiency_score": 0.9},
                    {"quality_score": 0.94, "cost_score": 0.81, "efficiency_score": 0.9},
                    {"quality_score": 0.95, "cost_score": 0.8, "efficiency_score": 0.89},
                ]
            )
        )
        baseline.write_text(
            json.dumps(
                [
                    {"quality_score": 0.84, "cost_score": 0.8, "efficiency_score": 0.8},
                    {"quality_score": 0.83, "cost_score": 0.79, "efficiency_score": 0.8},
                    {"quality_score": 0.84, "cost_score": 0.8, "efficiency_score": 0.79},
                ]
            )
        )
        self.run_py(
            "eval.py",
            "promote",
            "backend-steady",
            "backend-executor",
            "feature",
            str(low_candidate),
            str(baseline),
            "--root",
            str(self.root),
        )
        self.run_py(
            "eval.py",
            "promote",
            "backend-sharp",
            "backend-executor",
            "feature",
            str(high_candidate),
            str(baseline),
            "--root",
            str(self.root),
        )
        route = self.run_py(
            "route.py",
            "backend-executor",
            "feature",
            "--root",
            str(self.root),
        )
        self.assertEqual(route.returncode, 0, route.stdout + route.stderr)
        payload = json.loads(route.stdout)
        self.assertEqual(payload["source"], "learned:backend-sharp")
        self.assertTrue(payload["route_allowed"])

    def test_auto_runner_executes_matching_shadow_fixture_and_promotes(self) -> None:
        self.run_py("calibrate.py", "init-registry", "--root", str(self.root))
        register = self.run_py(
            "calibrate.py",
            "register-agent",
            "backend-shadow",
            "backend-executor",
            "feature",
            ".dynos/learned-agents/executors/backend-shadow.md",
            "task-20260401-002",
            "--root",
            str(self.root),
        )
        self.assertEqual(register.returncode, 0, register.stdout + register.stderr)
        fixtures_dir = self.root / "benchmarks" / "fixtures"
        fixtures_dir.mkdir(parents=True)
        fixture_path = fixtures_dir / "backend-shadow.json"
        fixture_path.write_text(
            json.dumps(
                {
                    "fixture_id": "backend-shadow-case",
                    "item_kind": "agent",
                    "target_name": "backend-shadow",
                    "role": "backend-executor",
                    "task_type": "feature",
                    "cases": [
                        {
                            "case_id": "1",
                            "baseline": {
                                "tests_passed": 8,
                                "tests_total": 10,
                                "findings": 2,
                                "files_touched": 5,
                                "duration_seconds": 120,
                                "tokens_used": 26000,
                            },
                            "candidate": {
                                "tests_passed": 10,
                                "tests_total": 10,
                                "findings": 0,
                                "files_touched": 4,
                                "duration_seconds": 100,
                                "tokens_used": 21000,
                            },
                        },
                        {
                            "case_id": "2",
                            "baseline": {
                                "tests_passed": 8,
                                "tests_total": 10,
                                "findings": 2,
                                "files_touched": 5,
                                "duration_seconds": 125,
                                "tokens_used": 27000,
                            },
                            "candidate": {
                                "tests_passed": 10,
                                "tests_total": 10,
                                "findings": 0,
                                "files_touched": 4,
                                "duration_seconds": 105,
                                "tokens_used": 21500,
                            },
                        },
                        {
                            "case_id": "3",
                            "baseline": {
                                "tests_passed": 8,
                                "tests_total": 10,
                                "findings": 2,
                                "files_touched": 5,
                                "duration_seconds": 130,
                                "tokens_used": 28000,
                            },
                            "candidate": {
                                "tests_passed": 10,
                                "tests_total": 10,
                                "findings": 0,
                                "files_touched": 4,
                                "duration_seconds": 110,
                                "tokens_used": 22000,
                            },
                        },
                    ],
                }
            )
        )
        auto = self.run_py("auto.py", "run", "--root", str(self.root))
        self.assertEqual(auto.returncode, 0, auto.stdout + auto.stderr)
        payload = json.loads(auto.stdout)
        self.assertEqual(payload["executed"], 1)
        registry = json.loads((self.persistent_dir / "learned-agents" / "registry.json").read_text())
        agent = registry["agents"][0]
        self.assertEqual(agent["mode"], "replace")
        self.assertTrue(agent["route_allowed"])
        queue = json.loads((self.root / ".dynos" / "automation" / "queue.json").read_text())
        self.assertEqual(queue["items"], [])

    def test_fixture_synthesis_and_auto_sync_create_generated_fixture(self) -> None:
        self.run_py("calibrate.py", "init-registry", "--root", str(self.root))
        register = self.run_py(
            "calibrate.py",
            "register-agent",
            "backend-synth",
            "backend-executor",
            "feature",
            ".dynos/learned-agents/executors/backend-synth.md",
            "task-20260401-001",
            "--root",
            str(self.root),
        )
        self.assertEqual(register.returncode, 0, register.stdout + register.stderr)
        synth = self.run_py(
            "fixture.py",
            "synthesize",
            "backend-synth",
            "backend-executor",
            "feature",
            "--root",
            str(self.root),
        )
        self.assertEqual(synth.returncode, 0, synth.stdout + synth.stderr)
        payload = json.loads(synth.stdout)
        self.assertEqual(payload["target_name"], "backend-synth")
        generated = self.root / "benchmarks" / "generated" / "agent-backend-synth-feature.json"
        self.assertTrue(generated.exists())
        auto = self.run_py("auto.py", "sync", "--root", str(self.root))
        self.assertEqual(auto.returncode, 0, auto.stdout + auto.stderr)
        queue = json.loads((self.root / ".dynos" / "automation" / "queue.json").read_text())
        self.assertEqual(len(queue["items"]), 1)
        self.assertIn("agent-backend-synth-feature.json", queue["items"][0]["fixture_path"])

    def test_structured_generation_registers_component_and_report_surfaces_it(self) -> None:
        output_path = self.root / ".dynos" / "learned-agents" / "executors" / "backend-crafted.md"
        generated = self.run_py(
            "generate.py",
            "backend-crafted",
            "backend-executor",
            "feature",
            str(output_path),
            "task-20260401-001",
            "--source-task",
            "task-20260401-001",
            "--root",
            str(self.root),
        )
        self.assertEqual(generated.returncode, 0, generated.stdout + generated.stderr)
        self.assertTrue(output_path.exists())
        content = output_path.read_text()
        self.assertIn("## Operating Focus", content)
        report = self.run_py("report.py", "--root", str(self.root))
        self.assertEqual(report.returncode, 0, report.stdout + report.stderr)
        payload = json.loads(report.stdout)
        self.assertEqual(payload["summary"]["learned_components"], 1)
        self.assertEqual(payload["summary"]["shadow_components"], 1)

    def test_freshness_policy_blocks_stale_route(self) -> None:
        for number in range(3, 9):
            task_id = f"task-20260401-00{number}"
            self.make_task(
                task_id,
                {
                    "task_id": task_id,
                    "task_outcome": "DONE",
                    "task_type": "feature",
                    "task_domains": "backend",
                    "task_risk_level": "medium",
                    "findings_by_auditor": {"code-quality-auditor": 0},
                    "findings_by_category": {"cq": 0},
                    "executor_repair_frequency": {"backend-executor": 0},
                    "spec_review_iterations": 1,
                    "repair_cycle_count": 0,
                    "subagent_spawn_count": 2,
                    "wasted_spawns": 0,
                    "auditor_zero_finding_streaks": {"code-quality-auditor": 1},
                    "executor_zero_repair_streak": 1,
                    "quality_score": 0.9,
                    "cost_score": 0.8,
                    "efficiency_score": 0.85,
                },
            )
        (self.root / ".dynos" / "policy.json").write_text(json.dumps({"freshness_task_window": 1}, indent=2) + "\n")
        self.run_py("calibrate.py", "init-registry", "--root", str(self.root))
        self.run_py(
            "calibrate.py",
            "register-agent",
            "backend-stale",
            "backend-executor",
            "feature",
            ".dynos/learned-agents/executors/backend-stale.md",
            "task-20260401-001",
            "--root",
            str(self.root),
        )
        candidate = self.root / "candidate-stale.json"
        baseline = self.root / "baseline-stale.json"
        candidate.write_text(json.dumps([{"quality_score": 0.95, "cost_score": 0.82, "efficiency_score": 0.9}] * 3))
        baseline.write_text(json.dumps([{"quality_score": 0.82, "cost_score": 0.8, "efficiency_score": 0.81}] * 3))
        self.run_py(
            "eval.py",
            "promote",
            "backend-stale",
            "backend-executor",
            "feature",
            str(candidate),
            str(baseline),
            "--root",
            str(self.root),
        )
        route = self.run_py("route.py", "backend-executor", "feature", "--root", str(self.root))
        self.assertEqual(route.returncode, 0, route.stdout + route.stderr)
        payload = json.loads(route.stdout)
        self.assertEqual(payload["source"], "generic")
        self.assertTrue(payload["freshness_blocked"])

    def test_dashboard_and_lineage_generation(self) -> None:
        self.run_py("calibrate.py", "init-registry", "--root", str(self.root))
        self.run_py(
            "calibrate.py",
            "register-agent",
            "backend-lineage",
            "backend-executor",
            "feature",
            ".dynos/learned-agents/executors/backend-lineage.md",
            "task-20260401-001",
            "--root",
            str(self.root),
        )
        self.run_py("fixture.py", "sync", "--root", str(self.root))
        dashboard = self.run_py("dashboard.py", "generate", "--root", str(self.root))
        self.assertEqual(dashboard.returncode, 0, dashboard.stdout + dashboard.stderr)
        payload = json.loads(dashboard.stdout)
        self.assertTrue((self.root / ".dynos" / "dashboard.html").exists())
        self.assertTrue((self.root / ".dynos" / "dashboard-data.json").exists())
        self.assertIn("summary", payload)
        lineage = self.run_py("lineage.py", "--root", str(self.root))
        self.assertEqual(lineage.returncode, 0, lineage.stdout + lineage.stderr)
        graph = json.loads(lineage.stdout)
        self.assertTrue(any(node["kind"] == "component" for node in graph["nodes"]))

    def test_patterns_generation_writes_policy_tables(self) -> None:
        self.run_py("calibrate.py", "init-registry", "--root", str(self.root))
        self.run_py(
            "calibrate.py",
            "register-agent",
            "backend-patterned",
            "backend-executor",
            "feature",
            ".dynos/learned-agents/executors/backend-patterned.md",
            "task-20260401-001",
            "--root",
            str(self.root),
        )
        patterns = self.run_py("patterns.py", "--root", str(self.root))
        self.assertEqual(patterns.returncode, 0, patterns.stdout + patterns.stderr)
        payload = json.loads(patterns.stdout)
        self.assertIn(str(self.persistent_dir / "project_rules.md"), payload["written_paths"])
        content = (self.persistent_dir / "project_rules.md").read_text()
        # Data tables removed from markdown (now JSON only) — only prevention rules + gold standard remain
        self.assertIn("## Prevention Rules", content)
        # Verify JSON policy files were written
        self.assertTrue((self.persistent_dir / "model-policy.json").exists())
        self.assertTrue((self.persistent_dir / "skip-policy.json").exists())
        self.assertTrue((self.persistent_dir / "effectiveness-scores.json").exists())

    def test_task_artifact_challenge_rollout_runs(self) -> None:
        self.run_py("calibrate.py", "init-registry", "--root", str(self.root))
        self.run_py(
            "calibrate.py",
            "register-agent",
            "backend-runner",
            "backend-executor",
            "feature",
            ".dynos/learned-agents/executors/backend-runner.md",
            "task-20260401-001",
            "--root",
            str(self.root),
        )
        task_dir = self.root / ".dynos" / "task-20260409-001"
        task_dir.mkdir(parents=True)
        (self.root / "README.md").write_text("Challenge fixture\n")
        (task_dir / "execution-graph.json").write_text(
            json.dumps(
                {
                    "segments": [
                        {
                            "id": "seg-1",
                            "executor": "backend-executor",
                            "files_expected": ["README.md"],
                            "depends_on": [],
                            "criteria_ids": [1],
                        }
                    ]
                },
                indent=2,
            )
            + "\n"
        )
        rollout = self.run_py(
            "challenge.py",
            "task-20260409-001",
            "backend-runner",
            "backend-executor",
            "feature",
            "--root",
            str(self.root),
            "--baseline-command",
            json.dumps(["python3", "-c", "import json; print(json.dumps({'tests_passed': 1, 'tests_total': 2, 'findings': 1, 'tokens_used': 12000}))"]),
            "--candidate-command",
            json.dumps(["python3", "-c", "import json; print(json.dumps({'tests_passed': 2, 'tests_total': 2, 'findings': 0, 'tokens_used': 12000}))"]),
        )
        self.assertEqual(rollout.returncode, 0, rollout.stdout + rollout.stderr)
        payload = json.loads(rollout.stdout)
        self.assertEqual(payload["execution_harness"], "rollout")
        self.assertEqual(payload["cases"][0]["winner"], "candidate")

    def test_maintainer_run_once_executes_cycle_and_writes_status(self) -> None:
        self.run_py("calibrate.py", "init-registry", "--root", str(self.root))
        self.run_py(
            "calibrate.py",
            "register-agent",
            "backend-maintainer",
            "backend-executor",
            "feature",
            ".dynos/learned-agents/executors/backend-maintainer.md",
            "task-20260401-001",
            "--root",
            str(self.root),
        )
        fixture_dir = self.root / "benchmarks" / "fixtures"
        fixture_dir.mkdir(parents=True)
        (fixture_dir / "backend-maintainer.json").write_text(
            json.dumps(
                {
                    "fixture_id": "backend-maintainer",
                    "item_kind": "agent",
                    "target_name": "backend-maintainer",
                    "role": "backend-executor",
                    "task_type": "feature",
                    "cases": [
                        {
                            "case_id": "1",
                            "baseline": {
                                "tests_passed": 8,
                                "tests_total": 10,
                                "findings": 2,
                                "files_touched": 5,
                                "duration_seconds": 120,
                                "tokens_used": 26000,
                            },
                            "candidate": {
                                "tests_passed": 10,
                                "tests_total": 10,
                                "findings": 0,
                                "files_touched": 4,
                                "duration_seconds": 100,
                                "tokens_used": 21000,
                            },
                        },
                        {
                            "case_id": "2",
                            "baseline": {
                                "tests_passed": 8,
                                "tests_total": 10,
                                "findings": 2,
                                "files_touched": 5,
                                "duration_seconds": 125,
                                "tokens_used": 27000,
                            },
                            "candidate": {
                                "tests_passed": 10,
                                "tests_total": 10,
                                "findings": 0,
                                "files_touched": 4,
                                "duration_seconds": 105,
                                "tokens_used": 21500,
                            },
                        },
                        {
                            "case_id": "3",
                            "baseline": {
                                "tests_passed": 8,
                                "tests_total": 10,
                                "findings": 2,
                                "files_touched": 5,
                                "duration_seconds": 130,
                                "tokens_used": 28000,
                            },
                            "candidate": {
                                "tests_passed": 10,
                                "tests_total": 10,
                                "findings": 0,
                                "files_touched": 4,
                                "duration_seconds": 110,
                                "tokens_used": 22000,
                            },
                        },
                    ],
                }
            )
        )
        maintain = self.run_py("daemon.py", "run-once", "--root", str(self.root))
        self.assertEqual(maintain.returncode, 0, maintain.stdout + maintain.stderr)
        payload = json.loads(maintain.stdout)
        self.assertTrue(payload["ok"])
        status = json.loads((self.root / ".dynos" / "maintenance" / "status.json").read_text())
        self.assertIn("last_cycle", status)
        registry = json.loads((self.persistent_dir / "learned-agents" / "registry.json").read_text())
        agent = registry["agents"][0]
        self.assertIn(agent["mode"], {"alongside", "replace"})
        self.assertTrue(agent["route_allowed"])
        self.assertTrue((self.root / ".dynos" / "dashboard.html").exists())
        self.assertTrue((self.persistent_dir / "project_rules.md").exists())

    def test_maintainer_invoke_alias_runs(self) -> None:
        invoke = self.run_py("daemon.py", "invoke", "--root", str(self.root))
        self.assertEqual(invoke.returncode, 0, invoke.stdout + invoke.stderr)
        payload = json.loads(invoke.stdout)
        self.assertIn("actions", payload)


if __name__ == "__main__":
    unittest.main()
