#!/usr/bin/env python3
"""Deterministic typecheck and lint runner for dynos-work.

Auto-detects project ecosystem and runs the appropriate type checker and
linter. Works for ANY project type — detects from config files.

Usage:
    python3 hooks/typecheck_lint.py --root <project-root>
    python3 hooks/typecheck_lint.py --root <project-root> --changed-files file1.py,file2.ts

Outputs JSON to stdout with pass/fail status and error details.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


def _run(cmd: list[str], cwd: str | None = None, timeout: int = 120) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return -1, "", f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return -2, "", f"timeout after {timeout}s"


# ---------------------------------------------------------------------------
# Type checkers
# ---------------------------------------------------------------------------

def check_python_types(root: Path, changed: list[str] | None) -> dict[str, Any]:
    """Run mypy if available."""
    targets = changed if changed else ["."]
    rc, stdout, stderr = _run(
        ["mypy", "--no-error-summary", "--no-color"] + targets,
        cwd=str(root), timeout=120,
    )
    if rc == -1:
        return {"tool": "mypy", "available": False, "reason": "mypy not installed"}
    errors = [l for l in stdout.splitlines() if ": error:" in l]
    return {
        "tool": "mypy",
        "available": True,
        "passed": rc == 0,
        "error_count": len(errors),
        "errors": errors[:20],
    }


def check_typescript_types(root: Path) -> dict[str, Any]:
    """Run tsc --noEmit if tsconfig.json exists."""
    if not (root / "tsconfig.json").exists():
        return {"tool": "tsc", "available": False, "reason": "no tsconfig.json"}
    rc, stdout, stderr = _run(
        ["npx", "tsc", "--noEmit", "--pretty", "false"],
        cwd=str(root), timeout=120,
    )
    if rc == -1:
        return {"tool": "tsc", "available": False, "reason": "npx/tsc not installed"}
    output = stdout + stderr
    errors = [l for l in output.splitlines() if ": error TS" in l]
    return {
        "tool": "tsc",
        "available": True,
        "passed": rc == 0,
        "error_count": len(errors),
        "errors": errors[:20],
    }


def check_go_types(root: Path) -> dict[str, Any]:
    """Run go vet if go.mod exists."""
    if not (root / "go.mod").exists():
        return {"tool": "go vet", "available": False, "reason": "no go.mod"}
    rc, stdout, stderr = _run(["go", "vet", "./..."], cwd=str(root), timeout=120)
    if rc == -1:
        return {"tool": "go vet", "available": False, "reason": "go not installed"}
    errors = [l for l in stderr.splitlines() if l.strip()]
    return {
        "tool": "go vet",
        "available": True,
        "passed": rc == 0,
        "error_count": len(errors),
        "errors": errors[:20],
    }


# ---------------------------------------------------------------------------
# Linters
# ---------------------------------------------------------------------------

def lint_python(root: Path, changed: list[str] | None) -> dict[str, Any]:
    """Run ruff or flake8 if available."""
    targets = changed if changed else ["."]
    # Try ruff first (fast)
    rc, stdout, stderr = _run(
        ["ruff", "check", "--output-format=json"] + targets,
        cwd=str(root), timeout=60,
    )
    if rc != -1:
        try:
            issues = json.loads(stdout) if stdout.strip() else []
        except json.JSONDecodeError:
            issues = []
        return {
            "tool": "ruff",
            "available": True,
            "passed": rc == 0,
            "issue_count": len(issues) if isinstance(issues, list) else 0,
            "issues": (issues[:20] if isinstance(issues, list) else []),
        }
    # Fallback to flake8
    rc, stdout, stderr = _run(
        ["flake8", "--format=json"] + targets,
        cwd=str(root), timeout=60,
    )
    if rc != -1:
        return {
            "tool": "flake8",
            "available": True,
            "passed": rc == 0,
            "issue_count": stdout.count("\n"),
            "issues": stdout.splitlines()[:20],
        }
    return {"tool": "ruff/flake8", "available": False, "reason": "neither ruff nor flake8 installed"}


def lint_javascript(root: Path) -> dict[str, Any]:
    """Run eslint if .eslintrc or eslint config exists."""
    has_config = any(
        (root / f).exists()
        for f in [".eslintrc", ".eslintrc.js", ".eslintrc.json", ".eslintrc.yml",
                  "eslint.config.js", "eslint.config.mjs", "eslint.config.ts"]
    )
    if not has_config:
        # Check package.json for eslintConfig
        pkg = root / "package.json"
        if pkg.exists():
            try:
                data = json.loads(pkg.read_text())
                if "eslintConfig" not in data:
                    return {"tool": "eslint", "available": False, "reason": "no eslint config found"}
            except (json.JSONDecodeError, OSError):
                return {"tool": "eslint", "available": False, "reason": "no eslint config found"}
        else:
            return {"tool": "eslint", "available": False, "reason": "no eslint config found"}

    rc, stdout, stderr = _run(
        ["npx", "eslint", ".", "--format=json", "--max-warnings=0"],
        cwd=str(root), timeout=120,
    )
    if rc == -1:
        return {"tool": "eslint", "available": False, "reason": "npx/eslint not installed"}
    try:
        results = json.loads(stdout) if stdout.strip() else []
        total_errors = sum(r.get("errorCount", 0) for r in results) if isinstance(results, list) else 0
        total_warnings = sum(r.get("warningCount", 0) for r in results) if isinstance(results, list) else 0
    except json.JSONDecodeError:
        total_errors = 0
        total_warnings = 0
    return {
        "tool": "eslint",
        "available": True,
        "passed": rc == 0,
        "error_count": total_errors,
        "warning_count": total_warnings,
    }


def lint_go(root: Path) -> dict[str, Any]:
    """Run golangci-lint if available and go.mod exists."""
    if not (root / "go.mod").exists():
        return {"tool": "golangci-lint", "available": False, "reason": "no go.mod"}
    rc, stdout, stderr = _run(
        ["golangci-lint", "run", "--out-format=json"],
        cwd=str(root), timeout=120,
    )
    if rc == -1:
        return {"tool": "golangci-lint", "available": False, "reason": "golangci-lint not installed"}
    try:
        data = json.loads(stdout) if stdout.strip() else {}
        issues = data.get("Issues", []) if isinstance(data, dict) else []
    except json.JSONDecodeError:
        issues = []
    return {
        "tool": "golangci-lint",
        "available": True,
        "passed": rc == 0,
        "issue_count": len(issues),
    }


# ---------------------------------------------------------------------------
# Ecosystem detection and runner
# ---------------------------------------------------------------------------

def run_checks(root: Path, changed_files: list[str] | None = None) -> dict[str, Any]:
    """Detect ecosystems and run appropriate checkers."""
    results: dict[str, Any] = {"typechecks": [], "linters": []}

    # Python
    py_markers = ["pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile"]
    if any((root / m).exists() for m in py_markers):
        py_changed = [f for f in (changed_files or []) if f.endswith(".py")]
        results["typechecks"].append(check_python_types(root, py_changed or None))
        results["linters"].append(lint_python(root, py_changed or None))

    # TypeScript/JavaScript
    if (root / "package.json").exists():
        results["typechecks"].append(check_typescript_types(root))
        results["linters"].append(lint_javascript(root))

    # Go
    if (root / "go.mod").exists():
        results["typechecks"].append(check_go_types(root))
        results["linters"].append(lint_go(root))

    # Summary
    all_checks = results["typechecks"] + results["linters"]
    available = [c for c in all_checks if c.get("available")]
    failed = [c for c in available if not c.get("passed", True)]
    results["summary"] = {
        "ecosystems_detected": len(set(c["tool"].split("/")[0] for c in all_checks if c.get("available"))),
        "checks_run": len(available),
        "checks_passed": len(available) - len(failed),
        "checks_failed": len(failed),
    }
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Deterministic typecheck and lint runner")
    parser.add_argument("--root", required=True, help="Project root directory")
    parser.add_argument("--changed-files", help="Comma-separated list of changed files")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    changed = args.changed_files.split(",") if args.changed_files else None
    result = run_checks(root, changed)
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
