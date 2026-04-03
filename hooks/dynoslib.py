#!/usr/bin/env python3
"""Deterministic control helpers for dynos-work."""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

REQUIRED_SPEC_HEADINGS = [
    "Task Summary",
    "User Context",
    "Acceptance Criteria",
    "Implicit Requirements Surfaced",
    "Out of Scope",
    "Assumptions",
    "Risk Notes",
]

REQUIRED_PLAN_HEADINGS = [
    "Technical Approach",
    "Reference Code",
    "Components / Modules",
    "Data Flow",
    "Error Handling Strategy",
    "Test Strategy",
    "Dependency Graph",
    "Open Questions",
]

VALID_EXECUTORS = {
    "ui-executor",
    "backend-executor",
    "ml-executor",
    "db-executor",
    "refactor-executor",
    "testing-executor",
    "integration-executor",
}

VALID_CLASSIFICATION_TYPES = {
    "feature",
    "bugfix",
    "refactor",
    "migration",
    "ml",
    "full-stack",
}

VALID_DOMAINS = {"ui", "backend", "db", "ml", "security"}
VALID_RISK_LEVELS = {"low", "medium", "high", "critical"}

# Shared composite score weights: (quality, efficiency, cost)
COMPOSITE_WEIGHTS = (0.6, 0.25, 0.15)

STAGE_ORDER = [
    "FOUNDRY_INITIALIZED",
    "CLASSIFY_AND_SPEC",
    "SPEC_NORMALIZATION",
    "SPEC_REVIEW",
    "PLANNING",
    "PLAN_REVIEW",
    "PLAN_AUDIT",
    "EXECUTION_GRAPH_BUILD",
    "PRE_EXECUTION_SNAPSHOT",
    "EXECUTION",
    "TEST_EXECUTION",
    "CHECKPOINT_AUDIT",
    "FINAL_AUDIT",
    "REPAIR_PLANNING",
    "REPAIR_EXECUTION",
    "DONE",
    "CANCELLED",
    "FAILED",
]

ALLOWED_STAGE_TRANSITIONS = {
    "CLASSIFY_AND_SPEC": {"SPEC_REVIEW", "PLANNING", "FAILED", "CANCELLED"},
    "FOUNDRY_INITIALIZED": {"SPEC_NORMALIZATION", "FAILED"},
    "SPEC_NORMALIZATION": {"SPEC_REVIEW", "FAILED"},
    "SPEC_REVIEW": {"SPEC_NORMALIZATION", "PLANNING", "FAILED"},
    "PLANNING": {"PLAN_REVIEW", "FAILED"},
    "PLAN_REVIEW": {"PLANNING", "PLAN_AUDIT", "FAILED"},
    "PLAN_AUDIT": {"PLANNING", "PRE_EXECUTION_SNAPSHOT", "FAILED"},
    "PRE_EXECUTION_SNAPSHOT": {"EXECUTION", "FAILED"},
    "EXECUTION": {"TEST_EXECUTION", "REPAIR_PLANNING", "FAILED"},
    "TEST_EXECUTION": {"CHECKPOINT_AUDIT", "REPAIR_PLANNING", "FAILED"},
    "CHECKPOINT_AUDIT": {"REPAIR_PLANNING", "DONE", "FAILED"},
    "REPAIR_PLANNING": {"REPAIR_EXECUTION", "FAILED"},
    "REPAIR_EXECUTION": {"CHECKPOINT_AUDIT", "REPAIR_PLANNING", "DONE", "FAILED"},
    "FINAL_AUDIT": {"CHECKPOINT_AUDIT", "DONE", "FAILED"},
    "EXECUTION_GRAPH_BUILD": {"PRE_EXECUTION_SNAPSHOT", "EXECUTION", "FAILED"},
    "DONE": set(),
    "CANCELLED": set(),
    "FAILED": set(),
}

NEXT_COMMAND = {
    "CLASSIFY_AND_SPEC": "/dynos-work:start",
    "FOUNDRY_INITIALIZED": "/dynos-work:start",
    "SPEC_NORMALIZATION": "/dynos-work:start",
    "SPEC_REVIEW": "/dynos-work:start",
    "PLANNING": "/dynos-work:plan",
    "PLAN_REVIEW": "/dynos-work:plan",
    "PLAN_AUDIT": "/dynos-work:plan",
    "EXECUTION_GRAPH_BUILD": "/dynos-work:execute",
    "PRE_EXECUTION_SNAPSHOT": "/dynos-work:execute",
    "EXECUTION": "/dynos-work:execute",
    "TEST_EXECUTION": "/dynos-work:execute",
    "CHECKPOINT_AUDIT": "/dynos-work:audit",
    "REPAIR_PLANNING": "/dynos-work:audit",
    "REPAIR_EXECUTION": "/dynos-work:audit",
    "FINAL_AUDIT": "/dynos-work:audit",
    "DONE": "Task complete",
    "CANCELLED": "Task cancelled",
    "FAILED": "Review failure state before continuing",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _persistent_project_dir(root: Path) -> Path:
    """Returns ~/.dynos/projects/{slug}/ for persistent project state.

    Stores accumulated intelligence: trajectories, patterns, learned agents,
    benchmarks, policy. Survives repo .dynos/ cleanup.
    """
    dynos_home = Path(os.environ.get("DYNOS_HOME", str(Path.home() / ".dynos")))
    slug = str(root.resolve()).strip("/").replace("/", "-")
    d = dynos_home / "projects" / slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def validate_generated_html(html_path: Path) -> list[str]:
    """Validate generated HTML for common template rendering bugs.

    Returns a list of error strings. Empty list means valid.
    """
    errors: list[str] = []
    try:
        content = html_path.read_text()
    except (FileNotFoundError, OSError) as exc:
        return [f"cannot read {html_path}: {exc}"]

    # Check for doubled braces in style blocks (template escaping bug)
    style_match = re.search(r"<style>(.*?)</style>", content, re.DOTALL)
    if style_match:
        style = style_match.group(1)
        double_open = len(re.findall(r"\{\{", style))
        double_close = len(re.findall(r"\}\}", style))
        if double_open > 0:
            errors.append(f"CSS contains {double_open} doubled '{{{{' sequences (template escaping bug)")
        if double_close > 0:
            errors.append(f"CSS contains {double_close} doubled '}}}}' sequences (template escaping bug)")

    # Check for doubled braces in script blocks
    script_match = re.search(r"<script>(.*?)</script>", content, re.DOTALL)
    if script_match:
        script = script_match.group(1)
        # Template literal ${{ is invalid JS (should be ${)
        template_bugs = len(re.findall(r"\$\{\{", script))
        if template_bugs > 0:
            errors.append(f"JS contains {template_bugs} '${{{{' sequences (should be '${{' in template literals)")

    # Check required element IDs are present
    required_ids = {"stats", "updated", "lineage", "routes", "queue", "sparkline", "gaps", "demotions", "runs"}
    for eid in required_ids:
        if f'id="{eid}"' not in content:
            errors.append(f"missing required element id='{eid}'")

    return errors


def load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def write_json(path: Path, data: dict) -> None:
    """Atomic JSON write: write to temp file then rename to avoid partial writes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(json.dumps(data, indent=2) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def require(path: Path) -> str:
    return path.read_text()


def collect_headings(markdown: str) -> set[str]:
    return {
        match.group(1).strip()
        for match in re.finditer(r"^##\s+(.+?)\s*$", markdown, flags=re.MULTILINE)
    }


def parse_acceptance_criteria(spec_text: str) -> list[int]:
    numbers: list[int] = []
    in_section = False
    for line in spec_text.splitlines():
        if line.startswith("## "):
            in_section = line.strip() == "## Acceptance Criteria"
            continue
        if not in_section:
            continue
        match = re.match(r"^(\d+)\.\s+\S+", line)
        if match:
            numbers.append(int(match.group(1)))
    return numbers


def detect_cycle(graph: dict) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()
    by_id = {segment["id"]: segment for segment in graph.get("segments", []) if isinstance(segment, dict) and "id" in segment}

    def walk(node_id: str) -> bool:
        if node_id in visited:
            return False
        if node_id in visiting:
            return True
        visiting.add(node_id)
        for dep in by_id.get(node_id, {}).get("depends_on", []):
            if isinstance(dep, str) and walk(dep):
                return True
        visiting.remove(node_id)
        visited.add(node_id)
        return False

    return any(walk(node_id) for node_id in by_id)


def validate_manifest(manifest: dict) -> list[str]:
    errors: list[str] = []
    for key in ("task_id", "created_at", "raw_input", "stage"):
        if key not in manifest:
            errors.append(f"manifest missing key: {key}")
    stage = manifest.get("stage")
    if stage not in ALLOWED_STAGE_TRANSITIONS:
        errors.append(f"manifest has invalid stage: {stage!r}")
    classification = manifest.get("classification")
    if classification is not None:
        ctype = classification.get("type")
        if ctype not in VALID_CLASSIFICATION_TYPES:
            errors.append(f"classification.type invalid: {ctype!r}")
        risk = classification.get("risk_level")
        if risk not in VALID_RISK_LEVELS:
            errors.append(f"classification.risk_level invalid: {risk!r}")
        domains = classification.get("domains")
        if not isinstance(domains, list) or not domains:
            errors.append("classification.domains must be a non-empty array")
        else:
            for domain in domains:
                if domain not in VALID_DOMAINS:
                    errors.append(f"classification domain invalid: {domain!r}")
    return errors


def compute_fast_track(manifest: dict) -> bool:
    """Determine if a task qualifies for fast-track execution.

    Fast-track when ALL conditions met:
    - risk_level is "low"
    - exactly 1 domain
    - classification exists
    """
    classification = manifest.get("classification")
    if not isinstance(classification, dict):
        return False
    if classification.get("risk_level") != "low":
        return False
    domains = classification.get("domains", [])
    if not isinstance(domains, list) or len(domains) != 1:
        return False
    return True


def apply_fast_track(task_dir: Path) -> bool:
    """Check fast-track eligibility and write to manifest. Returns True if fast-tracked."""
    manifest_path = task_dir / "manifest.json"
    manifest = load_json(manifest_path)
    fast = compute_fast_track(manifest)
    manifest["fast_track"] = fast
    write_json(manifest_path, manifest)
    return fast


def validate_task_artifacts(task_dir: Path, strict: bool = False) -> list[str]:
    errors: list[str] = []
    manifest_path = task_dir / "manifest.json"
    spec_path = task_dir / "spec.md"
    plan_path = task_dir / "plan.md"
    graph_path = task_dir / "execution-graph.json"

    try:
        manifest = load_json(manifest_path)
    except FileNotFoundError:
        return [f"missing required file: {manifest_path}"]
    except json.JSONDecodeError as exc:
        return [f"invalid JSON in {manifest_path}: {exc}"]

    errors.extend(validate_manifest(manifest))

    try:
        spec_text = require(spec_path)
    except FileNotFoundError:
        return errors + [f"missing required file: {spec_path}"]

    spec_headings = collect_headings(spec_text)
    for heading in REQUIRED_SPEC_HEADINGS:
        if heading not in spec_headings:
            errors.append(f"spec missing heading: {heading}")

    criteria_numbers = parse_acceptance_criteria(spec_text)
    if not criteria_numbers:
        errors.append("spec has no acceptance criteria")
    else:
        expected = list(range(1, len(criteria_numbers) + 1))
        if criteria_numbers != expected:
            errors.append(f"acceptance criteria numbering must be contiguous from 1: got {criteria_numbers}")

    if plan_path.exists():
        plan_text = require(plan_path)
        for heading in REQUIRED_PLAN_HEADINGS:
            if heading not in collect_headings(plan_text):
                errors.append(f"plan missing heading: {heading}")
        # Validate Reference Code paths exist in repo
        in_ref_section = False
        for line in plan_text.splitlines():
            if line.startswith("## "):
                in_ref_section = line.strip() == "## Reference Code"
                continue
            if not in_ref_section:
                continue
            # Look for file paths like `hooks/foo.py` or **`src/bar.ts`**
            for match in re.finditer(r"`([^`]+\.[a-zA-Z]{1,5})`", line):
                ref_path = match.group(1)
                if "to-be-created" in line.lower():
                    continue
                full = task_dir.parent.parent / ref_path
                if not full.exists():
                    errors.append(f"plan Reference Code path does not exist: {ref_path}")
    elif strict:
        errors.append(f"missing required file: {plan_path}")

    if graph_path.exists():
        try:
            graph = load_json(graph_path)
        except json.JSONDecodeError as exc:
            errors.append(f"invalid JSON in {graph_path}: {exc}")
            graph = {}
        segments = graph.get("segments")
        if not isinstance(segments, list) or not segments:
            errors.append("execution graph must contain a non-empty segments array")
        else:
            seen_files: dict[str, str] = {}
            segment_ids: list[str] = []
            criteria_set = set(criteria_numbers)
            for segment in segments:
                if not isinstance(segment, dict):
                    errors.append("every segment must be an object")
                    continue
                segment_id = segment.get("id")
                if not segment_id or not isinstance(segment_id, str):
                    errors.append("every segment must have a string id")
                    continue
                segment_ids.append(segment_id)
                executor = segment.get("executor")
                if executor not in VALID_EXECUTORS:
                    errors.append(f"{segment_id}: invalid executor {executor!r}")
                files_expected = segment.get("files_expected")
                if not isinstance(files_expected, list) or not files_expected:
                    errors.append(f"{segment_id}: files_expected must be a non-empty array")
                else:
                    for file_path in files_expected:
                        if not isinstance(file_path, str):
                            errors.append(f"{segment_id}: file path must be a string")
                            continue
                        if Path(file_path).is_absolute() or ".." in Path(file_path).parts:
                            errors.append(f"{segment_id}: file path must stay inside repo: {file_path}")
                        owner = seen_files.get(file_path)
                        if owner and owner != segment_id:
                            errors.append(f"file {file_path} appears in multiple segments: {owner}, {segment_id}")
                        else:
                            seen_files[file_path] = segment_id
                depends_on = segment.get("depends_on", [])
                if not isinstance(depends_on, list):
                    errors.append(f"{segment_id}: depends_on must be an array")
                criteria_ids = segment.get("criteria_ids")
                if not isinstance(criteria_ids, list) or not criteria_ids:
                    errors.append(f"{segment_id}: criteria_ids must be a non-empty array")
                else:
                    for criterion_id in criteria_ids:
                        if criterion_id not in criteria_set:
                            errors.append(f"{segment_id}: criteria_id {criterion_id!r} does not exist in spec")
            if len(segment_ids) != len(set(segment_ids)):
                errors.append("execution graph segment ids must be unique")
            by_id = {segment.get("id"): segment for segment in segments if isinstance(segment, dict) and segment.get("id")}
            for segment_id, segment in by_id.items():
                for dep in segment.get("depends_on", []):
                    if dep not in by_id:
                        errors.append(f"{segment_id}: depends_on references missing segment {dep}")
            if detect_cycle(graph):
                errors.append("execution graph must be acyclic")
            covered = {
                criterion_id
                for segment in segments
                if isinstance(segment, dict)
                for criterion_id in segment.get("criteria_ids", [])
                if isinstance(criterion_id, int)
            }
            missing = sorted(set(criteria_numbers) - covered)
            if missing:
                errors.append(f"uncovered acceptance criteria: {missing}")
    elif strict:
        errors.append(f"missing required file: {graph_path}")

    errors.extend(validate_repair_log(task_dir))
    errors.extend(validate_retrospective(task_dir))
    return errors


def validate_repair_log(task_dir: Path) -> list[str]:
    path = task_dir / "repair-log.json"
    if not path.exists():
        return []
    try:
        data = load_json(path)
    except json.JSONDecodeError as exc:
        return [f"invalid JSON in {path}: {exc}"]
    errors: list[str] = []
    batches = data.get("batches")
    if not isinstance(batches, list):
        return ["repair-log batches must be an array"]
    seen_batch_ids: set[str] = set()
    for batch in batches:
        if not isinstance(batch, dict):
            errors.append("repair-log batch must be an object")
            continue
        batch_id = batch.get("batch_id")
        if not isinstance(batch_id, str):
            errors.append("repair-log batch missing string batch_id")
        elif batch_id in seen_batch_ids:
            errors.append(f"duplicate repair batch id: {batch_id}")
        else:
            seen_batch_ids.add(batch_id)
        tasks = batch.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            errors.append(f"{batch_id}: tasks must be a non-empty array")
            continue
        for task in tasks:
            if not isinstance(task, dict):
                errors.append(f"{batch_id}: task must be an object")
                continue
            executor = task.get("assigned_executor")
            if executor not in VALID_EXECUTORS:
                errors.append(f"{batch_id}: invalid assigned_executor {executor!r}")
            files = task.get("files_to_modify")
            if not isinstance(files, list) or not files:
                errors.append(f"{batch_id}: files_to_modify must be a non-empty array")
            retry_count = task.get("retry_count", 0)
            if not isinstance(retry_count, int) or retry_count < 0:
                errors.append(f"{batch_id}: retry_count must be a non-negative integer")
    return errors


def validate_retrospective(task_dir: Path) -> list[str]:
    path = task_dir / "task-retrospective.json"
    if not path.exists():
        return []
    try:
        data = load_json(path)
    except json.JSONDecodeError as exc:
        return [f"invalid JSON in {path}: {exc}"]
    errors: list[str] = []
    required = {
        "task_id": str,
        "task_outcome": str,
        "task_type": str,
        "task_domains": str,
        "task_risk_level": str,
        "findings_by_auditor": dict,
        "findings_by_category": dict,
        "executor_repair_frequency": dict,
        "spec_review_iterations": int,
        "repair_cycle_count": int,
        "subagent_spawn_count": int,
        "wasted_spawns": int,
        "auditor_zero_finding_streaks": dict,
        "executor_zero_repair_streak": int,
    }
    for key, expected_type in required.items():
        value = data.get(key)
        if not isinstance(value, expected_type):
            errors.append(f"retrospective field {key!r} must be {expected_type.__name__}")
    for key in ("quality_score", "cost_score", "efficiency_score"):
        if key in data:
            value = data[key]
            if not isinstance(value, (int, float)) or not 0 <= value <= 1:
                errors.append(f"retrospective field {key!r} must be a number in [0, 1]")
    return errors


def transition_task(task_dir: Path, next_stage: str, *, force: bool = False) -> tuple[str, dict]:
    manifest_path = task_dir / "manifest.json"
    manifest = load_json(manifest_path)
    current_stage = manifest.get("stage")
    if next_stage not in ALLOWED_STAGE_TRANSITIONS:
        raise ValueError(f"Unknown stage: {next_stage}")
    if not force and next_stage not in ALLOWED_STAGE_TRANSITIONS.get(current_stage, set()):
        raise ValueError(f"Illegal stage transition: {current_stage} -> {next_stage}")
    manifest["stage"] = next_stage
    if next_stage == "DONE":
        manifest["completion_at"] = now_iso()
    if next_stage == "FAILED" and manifest.get("blocked_reason") is None:
        manifest["blocked_reason"] = "transitioned to FAILED"
    write_json(manifest_path, manifest)
    return current_stage, manifest


def next_command_for_stage(stage: str) -> str:
    return NEXT_COMMAND.get(stage, "Unknown stage")


def find_active_tasks(root: Path) -> list[Path]:
    dynos_dir = root / ".dynos"
    if not dynos_dir.exists():
        return []
    tasks: list[Path] = []
    for manifest_path in dynos_dir.glob("task-*/manifest.json"):
        try:
            manifest = load_json(manifest_path)
        except (json.JSONDecodeError, FileNotFoundError, OSError):
            continue
        if manifest.get("stage") not in {"DONE", "FAILED", "CANCELLED"}:
            tasks.append(manifest_path.parent)
    tasks.sort()
    return tasks


def check_segment_ownership(task_dir: Path, segment_id: str, files: Iterable[str]) -> list[str]:
    graph = load_json(task_dir / "execution-graph.json")
    for segment in graph.get("segments", []):
        if segment.get("id") == segment_id:
            allowed = set(segment.get("files_expected", []))
            return [file_path for file_path in files if file_path not in allowed]
    raise ValueError(f"Unknown segment id: {segment_id}")


def collect_retrospectives(root: Path) -> list[dict]:
    retrospectives: list[dict] = []
    for path in sorted((root / ".dynos").glob("task-*/task-retrospective.json")):
        try:
            data = load_json(path)
        except (json.JSONDecodeError, FileNotFoundError, OSError):
            continue
        data["_path"] = str(path)
        retrospectives.append(data)
    return retrospectives


def retrospective_task_ids(root: Path) -> list[str]:
    return [item.get("task_id") for item in collect_retrospectives(root) if isinstance(item.get("task_id"), str)]


def task_recency_index(root: Path, task_id: str | None) -> int | None:
    if not task_id:
        return None
    task_ids = retrospective_task_ids(root)
    if task_id not in task_ids:
        return None
    return len(task_ids) - 1 - task_ids.index(task_id)


def tasks_since(root: Path, task_id: str | None) -> int | None:
    return task_recency_index(root, task_id)


def trajectories_store_path(root: Path) -> Path:
    return _persistent_project_dir(root) / "trajectories.json"


def learned_agents_root(root: Path) -> Path:
    return _persistent_project_dir(root) / "learned-agents"


def learned_registry_path(root: Path) -> Path:
    return learned_agents_root(root) / "registry.json"


def benchmark_history_path(root: Path) -> Path:
    return _persistent_project_dir(root) / "benchmarks" / "history.json"


def benchmark_index_path(root: Path) -> Path:
    return _persistent_project_dir(root) / "benchmarks" / "index.json"


def automation_queue_path(root: Path) -> Path:
    return root / ".dynos" / "automation" / "queue.json"


def ensure_trajectory_store(root: Path) -> dict:
    path = trajectories_store_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or not path.read_text().strip():
        store = {"version": 1, "updated_at": now_iso(), "trajectories": []}
        write_json(path, store)
        return store
    data = load_json(path)
    if not isinstance(data, dict) or "trajectories" not in data:
        store = {"version": 1, "updated_at": now_iso(), "trajectories": []}
        write_json(path, store)
        return store
    return data


def _safe_float(value: object, default: float = 0.0) -> float:
    return float(value) if isinstance(value, (int, float)) else default


def make_trajectory_entry(retrospective: dict) -> dict:
    findings = retrospective.get("findings_by_category", {})
    categories = findings if isinstance(findings, dict) else {}
    task_domains = retrospective.get("task_domains", "")
    domains = [part.strip() for part in task_domains.split(",") if part.strip()]
    quality_score = _safe_float(retrospective.get("quality_score"), 0.0)
    cost_score = _safe_float(retrospective.get("cost_score"), 0.0)
    efficiency_score = _safe_float(retrospective.get("efficiency_score"), 0.0)
    if quality_score == 0 and cost_score == 0 and efficiency_score == 0:
        total_findings = sum(v for v in categories.values() if isinstance(v, int))
        repair_cycles = int(retrospective.get("repair_cycle_count", 0) or 0)
        spec_iterations = int(retrospective.get("spec_review_iterations", 0) or 0)
        total_tokens = _safe_float(retrospective.get("total_token_usage"), 0.0)
        subagent_spawns = int(retrospective.get("subagent_spawn_count", 0) or 0)
        risk_level = str(retrospective.get("task_risk_level", "medium"))
        agent_source = retrospective.get("agent_source", {})
        # Quality: cap at 0.9 when zero findings (may indicate auditor gaps)
        quality_score = 0.9 if total_findings == 0 else 1 / (1 + total_findings)
        # Efficiency: penalize repair cycles and excess spec iterations
        efficiency_score = max(0.0, 1 - (repair_cycles / 3) - (max(0, spec_iterations - 1) * 0.1))
        # Cost: normalize by risk-level token budget per spawn
        budget_per_spawn = {"low": 8000, "medium": 12000, "high": 18000, "critical": 25000}.get(risk_level, 12000)
        avg_tokens = total_tokens / max(1, subagent_spawns)
        cost_score = max(0.0, min(1.0, 1 / (1 + (avg_tokens / budget_per_spawn))))
        # Penalty: all-generic routing means no learned leverage
        if isinstance(agent_source, dict) and agent_source and all(v == "generic" for v in agent_source.values()):
            cost_score = max(0.0, cost_score - 0.05)
    wq, we, wc = COMPOSITE_WEIGHTS
    reward = round(wq * quality_score + we * efficiency_score + wc * cost_score, 6)
    return {
        "trajectory_id": retrospective["task_id"],
        "source_task_id": retrospective["task_id"],
        "version": 1,
        "created_at": now_iso(),
        "state": {
            "task_type": retrospective.get("task_type"),
            "task_domains": domains,
            "task_risk_level": retrospective.get("task_risk_level"),
            "findings_by_category": categories,
            "spec_review_iterations": int(retrospective.get("spec_review_iterations", 0) or 0),
            "repair_cycle_count": int(retrospective.get("repair_cycle_count", 0) or 0),
            "subagent_spawn_count": int(retrospective.get("subagent_spawn_count", 0) or 0),
            "wasted_spawns": int(retrospective.get("wasted_spawns", 0) or 0),
        },
        "action_summary": {
            "executor_repair_frequency": retrospective.get("executor_repair_frequency", {}),
            "auditor_zero_finding_streaks": retrospective.get("auditor_zero_finding_streaks", {}),
        },
        "reward": {
            "quality_score": quality_score,
            "cost_score": cost_score,
            "efficiency_score": efficiency_score,
            "composite_reward": reward,
        },
        "outcome": retrospective.get("task_outcome", "UNKNOWN"),
    }


def rebuild_trajectory_store(root: Path) -> dict:
    store = ensure_trajectory_store(root)
    trajectories = [make_trajectory_entry(item) for item in collect_retrospectives(root)]
    store["version"] = 1
    store["updated_at"] = now_iso()
    store["trajectories"] = sorted(trajectories, key=lambda item: item["trajectory_id"])
    write_json(trajectories_store_path(root), store)
    return store


def _domain_overlap(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def trajectory_similarity(query_state: dict, candidate: dict) -> float:
    candidate_state = candidate.get("state", {})
    score = 0.0
    if query_state.get("task_type") == candidate_state.get("task_type"):
        score += 0.35
    if query_state.get("task_risk_level") == candidate_state.get("task_risk_level"):
        score += 0.2
    score += 0.25 * _domain_overlap(
        query_state.get("task_domains", []), candidate_state.get("task_domains", [])
    )
    numeric_keys = ["repair_cycle_count", "subagent_spawn_count", "wasted_spawns", "spec_review_iterations"]
    numeric_score = 0.0
    for key in numeric_keys:
        qv = float(query_state.get(key, 0))
        cv = float(candidate_state.get(key, 0))
        numeric_score += 1 / (1 + abs(qv - cv))
    score += 0.2 * (numeric_score / len(numeric_keys))
    return round(score, 6)


def search_trajectories(root: Path, query_state: dict, limit: int = 3) -> list[dict]:
    store = ensure_trajectory_store(root)
    ranked = []
    for entry in store.get("trajectories", []):
        similarity = trajectory_similarity(query_state, entry)
        ranked.append({"similarity": similarity, "trajectory": entry})
    ranked.sort(key=lambda item: (-item["similarity"], item["trajectory"]["trajectory_id"]))
    return ranked[:limit]


def ensure_learned_registry(root: Path) -> dict:
    registry_path = learned_registry_path(root)
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    for dirname in ("auditors", "executors", "skills", ".archive", ".staging"):
        (registry_path.parent / dirname).mkdir(parents=True, exist_ok=True)
    if not registry_path.exists():
        registry = {"version": 1, "updated_at": now_iso(), "agents": [], "benchmarks": []}
        write_json(registry_path, registry)
        return registry
    data = load_json(registry_path)
    if not isinstance(data, dict) or "agents" not in data:
        registry = {"version": 1, "updated_at": now_iso(), "agents": [], "benchmarks": []}
        write_json(registry_path, registry)
        return registry
    return data


def ensure_benchmark_history(root: Path) -> dict:
    path = benchmark_history_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or not path.read_text().strip():
        history = {"version": 1, "updated_at": now_iso(), "runs": []}
        write_json(path, history)
        return history
    data = load_json(path)
    if not isinstance(data, dict) or "runs" not in data:
        history = {"version": 1, "updated_at": now_iso(), "runs": []}
        write_json(path, history)
        return history
    return data


def ensure_benchmark_index(root: Path) -> dict:
    path = benchmark_index_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or not path.read_text().strip():
        index = {"version": 1, "updated_at": now_iso(), "fixtures": []}
        write_json(path, index)
        return index
    data = load_json(path)
    if not isinstance(data, dict) or "fixtures" not in data:
        index = {"version": 1, "updated_at": now_iso(), "fixtures": []}
        write_json(path, index)
        return index
    return data


def ensure_automation_queue(root: Path) -> dict:
    path = automation_queue_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or not path.read_text().strip():
        queue = {"version": 1, "updated_at": now_iso(), "items": []}
        write_json(path, queue)
        return queue
    data = load_json(path)
    if not isinstance(data, dict) or "items" not in data:
        queue = {"version": 1, "updated_at": now_iso(), "items": []}
        write_json(path, queue)
        return queue
    return data


def benchmark_policy_config(root: Path) -> dict:
    path = _persistent_project_dir(root) / "policy.json"
    default = {
        "freshness_task_window": 5,
        "active_rebenchmark_task_window": 3,
        "shadow_rebenchmark_task_window": 2,
    }
    if not path.exists() or not path.read_text().strip():
        write_json(path, default)
        return default
    try:
        data = load_json(path)
    except (json.JSONDecodeError, FileNotFoundError, OSError):
        write_json(path, default)
        return default
    merged = {**default, **{k: v for k, v in data.items() if isinstance(v, int) and v >= 0}}
    if merged != data:
        write_json(path, merged)
    return merged


def compute_benchmark_summary(benchmarks: list[dict]) -> dict:
    if not benchmarks:
        return {
            "sample_count": 0,
            "mean_quality": 0.0,
            "mean_cost": 0.0,
            "mean_efficiency": 0.0,
            "mean_composite": 0.0,
        }
    quality = sum(_safe_float(item.get("quality_score")) for item in benchmarks) / len(benchmarks)
    cost = sum(_safe_float(item.get("cost_score")) for item in benchmarks) / len(benchmarks)
    efficiency = sum(_safe_float(item.get("efficiency_score")) for item in benchmarks) / len(benchmarks)
    wq, we, wc = COMPOSITE_WEIGHTS
    composite = sum(
        wq * _safe_float(item.get("quality_score"))
        + we * _safe_float(item.get("efficiency_score"))
        + wc * _safe_float(item.get("cost_score"))
        for item in benchmarks
    ) / len(benchmarks)
    return {
        "sample_count": len(benchmarks),
        "mean_quality": round(quality, 6),
        "mean_cost": round(cost, 6),
        "mean_efficiency": round(efficiency, 6),
        "mean_composite": round(composite, 6),
    }


def _category_summaries(results: list[dict]) -> dict[str, dict]:
    grouped: dict[str, list[dict]] = {}
    for item in results:
        category = str(item.get("category", "default"))
        grouped.setdefault(category, []).append(item)
    return {category: compute_benchmark_summary(items) for category, items in grouped.items()}


def evaluate_candidate(candidate_results: list[dict], baseline_results: list[dict], policy: dict | None = None) -> dict:
    policy = policy or {}
    candidate = compute_benchmark_summary(candidate_results)
    baseline = compute_benchmark_summary(baseline_results)
    delta_quality = round(candidate["mean_quality"] - baseline["mean_quality"], 6)
    delta_composite = round(candidate["mean_composite"] - baseline["mean_composite"], 6)
    min_samples = int(policy.get("min_samples", 3) or 3)
    min_quality_delta = float(policy.get("min_quality_delta", 0.03) or 0.03)
    min_composite_delta = float(policy.get("min_composite_delta", 0.02) or 0.02)
    must_pass_categories = [str(item) for item in policy.get("must_pass_categories", [])]
    candidate_by_category = _category_summaries(candidate_results)
    baseline_by_category = _category_summaries(baseline_results)
    category_regressions: list[dict] = []
    for category in must_pass_categories:
        c_summary = candidate_by_category.get(category, compute_benchmark_summary([]))
        b_summary = baseline_by_category.get(category, compute_benchmark_summary([]))
        quality_delta = round(c_summary["mean_quality"] - b_summary["mean_quality"], 6)
        composite_delta = round(c_summary["mean_composite"] - b_summary["mean_composite"], 6)
        regressed = quality_delta < 0 or composite_delta < 0
        category_regressions.append(
            {
                "category": category,
                "candidate": c_summary,
                "baseline": b_summary,
                "delta_quality": quality_delta,
                "delta_composite": composite_delta,
                "regressed": regressed,
            }
        )
    blocked_by_category = any(item["regressed"] for item in category_regressions)
    recommendation = "reject"
    mode = "shadow"
    if blocked_by_category:
        recommendation = "reject"
        mode = "shadow"
    elif candidate["sample_count"] >= min_samples and delta_quality >= min_quality_delta and delta_composite >= min_composite_delta:
        recommendation = "promote_replace"
        mode = "replace"
    elif candidate["sample_count"] >= min_samples and delta_quality >= 0 and delta_composite >= 0:
        recommendation = "promote_alongside"
        mode = "alongside"
    elif candidate["sample_count"] >= 1:
        recommendation = "keep_shadow"
        mode = "shadow"
    return {
        "candidate": candidate,
        "baseline": baseline,
        "policy": {
            "min_samples": min_samples,
            "min_quality_delta": min_quality_delta,
            "min_composite_delta": min_composite_delta,
            "must_pass_categories": must_pass_categories,
        },
        "candidate_by_category": candidate_by_category,
        "baseline_by_category": baseline_by_category,
        "category_regressions": category_regressions,
        "blocked_by_category": blocked_by_category,
        "delta_quality": delta_quality,
        "delta_composite": delta_composite,
        "recommendation": recommendation,
        "target_mode": mode,
    }


def register_learned_agent(
    root: Path,
    *,
    agent_name: str,
    role: str,
    task_type: str,
    path: str,
    generated_from: str,
    source: str = "learned",
    item_kind: str = "agent",
) -> dict:
    registry = ensure_learned_registry(root)
    agents = registry.setdefault("agents", [])
    existing = next(
        (
            agent
            for agent in agents
            if agent.get("agent_name") == agent_name
            and agent.get("role") == role
            and agent.get("task_type") == task_type
            and agent.get("item_kind", "agent") == item_kind
        ),
        None,
    )
    record = {
        "item_kind": item_kind,
        "agent_name": agent_name,
        "role": role,
        "task_type": task_type,
        "source": source,
        "path": path,
        "generated_from": generated_from,
        "generated_at": now_iso(),
        "mode": "shadow",
        "status": "active",
        "benchmark_summary": compute_benchmark_summary([]),
    }
    if existing:
        existing.update(record)
    else:
        agents.append(record)
    registry["updated_at"] = now_iso()
    write_json(learned_registry_path(root), registry)
    return registry


def apply_evaluation_to_registry(
    root: Path,
    agent_name: str,
    role: str,
    task_type: str,
    evaluation: dict,
    *,
    item_kind: str = "agent",
    context: dict | None = None,
) -> dict:
    registry = ensure_learned_registry(root)
    matched = None
    for agent in registry.get("agents", []):
        if (
            agent.get("agent_name") == agent_name
            and agent.get("role") == role
            and agent.get("task_type") == task_type
            and agent.get("item_kind", "agent") == item_kind
        ):
            matched = agent
            break
    if matched is None:
        raise ValueError(f"Registry entry not found: {agent_name} ({role}, {task_type}, {item_kind})")
    matched["benchmark_summary"] = evaluation["candidate"]
    matched["baseline_summary"] = evaluation["baseline"]
    matched["last_evaluation"] = {
        "evaluated_at": now_iso(),
        "delta_quality": evaluation["delta_quality"],
        "delta_composite": evaluation["delta_composite"],
        "recommendation": evaluation["recommendation"],
        "blocked_by_category": evaluation.get("blocked_by_category", False),
    }
    if context:
        matched["last_evaluation"].update(context)
    matched["last_benchmarked_task_offset"] = 0
    previous_mode = matched.get("mode", "shadow")
    matched["mode"] = evaluation["target_mode"]
    matched["route_allowed"] = matched["mode"] in {"alongside", "replace"}
    if evaluation["recommendation"] == "reject" and previous_mode in {"alongside", "replace"}:
        matched["status"] = "demoted_on_regression"
        matched["route_allowed"] = False
    elif matched["mode"] == "shadow":
        matched["status"] = "active_shadow"
    else:
        matched["status"] = "active"
    benchmarks = registry.setdefault("benchmarks", [])
    benchmarks.append(
        {
            "agent_name": agent_name,
            "item_kind": item_kind,
            "role": role,
            "task_type": task_type,
            "evaluated_at": now_iso(),
            "recommendation": evaluation["recommendation"],
            "delta_quality": evaluation["delta_quality"],
            "delta_composite": evaluation["delta_composite"],
            "blocked_by_category": evaluation.get("blocked_by_category", False),
            **(context or {}),
        }
    )
    if len(benchmarks) > MAX_REGISTRY_BENCHMARKS:
        registry["benchmarks"] = benchmarks[-MAX_REGISTRY_BENCHMARKS:]
    registry["updated_at"] = now_iso()
    write_json(learned_registry_path(root), registry)
    return registry


MAX_BENCHMARK_HISTORY_RUNS = 200
MAX_REGISTRY_BENCHMARKS = 200


def append_benchmark_run(root: Path, run: dict) -> dict:
    history = ensure_benchmark_history(root)
    runs = history.setdefault("runs", [])
    runs.append(run)
    if len(runs) > MAX_BENCHMARK_HISTORY_RUNS:
        history["runs"] = runs[-MAX_BENCHMARK_HISTORY_RUNS:]
    history["updated_at"] = now_iso()
    write_json(benchmark_history_path(root), history)
    return history


def upsert_fixture_trace(root: Path, fixture_record: dict) -> dict:
    index = ensure_benchmark_index(root)
    fixtures = index.setdefault("fixtures", [])
    matched = None
    for item in fixtures:
        if item.get("fixture_id") == fixture_record.get("fixture_id"):
            matched = item
            break
    if matched is None:
        fixtures.append(fixture_record)
    else:
        matched.update(fixture_record)
    index["updated_at"] = now_iso()
    write_json(benchmark_index_path(root), index)
    return index


def enqueue_automation_item(root: Path, item: dict) -> dict:
    queue = ensure_automation_queue(root)
    queue.setdefault("items", []).append(item)
    queue["updated_at"] = now_iso()
    write_json(automation_queue_path(root), queue)
    return queue


def replace_automation_queue(root: Path, items: list[dict]) -> dict:
    queue = ensure_automation_queue(root)
    queue["items"] = items
    queue["updated_at"] = now_iso()
    write_json(automation_queue_path(root), queue)
    return queue


def benchmark_fixtures_dir(root: Path) -> Path:
    return root / "benchmarks" / "fixtures"


def iter_benchmark_fixtures(root: Path) -> list[Path]:
    candidates = [benchmark_fixtures_dir(root), root / "benchmarks" / "generated"]
    fixtures: list[Path] = []
    for directory in candidates:
        if directory.exists():
            fixtures.extend(path for path in directory.rglob("*.json") if path.is_file() and not path.is_symlink())
    return sorted(fixtures)


def matching_fixtures_for_registry_entry(root: Path, entry: dict) -> list[Path]:
    matches: list[Path] = []
    for fixture_path in iter_benchmark_fixtures(root):
        try:
            fixture = load_json(fixture_path)
        except (json.JSONDecodeError, FileNotFoundError, OSError):
            continue
        if fixture.get("item_kind", "agent") != entry.get("item_kind", "agent"):
            continue
        if fixture.get("target_name") != entry.get("agent_name"):
            continue
        if fixture.get("role") != entry.get("role"):
            continue
        if fixture.get("task_type") != entry.get("task_type"):
            continue
        matches.append(fixture_path)
    return matches


def queue_identity(item: dict) -> tuple[str, str]:
    return (str(item.get("agent_name", "")), str(item.get("fixture_path", "")))


def retrospective_benchmark_score(retrospective: dict) -> dict:
    quality = _safe_float(retrospective.get("quality_score"), 0.0)
    cost = _safe_float(retrospective.get("cost_score"), 0.0)
    efficiency = _safe_float(retrospective.get("efficiency_score"), 0.0)
    wq, we, wc = COMPOSITE_WEIGHTS
    composite = wq * quality + we * efficiency + wc * cost
    return {
        "quality_score": round(quality, 6),
        "cost_score": round(cost, 6),
        "efficiency_score": round(efficiency, 6),
        "composite_score": round(composite, 6),
    }


def collect_task_summaries(root: Path) -> list[dict]:
    summaries: list[dict] = []
    for retrospective in collect_retrospectives(root):
        task_id = retrospective.get("task_id")
        if not isinstance(task_id, str):
            continue
        task_dir = root / ".dynos" / task_id
        manifest = {}
        if (task_dir / "manifest.json").exists():
            try:
                manifest = load_json(task_dir / "manifest.json")
            except Exception:
                manifest = {}
        task_domains = retrospective.get("task_domains", "")
        domains = [part.strip() for part in str(task_domains).split(",") if part.strip()]
        summaries.append(
            {
                "task_id": task_id,
                "task_type": retrospective.get("task_type"),
                "domains": domains,
                "risk_level": retrospective.get("task_risk_level"),
                "title": manifest.get("title") or manifest.get("raw_input") or task_id,
                "score": retrospective_benchmark_score(retrospective),
                "retrospective_path": str(task_dir / "task-retrospective.json"),
            }
        )
    summaries.sort(key=lambda item: item["task_id"])
    return summaries


def synthesize_fixture_for_entry(root: Path, entry: dict, *, limit: int = 5) -> dict | None:
    task_type = entry.get("task_type")
    generated_from = entry.get("generated_from")
    task_summaries = [item for item in collect_task_summaries(root) if item.get("task_type") == task_type]
    if not task_summaries:
        return None
    source_task = next((item for item in task_summaries if item["task_id"] == generated_from), None)
    ranked = sorted(
        task_summaries,
        key=lambda item: (
            0 if item["task_id"] == generated_from else 1,
            -float(item["score"]["composite_score"]),
            item["task_id"],
        ),
    )
    candidate_tasks = ranked[: max(1, min(limit, len(ranked)))]
    baseline_pool = [item for item in task_summaries if item["task_id"] not in {task["task_id"] for task in candidate_tasks}]
    if not baseline_pool:
        baseline_pool = task_summaries
    baseline_quality = sum(item["score"]["quality_score"] for item in baseline_pool) / len(baseline_pool)
    baseline_cost = sum(item["score"]["cost_score"] for item in baseline_pool) / len(baseline_pool)
    baseline_efficiency = sum(item["score"]["efficiency_score"] for item in baseline_pool) / len(baseline_pool)
    baseline_summary = {
        "quality_score": round(baseline_quality, 6),
        "cost_score": round(baseline_cost, 6),
        "efficiency_score": round(baseline_efficiency, 6),
    }
    wq, we, wc = COMPOSITE_WEIGHTS
    baseline_summary["composite_score"] = round(
        wq * baseline_summary["quality_score"]
        + we * baseline_summary["efficiency_score"]
        + wc * baseline_summary["cost_score"],
        6,
    )
    fixture_cases = []
    for item in candidate_tasks:
        fixture_cases.append(
            {
                "case_id": item["task_id"],
                "category": item["domains"][0] if item["domains"] else "default",
                "source_task_id": item["task_id"],
                "baseline": dict(baseline_summary),
                "candidate": dict(item["score"]),
            }
        )
    slug = f"{entry.get('item_kind', 'agent')}-{entry.get('agent_name')}-{task_type}".replace("/", "-")
    fixture_dir = root / "benchmarks" / "generated"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    fixture_path = fixture_dir / f"{slug}.json"
    fixture = {
        "fixture_id": slug,
        "item_kind": entry.get("item_kind", "agent"),
        "target_name": entry.get("agent_name"),
        "role": entry.get("role"),
        "task_type": task_type,
        "source_tasks": [item["task_id"] for item in candidate_tasks],
        "baseline_tasks": [item["task_id"] for item in baseline_pool],
        "synthesis": {
            "synthesized_at": now_iso(),
            "source_task": source_task["task_id"] if source_task else None,
            "strategy": "task_retrospective_scores_vs_task_type_baseline",
            "candidate_limit": len(candidate_tasks),
        },
        "policy": {
            "min_samples": min(3, len(candidate_tasks)) if candidate_tasks else 3,
            "min_quality_delta": 0.03,
            "min_composite_delta": 0.02,
        },
        "cases": fixture_cases,
    }
    fixture_path.write_text(json.dumps(fixture, indent=2) + "\n")
    upsert_fixture_trace(
        root,
        {
            "fixture_id": fixture["fixture_id"],
            "fixture_path": str(fixture_path),
            "item_kind": fixture["item_kind"],
            "target_name": fixture["target_name"],
            "role": fixture["role"],
            "task_type": fixture["task_type"],
            "source_tasks": fixture["source_tasks"],
            "baseline_tasks": fixture["baseline_tasks"],
            "synthesized_at": fixture["synthesis"]["synthesized_at"],
            "strategy": fixture["synthesis"]["strategy"],
        },
    )
    return fixture


def entry_is_stale(root: Path, entry: dict) -> tuple[bool, int | None, int]:
    policy = benchmark_policy_config(root)
    freshness_window = int(policy.get("freshness_task_window", 5) or 5)
    offset = tasks_since(root, entry.get("last_evaluation", {}).get("source_tasks", [None])[-1] if entry.get("last_evaluation", {}).get("source_tasks") else entry.get("generated_from"))
    if offset is None:
        offset = tasks_since(root, entry.get("generated_from"))
    return (offset is not None and offset > freshness_window, offset, freshness_window)


def resolve_registry_route(root: Path, role: str, task_type: str, *, item_kind: str = "agent") -> dict:
    registry = ensure_learned_registry(root)
    candidates = []
    freshness_blocked = False
    for item in registry.get("agents", []):
        if item.get("item_kind", "agent") != item_kind:
            continue
        if item.get("role") != role or item.get("task_type") != task_type:
            continue
        if not item.get("route_allowed", item.get("mode") in {"alongside", "replace"}):
            continue
        if item.get("status") in {"demoted_on_regression", "archived"}:
            continue
        is_stale, stale_offset, freshness_window = entry_is_stale(root, item)
        if is_stale:
            freshness_blocked = True
            continue
        candidates.append(item)
    if not candidates:
        return {
            "role": role,
            "task_type": task_type,
            "item_kind": item_kind,
            "source": "generic",
            "mode": "default",
            "path": "built-in",
            "freshness_blocked": freshness_blocked,
        }
    candidates.sort(
        key=lambda item: (
            -float(item.get("benchmark_summary", {}).get("mean_composite", 0.0)),
            item.get("agent_name", ""),
        )
    )
    chosen = candidates[0]
    return {
        "role": role,
        "task_type": task_type,
        "item_kind": item_kind,
        "source": f"learned:{chosen['agent_name']}",
        "mode": chosen.get("mode", "shadow"),
        "path": chosen.get("path"),
        "route_allowed": chosen.get("route_allowed", False),
        "status": chosen.get("status", "active"),
        "composite": chosen.get("benchmark_summary", {}).get("mean_composite", 0.0),
        "freshness_blocked": False,
    }


def benchmark_fixture_score(result: dict) -> dict:
    if all(key in result for key in ("quality_score", "cost_score", "efficiency_score")):
        quality = _safe_float(result.get("quality_score"))
        cost = _safe_float(result.get("cost_score"))
        efficiency = _safe_float(result.get("efficiency_score"))
        composite = result.get("composite_score")
        wq, we, wc = COMPOSITE_WEIGHTS
        if not isinstance(composite, (int, float)):
            composite = wq * quality + we * efficiency + wc * cost
        return {
            "quality_score": round(quality, 6),
            "efficiency_score": round(efficiency, 6),
            "cost_score": round(cost, 6),
            "composite_score": round(float(composite), 6),
        }
    tests_passed = int(result.get("tests_passed", 0) or 0)
    tests_total = int(result.get("tests_total", 0) or 0)
    findings = int(result.get("findings", 0) or 0)
    files_touched = int(result.get("files_touched", 0) or 0)
    duration_seconds = float(result.get("duration_seconds", 0) or 0)
    tokens_used = float(result.get("tokens_used", 0) or 0)
    quality = 1.0 if tests_total == 0 else max(0.0, min(1.0, tests_passed / max(1, tests_total)))
    quality *= 1 / (1 + findings)
    efficiency = 1 / (1 + max(0.0, duration_seconds / 300))
    cost = 1 / (1 + max(0.0, tokens_used / 50000) + max(0.0, files_touched / 40))
    wq, we, wc = COMPOSITE_WEIGHTS
    composite = wq * quality + we * efficiency + wc * cost
    return {
        "quality_score": round(quality, 6),
        "efficiency_score": round(efficiency, 6),
        "cost_score": round(cost, 6),
        "composite_score": round(composite, 6),
    }
