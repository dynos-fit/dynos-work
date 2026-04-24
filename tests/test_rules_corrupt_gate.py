"""Tests for _refuse_if_rules_corrupt sentinel gate wired to all 13 stage-advancing ctl commands.

AC 16: When .dynos/.rules_corrupt sentinel is present, each of the 13 stage-advancing ctl
       commands must exit 1 and stderr must contain 'prevention-rules.json is corrupt'.
AC 17: _refuse_if_rules_corrupt docstring does NOT contain 'AC 18 scope'. Contains text
       about blocking 'every stage-advancing ctl command'. Also mentions 'cmd_transition'.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CTL = ROOT / "hooks" / "ctl.py"

SENTINEL_MESSAGE = "prevention-rules.json is corrupt"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_task_dir(tmp_path: Path, task_id: str = "task-20260423-test") -> Path:
    """Create a minimal task directory rooted at tmp_path.

    Structure:
      tmp_path/
        .dynos/
          task-<id>/
            manifest.json
    """
    root = tmp_path / "project"
    task_dir = root / ".dynos" / task_id
    task_dir.mkdir(parents=True)
    # Write a minimal manifest.json so ctl.py can at least load it.
    (task_dir / "manifest.json").write_text(json.dumps({
        "task_id": task_id,
        "stage": "FOUNDRY_INITIALIZED",
        "classification": {
            "type": "feature",
            "risk_level": "low",
            "domains": ["backend"],
        },
    }))
    return task_dir


def _set_sentinel(task_dir: Path) -> None:
    """Create the sentinel file at <root>/.dynos/.rules_corrupt."""
    # root is task_dir.parent.parent (mirrors _root_for_task_dir logic)
    root = task_dir.parent.parent
    sentinel = root / ".dynos" / ".rules_corrupt"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("sentinel")


def _run_ctl(args: list[str], task_dir: Path | None = None) -> subprocess.CompletedProcess:
    """Run hooks/ctl.py with args, capturing stdout+stderr."""
    return subprocess.run(
        [sys.executable, str(CTL), *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )


# ---------------------------------------------------------------------------
# Parametrize: (subcommand, extra_args using str task_dir placeholder)
# ---------------------------------------------------------------------------

_TASK_DIR_TOKEN = "__TASK_DIR__"

_COMMANDS: list[tuple[str, list[str]]] = [
    # 1. transition
    ("transition", [_TASK_DIR_TOKEN, "EXECUTION"]),
    # 2. approve-stage
    ("approve-stage", [_TASK_DIR_TOKEN, "SPEC_REVIEW"]),
    # 3. audit-receipt
    ("audit-receipt", [_TASK_DIR_TOKEN, "test-auditor", "--route-mode", "generic"]),
    # 4. run-execution-segment-done
    ("run-execution-segment-done", [_TASK_DIR_TOKEN, "seg-1", "--injected-prompt-sha256", "abc123"]),
    # 5. run-audit-finish
    ("run-audit-finish", [_TASK_DIR_TOKEN]),
    # 6. run-repair-execution-ready
    ("run-repair-execution-ready", [_TASK_DIR_TOKEN]),
    # 7. run-plan-audit
    ("run-plan-audit", [_TASK_DIR_TOKEN]),
    # 8. run-start-classification
    ("run-start-classification", [_TASK_DIR_TOKEN]),
    # 9. run-spec-ready
    ("run-spec-ready", [_TASK_DIR_TOKEN]),
    # 10. run-repair-log-build
    ("run-repair-log-build", [_TASK_DIR_TOKEN]),
    # 11. run-execute-setup
    ("run-execute-setup", [_TASK_DIR_TOKEN]),
    # 12. run-execution-finish
    ("run-execution-finish", [_TASK_DIR_TOKEN]),
    # 13. run-repair-retry
    ("run-repair-retry", [_TASK_DIR_TOKEN]),
]

_COMMAND_IDS = [cmd for cmd, _ in _COMMANDS]


def _substitute_task_dir(args: list[str], task_dir: Path) -> list[str]:
    return [str(task_dir) if a == _TASK_DIR_TOKEN else a for a in args]


# ---------------------------------------------------------------------------
# AC 16 - Part A: sentinel present → exit 1 + correct stderr message
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("subcommand,extra_args", _COMMANDS, ids=_COMMAND_IDS)
def test_sentinel_present_blocks_command(subcommand: str, extra_args: list[str], tmp_path: Path) -> None:
    """AC 16: sentinel present → exit 1 and stderr contains sentinel message."""
    task_dir = _make_task_dir(tmp_path)
    _set_sentinel(task_dir)

    args = [subcommand] + _substitute_task_dir(extra_args, task_dir)
    proc = _run_ctl(args, task_dir)

    assert proc.returncode == 1, (
        f"{subcommand}: expected exit 1 when sentinel present, "
        f"got {proc.returncode}.\nstdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    assert SENTINEL_MESSAGE in proc.stderr, (
        f"{subcommand}: expected '{SENTINEL_MESSAGE}' in stderr.\n"
        f"stderr={proc.stderr!r}"
    )


# ---------------------------------------------------------------------------
# AC 16 - Part B: no sentinel → stderr does NOT contain sentinel message
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("subcommand,extra_args", _COMMANDS, ids=_COMMAND_IDS)
def test_no_sentinel_command_does_not_fail_with_sentinel_message(
    subcommand: str, extra_args: list[str], tmp_path: Path
) -> None:
    """AC 16 control: without sentinel, the corrupt-rules message must NOT appear."""
    task_dir = _make_task_dir(tmp_path)
    # No sentinel set — sentinel file deliberately absent

    args = [subcommand] + _substitute_task_dir(extra_args, task_dir)
    proc = _run_ctl(args, task_dir)

    assert SENTINEL_MESSAGE not in proc.stderr, (
        f"{subcommand}: sentinel message appeared without sentinel file.\n"
        f"stderr={proc.stderr!r}"
    )


# ---------------------------------------------------------------------------
# Additional test: sentinel fires before --force validation in cmd_transition
# ---------------------------------------------------------------------------

def test_sentinel_blocks_cmd_transition_before_force_validation(tmp_path: Path) -> None:
    """AC 16: sentinel fires even when --force is given without --reason/--approver.

    The sentinel check is the FIRST thing cmd_transition does; --force validation
    comes later. So even with an invalid --force invocation, the corrupt-rules
    message must appear and exit code must be 1.
    """
    task_dir = _make_task_dir(tmp_path)
    _set_sentinel(task_dir)

    # --force without --reason and --approver would normally cause exit code 2.
    # But the sentinel fires first, so we should see exit 1 + sentinel message.
    proc = _run_ctl(["transition", str(task_dir), "EXECUTION", "--force"], task_dir)

    assert proc.returncode == 1, (
        f"Expected exit 1 (sentinel), got {proc.returncode}.\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    assert SENTINEL_MESSAGE in proc.stderr, (
        f"Expected sentinel message in stderr.\nstderr={proc.stderr!r}"
    )
    # Critically: --force validation message should NOT appear
    assert "--force requires --reason" not in proc.stderr, (
        "Force-validation error appeared before sentinel check was applied.\n"
        f"stderr={proc.stderr!r}"
    )


# ---------------------------------------------------------------------------
# AC 17: docstring tests — read ctl.py source text
# ---------------------------------------------------------------------------

def _read_ctl_source() -> str:
    return CTL.read_text(encoding="utf-8")


def test_refuse_if_rules_corrupt_docstring_lacks_ac18_scope() -> None:
    """AC 17: 'AC 18 scope' must not appear in ctl.py."""
    source = _read_ctl_source()
    assert "AC 18 scope" not in source, (
        "Found forbidden text 'AC 18 scope' in hooks/ctl.py"
    )


def test_refuse_if_rules_corrupt_docstring_mentions_every_stage_advancing() -> None:
    """AC 17: _refuse_if_rules_corrupt docstring mentions 'every stage-advancing ctl command'."""
    source = _read_ctl_source()
    assert "every stage-advancing ctl command" in source, (
        "Expected 'every stage-advancing ctl command' in _refuse_if_rules_corrupt docstring"
    )


def test_refuse_if_rules_corrupt_docstring_mentions_cmd_transition() -> None:
    """AC 17: _refuse_if_rules_corrupt docstring mentions 'cmd_transition'."""
    source = _read_ctl_source()
    # Find the function definition and look within the docstring region
    func_start = source.find("def _refuse_if_rules_corrupt(")
    assert func_start != -1, "_refuse_if_rules_corrupt not found in ctl.py"
    # Find the end of the function by looking for the next top-level def
    next_func = source.find("\ndef ", func_start + 1)
    func_body = source[func_start:next_func] if next_func != -1 else source[func_start:]
    assert "cmd_transition" in func_body, (
        "Expected 'cmd_transition' in _refuse_if_rules_corrupt function body/docstring.\n"
        f"Function body excerpt: {func_body[:500]!r}"
    )


# ---------------------------------------------------------------------------
# Regression: sentinel path is rooted at project root, not task_dir
# ---------------------------------------------------------------------------

def test_sentinel_must_be_at_project_root_not_task_dir(tmp_path: Path) -> None:
    """The sentinel path is <project_root>/.dynos/.rules_corrupt.

    Placing it at <task_dir>/.rules_corrupt must NOT trigger the gate,
    proving the implementation looks at the correct grandparent root.
    """
    task_dir = _make_task_dir(tmp_path)
    # Wrong location: inside task_dir
    wrong_sentinel = task_dir / ".rules_corrupt"
    wrong_sentinel.write_text("wrong location")

    proc = _run_ctl(["transition", str(task_dir), "EXECUTION"], task_dir)

    assert SENTINEL_MESSAGE not in proc.stderr, (
        "Sentinel fired from wrong path (task_dir/.rules_corrupt).\n"
        f"stderr={proc.stderr!r}"
    )


def test_sentinel_correct_path_at_project_root(tmp_path: Path) -> None:
    """Confirm sentinel at <root>/.dynos/.rules_corrupt fires correctly."""
    task_dir = _make_task_dir(tmp_path)
    # Correct location mirrors _root_for_task_dir: task_dir.parent.parent
    root = task_dir.parent.parent
    correct_sentinel = root / ".dynos" / ".rules_corrupt"
    correct_sentinel.parent.mkdir(parents=True, exist_ok=True)
    correct_sentinel.write_text("correct location")

    proc = _run_ctl(["transition", str(task_dir), "EXECUTION"], task_dir)

    assert proc.returncode == 1
    assert SENTINEL_MESSAGE in proc.stderr, (
        f"Sentinel at correct path did not fire.\nstderr={proc.stderr!r}"
    )
