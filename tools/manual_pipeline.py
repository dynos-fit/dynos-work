#!/usr/bin/env python3
"""Manual terminal runner for the live dynos-work branch.

This script does not pretend the live branch is fully headless. Instead it
gives you a clean manual entrypoint:

  1. bootstrap a task directory under .dynos/
  2. validate the initial artifacts
  3. print the exact next terminal steps for the task's current stage

It is intentionally thin and honest. The live branch still expects humans/LLMs
to author spec/plan/graph/audit artifacts; this tool just removes the
bootstrap ceremony and the "what do I run next?" guesswork.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = REPO_ROOT / "hooks"
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib_core import load_json  # type: ignore  # noqa: E402


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def shell_env() -> dict[str, str]:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{HOOKS_DIR}:{REPO_ROOT}:{existing}" if existing else f"{HOOKS_DIR}:{REPO_ROOT}"
    return env


def run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd or REPO_ROOT),
        env=shell_env(),
        text=True,
        capture_output=True,
        check=check,
    )


def generate_task_id(root: Path) -> str:
    prefix = datetime.now().strftime("task-%Y%m%d-")
    dynos_dir = root / ".dynos"
    dynos_dir.mkdir(parents=True, exist_ok=True)
    existing = {
        path.name
        for path in dynos_dir.glob(f"{prefix}*")
        if path.is_dir() and path.name.startswith(prefix)
    }
    for idx in range(1, 1000):
        candidate = f"{prefix}{idx:03d}"
        if candidate not in existing:
            return candidate
    raise RuntimeError(f"could not allocate task id for prefix {prefix}")


def ensure_project_bootstrap(root: Path) -> list[str]:
    notes: list[str] = []
    (root / ".dynos").mkdir(parents=True, exist_ok=True)
    try:
        run(["python3", str(HOOKS_DIR / "registry.py"), "register", str(root)], cwd=root, check=False)
        notes.append("registry register attempted")
    except Exception as exc:  # pragma: no cover - defensive only
        notes.append(f"registry register failed: {exc}")
    try:
        run(["python3", str(HOOKS_DIR / "daemon.py"), "start", "--root", str(root)], cwd=root, check=False)
        notes.append("daemon start attempted")
    except Exception as exc:  # pragma: no cover - defensive only
        notes.append(f"daemon start failed: {exc}")
    return notes


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def create_task(root: Path, task_id: str, raw_input: str, title: str | None = None, input_type: str = "text") -> Path:
    task_dir = root / ".dynos" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "raw-input.md").write_text(raw_input.rstrip() + "\n", encoding="utf-8")
    (task_dir / "execution-log.md").touch()
    manifest = {
        "task_id": task_id,
        "created_at": now_iso(),
        "title": (title or raw_input.strip().splitlines()[0])[:80],
        "raw_input": raw_input,
        "input_type": input_type,
        "stage": "FOUNDRY_INITIALIZED",
        "classification": None,
        "retry_counts": {},
        "blocked_reason": None,
        "completed_at": None,
    }
    write_json(task_dir / "manifest.json", manifest)
    return task_dir


def validate_task(task_dir: Path) -> subprocess.CompletedProcess[str]:
    return run(["python3", str(HOOKS_DIR / "ctl.py"), "validate-task", str(task_dir)], cwd=REPO_ROOT, check=False)


def stage_guide(task_dir: Path) -> str:
    manifest = load_json(task_dir / "manifest.json")
    stage = manifest.get("stage", "UNKNOWN")
    task_id = manifest.get("task_id", task_dir.name)
    classification = manifest.get("classification")
    task_type = classification.get("type", "feature") if isinstance(classification, dict) else "feature"
    domains = classification.get("domains", []) if isinstance(classification, dict) else []
    domains_csv = ",".join(str(d) for d in domains) if isinstance(domains, list) else ""
    fast_track = bool(manifest.get("fast_track", False))

    common = [
        f"Task: {task_id}",
        f"Stage: {stage}",
        f"Task dir: {task_dir}",
        "",
    ]

    if stage == "FOUNDRY_INITIALIZED":
        common += [
            "Starting point:",
            "1. Read .dynos/task-.../raw-input.md",
            "2. Produce discovery-notes.md and design-decisions.md",
            "3. Write classification into manifest.json",
            "4. Advance to SPEC_NORMALIZATION:",
            f"   python3 hooks/ctl.py transition {task_dir} SPEC_NORMALIZATION",
            "",
            "Then write spec.md and validate again.",
        ]
    elif stage == "SPEC_NORMALIZATION":
        common += [
            "Next manual actions:",
            "1. Write spec.md",
            f"2. Validate artifacts: python3 hooks/ctl.py validate-task {task_dir}",
            f"3. Approve spec review when ready: python3 hooks/ctl.py approve-stage {task_dir} SPEC_REVIEW",
            "   Scheduler should advance SPEC_REVIEW -> PLANNING once the approval receipt is on disk.",
        ]
    elif stage in {"SPEC_REVIEW", "PLANNING", "PLAN_REVIEW", "PLAN_AUDIT"}:
        common += [
            "Planning flow:",
            "1. Write or update plan.md",
            "2. Write or update execution-graph.json",
            f"3. Validate: python3 hooks/ctl.py validate-task {task_dir} --strict",
            "4. When plan review is approved:",
            f"   python3 hooks/ctl.py approve-stage {task_dir} PLAN_REVIEW",
        ]
        if stage in {"PLANNING", "PLAN_REVIEW", "PLAN_AUDIT"}:
            common += [
                "",
                "Useful route command:",
                f"python3 hooks/router.py audit-plan --root . --task-type {task_type} --domains \"{domains_csv}\" {'--fast-track' if fast_track else ''}".rstrip(),
            ]
    elif stage in {"PRE_EXECUTION_SNAPSHOT", "EXECUTION", "TEST_EXECUTION"}:
        common += [
            "Execution flow:",
            f"1. Build executor plan: python3 hooks/router.py executor-plan --root . --task-type {task_type} --graph {task_dir / 'execution-graph.json'}",
            "2. Write executor-routing receipt",
            "3. For each segment, run router.py inject-prompt, spawn the executor manually, then write receipt_executor_done(...)",
            f"4. When execution is complete: python3 hooks/ctl.py transition {task_dir} TEST_EXECUTION",
            "5. Run tests and continue to audit",
        ]
    elif stage in {"CHECKPOINT_AUDIT", "REPAIR_PLANNING", "REPAIR_EXECUTION", "FINAL_AUDIT"}:
        common += [
            "Audit / repair flow:",
            f"1. Build audit plan: python3 hooks/router.py audit-plan --root . --task-type {task_type} --domains \"{domains_csv}\" {'--fast-track' if fast_track else ''}".rstrip(),
            "2. Spawn auditors manually, write audit reports to audit-reports/*.json",
            "3. Write audit receipts with hooks/ctl.py audit-receipt",
            f"4. If repairs are needed, build assignments: echo '{{\"findings\": [...]}}' | python3 hooks/ctl.py repair-plan --root . --task-type {task_type}",
            "5. Update repair-log.json and run repair executors",
            f"6. Feed outcomes back: echo '{{\"outcomes\": [...]}}' | python3 hooks/ctl.py repair-update --root . --task-type {task_type}",
            f"7. When clean, compute retrospective: python3 hooks/ctl.py compute-reward {task_dir} --write",
            f"8. Finish: python3 hooks/ctl.py transition {task_dir} DONE",
        ]
    elif stage == "DONE":
        common += [
            "Task is DONE.",
            "Eventbus/handlers should eventually calibrate it to CALIBRATED.",
            "If needed, inspect:",
            f"python3 hooks/ctl.py validate-receipts {task_dir}",
            f"python3 hooks/eventbus.py drain --root . --sync --task-dir {task_dir}",
        ]
    else:
        common += [
            "No stage-specific guide for this state.",
            f"Inspect next command with: python3 hooks/ctl.py next-command {task_dir}",
        ]

    return "\n".join(common)


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    notes = ensure_project_bootstrap(root)
    raw_input = args.text
    if args.input_file:
        raw_input = Path(args.input_file).read_text(encoding="utf-8")
    if not raw_input or not raw_input.strip():
        print("manual-pipeline init requires --text or --input-file", file=sys.stderr)
        return 1

    task_id = args.task_id or generate_task_id(root)
    task_dir = create_task(root, task_id, raw_input.strip(), title=args.title, input_type=args.input_type)
    result = validate_task(task_dir)

    output = {
        "task_id": task_id,
        "task_dir": str(task_dir),
        "bootstrap_notes": notes,
        "validate_returncode": result.returncode,
        "validate_stdout": result.stdout.strip(),
        "validate_stderr": result.stderr.strip(),
    }
    print(json.dumps(output, indent=2))
    print()
    print(stage_guide(task_dir))
    return 0 if result.returncode == 0 else 1


def cmd_guide(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir).resolve()
    print(stage_guide(task_dir))
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    result = validate_task(Path(args.task_dir).resolve())
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return result.returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    init_parser = sub.add_parser("init", help="Bootstrap a manual task under .dynos and print the next steps")
    init_parser.add_argument("--root", default=".", help="Project root")
    init_parser.add_argument("--task-id", default=None, help="Override task id (default: auto-generated)")
    init_parser.add_argument("--title", default=None, help="Optional manifest title")
    init_parser.add_argument("--text", default=None, help="Raw task text")
    init_parser.add_argument("--input-file", default=None, help="Read raw task text from a file")
    init_parser.add_argument("--input-type", default="text", help="Manifest input_type value")
    init_parser.set_defaults(func=cmd_init)

    guide_parser = sub.add_parser("guide", help="Print the next manual terminal steps for a task")
    guide_parser.add_argument("task_dir")
    guide_parser.set_defaults(func=cmd_guide)

    validate_parser = sub.add_parser("validate", help="Run hooks/ctl.py validate-task for a task")
    validate_parser.add_argument("task_dir")
    validate_parser.set_defaults(func=cmd_validate)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
