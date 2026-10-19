"""Planning role reads are bounded by active phase and referenced paths."""
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"hooks"))
from read_policy import ReadAttempt, decide_read


def _read(role, task, target, tool="Read", pattern=None):
    return ReadAttempt(role=role,task_dir=task,target=target,tool_name=tool,raw_pattern=pattern)


def test_planner_task_dir_and_allowlist_reads_do_not_consume_quota(tmp_path: Path):
    task=tmp_path/".dynos"/"task-20261018-003"; task.mkdir(parents=True)
    root=task.parent.parent
    (task/"active-planner-phase").write_text("plan")
    (task/"raw-input.md").write_text("Please update src/named.py")
    decision=decide_read(_read("planning",task,task/"raw-input.md"),audit_plan=None)
    assert decision.allowed and decision.quota_used is None
    named=root/"src"/"named.py"; named.parent.mkdir(); named.write_text("x")
    decision=decide_read(_read("planning",task,named),audit_plan=None)
    assert decision.allowed and decision.quota_used is None
    assert not (task/"planner-read-quota.json").exists()


def test_planner_quota_and_repo_wide_grep_denial(tmp_path: Path):
    from lib_tool_budget import PLANNER_PHASE_READ_ALLOWANCE
    task=tmp_path/".dynos"/"task-20261018-004"; task.mkdir(parents=True)
    root=task.parent.parent; (task/"active-planner-phase").write_text("plan")
    (task/"raw-input.md").write_text("change behavior")
    grep=decide_read(_read("planning",task,root,"Grep"),audit_plan=None)
    assert not grep.allowed and "explicit path" in grep.reason
    target=root/"other.py"; target.write_text("x")
    for i in range(PLANNER_PHASE_READ_ALLOWANCE["plan"]):
        assert decide_read(_read("planning",task,target),audit_plan=None).allowed
    denied=decide_read(_read("planning",task,target),audit_plan=None)
    assert not denied.allowed and "12/12" in denied.reason and "Reference Code" in denied.reason
