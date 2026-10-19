"""Planner repair retries are counted per phase and stop at the cap."""
import json,sys
from pathlib import Path
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"hooks"))
import ctl
from lib_receipts import receipt_validation_override


def test_retry_budget_increments_and_exhausts(tmp_path: Path,capsys):
    task=tmp_path/".dynos"/"task-20261018-005"; task.mkdir(parents=True)
    (task/"manifest.json").write_text(json.dumps({"task_id":task.name,"retry_counts":{}}))
    args=SimpleNamespace(task_dir=str(task),phase="spec")
    for attempt in (1,2,3):
        assert ctl.cmd_planner_retry(args)==0
        result=json.loads(capsys.readouterr().out)
        assert result["attempt"]==attempt
        assert result["allowed"] is (attempt<=2)
        assert result["max"]==2
    assert json.loads((task/"manifest.json").read_text())["retry_counts"]["spec"]==3


def test_override_requires_justification_and_defaults_off_for_high(tmp_path: Path):
    task=tmp_path/"project"/".dynos"/"task-high"
    task.mkdir(parents=True)
    (task/"manifest.json").write_text(json.dumps({
        "task_id":task.name,"created_at":"2026-01-01T00:00:00Z","raw_input":"x","stage":"PLANNING",
        "classification":{"type":"feature","domains":["backend"],"risk_level":"high"},
    }))
    (task/"spec.md").write_text("# malformed\n")
    try:
        receipt_validation_override(task,"plan.md","  ")
        assert False, "blank justification should be refused"
    except ValueError as exc:
        assert "justification" in str(exc)
    try:
        receipt_validation_override(task,"plan.md","accept specified risk")
        assert False, "high-risk override should default off"
    except ValueError as exc:
        assert "disabled by policy" in str(exc)
