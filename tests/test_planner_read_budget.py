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


# ---------------------------------------------------------------------------
# Hook-level enforcement: the quota must be reachable from pre_tool_use, not
# only from decide_read. (Regression: the hook's non-audit early return used
# to skip the planning branch entirely.)
# ---------------------------------------------------------------------------
import json
import os
import subprocess

HOOK_PATH = ROOT / "hooks" / "pre-tool-use"


def _hook(payload: dict, task: Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DYNOS_ROLE": "planning", "DYNOS_TASK_DIR": str(task)}
    return subprocess.run(
        ["bash", str(HOOK_PATH)], cwd=str(cwd), input=json.dumps(payload),
        text=True, capture_output=True, check=False, env=env,
    )


def test_hook_enforces_planner_quota_and_repo_wide_grep(tmp_path: Path):
    from lib_tool_budget import PLANNER_PHASE_READ_ALLOWANCE
    task = tmp_path / ".dynos" / "task-20261019-005"; task.mkdir(parents=True)
    root = task.parent.parent
    (task / "manifest.json").write_text(json.dumps({"task_id": task.name, "stage": "PLANNING"}))
    (task / "active-planner-phase").write_text("repair")
    (task / "raw-input.md").write_text("tighten validation")
    target = root / "module.py"; target.write_text("x = 1\n")

    inside = _hook({"tool_name": "Read", "tool_input": {"file_path": str(task / "raw-input.md")}, "cwd": str(root)}, task, root)
    assert inside.returncode == 0, inside.stderr

    grep_root = _hook({"tool_name": "Grep", "tool_input": {"pattern": "x", "path": str(root)}, "cwd": str(root)}, task, root)
    assert grep_root.returncode == 2 and "explicit path" in grep_root.stderr

    quota = PLANNER_PHASE_READ_ALLOWANCE["repair"]
    for _ in range(quota):
        ok = _hook({"tool_name": "Read", "tool_input": {"file_path": str(target)}, "cwd": str(root)}, task, root)
        assert ok.returncode == 0, ok.stderr
    denied = _hook({"tool_name": "Read", "tool_input": {"file_path": str(target)}, "cwd": str(root)}, task, root)
    assert denied.returncode == 2, denied.stderr
    assert f"{quota}/{quota}" in denied.stderr and "Reference Code" in denied.stderr


def test_hook_leaves_executor_reads_unrestricted(tmp_path: Path):
    task = tmp_path / ".dynos" / "task-20261019-006"; task.mkdir(parents=True)
    root = task.parent.parent
    (task / "manifest.json").write_text(json.dumps({"task_id": task.name, "stage": "EXECUTION"}))
    target = root / "module.py"; target.write_text("x = 1\n")
    env = {**os.environ, "DYNOS_ROLE": "backend-executor", "DYNOS_TASK_DIR": str(task)}
    result = subprocess.run(
        ["bash", str(HOOK_PATH)], cwd=str(root), text=True, capture_output=True, check=False, env=env,
        input=json.dumps({"tool_name": "Grep", "tool_input": {"pattern": "x", "path": str(root)}, "cwd": str(root)}),
    )
    assert result.returncode == 0, result.stderr
