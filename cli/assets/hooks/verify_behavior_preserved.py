#!/usr/bin/env python3
"""Run a project's test suite at two git refs and assert no regression.

Catches refactors that silently change behavior. Pairs naturally with
`agents/refactor-executor.md`: refactor agents promise "no behavior change",
but they're prone to regressions that LLM auditors miss. A real test runner
is the deterministic ground truth.

How it works:
  1. Create a temporary git worktree at the BEFORE ref.
  2. Run the test suite there — capture passing test IDs.
  3. Create a temporary git worktree at the AFTER ref.
  4. Run the same test suite — capture passing test IDs.
  5. Diff: any test that passed BEFORE and fails (or is missing) AFTER is a
     regression.
  6. Cleanup worktrees.
  7. Exit 0 if no regressions, 1 if regressions found, 2 if can't run.

Why git worktrees and not `git stash` + `git checkout`:
  - No risk to the user's working tree (no stash needed).
  - Safe under interrupt — worktrees are independent dirs.
  - Cleanup is `git worktree remove` (atomic).

Usage:
    python3 hooks/verify_behavior_preserved.py --before HEAD~1 --after HEAD
    python3 hooks/verify_behavior_preserved.py --before main --after my-branch
    python3 hooks/verify_behavior_preserved.py --before HEAD~5 --after HEAD --command "pytest tests/unit/"
    python3 hooks/verify_behavior_preserved.py --before HEAD~1 --after HEAD --json

Test framework auto-detection (override with --command):
  - pytest.ini / pyproject.toml [tool.pytest] / setup.cfg [tool:pytest] -> pytest tests/
  - package.json with a "test" script -> npm test
  - Cargo.toml -> cargo test
  - go.mod -> go test ./...

Exit codes:
    0 — no regressions (every passing test in BEFORE still passes in AFTER)
    1 — at least one regression (test passed before, fails or missing after)
    2 — could not run (bad refs, no test framework, worktree failure)
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Worktree management
# ---------------------------------------------------------------------------

class WorktreeFailure(Exception):
    """Raised when a worktree operation fails — caller should report exit 2."""


def create_worktree(repo: Path, ref: str, dest: Path) -> None:
    """Create a detached worktree at `dest` checked out at `ref`.

    Detached so the worktree doesn't claim the branch — the original repo
    keeps full control of branches and HEAD.
    """
    r = subprocess.run(
        ["git", "worktree", "add", "--detach", str(dest), ref],
        cwd=repo, capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise WorktreeFailure(
            f"worktree add failed for ref {ref!r}: {r.stderr.strip()}"
        )


def remove_worktree(repo: Path, dest: Path) -> None:
    """Remove the worktree (best-effort — log but don't raise on cleanup failure)."""
    if not dest.exists():
        return
    r = subprocess.run(
        ["git", "worktree", "remove", "--force", str(dest)],
        cwd=repo, capture_output=True, text=True,
    )
    if r.returncode != 0:
        # Manual cleanup if git itself can't
        try:
            shutil.rmtree(dest, ignore_errors=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Test framework detection
# ---------------------------------------------------------------------------

@dataclass
class TestFramework:
    name: str               # "pytest", "npm", "cargo", "go"
    command: list[str]      # argv
    parser: str             # which parser to use on output


def detect_framework(worktree: Path) -> TestFramework | None:
    """Look at the worktree contents to guess the right test runner."""
    if (worktree / "pytest.ini").exists():
        return TestFramework("pytest", ["pytest", "tests/", "-v", "--tb=no"], "pytest")
    pyproject = worktree / "pyproject.toml"
    if pyproject.exists():
        text = pyproject.read_text()
        if "[tool.pytest" in text or '"pytest"' in text or "'pytest'" in text:
            return TestFramework("pytest", ["pytest", "tests/", "-v", "--tb=no"], "pytest")
    if (worktree / "setup.cfg").exists():
        text = (worktree / "setup.cfg").read_text()
        if "[tool:pytest]" in text:
            return TestFramework("pytest", ["pytest", "tests/", "-v", "--tb=no"], "pytest")
    # Default: if there's a tests/ dir of .py files, assume pytest is intended.
    if (worktree / "tests").exists() and any(
        (worktree / "tests").rglob("test_*.py")
    ):
        return TestFramework("pytest", ["pytest", "tests/", "-v", "--tb=no"], "pytest")
    pkg = worktree / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text())
            if "scripts" in data and "test" in data["scripts"]:
                return TestFramework("npm", ["npm", "test", "--silent"], "generic")
        except json.JSONDecodeError:
            pass
    if (worktree / "Cargo.toml").exists():
        return TestFramework("cargo", ["cargo", "test", "--quiet"], "generic")
    if (worktree / "go.mod").exists():
        return TestFramework("go", ["go", "test", "./..."], "generic")
    return None


# ---------------------------------------------------------------------------
# Run + parse
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    ref: str
    framework: str
    passing: set[str]       # test IDs that passed
    failing: set[str]       # test IDs that failed
    skipped: set[str]       # test IDs that were skipped
    error: str | None       # populated if framework couldn't run at all


# pytest "PASSED" / "FAILED" lines look like:
#   tests/test_foo.py::TestBar::test_baz PASSED                        [ 5%]
#   tests/test_foo.py::TestBar::test_qux FAILED                        [ 6%]
PYTEST_LINE_RE = re.compile(
    r"^(?P<id>\S+)\s+(?P<status>PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)",
    re.MULTILINE,
)


def parse_pytest(stdout: str) -> tuple[set[str], set[str], set[str]]:
    passing: set[str] = set()
    failing: set[str] = set()
    skipped: set[str] = set()
    for m in PYTEST_LINE_RE.finditer(stdout):
        test_id, status = m.group("id"), m.group("status")
        if status in ("PASSED", "XPASS"):
            passing.add(test_id)
        elif status in ("FAILED", "ERROR", "XFAIL"):
            failing.add(test_id)
        elif status == "SKIPPED":
            skipped.add(test_id)
    return passing, failing, skipped


def parse_generic(stdout: str, returncode: int) -> tuple[set[str], set[str], set[str]]:
    """For frameworks where we don't have a per-test ID parser yet, treat the
    overall return code as a single 'composite' test."""
    if returncode == 0:
        return {"<all tests passed>"}, set(), set()
    return set(), {"<test suite failed>"}, set()


def run_at_worktree(
    worktree: Path, ref: str, command: list[str] | None
) -> RunResult:
    if command is None:
        fw = detect_framework(worktree)
        if fw is None:
            return RunResult(
                ref=ref, framework="unknown",
                passing=set(), failing=set(), skipped=set(),
                error="no test framework auto-detected (pass --command)",
            )
        cmd = fw.command
        parser = fw.parser
        framework_name = fw.name
    else:
        cmd = command
        parser = "pytest" if cmd and cmd[0] == "pytest" else "generic"
        framework_name = cmd[0] if cmd else "custom"

    try:
        r = subprocess.run(
            cmd, cwd=worktree, capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired:
        return RunResult(
            ref=ref, framework=framework_name,
            passing=set(), failing=set(), skipped=set(),
            error="test run timed out (>600s)",
        )
    except FileNotFoundError as e:
        return RunResult(
            ref=ref, framework=framework_name,
            passing=set(), failing=set(), skipped=set(),
            error=f"test runner not found: {e}",
        )

    if parser == "pytest":
        passing, failing, skipped = parse_pytest(r.stdout)
    else:
        passing, failing, skipped = parse_generic(r.stdout, r.returncode)

    return RunResult(
        ref=ref, framework=framework_name,
        passing=passing, failing=failing, skipped=skipped, error=None,
    )


# ---------------------------------------------------------------------------
# Diff + report
# ---------------------------------------------------------------------------

@dataclass
class DiffReport:
    before_ref: str
    after_ref: str
    framework: str
    before_passing: int
    after_passing: int
    regressions: list[str]   # passed before, fails/missing after
    new_passes: list[str]    # not passing before, passing after (informational)
    no_longer_run: list[str] # passed before, doesn't appear in after results


def diff_runs(before: RunResult, after: RunResult) -> DiffReport:
    regressions = sorted(
        # Tests that passed BEFORE but now fail
        (before.passing & after.failing)
        # Tests that passed BEFORE but no longer appear (deleted? renamed?)
        | (before.passing - after.passing - after.failing - after.skipped)
    )
    # New passes = anything passing after that wasn't passing before. Includes
    # previously-failing tests that got fixed (a positive signal, worth surfacing).
    new_passes = sorted(after.passing - before.passing)
    no_longer_run = sorted(
        before.passing - after.passing - after.failing - after.skipped
    )
    return DiffReport(
        before_ref=before.ref,
        after_ref=after.ref,
        framework=before.framework,
        before_passing=len(before.passing),
        after_passing=len(after.passing),
        regressions=regressions,
        new_passes=new_passes,
        no_longer_run=no_longer_run,
    )


def render_human(report: DiffReport, before: RunResult, after: RunResult) -> str:
    lines = [
        f"Before ({report.before_ref}): {report.before_passing} passing",
        f"After  ({report.after_ref}): {report.after_passing} passing",
        f"Framework: {report.framework}",
        "",
    ]
    if report.regressions:
        lines.append(f"REGRESSIONS ({len(report.regressions)}):")
        for r in report.regressions:
            tag = " (no longer run)" if r in report.no_longer_run else ""
            lines.append(f"  - {r}{tag}")
        lines.append("")
    else:
        lines.append("No regressions.")
        lines.append("")
    if report.new_passes:
        lines.append(f"New passes ({len(report.new_passes)}):")
        for r in report.new_passes[:10]:
            lines.append(f"  + {r}")
        if len(report.new_passes) > 10:
            lines.append(f"  ... ({len(report.new_passes) - 10} more)")
    return "\n".join(lines)


def render_json(report: DiffReport) -> str:
    return json.dumps(asdict(report), indent=2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Run tests at two git refs and assert no regression."
    )
    ap.add_argument("--before", required=True,
                    help="Git ref for the BEFORE state (e.g. HEAD~1, main, abc123)")
    ap.add_argument("--after", required=True,
                    help="Git ref for the AFTER state (e.g. HEAD, my-branch)")
    ap.add_argument("--repo", type=Path, default=Path.cwd(),
                    help="Repo root (default: cwd)")
    ap.add_argument("--command", nargs="+",
                    help="Override test command (e.g. --command pytest tests/unit)")
    ap.add_argument("--json", action="store_true",
                    help="Emit JSON instead of human report.")
    args = ap.parse_args()

    repo = args.repo.resolve()
    if not (repo / ".git").exists():
        print(f"not a git repo: {repo}", file=sys.stderr)
        return 2

    # Validate refs exist before doing expensive worktree work
    for ref in (args.before, args.after):
        r = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", ref],
            cwd=repo, capture_output=True, text=True,
        )
        if r.returncode != 0:
            print(f"git ref not found: {ref}", file=sys.stderr)
            return 2

    # Create temp dirs for both worktrees
    base_tmp = Path(tempfile.mkdtemp(prefix="dynos-verify-"))
    before_dir = base_tmp / "before"
    after_dir = base_tmp / "after"

    try:
        try:
            create_worktree(repo, args.before, before_dir)
            create_worktree(repo, args.after, after_dir)
        except WorktreeFailure as e:
            print(f"worktree setup failed: {e}", file=sys.stderr)
            return 2

        before_run = run_at_worktree(before_dir, args.before, args.command)
        after_run = run_at_worktree(after_dir, args.after, args.command)

        if before_run.error or after_run.error:
            err = before_run.error or after_run.error
            print(f"could not run tests: {err}", file=sys.stderr)
            return 2

        report = diff_runs(before_run, after_run)
        if args.json:
            print(render_json(report))
        else:
            print(render_human(report, before_run, after_run))

        return 1 if report.regressions else 0
    finally:
        # Always clean up worktrees and temp dir, even on error
        remove_worktree(repo, before_dir)
        remove_worktree(repo, after_dir)
        try:
            shutil.rmtree(base_tmp, ignore_errors=True)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
