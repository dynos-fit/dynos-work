"""Task rules checks are scoped to the task's own diff (field report 7.6.0, bug 8).

``run-rules-check --mode task`` (the default) checks the files changed since
the task's ``record-snapshot`` commit plus untracked files, so a shared rule
does not fail a task on code it never touched. Repo-wide templates keep a
repo-wide view but fail only on violations the change set caused.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOOKS = ROOT / "hooks"
if str(HOOKS) not in sys.path:
    sys.path.insert(0, str(HOOKS))

import rules_engine  # noqa: E402
from lib_core import _effective_rules_drift, _persistent_project_dir, _rules_check_err  # noqa: E402
from lib_receipts import receipt_rules_check_passed  # noqa: E402

PATTERN_RULE = {
    "rule_id": "no-legacy-client",
    "template": "pattern_must_not_appear",
    "params": {"regex": "LegacyClient", "scope": "**/*.py"},
    "severity": "error",
}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    ).stdout.strip()


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "dynos-home"))
    monkeypatch.setenv("DYNOS_EVENT_SECRET", "s")
    proj = tmp_path / "proj"
    proj.mkdir()
    _git(proj, "init", "-q")
    (proj / ".gitignore").write_text(".dynos/\n")
    (proj / "old.py").write_text("client = LegacyClient()\n")  # pre-existing violation
    (proj / "app.py").write_text("x = 1\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-q", "-m", "base")
    return proj


def _task(proj: Path, *, snapshot: bool = True) -> Path:
    td = proj / ".dynos" / "task-20261031-002"
    (td / "receipts").mkdir(parents=True)
    manifest = {"task_id": td.name, "stage": "EXECUTION"}
    if snapshot:
        manifest["snapshot"] = {"head_sha": _git(proj, "rev-parse", "HEAD")}
    (td / "manifest.json").write_text(json.dumps(manifest))
    return td


def _rules(proj: Path, rules: list[dict]) -> None:
    path = _persistent_project_dir(proj) / "prevention-rules.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"rules": rules}))


# --- per-file templates ---

def test_untouched_pre_existing_violation_no_longer_fails(project: Path):
    _rules(project, [PATTERN_RULE])
    td = _task(project)
    (project / "app.py").write_text("x = 2\n")  # the task's change
    receipt = json.loads(receipt_rules_check_passed(td, "task").read_text())
    assert receipt["mode"] == "diff"
    assert receipt["requested_mode"] == "task"
    assert receipt["diff_base"] == _git(project, "rev-parse", "HEAD")
    assert receipt["error_violations"] == 0
    # The DONE drift gate and the receipt still agree.
    assert _effective_rules_drift(td, receipt) is None
    assert _rules_check_err(td, "CHECKPOINT_AUDIT", "DONE") is None


def test_violation_in_changed_file_still_fails(project: Path):
    _rules(project, [PATTERN_RULE])
    td = _task(project)
    (project / "app.py").write_text("c = LegacyClient()\n")
    with pytest.raises(ValueError, match="error_violations=1"):
        receipt_rules_check_passed(td, "task")


def test_violation_in_new_untracked_file_fails(project: Path):
    _rules(project, [PATTERN_RULE])
    td = _task(project)
    (project / "new_mod.py").write_text("c = LegacyClient()\n")
    with pytest.raises(ValueError, match="error_violations=1"):
        receipt_rules_check_passed(td, "diff")


def test_explicit_all_mode_still_scans_whole_repo(project: Path):
    _rules(project, [PATTERN_RULE])
    td = _task(project)
    with pytest.raises(ValueError, match="mode='all'"):
        receipt_rules_check_passed(td, "all")


def test_no_snapshot_falls_back_to_all(project: Path):
    _rules(project, [PATTERN_RULE])
    td = _task(project, snapshot=False)
    with pytest.raises(ValueError, match="mode='all'"):
        receipt_rules_check_passed(td, "task")
    _rules(project, [])
    td2 = project / ".dynos" / "task-20261031-003"
    (td2 / "receipts").mkdir(parents=True)
    (td2 / "manifest.json").write_text(json.dumps({"task_id": td2.name, "stage": "EXECUTION"}))
    receipt = json.loads(receipt_rules_check_passed(td2, "diff").read_text())
    assert receipt["mode"] == "all" and receipt["requested_mode"] == "diff"
    assert "snapshot" in receipt["diff_fallback_reason"]


def test_ctl_default_mode_is_task_diff(project: Path, tmp_path: Path):
    _rules(project, [PATTERN_RULE])
    td = _task(project)
    out = subprocess.run(
        [sys.executable, str(HOOKS / "ctl.py"), "run-rules-check", str(td)],
        capture_output=True, text=True,
        env={**os.environ, "DYNOS_HOME": str(tmp_path / "dynos-home"), "DYNOS_EVENT_SECRET": "s"},
    )
    assert out.returncode == 0, out.stderr
    assert json.loads((td / "receipts" / "rules-check-passed.json").read_text())["mode"] == "diff"


# --- repo-wide templates ---

CALLER_RULE = {
    "rule_id": "need-three-callers",
    "template": "caller_count_required",
    "params": {"symbol": "audit", "scope": "callers/*.py", "min_count": 3},
    "severity": "error",
}


def _caller_project(project: Path) -> str:
    (project / "callers").mkdir()
    (project / "callers" / "a.py").write_text("audit()\naudit()\n")  # 2 < 3: pre-existing deficit
    (project / "callers" / "b.py").write_text("y = 0\n")
    _git(project, "add", "-A")
    _git(project, "commit", "-q", "-m", "callers")
    return _git(project, "rev-parse", "HEAD")


def _diff_check(project: Path, base: str, rule: dict) -> list:
    return rules_engine.run_checks(project, "diff", raw_rules=[rule], base=base)


def test_caller_count_pre_existing_deficit_is_not_attributed(project: Path):
    base = _caller_project(project)
    assert _diff_check(project, base, CALLER_RULE) == []  # glob untouched
    # Whole-repo mode still reports the deficit.
    assert rules_engine.run_checks(project, "all", raw_rules=[CALLER_RULE])
    # Touching the glob without lowering the count is not attributed either.
    (project / "callers" / "b.py").write_text("y = 1\n")
    assert _diff_check(project, base, CALLER_RULE) == []


def test_caller_count_fails_when_change_removes_callers(project: Path):
    base = _caller_project(project)
    (project / "callers" / "a.py").write_text("audit()\n")
    violations = _diff_check(project, base, CALLER_RULE)
    assert [v.rule_id for v in violations] == ["need-three-callers"]


def test_caller_count_fails_when_change_deletes_a_caller_file(project: Path):
    base = _caller_project(project)
    (project / "callers" / "a.py").unlink()
    assert [v.rule_id for v in _diff_check(project, base, CALLER_RULE)] == ["need-three-callers"]


def test_co_modification_uses_the_diff_as_change_set(project: Path):
    base = _git(project, "rev-parse", "HEAD")
    rule = {
        "rule_id": "app-needs-docs", "template": "co_modification_required",
        "params": {"trigger_glob": "app.py", "must_modify_glob": "docs/*.md"},
        "severity": "error",
    }
    (project / "app.py").write_text("x = 3\n")
    assert [v.rule_id for v in _diff_check(project, base, rule)] == ["app-needs-docs"]
    (project / "docs").mkdir()
    (project / "docs" / "app.md").write_text("changed\n")
    assert _diff_check(project, base, rule) == []


def test_signature_lock_reports_only_when_its_source_changed(project: Path, monkeypatch):
    (project / "locked_mod_t8.py").write_text("def f(a, b):\n    return a\n")
    _git(project, "add", "-A")
    _git(project, "commit", "-q", "-m", "mod")
    base = _git(project, "rev-parse", "HEAD")
    monkeypatch.syspath_prepend(str(project))
    rule = {
        "rule_id": "lock-f", "template": "signature_lock",
        "params": {"module": "locked_mod_t8", "function": "f", "expected_params": ["a"]},
        "severity": "error",
    }
    # Pre-existing mismatch in an untouched module: not this task's.
    assert _diff_check(project, base, rule) == []
    assert rules_engine.run_checks(project, "all", raw_rules=[rule])
    # The task edits the module: the mismatch is attributed to it.
    (project / "locked_mod_t8.py").write_text("def f(a, b):\n    return b\n")
    assert [v.rule_id for v in _diff_check(project, base, rule)] == ["lock-f"]


def test_every_name_reports_only_when_its_module_changed(project: Path, monkeypatch):
    (project / "names_mod_t8.py").write_text("NAMES = ['x']\nx = 1\n")
    _git(project, "add", "-A")
    _git(project, "commit", "-q", "-m", "names")
    base = _git(project, "rev-parse", "HEAD")
    monkeypatch.syspath_prepend(str(project))
    rule = {
        "rule_id": "names-callable", "template": "every_name_in_X_satisfies_Y",
        "params": {"module": "names_mod_t8", "container": "NAMES", "predicate": "callable"},
        "severity": "error",
    }
    assert _diff_check(project, base, rule) == []
    (project / "names_mod_t8.py").write_text("NAMES = ['x']\nx = 2\n")
    assert [v.rule_id for v in _diff_check(project, base, rule)] == ["names-callable"]


def test_diff_mode_without_base_is_an_error(project: Path):
    with pytest.raises(ValueError, match="base"):
        rules_engine.run_checks(project, "diff", raw_rules=[PATTERN_RULE])
