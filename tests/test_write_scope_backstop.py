"""Git write-scope backstop (7.6.0 field report, bug 2 follow-up).

The PreToolUse hook only parses command text, so a write made by an
interpreter or subprocess can slip past it. lib_write_scope compares what
git says changed since the task snapshot with the declared write scope
(segment files_expected/files_optional, every repair cycle's
files_to_modify, the TDD receipt's test files, human acknowledgments):

* segment-done and the findings gate report ``out_of_scope_files``;
* the DONE gate (transition_task and the force dry-run mirror) refuses
  while unacknowledged out-of-scope files exist;
* ``ctl acknowledge-out-of-scope`` (ctl-owned write-scope.json) lets a human
  confirm a change belongs to the task.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

import lib_write_scope  # noqa: E402
from lib_core import _compute_bypassed_gates_for_force, transition_task  # noqa: E402
from lib_write_scope import (  # noqa: E402
    baseline_dirty_files,
    done_gate_error,
    write_scope_report,
)
from write_policy import WriteAttempt, decide_write  # noqa: E402

TASK = "task-20261031-WS"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    ).stdout.strip()


def _project(tmp_path: Path, *, baseline: bool = True, dirty_before: dict | None = None,
             stage: str = "CHECKPOINT_AUDIT") -> Path:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("a = 1\n")
    (root / "README.md").write_text("readme\n")
    (root / "notes.txt").write_text("notes\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    sha = _git(root, "rev-parse", "HEAD")
    for rel, text in (dirty_before or {}).items():
        (root / rel).write_text(text)
    td = root / ".dynos" / TASK
    td.mkdir(parents=True)
    snapshot = {"head_sha": sha, "branch": "main", "recorded_at": "2026-10-31T00:00:00Z"}
    if baseline:
        snapshot["baseline_dirty"] = baseline_dirty_files(root)
    (td / "manifest.json").write_text(json.dumps({
        "task_id": TASK, "stage": stage, "snapshot": snapshot,
        "classification": {"risk_level": "medium"},
    }))
    (td / "execution-graph.json").write_text(json.dumps({"segments": [
        {"id": "seg-1", "files_expected": ["src/a.py"], "files_optional": ["docs/"]},
    ]}))
    return td


def _ctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "ctl.py"), *args],
        capture_output=True, text=True, check=False,
    )


def test_in_scope_changes_and_dynos_state_are_not_flagged(tmp_path):
    td = _project(tmp_path)
    root = td.parent.parent
    (root / "src" / "a.py").write_text("a = 2\n")
    (root / "docs").mkdir()
    (root / "docs" / "new.md").write_text("doc\n")  # files_optional dir entry
    (td / "events.jsonl").write_text("{}\n")
    report = write_scope_report(td)
    assert report["status"] == "checked" and report["enforced"] is True
    assert report["out_of_scope_files"] == []
    assert done_gate_error(td, "CHECKPOINT_AUDIT", "DONE") is None


def test_subprocess_write_outside_scope_is_flagged_and_refuses_done(tmp_path):
    td = _project(tmp_path)
    root = td.parent.parent
    # The bug-2 shape: an interpreter writes a file the hook never saw.
    subprocess.run(
        [sys.executable, "-c", "open('README.md','w').write('changed\\n')"],
        cwd=root, check=True,
    )
    (root / "follow-up.md").write_text("untracked new file\n")
    report = write_scope_report(td)
    assert report["out_of_scope_files"] == ["README.md", "follow-up.md"]
    err = done_gate_error(td, "CHECKPOINT_AUDIT", "DONE")
    assert err and "README.md" in err and "follow-up.md" in err
    assert "acknowledge-out-of-scope" in err


def test_deleting_an_out_of_scope_file_is_flagged(tmp_path):
    td = _project(tmp_path)
    (td.parent.parent / "notes.txt").unlink()
    assert write_scope_report(td)["out_of_scope_files"] == ["notes.txt"]


def test_baseline_dirty_file_only_flagged_when_it_changes_again(tmp_path):
    td = _project(tmp_path, dirty_before={"notes.txt": "user edit before task\n"})
    root = td.parent.parent
    assert write_scope_report(td)["out_of_scope_files"] == []
    (root / "notes.txt").write_text("task edited it too\n")
    assert write_scope_report(td)["out_of_scope_files"] == ["notes.txt"]


def test_repair_history_and_live_repair_log_extend_scope(tmp_path):
    td = _project(tmp_path)
    root = td.parent.parent
    (root / "README.md").write_text("fixed in repair cycle 1\n")
    (root / "notes.txt").write_text("fixed in repair cycle 2\n")
    (td / "repair-log-history.json").write_text(json.dumps({"cycles": [
        {"repair_cycle": 1, "repair_log": {"repair_cycle": 1, "batches": [
            {"tasks": [{"finding_id": "F1", "files_to_modify": ["README.md"]}]}]}},
    ]}))
    (td / "repair-log.json").write_text(json.dumps({"repair_cycle": 2, "batches": [
        {"tasks": [{"finding_id": "F2", "files_to_modify": ["notes.txt"]}]}]}))
    assert write_scope_report(td)["out_of_scope_files"] == []


def test_tdd_receipt_test_files_are_in_scope(tmp_path):
    td = _project(tmp_path)
    root = td.parent.parent
    (root / "tests").mkdir()
    (root / "tests" / "test_a.py").write_text("def test(): pass\n")
    (td / "receipts").mkdir()
    (td / "receipts" / "tdd-tests.json").write_text(json.dumps(
        {"test_file_paths": ["tests/test_a.py"]}))
    assert write_scope_report(td)["out_of_scope_files"] == []


def test_legacy_tasks_are_never_refused(tmp_path):
    td = _project(tmp_path, baseline=False)
    (td.parent.parent / "README.md").write_text("changed\n")
    report = write_scope_report(td)
    assert report["out_of_scope_files"] == ["README.md"]  # reported ...
    assert report["enforced"] is False
    assert done_gate_error(td, "CHECKPOINT_AUDIT", "DONE") is None  # ... not refused
    manifest = json.loads((td / "manifest.json").read_text())
    del manifest["snapshot"]
    (td / "manifest.json").write_text(json.dumps(manifest))
    assert write_scope_report(td)["status"] == "skipped"
    assert done_gate_error(td, "CHECKPOINT_AUDIT", "DONE") is None


def test_acknowledge_out_of_scope_command(tmp_path):
    td = _project(tmp_path)
    root = td.parent.parent
    (root / "README.md").write_text("changed\n")
    (root / "notes.txt").write_text("changed\n")
    no_reason = _ctl("acknowledge-out-of-scope", str(td), "--file", "README.md", "--reason", " ")
    assert no_reason.returncode == 1 and "--reason" in no_reason.stderr
    unknown = _ctl("acknowledge-out-of-scope", str(td), "--file", "src/a.py", "--reason", "x")
    assert unknown.returncode == 1 and "not currently out of scope" in unknown.stderr
    ok = _ctl("acknowledge-out-of-scope", str(td), "--file", "README.md",
              "--file", str(root / "notes.txt"), "--reason", "human: release notes bump")
    assert ok.returncode == 0, ok.stderr
    out = json.loads(ok.stdout)
    assert out["acknowledged"] == ["README.md", "notes.txt"]
    assert out["out_of_scope_files"] == []
    stored = json.loads((td / "write-scope.json").read_text())["acknowledged"]
    assert {e["file"] for e in stored} == {"README.md", "notes.txt"}
    assert all(e["reason"] == "human: release notes bump" for e in stored)
    report = write_scope_report(td)
    assert report["out_of_scope_files"] == []
    assert report["acknowledged_files"] == ["README.md", "notes.txt"]
    assert done_gate_error(td, "CHECKPOINT_AUDIT", "DONE") is None
    assert "[SCOPE-ACK]" in (td / "execution-log.md").read_text()


@pytest.mark.parametrize("role", ["orchestrator", "backend-executor", "execute-inline",
                                  "audit-security", "planning", "repair-coordinator"])
def test_agents_cannot_write_write_scope_record(tmp_path, role):
    td = _project(tmp_path)
    decision = decide_write(WriteAttempt(
        role=role, task_dir=td, path=td / "write-scope.json",
        operation="create", source="agent",
    ))
    assert decision.allowed is False
    assert "acknowledge-out-of-scope" in (decision.wrapper_command or "")


def test_findings_gate_and_segment_report_out_of_scope_files(tmp_path):
    td = _project(tmp_path)
    (td.parent.parent / "README.md").write_text("changed\n")
    res = _ctl("run-audit-findings-gate", str(td))
    assert res.returncode == 0, res.stderr
    payload = json.loads(res.stdout)
    assert payload["out_of_scope_files"] == ["README.md"]
    assert payload["write_scope_status"] == "checked"
    assert "acknowledge-out-of-scope" in payload["out_of_scope_hint"]
    import ctl  # noqa: PLC0415
    assert ctl._write_scope_output(td)["out_of_scope_files"] == ["README.md"]


def test_force_dry_run_lists_write_scope_gate(tmp_path):
    td = _project(tmp_path)
    (td.parent.parent / "README.md").write_text("changed\n")
    manifest = json.loads((td / "manifest.json").read_text())
    errs = _compute_bypassed_gates_for_force(
        task_dir=td, manifest=manifest, current_stage="CHECKPOINT_AUDIT", next_stage="DONE",
    )
    assert any("outside the declared write scope" in e and "README.md" in e for e in errs)


def test_transition_task_refuses_done_until_acknowledged(tmp_path, monkeypatch):
    import router  # noqa: PLC0415
    import rules_engine  # noqa: PLC0415
    from lib_receipts import (  # noqa: PLC0415
        receipt_audit_routing, receipt_postmortem_skipped,
        receipt_retrospective, receipt_rules_check_passed,
    )

    monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "dynos-home"))
    monkeypatch.setattr(router, "_load_auditor_registry", lambda root: {
        "always": [], "fast_track": [], "domain_conditional": {},
    })
    td = _project(tmp_path)
    (td / "task-retrospective.json").write_text(json.dumps({"quality_score": 0.9}))
    (td / "audit-reports").mkdir()
    (td / "audit-reports" / "report.json").write_text(json.dumps({"findings": []}))
    receipt_retrospective(td)
    monkeypatch.setattr(rules_engine, "run_checks", lambda root, mode, **kw: [])
    receipt_rules_check_passed(td, "all")
    receipt_audit_routing(td, [])
    receipt_postmortem_skipped(td, "no-findings", "d" * 64, subsumed_by=[])

    (td.parent.parent / "README.md").write_text("written by a subprocess\n")
    with pytest.raises(ValueError) as exc:
        transition_task(td, "DONE")
    assert "outside the declared write scope" in str(exc.value)
    assert "README.md" in str(exc.value)

    ack = _ctl("acknowledge-out-of-scope", str(td), "--file", "README.md", "--reason", "human ok")
    assert ack.returncode == 0, ack.stderr
    transition_task(td, "DONE")
    assert json.loads((td / "manifest.json").read_text())["stage"] == "DONE"


def test_record_snapshot_stores_baseline(tmp_path):
    td = _project(tmp_path, stage="PRE_EXECUTION_SNAPSHOT", dirty_before={"notes.txt": "dirty\n"})
    manifest = json.loads((td / "manifest.json").read_text())
    del manifest["snapshot"]
    (td / "manifest.json").write_text(json.dumps(manifest))
    res = _ctl("record-snapshot", str(td))
    assert res.returncode == 0, res.stderr
    snap = json.loads((td / "manifest.json").read_text())["snapshot"]
    assert "notes.txt" in snap["baseline_dirty"]
    assert all(not k.startswith(".dynos") for k in snap["baseline_dirty"])


def test_project_nested_in_a_larger_repository(tmp_path):
    repo = tmp_path / "repo"
    root = repo / "pkg"
    root.mkdir(parents=True)
    _git(repo, "init", "-q")
    (repo / "outside.txt").write_text("x\n")
    (root / "a.py").write_text("a\n")
    (root / "b.md").write_text("b\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    sha = _git(repo, "rev-parse", "HEAD")
    (root / "b.md").write_text("dirty before the task\n")
    td = root / ".dynos" / TASK
    td.mkdir(parents=True)
    (td / "manifest.json").write_text(json.dumps({"task_id": TASK, "stage": "CHECKPOINT_AUDIT",
        "snapshot": {"head_sha": sha, "baseline_dirty": baseline_dirty_files(root)}}))
    (td / "execution-graph.json").write_text(json.dumps({"segments": [
        {"id": "s", "files_expected": ["a.py"]}]}))
    (root / "a.py").write_text("changed\n")
    (root / "new.md").write_text("stray\n")
    assert write_scope_report(td)["out_of_scope_files"] == ["new.md"]
