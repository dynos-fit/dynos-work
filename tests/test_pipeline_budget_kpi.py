"""TDD-first tests for pipeline_budget KPI in retrospective (task-20260430-009).

The latency investigation (/tmp/bug_report.json) recommendation #5: add a
pipeline-budget KPI to task-retrospective.json so future gate-addition
PRs can be vetoed on regression. Without this, every well-meaning
"perf:" or "fix:" commit (like CG-013) can silently grow the audit
phase without anyone noticing until the user complains.

Contract:

  compute_pipeline_budget(task_dir) returns a dict with at minimum:
    - audit_phase_llm_calls:    count of token-usage events where
                                phase=audit AND type=spawn
    - audit_phase_input_tokens: sum of input_tokens across all audit
                                phase events
    - audit_phase_output_tokens: sum of output_tokens across audit
                                 phase events
    - audit_phase_total_tokens:  audit_phase_input + audit_phase_output

  When token-usage.json is missing or empty, returns zeros for all
  fields (graceful — old tasks and new tasks pre-token-record produce
  meaningful baselines without erroring).

Compute_reward integrates it into the retrospective dict under the
key 'pipeline_budget'. validate_retrospective tolerates the new key
(it is optional in the schema).

Audit-only fields are intentionally — execution and planning phases
have their own perf shape and the bug investigation specifically named
the audit phase as the latency hotspot.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))


def _make_task_dir(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260430-PB"
    td.mkdir(parents=True)
    (td / "manifest.json").write_text(json.dumps({"task_id": td.name}))
    return td


def _write_token_usage(td: Path, events: list[dict]) -> None:
    (td / "token-usage.json").write_text(json.dumps({
        "events": events,
        "agents": {},
        "by_agent": {},
        "by_model": {},
        "total": sum(e.get("tokens", 0) for e in events),
        "total_input_tokens": sum(e.get("input_tokens", 0) for e in events),
        "total_output_tokens": sum(e.get("output_tokens", 0) for e in events),
    }))


def test_compute_pipeline_budget_counts_audit_spawns(tmp_path: Path):
    from lib_validate import compute_pipeline_budget
    td = _make_task_dir(tmp_path)
    _write_token_usage(td, [
        {"phase": "execution", "type": "spawn", "input_tokens": 5000, "output_tokens": 1000},
        {"phase": "audit", "type": "spawn", "input_tokens": 30000, "output_tokens": 2000},
        {"phase": "audit", "type": "spawn", "input_tokens": 28000, "output_tokens": 1800},
        {"phase": "audit", "type": "deterministic", "input_tokens": 0, "output_tokens": 0},
        {"phase": "audit", "type": "spawn", "input_tokens": 35000, "output_tokens": 3000},
    ])
    pb = compute_pipeline_budget(td)
    assert pb["audit_phase_llm_calls"] == 3
    assert pb["audit_phase_input_tokens"] == 30000 + 28000 + 0 + 35000
    assert pb["audit_phase_output_tokens"] == 2000 + 1800 + 0 + 3000
    assert pb["audit_phase_total_tokens"] == pb["audit_phase_input_tokens"] + pb["audit_phase_output_tokens"]


def test_compute_pipeline_budget_excludes_non_audit_phases(tmp_path: Path):
    from lib_validate import compute_pipeline_budget
    td = _make_task_dir(tmp_path)
    _write_token_usage(td, [
        {"phase": "planning", "type": "spawn", "input_tokens": 100, "output_tokens": 50},
        {"phase": "execution", "type": "spawn", "input_tokens": 100, "output_tokens": 50},
        {"phase": "repair", "type": "spawn", "input_tokens": 100, "output_tokens": 50},
    ])
    pb = compute_pipeline_budget(td)
    assert pb["audit_phase_llm_calls"] == 0
    assert pb["audit_phase_input_tokens"] == 0
    assert pb["audit_phase_output_tokens"] == 0


def test_compute_pipeline_budget_empty_or_missing_returns_zeros(tmp_path: Path):
    from lib_validate import compute_pipeline_budget
    td = _make_task_dir(tmp_path)
    # No token-usage.json at all.
    pb = compute_pipeline_budget(td)
    assert pb["audit_phase_llm_calls"] == 0
    assert pb["audit_phase_input_tokens"] == 0
    assert pb["audit_phase_output_tokens"] == 0
    # Now an empty file.
    (td / "token-usage.json").write_text(json.dumps({"events": []}))
    pb2 = compute_pipeline_budget(td)
    assert pb2["audit_phase_llm_calls"] == 0


def test_compute_pipeline_budget_tolerates_malformed_events(tmp_path: Path):
    from lib_validate import compute_pipeline_budget
    td = _make_task_dir(tmp_path)
    _write_token_usage(td, [
        {"phase": "audit", "type": "spawn", "input_tokens": 1000, "output_tokens": 500},
        {"phase": "audit", "type": "spawn"},  # missing tokens
        "not-a-dict",  # malformed
        {},  # empty event
    ])
    pb = compute_pipeline_budget(td)
    assert pb["audit_phase_llm_calls"] == 2  # both audit+spawn entries count
    assert pb["audit_phase_input_tokens"] == 1000
    assert pb["audit_phase_output_tokens"] == 500


def test_compute_reward_integrates_pipeline_budget(tmp_path: Path):
    """End-to-end: compute_reward must place pipeline_budget under that
    exact key in the retrospective dict it returns."""
    from lib_validate import compute_reward
    td = _make_task_dir(tmp_path)
    _write_token_usage(td, [
        {"phase": "audit", "type": "spawn", "input_tokens": 50000, "output_tokens": 2000},
    ])
    retro = compute_reward(td)
    assert "pipeline_budget" in retro, (
        f"compute_reward must surface pipeline_budget for the retro receipt; "
        f"keys present: {sorted(retro.keys())}"
    )
    pb = retro["pipeline_budget"]
    assert isinstance(pb, dict)
    assert pb.get("audit_phase_llm_calls") == 1
    assert pb.get("audit_phase_input_tokens") == 50000


def test_validate_retrospective_accepts_pipeline_budget(tmp_path: Path):
    """validate_retrospective must not flag pipeline_budget as an error
    once compute_reward is writing it."""
    from lib_validate import validate_retrospective
    td = _make_task_dir(tmp_path)
    retro = {
        "task_id": td.name,
        "task_type": "feature",
        "quality_score": 0.9,
        "cost_score": 0.5,
        "efficiency_score": 0.7,
        "task_risk_level": "medium",
        "task_domains": ["backend"],
        "model_used_by_agent": {},
        "auditor_zero_finding_streaks": {},
        "executor_repair_frequency": {},
        "findings_by_category": {},
        "findings_by_auditor": {},
        "repair_cycle_count": 0,
        "spec_review_iterations": 1,
        "subagent_spawn_count": 3,
        "wasted_spawns": 0,
        "total_token_usage": 100000,
        "agent_source": {},
        "task_outcome": "DONE",
        "pipeline_budget": {
            "audit_phase_llm_calls": 3,
            "audit_phase_input_tokens": 90000,
            "audit_phase_output_tokens": 5000,
            "audit_phase_total_tokens": 95000,
        },
    }
    (td / "task-retrospective.json").write_text(json.dumps(retro))
    errs = validate_retrospective(td)
    bad = [e for e in errs if "pipeline_budget" in e]
    assert not bad, f"validate_retrospective complained about pipeline_budget: {bad}"
