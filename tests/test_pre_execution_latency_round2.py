"""Round-2 pre-execution latency fixes.

Covers the deterministic pieces that keep /dynos-work:start short:
artifact ceilings, the planner rewrite cap, the planner bash guard, the
deterministic plan audit, the amendment window, task-churn counts,
planning_mode persistence, and measured spawn durations in the timeline.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

import ctl  # noqa: E402
from lib_validate import (  # noqa: E402
    MAX_ACCEPTANCE_CRITERIA,
    MAX_PLAN_WORDS,
    MAX_SPEC_WORDS,
    artifact_ceilings,
    compute_planning_timeline,
    planning_path_for,
    risk_bearing_domains,
    validate_task_artifacts,
)
from write_policy import WriteAttempt, decide_write  # noqa: E402


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _spec(n_criteria: int, filler_words: int = 0) -> str:
    criteria = "\n".join(f"{i}. Criterion {i} holds." for i in range(1, n_criteria + 1))
    filler = " ".join(["word"] * filler_words)
    return (
        "# Normalized Spec\n\n"
        f"## Task Summary\nSummary. {filler}\n\n"
        "## User Context\nUser.\n\n"
        f"## Acceptance Criteria\n{criteria}\n\n"
        "## Implicit Requirements Surfaced\nNone.\n\n"
        "## Out of Scope\nNothing.\n\n"
        "## Assumptions\nsafe assumption: none\n\n"
        "## Risk Notes\nNone.\n"
    )


def _plan(filler_words: int = 0) -> str:
    filler = " ".join(["word"] * filler_words)
    return textwrap.dedent(f"""\
        # Implementation Plan

        ## Technical Approach
        Approach. {filler}

        ## Reference Code
        None.

        ## Components / Modules
        ### Component: Widget
        - **Files:** src/widget.py

        ## Data Flow
        Flow.

        ## Error Handling Strategy
        Errors.

        ## Test Strategy
        Tests.

        ## Dependency Graph
        None.

        ## Open Questions
        None.
    """)


def _task(tmp_path: Path, *, stage: str = "PLANNING", risk: str = "medium",
          domains: list[str] | None = None, spec: str | None = None,
          plan: str | None = None, with_graph: bool = True) -> Path:
    project = tmp_path / "project"
    task_dir = project / ".dynos" / "task-20261019-001"
    (task_dir / "receipts").mkdir(parents=True)
    manifest = {
        "task_id": task_dir.name,
        "created_at": "2026-10-19T00:00:00Z",
        "title": "t",
        "raw_input": "x",
        "stage": stage,
        "classification": {
            "type": "feature",
            "domains": domains or ["backend"],
            "risk_level": risk,
            "notes": "",
            "tdd_required": False,
        },
        "retry_counts": {},
        "blocked_reason": None,
        "completion_at": None,
    }
    (task_dir / "manifest.json").write_text(json.dumps(manifest))
    (task_dir / "spec.md").write_text(spec if spec is not None else _spec(3))
    (task_dir / "plan.md").write_text(plan if plan is not None else _plan())
    if with_graph:
        (task_dir / "execution-graph.json").write_text(json.dumps({
            "task_id": task_dir.name,
            "segments": [{
                "id": "seg-1", "executor": "backend-executor", "description": "d",
                "files_expected": ["src/widget.py"], "depends_on": [],
                "parallelizable": True, "criteria_ids": [1, 2, 3],
            }],
        }))
    return task_dir


def _run_ctl(root: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    merged = {**os.environ, "DYNOS_HOME": str(root / "dynos-home")}
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "ctl.py"), *args],
        cwd=root, capture_output=True, text=True, env=merged,
    )


def _last_json(stdout: str) -> dict:
    """run-start-classification prints the veto decision then its own payload."""
    decoder = json.JSONDecoder()
    idx = 0
    last: dict = {}
    text = stdout.strip()
    while idx < len(text):
        while idx < len(text) and text[idx].isspace():
            idx += 1
        if idx >= len(text):
            break
        obj, end = decoder.raw_decode(text, idx)
        if isinstance(obj, dict):
            last = obj
        idx = end
    return last


def _run_hook(root: Path, payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "pre_tool_use.py")],
        input=json.dumps(payload), cwd=root, capture_output=True, text=True,
        env={**os.environ, "DYNOS_HOME": str(root / "dynos-home")},
    )


# ---------------------------------------------------------------------------
# P0: classification and path
# ---------------------------------------------------------------------------

def test_risk_bearing_domains_drops_testing_and_docs():
    assert risk_bearing_domains(["backend", "testing", "docs", "ml"]) == ["backend", "ml"]
    assert risk_bearing_domains(["testing", "docs"]) == []
    assert risk_bearing_domains("backend") == []


def test_planning_path_combined_is_default_for_observed_shapes():
    # The 22 observed tasks: 18 high, mostly 4–5 domains including testing/docs.
    assert planning_path_for("high", ["backend", "ml", "testing", "docs", "infra"], "feature") == "combined"
    assert planning_path_for("high", ["ui", "infra", "testing"], "feature") == "combined"
    assert planning_path_for("high", ["backend", "security", "testing", "docs"], "feature") == "staged"
    assert planning_path_for("high", ["db", "backend"], "feature") == "staged"
    assert planning_path_for("critical", ["ui"], "feature") == "staged"
    assert planning_path_for("medium", ["db", "security"], "feature") == "combined"
    assert planning_path_for("low", ["backend"], "migration") == "staged"


def test_run_start_classification_reports_arch_design_flag(tmp_path: Path):
    task_dir = _task(tmp_path, stage="FOUNDRY_INITIALIZED", risk="high", with_graph=False)
    (task_dir / "spec.md").unlink()
    (task_dir / "plan.md").unlink()
    manifest = json.loads((task_dir / "manifest.json").read_text())
    del manifest["classification"]["tdd_required"]
    (task_dir / "manifest.json").write_text(json.dumps(manifest))
    result = _run_ctl(tmp_path / "project", "run-start-classification", str(task_dir))
    assert result.returncode == 0, result.stderr + result.stdout
    payload = _last_json(result.stdout)
    assert payload["planning_path"] == "combined"
    assert payload["tdd_required"] is False
    assert payload["arch_design_required"] is False
    stored = json.loads((task_dir / "manifest.json").read_text())
    assert stored["arch_design_required"] is False
    assert stored["classification"]["tdd_required"] is False


# ---------------------------------------------------------------------------
# P1: artifact ceilings
# ---------------------------------------------------------------------------

def test_artifact_ceiling_defaults_and_policy_override():
    assert artifact_ceilings({}) == {
        "max_acceptance_criteria": MAX_ACCEPTANCE_CRITERIA,
        "max_spec_words": MAX_SPEC_WORDS,
        "max_plan_words": MAX_PLAN_WORDS,
    }
    assert artifact_ceilings({"max_acceptance_criteria": 40})["max_acceptance_criteria"] == 40
    assert artifact_ceilings({"max_acceptance_criteria": 0})["max_acceptance_criteria"] == MAX_ACCEPTANCE_CRITERIA
    assert artifact_ceilings({"max_acceptance_criteria": True})["max_acceptance_criteria"] == MAX_ACCEPTANCE_CRITERIA


def test_spec_with_too_many_criteria_is_rejected(tmp_path: Path):
    task_dir = _task(tmp_path, spec=_spec(MAX_ACCEPTANCE_CRITERIA + 1))
    errors = validate_task_artifacts(task_dir, run_gap=False)
    assert any("acceptance criteria; ceiling is" in e for e in errors), errors


def test_spec_at_ceiling_passes(tmp_path: Path):
    task_dir = _task(tmp_path, spec=_spec(MAX_ACCEPTANCE_CRITERIA))
    errors = validate_task_artifacts(task_dir, run_gap=False)
    assert not any("ceiling" in e for e in errors), errors


def test_oversized_spec_and_plan_are_rejected(tmp_path: Path):
    task_dir = _task(tmp_path, spec=_spec(3, filler_words=MAX_SPEC_WORDS), plan=_plan(filler_words=MAX_PLAN_WORDS))
    errors = validate_task_artifacts(task_dir, run_gap=False)
    assert any(e.startswith("spec.md is ") and "words; ceiling" in e for e in errors), errors
    assert any(e.startswith("plan.md is ") and "words; ceiling" in e for e in errors), errors


def test_policy_raises_ceiling(tmp_path: Path, monkeypatch):
    task_dir = _task(tmp_path, spec=_spec(MAX_ACCEPTANCE_CRITERIA + 5))
    monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "dynos-home"))
    from lib_core import _persistent_project_dir
    policy_dir = _persistent_project_dir(tmp_path / "project")
    policy_dir.mkdir(parents=True, exist_ok=True)
    (policy_dir / "policy.json").write_text(json.dumps({"max_acceptance_criteria": 30}))
    errors = validate_task_artifacts(task_dir, run_gap=False)
    assert not any("ceiling" in e for e in errors), errors


# ---------------------------------------------------------------------------
# P1: planner rewrite cap and bash guard
# ---------------------------------------------------------------------------

def test_planner_rewrite_cap_denies_after_allowance(tmp_path: Path):
    from lib_tool_budget import PLANNER_WRITE_TURNS
    task_dir = _task(tmp_path)
    (task_dir / "active-planner-phase").write_text("plan")
    attempt = WriteAttempt(role="planning", task_dir=task_dir, path=task_dir / "plan.md",
                           operation="modify", source="agent")
    for _ in range(PLANNER_WRITE_TURNS):
        assert decide_write(attempt).allowed is True
    denied = decide_write(attempt)
    assert denied.allowed is False
    assert "rewrite cap" in denied.reason
    # A different artifact still has its own allowance.
    other = WriteAttempt(role="planning", task_dir=task_dir, path=task_dir / "spec.md",
                         operation="modify", source="agent")
    assert decide_write(other).allowed is True
    # A new phase (repair) starts from zero.
    (task_dir / "active-planner-phase").write_text("repair")
    assert decide_write(attempt).allowed is True


def test_planner_rewrite_cap_resets_for_each_spawn_in_the_same_phase(tmp_path: Path):
    """A second repair spawn must not inherit the first one's exhausted cap."""
    from lib_core import set_planner_phase
    from lib_tool_budget import PLANNER_WRITE_TURNS
    task_dir = _task(tmp_path)
    attempt = WriteAttempt(role="planning", task_dir=task_dir, path=task_dir / "plan.md",
                           operation="modify", source="agent")
    set_planner_phase(task_dir, "repair")
    for _ in range(PLANNER_WRITE_TURNS):
        assert decide_write(attempt).allowed is True
    assert decide_write(attempt).allowed is False
    set_planner_phase(task_dir, "repair")
    assert decide_write(attempt).allowed is True


def test_planner_bash_guard_blocks_validators_and_stage_commands(tmp_path: Path):
    task_dir = _task(tmp_path)
    root = tmp_path / "project"
    (task_dir / "active-segment-role").write_text("planning")
    for command in (
        "python3 hooks/validate_task_artifacts.py .dynos/task-20261019-001 --json",
        "$DYNOS hook plan_gap_analysis --root . --task-dir .dynos/task-20261019-001",
        "$DYNOS ctl run-plan-audit .dynos/task-20261019-001",
        "$DYNOS ctl transition .dynos/task-20261019-001 PLAN_REVIEW",
        "$DYNOS ctl approve-stage .dynos/task-20261019-001 PLAN_REVIEW",
    ):
        result = _run_hook(root, {"tool_name": "Bash", "tool_input": {"command": command},
                                  "cwd": str(root), "session_id": "s"})
        assert result.returncode == 2, (command, result.returncode, result.stderr)
        assert "planner does not run validators" in result.stderr
    # Persisting the graph through ctl is still allowed.
    ok = _run_hook(root, {"tool_name": "Bash", "tool_input": {
        "command": "$DYNOS ctl write-execution-graph .dynos/task-20261019-001 --from - <<'J'\n{}\nJ"},
        "cwd": str(root), "session_id": "s"})
    assert "planner does not run validators" not in ok.stderr
    assert ok.returncode != 2 or "planner does not run" not in ok.stderr


# ---------------------------------------------------------------------------
# P2: deterministic plan audit
# ---------------------------------------------------------------------------

def test_run_plan_audit_writes_receipt_for_high_risk_without_llm(tmp_path: Path, capsys):
    task_dir = _task(tmp_path, stage="PLAN_AUDIT", risk="high")
    rc = ctl.cmd_run_plan_audit(SimpleNamespace(task_dir=str(task_dir), report_path=None,
                                                tokens_used=0, model=None))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0, out
    assert out["status"] == "passed"
    assert out["mode"] == "deterministic_only"
    assert out["llm_audit_required"] is False
    receipt = json.loads((task_dir / "receipts" / "plan-audit-check.json").read_text())
    assert receipt["model_used"] == "deterministic"
    assert receipt["plan_sha256"]
    # Second run is idempotent: receipt reused, not rewritten.
    first_mtime = (task_dir / "receipts" / "plan-audit-check.json").stat().st_mtime_ns
    rc2 = ctl.cmd_run_plan_audit(SimpleNamespace(task_dir=str(task_dir), report_path=None,
                                                 tokens_used=0, model=None))
    out2 = json.loads(capsys.readouterr().out)
    assert rc2 == 0 and out2["receipt_cached"] is True
    assert (task_dir / "receipts" / "plan-audit-check.json").stat().st_mtime_ns == first_mtime


def test_run_plan_audit_llm_opt_in_via_policy(tmp_path: Path, capsys, monkeypatch):
    task_dir = _task(tmp_path, stage="PLAN_AUDIT", risk="critical")
    monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "dynos-home"))
    from lib_core import _persistent_project_dir
    policy_dir = _persistent_project_dir(tmp_path / "project")
    policy_dir.mkdir(parents=True, exist_ok=True)
    (policy_dir / "policy.json").write_text(json.dumps({"plan_audit_llm": ["critical"]}))
    rc = ctl.cmd_run_plan_audit(SimpleNamespace(task_dir=str(task_dir), report_path=None,
                                                tokens_used=0, model=None))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["status"] == "llm_audit_required"
    assert out["llm_audit_opt_in"] is True


# ---------------------------------------------------------------------------
# P3: amendment window and churn
# ---------------------------------------------------------------------------

def _seed_spec_receipt(task_dir: Path) -> None:
    from lib_receipts import hash_file
    (task_dir / "receipts" / "spec-validated.json").write_text(json.dumps({
        "step": "spec-validated", "ts": "2026-10-19T00:00:00Z", "valid": True,
        "spec_sha256": hash_file(task_dir / "spec.md"), "criteria_count": 3,
    }))


def test_amend_artifact_second_amendment_refused(tmp_path: Path):
    task_dir = _task(tmp_path, stage="SPEC_REVIEW")
    _seed_spec_receipt(task_dir)
    root = tmp_path / "project"
    (task_dir / "spec.md").write_text(_spec(3) + "\nEdit one.\n")
    first = _run_ctl(root, "amend-artifact", str(task_dir), "spec", "--reason", "first")
    assert first.returncode == 0, first.stderr
    (task_dir / "spec.md").write_text(_spec(3) + "\nEdit two.\n")
    second = _run_ctl(root, "amend-artifact", str(task_dir), "spec", "--reason", "second")
    assert second.returncode == 1
    assert "amendment window exhausted" in second.stderr
    assert len(list((task_dir / "receipts").glob("amend-spec-*.json"))) == 1


def test_amend_artifact_policy_widens_window(tmp_path: Path):
    task_dir = _task(tmp_path, stage="SPEC_REVIEW")
    _seed_spec_receipt(task_dir)
    root = tmp_path / "project"
    env = {"DYNOS_HOME": str(tmp_path / "dynos-home")}
    from lib_core import _persistent_project_dir
    os.environ["DYNOS_HOME"] = env["DYNOS_HOME"]
    try:
        policy_dir = _persistent_project_dir(root)
    finally:
        os.environ.pop("DYNOS_HOME", None)
    policy_dir.mkdir(parents=True, exist_ok=True)
    (policy_dir / "policy.json").write_text(json.dumps({"max_amendments_per_artifact": 2}))
    for n in (1, 2):
        (task_dir / "spec.md").write_text(_spec(3) + f"\nEdit {n}.\n")
        result = _run_ctl(root, "amend-artifact", str(task_dir), "spec", "--reason", f"edit {n}", env=env)
        assert result.returncode == 0, result.stderr
    (task_dir / "spec.md").write_text(_spec(3) + "\nEdit 3.\n")
    third = _run_ctl(root, "amend-artifact", str(task_dir), "spec", "--reason", "edit 3", env=env)
    assert third.returncode == 1


def test_task_churn_counts_and_warns(tmp_path: Path):
    task_dir = _task(tmp_path)
    events = [
        {"event": "receipt_written", "step": "human-approval-SPEC_REVIEW"},
        {"event": "receipt_written", "step": "human-approval-PLAN_REVIEW"},
        {"event": "receipt_written", "step": "human-approval-PLAN_REVIEW"},
        {"event": "receipt_written", "step": "human-approval-PLAN_REVIEW"},
        {"event": "receipt_written", "step": "human-approval-TDD_REVIEW"},
        {"event": "gate_refused", "reason": "x"},
        {"event": "write_policy_denied", "role": "planning"},
        {"event": "planner_bash_denied", "command": "validate"},
        {"event": "stage_transition"},
    ]
    (task_dir / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    (task_dir / "receipts" / "amend-plan-20261019T000000Z.json").write_text("{}")
    data = ctl.compute_task_churn(task_dir)
    counts = data["counts"]
    assert counts["approval_receipts"] == 5
    assert counts["amendments"] == 1
    assert counts["gate_refused"] == 1
    assert counts["write_policy_denied"] == 1
    assert counts["planner_bash_denied"] == 1
    joined = "\n".join(data["warnings"])
    assert "re-approval loop" in joined
    assert "amendment" in joined
    assert "gate refusal" in joined
    assert "validators or stage commands" in joined
    result = _run_ctl(tmp_path / "project", "task-churn", str(task_dir), "--json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["counts"]["approval_receipts"] == 5


# ---------------------------------------------------------------------------
# P4: instrumentation
# ---------------------------------------------------------------------------

def test_run_planning_mode_persists_mode(tmp_path: Path, capsys):
    task_dir = _task(tmp_path, stage="PLANNING")
    rc = ctl.cmd_run_planning_mode(SimpleNamespace(task_dir=str(task_dir)))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    manifest = json.loads((task_dir / "manifest.json").read_text())
    assert manifest["planning_mode"] == out["planning_mode"] == "standard"
    manifest["planning_path"] = "combined"
    (task_dir / "manifest.json").write_text(json.dumps(manifest))
    ctl.cmd_run_planning_mode(SimpleNamespace(task_dir=str(task_dir)))
    capsys.readouterr()
    assert json.loads((task_dir / "manifest.json").read_text())["planning_mode"] == "combined"


def test_timeline_prefers_stop_rows_and_flags_unmeasured(tmp_path: Path):
    task = tmp_path / ".dynos" / "task-20261019-002"
    (task / "receipts").mkdir(parents=True)
    (task / "manifest.json").write_text(json.dumps({"created_at": "2026-10-19T00:00:00Z"}))
    transitions = [{"from": "PLAN_AUDIT", "to": "PRE_EXECUTION_SNAPSHOT", "at": "2026-10-19T02:00:00Z"}]
    (task / "stage-transitions.jsonl").write_text("".join(json.dumps(x) + "\n" for x in transitions))
    rows = [
        # Spawn 1: pre/post same second, stop row carries the real 15-minute span.
        {"phase": "pre", "subagent_type": "dynos-work:planning", "timestamp": "2026-10-19T00:05:00Z"},
        {"phase": "post", "subagent_type": "dynos-work:planning", "timestamp": "2026-10-19T00:05:00Z"},
        {"phase": "stop", "subagent_type": "dynos-work:planning", "timestamp": "2026-10-19T00:20:05Z",
         "started_at": "2026-10-19T00:05:03Z", "ended_at": "2026-10-19T00:20:03Z", "seconds": 900},
        # Spawn 2: pre/post same second, no stop row (old hook) → unmeasured.
        {"phase": "pre", "subagent_type": "dynos-work:planning", "timestamp": "2026-10-19T00:30:00Z"},
        {"phase": "post", "subagent_type": "dynos-work:planning", "timestamp": "2026-10-19T00:30:00Z"},
    ]
    (task / "spawn-log.jsonl").write_text("".join(json.dumps(x) + "\n" for x in rows))
    out = compute_planning_timeline(task)
    assert out["pre_execution_seconds"] == 7200
    assert out["model_seconds"] == 900
    assert out["unmeasured_spawns"] == 1
    sources = [row["source"] for row in out["planner_spawns"]]
    assert sources == ["transcript", "unmeasured"]
    assert out["planner_spawns"][1]["seconds"] is None
    assert any("no measured duration" in e for e in out["errors"])
    assert len(out["planner_spawns"]) == 2  # stop row consumed its pre row; no double count


def test_timeline_reports_missing_transitions_as_error(tmp_path: Path):
    task = tmp_path / ".dynos" / "task-20261019-003"
    task.mkdir(parents=True)
    (task / "manifest.json").write_text(json.dumps({"created_at": "2026-10-19T00:00:00Z"}))
    out = compute_planning_timeline(task)
    assert out["pre_execution_seconds"] is None
    assert any("stage-transitions.jsonl missing" in e for e in out["errors"])
    rc = ctl.cmd_timeline(SimpleNamespace(task_dir=str(task), json=True))
    assert rc == 1


def test_subagent_stop_hook_writes_stop_row(tmp_path: Path):
    from lib_tokens_hook import _parse_transcript, record_spawn_stop
    transcript = tmp_path / "agent.jsonl"
    lines = [
        {"type": "user", "timestamp": "2026-10-19T00:00:00.000Z", "message": {"role": "user", "content": "go"}},
        {"type": "assistant", "timestamp": "2026-10-19T00:12:30.000Z", "agentId": "a1",
         "message": {"model": "claude-opus-4-1", "usage": {"input_tokens": 10, "output_tokens": 5}}},
    ]
    transcript.write_text("".join(json.dumps(x) + "\n" for x in lines))
    result = _parse_transcript(transcript)
    assert result["first_ts"] == "2026-10-19T00:00:00.000Z"
    assert result["last_ts"] == "2026-10-19T00:12:30.000Z"
    task_dir = tmp_path / ".dynos" / "task-20261019-004"
    task_dir.mkdir(parents=True)
    row = record_spawn_stop(task_dir, agent_type="dynos-work:planning", agent_desc="spec",
                            session_id="s", result=result)
    assert row is not None and row["seconds"] == 750
    stored = [json.loads(l) for l in (task_dir / "spawn-log.jsonl").read_text().splitlines()]
    assert stored[0]["phase"] == "stop" and stored[0]["subagent_type"] == "dynos-work:planning"


def test_receipt_log_line_never_prints_tokens_zero(tmp_path: Path):
    task_dir = _task(tmp_path, stage="SPEC_NORMALIZATION")
    (task_dir / "spawn-log.jsonl").write_text(json.dumps({
        "phase": "post", "subagent_type": "dynos-work:planning", "timestamp": "2026-10-19T00:01:00Z",
        "prompt_sha256": "p", "result_sha256": "r",
    }) + "\n")
    from lib_receipts import receipt_planner_spawn
    receipt_planner_spawn(task_dir, "discovery", 0)
    log = (task_dir / "execution-log.md").read_text()
    assert "tokens=0" not in log
    assert "planner discovery — tokens=hook-recorded" in log
