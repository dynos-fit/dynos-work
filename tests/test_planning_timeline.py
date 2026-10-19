"""Planning latency buckets use stage, spawn, and approval timestamps."""
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"hooks"))
from lib_validate import compute_planning_timeline


def test_timeline_buckets_and_respawns(tmp_path: Path):
    task=tmp_path/".dynos"/"task-20261018-001"; (task/"receipts").mkdir(parents=True)
    (task/"manifest.json").write_text(json.dumps({"created_at":"2026-10-18T00:00:00Z"}))
    transitions=[{"from":"PLANNING","to":"SPEC_REVIEW","at":"2026-10-18T00:30:00Z"},
                 {"from":"TDD_REVIEW","to":"PRE_EXECUTION_SNAPSHOT","at":"2026-10-18T02:00:00Z"}]
    (task/"stage-transitions.jsonl").write_text("".join(json.dumps(x)+"\n" for x in transitions))
    rows=[]
    for start,end in (("00:05","00:15"),("00:20","00:30")):
        rows.extend([{"phase":"pre","subagent_type":"dynos-work:planning","timestamp":f"2026-10-18T{start}:00Z","prompt_sha256":"a"},
                     {"phase":"post","subagent_type":"dynos-work:planning","timestamp":f"2026-10-18T{end}:00Z","result_sha256":"b"}])
    (task/"spawn-log.jsonl").write_text("".join(json.dumps(x)+"\n" for x in rows))
    (task/"receipts"/"human-approval-SPEC_REVIEW.json").write_text(json.dumps({"written_at":"2026-10-18T01:00:00Z"}))
    out=compute_planning_timeline(task)
    assert out["pre_execution_seconds"]==7200
    assert out["model_seconds"]==1200
    assert out["approval_seconds"]==1800
    assert out["idle_seconds"]==3600
    assert out["orchestrator_seconds"]==600
    assert out["planner_respawns"]==1
    assert out["planner_spawns"][0]["seconds"]==600


def test_timeline_degrades_when_files_are_missing(tmp_path: Path):
    out=compute_planning_timeline(tmp_path)
    assert out["pre_execution_seconds"] is None
    assert out["planner_spawns"]==[]
    assert out["model_seconds"]==out["approval_seconds"]==out["idle_seconds"]==0


def test_start_regression_candidate_uses_five_task_median(tmp_path: Path):
    sys.path.insert(0, str(ROOT / "memory"))
    from postmortem_analysis import build_analysis_prompt
    root=tmp_path/"project"; dynos=root/".dynos"; current=dynos/"task-current"
    current.mkdir(parents=True)
    (current/"manifest.json").write_text(json.dumps({"task_id":"task-current","stage":"DONE","title":"current"}))
    (current/"task-retrospective.json").write_text(json.dumps({
        "task_id":"task-current","quality_score":1.0,"repair_cycle_count":0,
        "planning_timeline":{"pre_execution_seconds":200,"planner_spawns":[{},{}]},
    }))
    for index in range(5):
        prior=dynos/f"task-prior-{index}"; prior.mkdir(parents=True)
        (prior/"task-retrospective.json").write_text(json.dumps({
            "planning_timeline":{"pre_execution_seconds":100,"planner_spawns":[{}]},
        }))
    data=build_analysis_prompt(current)
    assert data["has_findings"] is True
    assert "prevention rule candidates" in data["prompt"].lower()
    assert "more than 1.5×" in data["prompt"]
