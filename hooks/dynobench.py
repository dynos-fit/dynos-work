#!/usr/bin/env python3
"""Fixture benchmark runner for agents and skills."""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import argparse
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from dynoslib import (
    append_benchmark_run,
    apply_evaluation_to_registry,
    benchmark_fixture_score,
    evaluate_candidate,
    now_iso,
    upsert_fixture_trace,
)

ALLOWED_COMMAND_PREFIXES = (
    "python3", "python", "node", "npm", "npx", "pytest", "jest",
    "go", "cargo", "make", "sh", "bash",
)


def _validate_command(command: list[str], sandbox: Path) -> None:
    """Reject commands not on the allowlist or that escape the sandbox."""
    if not command:
        raise SystemExit("empty command in sandbox variant")
    executable = Path(command[0]).name
    if executable not in ALLOWED_COMMAND_PREFIXES:
        raise SystemExit(
            f"command {command[0]!r} not in sandbox allowlist: {ALLOWED_COMMAND_PREFIXES}"
        )
    for arg in command[1:]:
        resolved = (sandbox / arg).resolve()
        if resolved.is_absolute() and not str(resolved).startswith(str(sandbox)):
            if Path(arg).is_absolute() or ".." in arg:
                raise SystemExit(
                    f"command argument escapes sandbox: {arg!r}"
                )


def load_fixture(path: Path) -> dict:
    fixture = json.loads(path.read_text())
    if not isinstance(fixture, dict) or not isinstance(fixture.get("cases"), list):
        raise SystemExit("fixture must be a JSON object with a 'cases' array")
    fixture["_fixture_dir"] = str(path.parent)
    return fixture


def snapshot_tree(root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        rel = str(path.relative_to(root))
        st = path.stat()
        snapshot[rel] = f"{st.st_size}:{st.st_mtime_ns}"
    return snapshot


def touched_files(before: dict[str, str], after: dict[str, str]) -> int:
    touched = 0
    for path, digest in after.items():
        if before.get(path) != digest:
            touched += 1
    for path in before:
        if path not in after:
            touched += 1
    return touched


def _assert_path_within(resolved: Path, parent: Path, label: str) -> None:
    """Raise if resolved path escapes the expected parent directory."""
    try:
        resolved.relative_to(parent)
    except ValueError:
        raise SystemExit(f"path traversal blocked: {resolved} escapes {label} ({parent})")


def materialize_sandbox(case: dict) -> Path:
    sandbox = Path(tempfile.mkdtemp(prefix="dynos-bench-", dir="/tmp"))
    for relative_path, content in case.get("sandbox", {}).get("files", {}).items():
        target = (sandbox / relative_path).resolve()
        _assert_path_within(target, sandbox, "sandbox")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    fixture_dir = Path(case.get("_fixture_dir", ".")).resolve()
    for copy_entry in case.get("sandbox", {}).get("copy_paths", []):
        if isinstance(copy_entry, str):
            source_rel = copy_entry
            target_rel = copy_entry
        else:
            source_rel = copy_entry["source"]
            target_rel = copy_entry.get("target", source_rel)
        source = (fixture_dir / source_rel).resolve()
        _assert_path_within(source, fixture_dir, "fixture_dir")
        target = (sandbox / target_rel).resolve()
        _assert_path_within(target, sandbox, "sandbox")
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target)
    for copy_entry in case.get("sandbox", {}).get("copy_repo_paths", []):
        if isinstance(copy_entry, str):
            source_rel = copy_entry
            target_rel = copy_entry
        else:
            source_rel = copy_entry["source"]
            target_rel = copy_entry.get("target", source_rel)
        repo_root = Path(case.get("_repo_root", ".")).resolve()
        source = (repo_root / source_rel).resolve()
        _assert_path_within(source, repo_root, "repo_root")
        target = (sandbox / target_rel).resolve()
        _assert_path_within(target, sandbox, "sandbox")
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target)
    return sandbox


def parse_output_metrics(stdout: str) -> dict:
    stdout = stdout.strip()
    if not stdout:
        return {}
    lines = [line for line in stdout.splitlines() if line.strip()]
    for candidate in reversed(lines):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def merge_metrics(existing: dict, parsed: dict) -> dict:
    merged = dict(existing)
    additive_keys = {"tokens_used", "findings"}
    for key, value in parsed.items():
        if key in additive_keys and isinstance(value, (int, float)):
            merged[key] = merged.get(key, 0) + value
        else:
            merged[key] = value
    return merged


def normalize_commands(variant: dict) -> list[list[str]]:
    if isinstance(variant.get("command"), list) and variant["command"] and all(
        isinstance(item, str) for item in variant["command"]
    ):
        return [variant["command"]]
    commands = variant.get("commands", [])
    if isinstance(commands, list) and commands and all(isinstance(cmd, list) for cmd in commands):
        return commands
    raise SystemExit("sandbox variant must declare 'command' or 'commands'")


def normalize_command_list(commands: object, *, label: str) -> list[list[str]]:
    if commands in (None, [], ()):
        return []
    if isinstance(commands, list) and commands and all(isinstance(item, list) for item in commands):
        return commands
    raise SystemExit(f"{label} must be a list of command arrays")


def run_command_sequence(sandbox: Path, commands: list[list[str]], env: dict[str, str]) -> tuple[int, str, str, dict]:
    combined_stdout: list[str] = []
    combined_stderr: list[str] = []
    merged_metrics: dict = {}
    exit_code = 0
    for command in commands:
        _validate_command(command, sandbox)
        completed = subprocess.run(
            command,
            cwd=sandbox,
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        combined_stdout.append(completed.stdout.strip())
        combined_stderr.append(completed.stderr.strip())
        merged_metrics = merge_metrics(merged_metrics, parse_output_metrics(completed.stdout))
        if completed.returncode != 0:
            exit_code = completed.returncode
            break
    return (
        exit_code,
        "\n".join(part for part in combined_stdout if part),
        "\n".join(part for part in combined_stderr if part),
        merged_metrics,
    )


def run_variant_in_sandbox(case: dict, variant_name: str) -> dict:
    variant = case.get(variant_name, {})
    sandbox = materialize_sandbox(case)
    try:
        before = snapshot_tree(sandbox)
        started = time.monotonic()
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(sandbox),
            "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
            "DYNOS_BENCH_SANDBOX": str(sandbox),
        }
        if isinstance(variant.get("env"), dict):
            env.update({str(k): str(v) for k, v in variant["env"].items()})
        setup_commands = normalize_command_list(variant.get("setup_commands", []), label="setup_commands")
        if setup_commands:
            run_command_sequence(sandbox, setup_commands, env)
        exit_code, stdout, stderr, parsed = run_command_sequence(
            sandbox,
            normalize_commands(variant),
            env,
        )
        duration_seconds = time.monotonic() - started
        after = snapshot_tree(sandbox)
        tests_passed = int(parsed.get("tests_passed", exit_code == 0))
        tests_total = int(parsed.get("tests_total", 1))
        findings = int(parsed.get("findings", 0 if exit_code == 0 else 1))
        files_touched = int(parsed.get("files_touched", touched_files(before, after)))
        tokens_used = float(parsed.get("tokens_used", 0))
        teardown_commands = normalize_command_list(variant.get("teardown_commands", []), label="teardown_commands")
        if teardown_commands:
            run_command_sequence(sandbox, teardown_commands, env)
        return {
            "tests_passed": tests_passed,
            "tests_total": tests_total,
            "findings": findings,
            "files_touched": files_touched,
            "duration_seconds": round(duration_seconds, 6),
            "tokens_used": tokens_used,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "sandbox_mode": True,
        }
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def resolve_case_results(case: dict) -> tuple[dict, dict]:
    if case.get("sandbox"):
        return run_variant_in_sandbox(case, "baseline"), run_variant_in_sandbox(case, "candidate")
    return case.get("baseline", {}), case.get("candidate", {})


def run_fixture(fixture: dict) -> dict:
    candidate_results = []
    baseline_results = []
    case_summaries = []
    policy = fixture.get("policy", {})
    for case in fixture["cases"]:
        case["_fixture_dir"] = fixture.get("_fixture_dir", ".")
        case["_repo_root"] = fixture.get("_repo_root", ".")
        baseline_raw, candidate_raw = resolve_case_results(case)
        baseline_score = benchmark_fixture_score(baseline_raw)
        candidate_score = benchmark_fixture_score(candidate_raw)
        category = str(case.get("category", "default"))
        baseline_results.append({**baseline_score, "category": category})
        candidate_results.append({**candidate_score, "category": category})
        case_summaries.append(
            {
                "case_id": case.get("case_id"),
                "category": category,
                "execution_mode": "sandbox" if case.get("sandbox") else "static",
                "baseline_observed": baseline_raw,
                "candidate_observed": candidate_raw,
                "baseline": baseline_score,
                "candidate": candidate_score,
                "winner": "candidate"
                if candidate_score["composite_score"] > baseline_score["composite_score"]
                else "baseline"
                if candidate_score["composite_score"] < baseline_score["composite_score"]
                else "tie",
            }
        )
    evaluation = evaluate_candidate(candidate_results, baseline_results, policy=policy)
    return {
        "fixture_id": fixture.get("fixture_id"),
        "item_kind": fixture.get("item_kind", "agent"),
        "target_name": fixture.get("target_name"),
        "role": fixture.get("role"),
        "task_type": fixture.get("task_type"),
        "policy": evaluation["policy"],
        "cases": case_summaries,
        "evaluation": evaluation,
    }


def cmd_run(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    fixture_path = Path(args.fixture_json).resolve()
    fixture = load_fixture(fixture_path)
    fixture["_repo_root"] = str(root)
    result = run_fixture(fixture)
    run_record = {
        "run_id": f"{result['fixture_id']}:{now_iso()}",
        "executed_at": now_iso(),
        "fixture_path": str(fixture_path),
        **result,
    }
    append_benchmark_run(root, run_record)
    upsert_fixture_trace(
        root,
        {
            "fixture_id": result["fixture_id"],
            "fixture_path": str(fixture_path),
            "item_kind": result["item_kind"],
            "target_name": result["target_name"],
            "role": result["role"],
            "task_type": result["task_type"],
            "source_tasks": fixture.get("source_tasks", []),
            "baseline_tasks": fixture.get("baseline_tasks", []),
            "last_run_id": run_record["run_id"],
            "last_run_at": run_record["executed_at"],
        },
    )

    if args.update_registry:
        apply_evaluation_to_registry(
            root,
            fixture["target_name"],
            fixture["role"],
            fixture["task_type"],
            result["evaluation"],
            item_kind=fixture.get("item_kind", "agent"),
            context={
                "fixture_id": result["fixture_id"],
                "fixture_path": str(fixture_path),
                "run_id": run_record["run_id"],
                "source_tasks": fixture.get("source_tasks", []),
            },
        )

    print(json.dumps(run_record, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run a benchmark fixture")
    run_parser.add_argument("fixture_json")
    run_parser.add_argument("--root", default=".")
    run_parser.add_argument("--update-registry", action="store_true")
    run_parser.set_defaults(func=cmd_run)
    return parser


if __name__ == "__main__":
    from dyno_cli_base import cli_main
    raise SystemExit(cli_main(build_parser))
