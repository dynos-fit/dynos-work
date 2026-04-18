#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _make_task(dynos_home, task_id: str, retrospective: dict) -> None:
    task_dir = dynos_home.root / ".dynos" / task_id
    task_dir.mkdir(parents=True)
    (task_dir / "task-retrospective.json").write_text(json.dumps(retrospective, indent=2) + "\n")


def _run_py(dynos_home, script: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(ROOT / "hooks" / script), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "DYNOS_HOME": str(dynos_home.dynos_home)},
    )


def _seed_tasks(dynos_home) -> None:
    _make_task(
        dynos_home,
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
    _make_task(
        dynos_home,
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


def test_rebuild_trajectory_store_and_search(dynos_home) -> None:
    _seed_tasks(dynos_home)
    root = dynos_home.root
    rebuild = _run_py(dynos_home, "trajectory.py", "rebuild", "--root", str(root))
    assert rebuild.returncode == 0, rebuild.stdout + rebuild.stderr
    query_path = root / "query.json"
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
    search = _run_py(dynos_home, "trajectory.py", "search", str(query_path), "--root", str(root), "--limit", "1")
    assert search.returncode == 0, search.stdout + search.stderr
    results = json.loads(search.stdout)
    assert results[0]["trajectory"]["source_task_id"] == "task-20260401-001"


def test_register_and_promote_learned_agent(dynos_home) -> None:
    _seed_tasks(dynos_home)
    root = dynos_home.root
    init = _run_py(dynos_home, "agent_generator.py", "init-registry", "--root", str(root))
    assert init.returncode == 0
    register = _run_py(
        dynos_home,
        "agent_generator.py",
        "register-agent",
        "backend-sharp",
        "backend-executor",
        "feature",
        ".dynos/learned-agents/executors/backend-sharp.md",
        "task-20260401-002",
        "--root",
        str(root),
    )
    assert register.returncode == 0, register.stdout + register.stderr

    candidate_path = root / "candidate.json"
    baseline_path = root / "baseline.json"
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

    promote = _run_py(
        dynos_home,
        "eval.py",
        "promote",
        "backend-sharp",
        "backend-executor",
        "feature",
        str(candidate_path),
        str(baseline_path),
        "--root",
        str(root),
    )
    assert promote.returncode == 0, promote.stdout + promote.stderr
    output = json.loads(promote.stdout)
    assert output["target_mode"] == "replace"

    registry = json.loads((dynos_home.persistent_dir / "learned-agents" / "registry.json").read_text())
    agent = registry["agents"][0]
    assert agent["mode"] == "replace"
    assert agent["last_evaluation"]["recommendation"] == "promote_replace"


def test_skill_shadow_mode_and_benchmark_runner(dynos_home) -> None:
    _seed_tasks(dynos_home)
    root = dynos_home.root
    _run_py(dynos_home, "agent_generator.py", "init-registry", "--root", str(root))
    register = _run_py(
        dynos_home,
        "agent_generator.py",
        "register-agent",
        "plan-tightener",
        "plan-skill",
        "feature",
        ".dynos/learned-agents/skills/plan-tightener.md",
        "task-20260401-002",
        "--item-kind",
        "skill",
        "--root",
        str(root),
    )
    assert register.returncode == 0, register.stdout + register.stderr
    fixture_path = root / "fixture.json"
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
    bench = _run_py(dynos_home, "bench.py", "run", str(fixture_path), "--root", str(root), "--update-registry")
    assert bench.returncode == 0, bench.stdout + bench.stderr
    payload = json.loads(bench.stdout)
    assert payload["item_kind"] == "skill"
    assert payload["evaluation"]["target_mode"] == "replace"
    registry = json.loads((dynos_home.persistent_dir / "learned-agents" / "registry.json").read_text())
    skill = registry["agents"][0]
    assert skill["item_kind"] == "skill"
    assert skill["mode"] == "replace"
    history = json.loads((dynos_home.persistent_dir / "benchmarks" / "history.json").read_text())
    assert len(history["runs"]) == 1


def test_sandbox_benchmark_runner_executes_commands(dynos_home) -> None:
    _seed_tasks(dynos_home)
    root = dynos_home.root
    fixture_path = root / "sandbox-fixture.json"
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
    bench = _run_py(dynos_home, "bench.py", "run", str(fixture_path), "--root", str(root))
    assert bench.returncode == 0, bench.stdout + bench.stderr
    payload = json.loads(bench.stdout)
    assert payload["cases"][0]["execution_mode"] == "sandbox"
    assert payload["cases"][0]["candidate_observed"]["tests_passed"] == 10
    assert payload["cases"][0]["candidate_observed"]["files_touched"] >= 1


def test_task_fixture_runner_supports_command_sequences(dynos_home) -> None:
    _seed_tasks(dynos_home)
    root = dynos_home.root
    fixture_path = root / "task-fixture.json"
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
    bench = _run_py(dynos_home, "bench.py", "run", str(fixture_path), "--root", str(root))
    assert bench.returncode == 0, bench.stdout + bench.stderr
    payload = json.loads(bench.stdout)
    assert payload["cases"][0]["candidate_observed"]["tests_passed"] == 3
    assert payload["cases"][0]["baseline_observed"]["tests_passed"] == 1
    assert payload["cases"][0]["winner"] == "candidate"


def test_repo_rollout_harness_copies_repo_paths(dynos_home) -> None:
    _seed_tasks(dynos_home)
    root = dynos_home.root
    (root / "README.md").write_text("Repo fixture\n")
    fixture_path = root / "rollout-fixture.json"
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
    rollout = _run_py(dynos_home, "rollout.py", str(fixture_path), "--root", str(root))
    assert rollout.returncode == 0, rollout.stdout + rollout.stderr
    payload = json.loads(rollout.stdout)
    assert payload["execution_harness"] == "rollout"
    assert payload["cases"][0]["candidate_observed"]["tests_passed"] == 2
    assert payload["cases"][0]["baseline_observed"]["tests_passed"] == 1


def test_must_pass_category_blocks_promotion_and_demotes_active_component(dynos_home) -> None:
    _seed_tasks(dynos_home)
    root = dynos_home.root
    _run_py(dynos_home, "agent_generator.py", "init-registry", "--root", str(root))
    register = _run_py(
        dynos_home,
        "agent_generator.py",
        "register-agent",
        "security-hardener",
        "security-auditor",
        "feature",
        ".dynos/learned-agents/auditors/security-hardener.md",
        "task-20260401-002",
        "--root",
        str(root),
    )
    assert register.returncode == 0, register.stdout + register.stderr

    candidate_path = root / "candidate-regress.json"
    baseline_path = root / "baseline-regress.json"
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

    promote = _run_py(
        dynos_home,
        "eval.py",
        "promote",
        "security-hardener",
        "security-auditor",
        "feature",
        str(candidate_path),
        str(baseline_path),
        "--root",
        str(root),
    )
    assert promote.returncode == 0, promote.stdout + promote.stderr
    registry = json.loads((dynos_home.persistent_dir / "learned-agents" / "registry.json").read_text())
    agent = registry["agents"][0]
    assert agent["mode"] in {"alongside", "replace"}
    assert agent["route_allowed"]

    fixture_path = root / "must-pass-fixture.json"
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
    bench = _run_py(dynos_home, "bench.py", "run", str(fixture_path), "--root", str(root), "--update-registry")
    assert bench.returncode == 0, bench.stdout + bench.stderr
    payload = json.loads(bench.stdout)
    assert payload["evaluation"]["blocked_by_category"]
    assert payload["evaluation"]["recommendation"] == "reject"
    registry = json.loads((dynos_home.persistent_dir / "learned-agents" / "registry.json").read_text())
    agent = registry["agents"][0]
    assert agent["status"] == "demoted_on_regression"
    assert not agent["route_allowed"]


def test_auto_runner_executes_matching_shadow_fixture_and_promotes(dynos_home) -> None:
    _seed_tasks(dynos_home)
    root = dynos_home.root
    _run_py(dynos_home, "agent_generator.py", "init-registry", "--root", str(root))
    register = _run_py(
        dynos_home,
        "agent_generator.py",
        "register-agent",
        "backend-shadow",
        "backend-executor",
        "feature",
        ".dynos/learned-agents/executors/backend-shadow.md",
        "task-20260401-002",
        "--root",
        str(root),
    )
    assert register.returncode == 0, register.stdout + register.stderr
    fixtures_dir = root / "benchmarks" / "fixtures"
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
    auto = _run_py(dynos_home, "auto.py", "run", "--root", str(root))
    assert auto.returncode == 0, auto.stdout + auto.stderr
    payload = json.loads(auto.stdout)
    assert payload["executed"] == 1
    registry = json.loads((dynos_home.persistent_dir / "learned-agents" / "registry.json").read_text())
    agent = registry["agents"][0]
    assert agent["mode"] == "replace"
    assert agent["route_allowed"]
    queue = json.loads((root / ".dynos" / "automation" / "queue.json").read_text())
    assert queue["items"] == []


def test_fixture_synthesis_and_auto_sync_create_generated_fixture(dynos_home) -> None:
    _seed_tasks(dynos_home)
    root = dynos_home.root
    _run_py(dynos_home, "agent_generator.py", "init-registry", "--root", str(root))
    register = _run_py(
        dynos_home,
        "agent_generator.py",
        "register-agent",
        "backend-synth",
        "backend-executor",
        "feature",
        ".dynos/learned-agents/executors/backend-synth.md",
        "task-20260401-001",
        "--root",
        str(root),
    )
    assert register.returncode == 0, register.stdout + register.stderr
    synth = _run_py(
        dynos_home,
        "fixture.py",
        "synthesize",
        "backend-synth",
        "backend-executor",
        "feature",
        "--root",
        str(root),
    )
    assert synth.returncode == 0, synth.stdout + synth.stderr
    payload = json.loads(synth.stdout)
    assert payload["target_name"] == "backend-synth"
    generated = root / "benchmarks" / "generated" / "agent-backend-synth-feature.json"
    assert generated.exists()
    auto = _run_py(dynos_home, "auto.py", "sync", "--root", str(root))
    assert auto.returncode == 0, auto.stdout + auto.stderr
    queue = json.loads((root / ".dynos" / "automation" / "queue.json").read_text())
    assert len(queue["items"]) == 1
    assert "agent-backend-synth-feature.json" in queue["items"][0]["fixture_path"]


def test_dashboard_and_lineage_generation(dynos_home) -> None:
    _seed_tasks(dynos_home)
    root = dynos_home.root
    _run_py(dynos_home, "agent_generator.py", "init-registry", "--root", str(root))
    _run_py(
        dynos_home,
        "agent_generator.py",
        "register-agent",
        "backend-lineage",
        "backend-executor",
        "feature",
        ".dynos/learned-agents/executors/backend-lineage.md",
        "task-20260401-001",
        "--root",
        str(root),
    )
    _run_py(dynos_home, "fixture.py", "sync", "--root", str(root))
    dashboard = _run_py(dynos_home, "dashboard.py", "generate", "--root", str(root))
    assert dashboard.returncode == 0, dashboard.stdout + dashboard.stderr
    payload = json.loads(dashboard.stdout)
    assert (root / ".dynos" / "dashboard.html").exists()
    assert (root / ".dynos" / "dashboard-data.json").exists()
    assert "summary" in payload
    lineage = _run_py(dynos_home, "lineage.py", "--root", str(root))
    assert lineage.returncode == 0, lineage.stdout + lineage.stderr
    graph = json.loads(lineage.stdout)
    assert any(node["kind"] == "component" for node in graph["nodes"])


def test_patterns_generation_writes_policy_tables(dynos_home) -> None:
    _seed_tasks(dynos_home)
    root = dynos_home.root
    _run_py(dynos_home, "agent_generator.py", "init-registry", "--root", str(root))
    _run_py(
        dynos_home,
        "agent_generator.py",
        "register-agent",
        "backend-patterned",
        "backend-executor",
        "feature",
        ".dynos/learned-agents/executors/backend-patterned.md",
        "task-20260401-001",
        "--root",
        str(root),
    )
    patterns = _run_py(dynos_home, "patterns.py", "--root", str(root))
    assert patterns.returncode == 0, patterns.stdout + patterns.stderr
    payload = json.loads(patterns.stdout)
    assert str(dynos_home.persistent_dir / "project_rules.md") in payload["written_paths"]
    content = (dynos_home.persistent_dir / "project_rules.md").read_text()
    # Data tables removed from markdown (now JSON only) — only prevention rules + gold standard remain
    assert "## Prevention Rules" in content
    # Verify JSON policy files were written
    assert (dynos_home.persistent_dir / "model-policy.json").exists()
    assert (dynos_home.persistent_dir / "skip-policy.json").exists()
    assert (dynos_home.persistent_dir / "effectiveness-scores.json").exists()


def test_task_artifact_challenge_rollout_runs(dynos_home) -> None:
    _seed_tasks(dynos_home)
    root = dynos_home.root
    _run_py(dynos_home, "agent_generator.py", "init-registry", "--root", str(root))
    _run_py(
        dynos_home,
        "agent_generator.py",
        "register-agent",
        "backend-runner",
        "backend-executor",
        "feature",
        ".dynos/learned-agents/executors/backend-runner.md",
        "task-20260401-001",
        "--root",
        str(root),
    )
    task_dir = root / ".dynos" / "task-20260409-001"
    task_dir.mkdir(parents=True)
    (root / "README.md").write_text("Challenge fixture\n")
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
    rollout = _run_py(
        dynos_home,
        "challenge.py",
        "task-20260409-001",
        "backend-runner",
        "backend-executor",
        "feature",
        "--root",
        str(root),
        "--baseline-command",
        json.dumps(["python3", "-c", "import json; print(json.dumps({'tests_passed': 1, 'tests_total': 2, 'findings': 1, 'tokens_used': 12000}))"]),
        "--candidate-command",
        json.dumps(["python3", "-c", "import json; print(json.dumps({'tests_passed': 2, 'tests_total': 2, 'findings': 0, 'tokens_used': 12000}))"]),
    )
    assert rollout.returncode == 0, rollout.stdout + rollout.stderr
    payload = json.loads(rollout.stdout)
    assert payload["execution_harness"] == "rollout"
    assert payload["cases"][0]["winner"] == "candidate"


def test_maintainer_run_once_executes_cycle_and_writes_status(dynos_home) -> None:
    _seed_tasks(dynos_home)
    root = dynos_home.root
    _run_py(dynos_home, "agent_generator.py", "init-registry", "--root", str(root))
    _run_py(
        dynos_home,
        "agent_generator.py",
        "register-agent",
        "backend-maintainer",
        "backend-executor",
        "feature",
        ".dynos/learned-agents/executors/backend-maintainer.md",
        "task-20260401-001",
        "--root",
        str(root),
    )
    fixture_dir = root / "benchmarks" / "fixtures"
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
    maintain = _run_py(dynos_home, "daemon.py", "run-once", "--root", str(root))
    assert maintain.returncode == 0, maintain.stdout + maintain.stderr
    payload = json.loads(maintain.stdout)
    assert payload["ok"] is True
    status = json.loads((dynos_home.root / ".dynos" / "maintenance" / "status.json").read_text())
    assert "last_cycle" in status
    # Daemon now only runs trajectory rebuild — bench/eval/dashboard/policy
    # are handled by the eventbus. Agent promotion and dashboard generation
    # are no longer the daemon's responsibility.


def test_maintainer_invoke_alias_runs(dynos_home) -> None:
    _seed_tasks(dynos_home)
    root = dynos_home.root
    invoke = _run_py(dynos_home, "daemon.py", "invoke", "--root", str(root))
    assert invoke.returncode == 0, invoke.stdout + invoke.stderr
    payload = json.loads(invoke.stdout)
    assert "actions" in payload
