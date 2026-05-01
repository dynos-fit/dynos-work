"""TDD-first tests for claude-md-auditor risk gate (task-20260430-007).

The claude-md-auditor was added on 2026-04-30 as a third mandatory blocking
auditor with the directive "runs on every task, every audit cycle, cannot
be skipped." Per the latency investigation (see /tmp/bug_report.json from
the same date), this is the largest single driver of audit-phase
slowdown: every task, regardless of size or whether CLAUDE.md was
touched, pays for an extra full auditor spawn plus its ensemble cascade.

This test suite drives the risk-gating contract:

  - Skip claude-md-auditor when (CLAUDE.md is NOT in the diff) AND
    (risk_level in {"low", "medium"}). The auditor exists to catch
    drift between code and the project rules; if neither is plausibly
    in scope (low/medium-risk task that does not touch CLAUDE.md), the
    cost is wasted.
  - Run claude-md-auditor when risk_level in {"high", "critical"},
    regardless of diff. High-risk tasks have higher consequences for
    rule drift, so the cost is justified.
  - Run claude-md-auditor whenever CLAUDE.md (or any nested
    CLAUDE.md) is in the diff, regardless of risk. Direct edits to
    the rules file always merit verification.
  - Fail-open when diff_files cannot be computed (None). The auditor
    runs as before — risk-gating is opt-in via the --diff-base CLI flag.

The other two mandatory blocking auditors (security, spec-completion)
are NOT risk-gated by this change. They have a different cost/value
shape and the latency investigation does not single them out.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))


def _build_plan(*, risk_level: str, diff_files: list[str] | None, fast_track: bool = False) -> dict:
    from router import build_audit_plan
    return build_audit_plan(
        Path.cwd(),
        task_type="backend",
        domains=[],
        fast_track=fast_track,
        risk_level=risk_level,
        task_id="test-task",
        diff_files=diff_files,
    )


def _entry_for(plan: dict, name: str) -> dict | None:
    for e in plan.get("auditors", []):
        if e.get("name") == name:
            return e
    return None


# ---------------------------------------------------------------------------
# Skip path: low/medium risk + CLAUDE.md not in diff → action=skip
# ---------------------------------------------------------------------------
def test_low_risk_no_claude_md_in_diff_skips_auditor(tmp_path: Path):
    plan = _build_plan(risk_level="low", diff_files=["src/foo.py", "tests/test_foo.py"])
    e = _entry_for(plan, "claude-md-auditor")
    assert e is not None, "claude-md-auditor must be in the plan"
    assert e.get("action") == "skip", f"expected skip; got {e}"
    reason = (e.get("reason") or "").lower()
    assert "claude-md" in reason or "claude.md" in reason or "risk" in reason


def test_medium_risk_no_claude_md_in_diff_skips_auditor(tmp_path: Path):
    plan = _build_plan(risk_level="medium", diff_files=["hooks/router.py"])
    e = _entry_for(plan, "claude-md-auditor")
    assert e is not None
    assert e.get("action") == "skip"


# ---------------------------------------------------------------------------
# Run path: high/critical risk → action=spawn regardless of diff
# ---------------------------------------------------------------------------
def test_high_risk_runs_auditor_regardless_of_diff(tmp_path: Path):
    plan = _build_plan(risk_level="high", diff_files=["src/foo.py"])
    e = _entry_for(plan, "claude-md-auditor")
    assert e is not None
    assert e.get("action") == "spawn"


def test_critical_risk_runs_auditor_regardless_of_diff(tmp_path: Path):
    plan = _build_plan(risk_level="critical", diff_files=["src/foo.py"])
    e = _entry_for(plan, "claude-md-auditor")
    assert e is not None
    assert e.get("action") == "spawn"


# ---------------------------------------------------------------------------
# Run path: CLAUDE.md in diff → action=spawn regardless of risk
# ---------------------------------------------------------------------------
def test_claude_md_in_diff_runs_auditor_at_low_risk(tmp_path: Path):
    plan = _build_plan(risk_level="low", diff_files=["CLAUDE.md", "src/foo.py"])
    e = _entry_for(plan, "claude-md-auditor")
    assert e is not None
    assert e.get("action") == "spawn"


def test_nested_claude_md_in_diff_runs_auditor(tmp_path: Path):
    plan = _build_plan(risk_level="medium", diff_files=["packages/lib/CLAUDE.md"])
    e = _entry_for(plan, "claude-md-auditor")
    assert e is not None
    assert e.get("action") == "spawn"


# ---------------------------------------------------------------------------
# Fail-open path: diff_files=None → run as before
# ---------------------------------------------------------------------------
def test_diff_files_none_runs_auditor_failopen(tmp_path: Path):
    """When the caller cannot determine diff_files (e.g., snapshot SHA
    missing), fall open to the pre-gate behavior — run the auditor.
    Risk-gating is opt-in; partial information must not silently skip."""
    plan = _build_plan(risk_level="low", diff_files=None)
    e = _entry_for(plan, "claude-md-auditor")
    assert e is not None
    assert e.get("action") == "spawn"


# ---------------------------------------------------------------------------
# Other always-on auditors are NOT risk-gated by this change
# ---------------------------------------------------------------------------
def test_security_and_spec_completion_unaffected(tmp_path: Path):
    plan = _build_plan(risk_level="low", diff_files=["src/foo.py"])
    sec = _entry_for(plan, "security-auditor")
    spec = _entry_for(plan, "spec-completion-auditor")
    assert sec is not None and sec.get("action") == "spawn", \
        "security-auditor must continue to run on every task"
    assert spec is not None and spec.get("action") == "spawn", \
        "spec-completion-auditor must continue to run on every task"
