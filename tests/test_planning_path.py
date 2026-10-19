"""Planning path eligibility remains independent of audit fast_track."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"hooks"))
import ctl
from lib_validate import compute_planning_path


def test_planning_path_matrix():
    def m(risk, domains, kind="feature"):
        return {"classification":{"risk_level":risk,"domains":domains,"type":kind}}
    assert compute_planning_path(m("low",["backend"]))=="combined"
    assert compute_planning_path(m("medium",["backend","db"]))=="combined"
    assert compute_planning_path(m("high",["backend"]))=="staged"
    assert compute_planning_path(m("medium",["a","b","c"]))=="staged"
    assert compute_planning_path(m("low",["migration"],"migration"))=="staged"


def test_combined_stage_walk_refuses_missing_spec(tmp_path: Path, capsys):
    task=tmp_path/".dynos"/"task-20261018-001"; task.mkdir(parents=True)
    (task/"manifest.json").write_text(json.dumps({"planning_path":"combined","stage":"SPEC_NORMALIZATION"}))
    assert ctl.cmd_run_combined_stage_walk(SimpleNamespace(task_dir=str(task)))==1
    assert "missing spec.md" in capsys.readouterr().out


def test_hierarchy_depends_on_independent_subsystems_not_criteria():
    from lib_validate import compute_planning_mode
    one=[{"name":"ui","paths":["src/a"],"independent":True}]
    three=[{"name":str(i),"paths":[f"src/{i}"],"independent":True} for i in range(3)]
    assert compute_planning_mode("medium",one)=="standard"
    assert compute_planning_mode("medium",three)=="hierarchical"
    assert compute_planning_mode("high",one)=="standard"
    assert compute_planning_mode("high",three[:2])=="hierarchical"


def test_classification_rejects_overlapping_subsystem_paths(tmp_path: Path):
    task=tmp_path/".dynos"/"task-20261018-002"; task.mkdir(parents=True)
    with __import__("pytest").raises(ValueError,match="paths overlap"):
        ctl._normalize_classification_payload(task,{"type":"feature","domains":["backend"],"risk_level":"medium",
          "subsystems":[{"name":"a","paths":["src/shared"],"independent":True},
                        {"name":"b","paths":["src/shared"],"independent":True}]})
