#!/usr/bin/env python3
"""MCTS-lite design dreaming runner for dynos-work."""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import argparse
import json
import random
import shutil
import tempfile
from pathlib import Path

from lib_core import now_iso
from lib_defaults import (
    COMPONENT_MIN_ACCEPTABLE,
    DESIGN_ARCH_COMPLEXITY_DIVISOR,
    DESIGN_COMPLEXITY_PENALTIES,
    DESIGN_FILE_PENALTY_CAP,
    DESIGN_FILE_PENALTY_PER_FILE,
    DESIGN_MAINTAINABILITY_KEYWORD_WEIGHT,
    DESIGN_RISK_PENALTIES,
    DESIGN_SECURITY_PENALTY_CAP,
    DESIGN_SECURITY_PENALTY_PER_HIT,
    DESIGN_WEIGHT_COMPLEXITY,
    DESIGN_WEIGHT_MAINTAINABILITY,
    DESIGN_WEIGHT_SECURITY,
    DESIGN_WEIGHT_TRAJECTORY,
    MCTS_DEFAULT_ITERATIONS,
    MCTS_ROLLOUT_NOISE_MAX,
    MCTS_ROLLOUT_NOISE_MIN,
    RELATED_TRAJECTORIES_LIMIT,
    SCORE_THRESHOLD_ACCEPTABLE,
    SCORE_THRESHOLD_PREFERRED,
    SECURITY_FINDINGS_THRESHOLD_CRITICAL,
    SECURITY_FINDINGS_THRESHOLD_LOW,
    TRAJECTORY_MIN_ACCEPTABLE,
)
from lib_trajectory import search_trajectories
from state import encode_state


HIGH_RISK_KEYWORDS = {
    "auth",
    "security",
    "migration",
    "database",
    "db",
    "payment",
    "oauth",
    "encryption",
    "queue",
    "async",
    "cache",
}


def option_static_score(option: dict, state: dict, prior_similarity: float) -> dict:
    description = option.get("description", "")
    files = option.get("files", [])
    if not isinstance(files, list):
        files = []
    complexity_hint = str(option.get("complexity", "medium")).lower()
    risk_hint = str(option.get("risk", "medium")).lower()
    keyword_hits = sum(1 for word in HIGH_RISK_KEYWORDS if word in description.lower())
    file_penalty = min(DESIGN_FILE_PENALTY_CAP, len(files) * DESIGN_FILE_PENALTY_PER_FILE)
    complexity_penalty = DESIGN_COMPLEXITY_PENALTIES.get(complexity_hint, 0.12)
    risk_penalty = DESIGN_RISK_PENALTIES.get(risk_hint, 0.1)
    security_penalty = min(DESIGN_SECURITY_PENALTY_CAP, keyword_hits * DESIGN_SECURITY_PENALTY_PER_HIT)
    complexity_component = max(0.0, 1.0 - complexity_penalty - file_penalty)
    maintainability_component = max(0.0, 1.0 - (DESIGN_MAINTAINABILITY_KEYWORD_WEIGHT * keyword_hits) - min(DESIGN_SECURITY_PENALTY_CAP, state["architecture_complexity_score"] / DESIGN_ARCH_COMPLEXITY_DIVISOR))
    security_component = max(0.0, 1.0 - risk_penalty - security_penalty)
    trajectory_component = min(1.0, prior_similarity)
    base_score = (
        DESIGN_WEIGHT_COMPLEXITY * complexity_component
        + DESIGN_WEIGHT_MAINTAINABILITY * maintainability_component
        + DESIGN_WEIGHT_SECURITY * security_component
        + DESIGN_WEIGHT_TRAJECTORY * trajectory_component
    )
    return {
        "complexity_component": round(complexity_component, 6),
        "maintainability_component": round(maintainability_component, 6),
        "security_component": round(security_component, 6),
        "trajectory_component": round(trajectory_component, 6),
        "base_score": round(base_score, 6),
        "keyword_hits": keyword_hits,
    }


def simulate_option(option: dict, state: dict, prior_similarity: float, iterations: int, seed: int) -> dict:
    rng = random.Random(seed)
    metrics = option_static_score(option, state, prior_similarity)
    visits = 0
    total_reward = 0.0
    best_reward = 0.0
    samples: list[float] = []
    for _ in range(iterations):
        visits += 1
        noise = rng.uniform(MCTS_ROLLOUT_NOISE_MIN, MCTS_ROLLOUT_NOISE_MAX)
        rollout_reward = min(1.0, max(0.0, metrics["base_score"] + noise))
        samples.append(rollout_reward)
        total_reward += rollout_reward
        best_reward = max(best_reward, rollout_reward)
    average_reward = total_reward / max(1, visits)
    return {
        "visits": visits,
        "average_reward": round(average_reward, 6),
        "best_reward": round(best_reward, 6),
        "samples": [round(value, 6) for value in samples],
        "metrics": metrics,
    }


def run_mcts(options: list[dict], state: dict, priors: dict[str, float], iterations: int) -> list[dict]:
    results: list[dict] = []
    for index, option in enumerate(options):
        option_id = option.get("id", f"option-{index + 1}")
        prior = priors.get(option_id, 0.0)
        result = simulate_option(option, state, prior, iterations, seed=index + 1)
        result["option_id"] = option_id
        result["description"] = option.get("description", "")
        result["files"] = option.get("files", [])
        results.append(result)
    results.sort(key=lambda item: (-item["average_reward"], -item["best_reward"], item["option_id"]))
    return results


def recommendation_for_score(score: float) -> tuple[str, str]:
    if score >= SCORE_THRESHOLD_PREFERRED:
        return "PASS", "Preferred"
    if score >= SCORE_THRESHOLD_ACCEPTABLE:
        return "PASS", "Acceptable"
    return "FAIL", "High Risk"


def certificate_for_result(subtask: str, result: dict, related_trajectories: list[dict]) -> dict:
    outcome, recommendation = recommendation_for_score(result["average_reward"])
    security_findings = 0 if result["metrics"]["security_component"] >= SECURITY_FINDINGS_THRESHOLD_LOW else 1
    if result["metrics"]["security_component"] < SECURITY_FINDINGS_THRESHOLD_CRITICAL:
        security_findings = 3
    return {
        "subtask": subtask,
        "option_id": result["option_id"],
        "result": outcome,
        "recommendation": recommendation,
        "score": result["average_reward"],
        "performance_metrics": {
            "average_reward": result["average_reward"],
            "best_reward": result["best_reward"],
            "complexity_component": result["metrics"]["complexity_component"],
            "maintainability_component": result["metrics"]["maintainability_component"],
            "trajectory_component": result["metrics"]["trajectory_component"],
        },
        "security_score": {
            "findings": security_findings,
            "component_score": result["metrics"]["security_component"],
        },
        "failure_modes": derive_failure_modes(result),
        "related_trajectories": [
            {
                "trajectory_id": item["trajectory"]["trajectory_id"],
                "similarity": item["similarity"],
            }
            for item in related_trajectories[:RELATED_TRAJECTORIES_LIMIT]
        ],
    }


def derive_failure_modes(result: dict) -> list[str]:
    modes: list[str] = []
    metrics = result["metrics"]
    if metrics["security_component"] < COMPONENT_MIN_ACCEPTABLE:
        modes.append("Security-sensitive surface area is high for this option.")
    if metrics["complexity_component"] < COMPONENT_MIN_ACCEPTABLE:
        modes.append("Implementation breadth may slow delivery and increase repair risk.")
    if metrics["maintainability_component"] < COMPONENT_MIN_ACCEPTABLE:
        modes.append("Option introduces maintenance complexity relative to current repo state.")
    if metrics["trajectory_component"] < TRAJECTORY_MIN_ACCEPTABLE:
        modes.append("Few similar successful trajectories support this design path.")
    return modes or ["No dominant failure mode detected in sandbox scoring."]


def create_sandbox(task_id: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"dynos-dream-{task_id}-", dir="/tmp"))


def write_sandbox_artifacts(sandbox: Path, payload: dict, certificates: list[dict]) -> None:
    (sandbox / "design-options.json").write_text(json.dumps(payload, indent=2) + "\n")
    (sandbox / "design-certificates.json").write_text(json.dumps(certificates, indent=2) + "\n")


def cmd_dream(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    payload = json.loads(Path(args.options_json).read_text())
    task_id = payload.get("task_id", "task")
    subtask = payload.get("subtask", "design-review")
    options = payload.get("options", [])
    if not isinstance(options, list) or not options:
        raise SystemExit("options_json must contain a non-empty 'options' array")

    state = encode_state(root)
    query_state = {
        "task_type": payload.get("task_type", "feature"),
        "task_domains": payload.get("task_domains", []),
        "task_risk_level": payload.get("task_risk_level", "medium"),
        "repair_cycle_count": 0,
        "subagent_spawn_count": 0,
        "wasted_spawns": 0,
        "spec_review_iterations": 1,
    }
    related = search_trajectories(root, query_state, limit=RELATED_TRAJECTORIES_LIMIT)
    prior_similarity = max((item["similarity"] for item in related), default=0.0)
    priors = {
        option.get("id", f"option-{index + 1}"): prior_similarity
        for index, option in enumerate(options)
    }

    sandbox = create_sandbox(task_id)
    try:
        results = run_mcts(options, state, priors, args.iterations)
        certificates = [certificate_for_result(subtask, result, related) for result in results]
        write_sandbox_artifacts(sandbox, payload, certificates)
        output = {
            "version": 1,
            "generated_at": now_iso(),
            "task_id": task_id,
            "subtask": subtask,
            "sandbox": str(sandbox),
            "state_signature": state,
            "search_strategy": {
                "algorithm": "mcts-lite",
                "iterations_per_option": args.iterations,
                "related_trajectory_count": len(related),
            },
            "design_certificates": certificates,
        }
        print(json.dumps(output, indent=2))
    finally:
        if not args.keep_sandbox:
            shutil.rmtree(sandbox, ignore_errors=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("options_json", help="JSON file with task_id, subtask, and options")
    parser.add_argument("--root", default=".")
    parser.add_argument("--iterations", type=int, default=MCTS_DEFAULT_ITERATIONS)
    parser.add_argument("--keep-sandbox", action="store_true")
    parser.set_defaults(func=cmd_dream)
    return parser


if __name__ == "__main__":
    from cli_base import cli_main
    raise SystemExit(cli_main(build_parser))
