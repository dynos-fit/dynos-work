#!/usr/bin/env python3
"""Core utilities, constants, and path helpers for dynos-work."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_EXECUTORS: set[str] = {
    "ui-executor",
    "backend-executor",
    "ml-executor",
    "db-executor",
    "refactor-executor",
    "testing-executor",
    "integration-executor",
    "docs-executor",
}

VALID_CLASSIFICATION_TYPES: set[str] = {
    "feature",
    "bugfix",
    "refactor",
    "migration",
    "ml",
    "full-stack",
}

VALID_DOMAINS: set[str] = {"ui", "backend", "db", "ml", "security"}
VALID_RISK_LEVELS: set[str] = {"low", "medium", "high", "critical"}

COMPOSITE_WEIGHTS: tuple[float, float, float] = (0.6, 0.25, 0.15)

STAGE_ORDER: list[str] = [
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

ALLOWED_STAGE_TRANSITIONS: dict[str, set[str]] = {
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
    "REPAIR_EXECUTION": {"CHECKPOINT_AUDIT", "REPAIR_PLANNING", "FAILED"},
    "FINAL_AUDIT": {"CHECKPOINT_AUDIT", "DONE", "FAILED"},
    "EXECUTION_GRAPH_BUILD": {"PRE_EXECUTION_SNAPSHOT", "EXECUTION", "FAILED"},
    "DONE": set(),
    "CANCELLED": set(),
    "FAILED": set(),
}

NEXT_COMMAND: dict[str, str] = {
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

TOKEN_ESTIMATES: dict[str, int] = {"opus": 45000, "sonnet": 25000, "haiku": 12000, "default": 20000}


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def now_iso() -> str:
    """Return current UTC time as ISO-8601 string with Z suffix."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_float(value: object, default: float = 0.0) -> float:
    """Safely convert a value to float, returning default if not numeric."""
    return float(value) if isinstance(value, (int, float)) else default


def _safe_int(value: object, default: int = 0) -> int:
    """Safely convert a value to int, returning default if not numeric."""
    return int(value) if isinstance(value, (int, float)) else default


def load_json(path: Path) -> dict:
    """Read and parse a JSON file."""
    return json.loads(path.read_text())


def write_json(path: Path, data: Any) -> None:
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
    """Read and return the text content of a file."""
    return path.read_text()


# ---------------------------------------------------------------------------
# Persistent project directory
# ---------------------------------------------------------------------------

def _persistent_project_dir(root: Path) -> Path:
    """Returns ~/.dynos/projects/{slug}/ for persistent project state.

    Pure path resolution — NO side effects. Does NOT create directories.
    Callers that need the directory to exist should call
    ensure_persistent_project_dir() instead.
    """
    resolved = str(root.resolve())
    dynos_home = Path(os.environ.get("DYNOS_HOME", str(Path.home() / ".dynos")))
    slug = resolved.strip("/").replace("/", "-")
    return dynos_home / "projects" / slug


def ensure_persistent_project_dir(root: Path) -> Path:
    """Returns ~/.dynos/projects/{slug}/, creating it if needed.

    Use this when you need to WRITE to the persistent directory.
    Use _persistent_project_dir() for read-only path resolution.
    """
    d = _persistent_project_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    return d


def is_learning_enabled(root: Path) -> bool:
    """Check whether the learning layer is enabled for this project.

    Reads ``learning_enabled`` from ``~/.dynos/projects/{slug}/policy.json``.
    Defaults to ``True`` when the file or key is missing — learning is opt-out.
    """
    try:
        policy_path = _persistent_project_dir(root) / "policy.json"
        data = json.loads(policy_path.read_text())
        if isinstance(data, dict):
            return bool(data.get("learning_enabled", True))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return True


def project_dir(root: Path) -> Path:
    """Returns ~/.dynos/projects/{slug}/ for persistent project-specific state.

    This is the safe home for postmortems, improvements, and other
    project-specific data that should not live in the repo's .dynos/.
    """
    return _persistent_project_dir(root)


def is_pid_running(pid: int) -> bool:
    """Check whether a PID is alive."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def trajectories_store_path(root: Path) -> Path:
    """Path to the trajectories store JSON file."""
    return _persistent_project_dir(root) / "trajectories.json"


def learned_agents_root(root: Path) -> Path:
    """Root directory for learned agents."""
    return _persistent_project_dir(root) / "learned-agents"


def learned_registry_path(root: Path) -> Path:
    """Path to the learned agent registry JSON file."""
    return learned_agents_root(root) / "registry.json"


def benchmark_history_path(root: Path) -> Path:
    """Path to benchmark history JSON file."""
    return _persistent_project_dir(root) / "benchmarks" / "history.json"


def benchmark_index_path(root: Path) -> Path:
    """Path to benchmark index JSON file."""
    return _persistent_project_dir(root) / "benchmarks" / "index.json"


def automation_queue_path(root: Path) -> Path:
    """Path to the automation queue JSON file."""
    return root / ".dynos" / "automation" / "queue.json"


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

def project_policy(root: Path) -> dict:
    """Read project policy from persistent dir. Accepts all JSON value types."""
    path = _persistent_project_dir(root) / "policy.json"
    default: dict[str, Any] = {
        "freshness_task_window": 5,
        "active_rebenchmark_task_window": 3,
        "shadow_rebenchmark_task_window": 2,
        "token_budget_multiplier": 1.0,
        "fast_track_skip_plan_audit": False,
    }
    if not path.exists() or not path.read_text().strip():
        write_json(path, default)
        return default
    try:
        data = load_json(path)
    except (json.JSONDecodeError, FileNotFoundError, OSError):
        write_json(path, default)
        return default
    merged = {**default, **data}
    return merged


def benchmark_policy_config(root: Path) -> dict:
    """Legacy alias. Returns project policy."""
    return project_policy(root)


# ---------------------------------------------------------------------------
# Task state management
# ---------------------------------------------------------------------------

def transition_task(task_dir: Path, next_stage: str, *, force: bool = False) -> tuple[str, dict]:
    """Transition a task to a new stage, enforcing allowed transitions."""
    manifest_path = task_dir / "manifest.json"
    manifest = load_json(manifest_path)
    current_stage = manifest.get("stage")
    if next_stage not in ALLOWED_STAGE_TRANSITIONS:
        raise ValueError(f"Unknown stage: {next_stage}")
    if not force and next_stage not in ALLOWED_STAGE_TRANSITIONS.get(current_stage, set()):
        raise ValueError(f"Illegal stage transition: {current_stage} -> {next_stage}")
    # ---- Receipt-based transition gates (unmissable) ----
    if not force:
        from lib_receipts import read_receipt, validate_chain
        gate_errors: list[str] = []

        # EXECUTION requires plan-validated receipt
        if next_stage == "EXECUTION":
            if read_receipt(task_dir, "plan-validated") is None:
                gate_errors.append("receipt: plan-validated (plan was never validated)")

        # CHECKPOINT_AUDIT requires executor-routing receipt
        if next_stage == "CHECKPOINT_AUDIT":
            if read_receipt(task_dir, "executor-routing") is None:
                gate_errors.append("receipt: executor-routing (executor routing was never recorded)")

        # REPAIR_PLANNING requires no finding has exceeded max retries.
        # This is the deterministic hard cap: the LLM cannot loop past 3
        # repair attempts because the state machine itself refuses the
        # transition. Code enforces; prompts advise.
        MAX_REPAIR_RETRIES = 3
        if next_stage == "REPAIR_PLANNING" and current_stage == "REPAIR_EXECUTION":
            repair_log_path = task_dir / "repair-log.json"
            if repair_log_path.exists():
                repair_log = load_json(repair_log_path)
                exhausted: list[str] = []
                for batch in repair_log.get("batches", []):
                    for task_entry in batch.get("tasks", []):
                        fid = task_entry.get("finding_id", "unknown")
                        retries = task_entry.get("retry_count", 0)
                        if retries >= MAX_REPAIR_RETRIES:
                            exhausted.append(f"{fid} (retry_count={retries})")
                if exhausted:
                    gate_errors.append(
                        f"repair cap exceeded (max {MAX_REPAIR_RETRIES} retries) "
                        f"for finding(s): {', '.join(exhausted)}. "
                        f"Transition to FAILED instead — human review needed."
                    )

        # DONE requires everything
        if next_stage == "DONE":
            if not (task_dir / "task-retrospective.json").exists():
                gate_errors.append("task-retrospective.json (run /dynos-work:audit to generate)")
            if not list(task_dir.glob("audit-reports/*.json")):
                gate_errors.append("audit-reports/ (no audit reports found — audit was never run)")
            if read_receipt(task_dir, "retrospective") is None:
                gate_errors.append("receipt: retrospective (reward was never computed via receipts)")
            if read_receipt(task_dir, "post-completion") is None:
                gate_errors.append("receipt: post-completion (post-completion pipeline never ran)")

        if gate_errors:
            raise ValueError(
                f"Cannot transition to {next_stage} — missing required artifacts:\n"
                + "\n".join(f"  - {e}" for e in gate_errors)
            )

    manifest["stage"] = next_stage
    if next_stage == "DONE":
        manifest["completion_at"] = now_iso()
    if next_stage == "FAILED" and manifest.get("blocked_reason") is None:
        manifest["blocked_reason"] = "transitioned to FAILED"
    write_json(manifest_path, manifest)

    # ---- Auto-append to execution-log.md ----
    _auto_log(task_dir, current_stage, next_stage, force)

    # ---- Log stage transition to events.jsonl ----
    from lib_log import log_event
    log_event(
        task_dir.parent.parent,
        "stage_transition",
        task=manifest["task_id"],
        from_stage=current_stage,
        to_stage=next_stage,
        forced=force,
    )

    # ---- Auto-emit stage-transition event to per-task token ledger ----
    try:
        from lib_tokens import record_tokens, phase_for_stage
        record_tokens(
            task_dir=task_dir,
            agent="transition_task",
            model="none",
            input_tokens=0,
            output_tokens=0,
            phase=phase_for_stage(next_stage),
            stage=next_stage,
            event_type="stage-transition",
            detail=f"{current_stage} → {next_stage}" + (" (forced)" if force else ""),
        )
    except Exception:
        pass  # Never block a stage transition for a logging failure

    # ---- Fire post-completion pipeline on DONE ----
    if next_stage == "DONE":
        _fire_task_completed(task_dir)

    return current_stage, manifest


def append_execution_log(task_dir: Path, message: str) -> None:
    """Append a timestamped line to the task's execution-log.md.

    This is the ONLY function that should write to execution-log.md.
    Format: {ISO timestamp} {message}\n
    """
    try:
        log_path = task_dir / "execution-log.md"
        line = f"{now_iso()} {message}\n"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass  # Never block pipeline for log write failure


def _auto_log(task_dir: Path, from_stage: str, to_stage: str, forced: bool) -> None:
    """Auto-append stage transition to execution-log.md. Called by transition_task()."""
    force_tag = " (forced)" if forced else ""
    if to_stage == "DONE":
        append_execution_log(task_dir, f"[ADVANCE] {from_stage} → DONE{force_tag}")
    elif to_stage == "FAILED":
        append_execution_log(task_dir, f"[FAILED] {from_stage} → FAILED{force_tag}")
    else:
        append_execution_log(task_dir, f"[STAGE] → {to_stage}{force_tag}")


def _fire_task_completed(task_dir: Path) -> None:
    """Run the post-completion pipeline: event emit → drain (learn, postmortem, evolve, etc).

    This is the ONLY place that fires the pipeline. Never swallow errors silently
    — print them but don't block the transition.
    """
    import subprocess

    root = task_dir.parent.parent  # .dynos/task-xxx -> repo root
    hooks_dir = root / "hooks"
    env_path = f"{hooks_dir}:{__import__('os').environ.get('PYTHONPATH', '')}"

    # Step 1: Emit task-completed event
    try:
        subprocess.run(
            ["python3", str(hooks_dir / "lib_events.py"), "emit",
             "--root", str(root), "--type", "task-completed", "--source", "task"],
            env={**__import__("os").environ, "PYTHONPATH": env_path},
            capture_output=True, text=True, timeout=10,
        )
    except Exception as exc:
        print(f"[dynos] event emit failed: {exc}")

    # Step 2: Drain all events (learn → trajectory → evolve → postmortem → improve)
    try:
        result = subprocess.run(
            ["python3", str(hooks_dir / "eventbus.py"), "drain",
             "--root", str(root)],
            env={**__import__("os").environ, "PYTHONPATH": env_path},
            capture_output=True, text=True, timeout=120,
        )
        if result.stdout.strip():
            print(f"[dynos] post-completion pipeline: {result.stdout.strip()}")
        if result.returncode != 0 and result.stderr.strip():
            print(f"[dynos] pipeline warning: {result.stderr.strip()}")
    except Exception as exc:
        print(f"[dynos] event drain failed: {exc}")


def next_command_for_stage(stage: str) -> str:
    """Return the next CLI command for a given stage."""
    return NEXT_COMMAND.get(stage, "Unknown stage")


def find_active_tasks(root: Path) -> list[Path]:
    """Find all active (non-terminal) tasks under .dynos/."""
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


# ---------------------------------------------------------------------------
# Retrospective helpers
# ---------------------------------------------------------------------------

def collect_retrospectives(root: Path) -> list[dict]:
    """Collect all task retrospective JSON files from .dynos/."""
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
    """Return list of task IDs from retrospectives."""
    return [item.get("task_id") for item in collect_retrospectives(root) if isinstance(item.get("task_id"), str)]


def task_recency_index(root: Path, task_id: Optional[str]) -> Optional[int]:
    """Return how many tasks ago a given task_id appeared (0 = most recent)."""
    if not task_id:
        return None
    task_ids = retrospective_task_ids(root)
    if task_id not in task_ids:
        return None
    return len(task_ids) - 1 - task_ids.index(task_id)


def tasks_since(root: Path, task_id: Optional[str]) -> Optional[int]:
    """Return the number of tasks since a given task_id."""
    return task_recency_index(root, task_id)
