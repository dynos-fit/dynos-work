"""Field report 7.6.0: receipt rewrites, attempt history, pinned rules.

* A receipt legitimately rewritten through ``write_receipt`` (a planner
  receipt after a plan-audit bounce, a re-audit receipt) appends a new chain
  entry; the chain validates against the newest entry per path, and the
  DONE gate refuses a chain that does not validate.
* A rewritten receipt or injected-prompt sidecar keeps the earlier attempt
  under ``_superseded/``.
* The shared prevention rules are pinned per task, so a rule another task
  adds mid-task does not fail the rules check or trip the DONE drift gate.
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
from lib_chain import extend_chain_for_artifact, validate_chain  # noqa: E402
from lib_core import _effective_rules_drift, _persistent_project_dir, _receipt_chain_err  # noqa: E402
from lib_receipts import (  # noqa: E402
    pin_rules_snapshot,
    pinned_rules,
    read_receipt,
    receipt_rules_check_passed,
    write_receipt,
)
from receipts.core import superseded_versions  # noqa: E402


@pytest.fixture
def task_dir(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("DYNOS_EVENT_SECRET", "fixed-project-secret")
    monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "dynos-home"))
    td = tmp_path / "project" / ".dynos" / "task-20261031-001"
    (td / "receipts").mkdir(parents=True)
    (td / "manifest.json").write_text(json.dumps({"task_id": td.name, "stage": "PLANNING"}))
    return td


# --- Bug 1: chain survives legitimate rewrites, gate refuses a broken chain ---

def test_rewritten_receipt_validates_against_newest_entry(task_dir: Path):
    write_receipt(task_dir, "planner-spec", phase="spec")
    for attempt in range(3):  # two plan-audit bounces back to PLANNING
        write_receipt(task_dir, "planner-plan", phase="plan", attempt=attempt)
    result = validate_chain(task_dir)
    assert result.status == "valid", result
    assert _receipt_chain_err(task_dir, "FINAL_AUDIT", "DONE") is None


def test_out_of_band_receipt_rewrite_still_detected(task_dir: Path):
    path = write_receipt(task_dir, "planner-plan", phase="plan")
    write_receipt(task_dir, "planner-plan", phase="plan", attempt=2)
    path.write_text(json.dumps({"step": "planner-plan", "forged": True}))
    result = validate_chain(task_dir)
    assert result.status == "content_mismatch"
    assert result.first_failed_file_path == "receipts/planner-plan.json"
    err = _receipt_chain_err(task_dir, "FINAL_AUDIT", "DONE")
    assert err is not None and "content_mismatch" in err


def test_manifest_rewrites_after_chaining_are_not_a_mismatch(task_dir: Path):
    manifest = task_dir / "manifest.json"
    extend_chain_for_artifact(task_dir, manifest)  # record-snapshot chains it
    manifest.write_text(json.dumps({"task_id": task_dir.name, "stage": "FINAL_AUDIT"}))
    assert validate_chain(task_dir).status == "valid"


def test_missing_chain_does_not_refuse_done(task_dir: Path):
    assert _receipt_chain_err(task_dir, "FINAL_AUDIT", "DONE") is None


def test_transition_to_done_refuses_broken_chain(task_dir: Path, monkeypatch):
    import lib_core

    monkeypatch.setattr(lib_core, "require_receipts_for_done", lambda _td: [])
    path = write_receipt(task_dir, "audit-routing", auditors=[])
    path.write_text("{}")  # rewritten outside write_receipt
    manifest = json.loads((task_dir / "manifest.json").read_text())
    manifest["stage"] = "FINAL_AUDIT"
    (task_dir / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="task-receipt chain content_mismatch"):
        lib_core.transition_task(task_dir, "DONE")
    bypassed = lib_core._compute_bypassed_gates_for_force(
        task_dir=task_dir, manifest=manifest, current_stage="FINAL_AUDIT", next_stage="DONE",
    )
    assert any("task-receipt chain content_mismatch" in g for g in bypassed)


# --- Bug 11: earlier attempts are kept, not overwritten ---

def test_rewritten_receipt_keeps_superseded_versions(task_dir: Path):
    path = write_receipt(task_dir, "audit-performance-auditor", finding_count=3)
    first = path.read_bytes()
    write_receipt(task_dir, "audit-performance-auditor", finding_count=0)
    versions = superseded_versions(path)
    assert [p.name for p in versions] == ["audit-performance-auditor.v1.json"]
    assert versions[0].read_bytes() == first
    assert read_receipt(task_dir, "audit-performance-auditor")["finding_count"] == 0
    # Readers that glob receipts/*.json do not see the archive.
    assert all(p.parent.name == "receipts" for p in (task_dir / "receipts").glob("*.json"))
    write_receipt(task_dir, "audit-performance-auditor", finding_count=1)
    assert len(superseded_versions(path)) == 2
    # A shard receipt with the same prefix keeps its own history.
    write_receipt(task_dir, "audit-performance-auditor-haiku", finding_count=0)
    assert len(superseded_versions(path)) == 2


def test_prompt_sidecar_rewrite_keeps_previous_pair(task_dir: Path):
    import router

    sidecar_dir = task_dir / "receipts" / "_injected-auditor-prompts"
    router._write_prompt_sidecar(sidecar_dir, "performance-auditor-haiku", b"attempt 1\n", task_dir=task_dir)
    router._write_prompt_sidecar(sidecar_dir, "performance-auditor-haiku", b"attempt 1\n", task_dir=task_dir)
    assert not (sidecar_dir / "_superseded").exists()  # identical re-write
    router._write_prompt_sidecar(sidecar_dir, "performance-auditor-haiku", b"attempt 2\n", task_dir=task_dir)
    txt = superseded_versions(sidecar_dir / "performance-auditor-haiku.txt")
    sha = superseded_versions(sidecar_dir / "performance-auditor-haiku.sha256")
    assert [p.read_bytes() for p in txt] == [b"attempt 1\n"]
    assert len(sha) == 1
    assert (sidecar_dir / "performance-auditor-haiku.txt").read_bytes() == b"attempt 2\n"


# --- Bug 8: prevention rules pinned per task ---

ERROR_RULE = {
    "rule_id": "no-legacy-client",
    "template": "pattern_must_not_appear",
    "params": {"regex": "LegacyClient", "scope": "**/*.py"},
    "severity": "error",
}


def _write_rules(td: Path, rules: list[dict]) -> None:
    path = _persistent_project_dir(td.parent.parent) / "prevention-rules.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"rules": rules}))


def test_rule_added_mid_task_neither_fails_check_nor_drifts(task_dir: Path, monkeypatch):
    project = task_dir.parent.parent
    (project / "old.py").write_text("client = LegacyClient()\n")  # untouched code
    monkeypatch.setattr(rules_engine, "_resolve_files", lambda root, mode, **_kw: [project / "old.py"])
    _write_rules(task_dir, [])
    assert pin_rules_snapshot(task_dir) == []

    receipt = json.loads(receipt_rules_check_passed(task_dir, "all").read_text())
    assert receipt["rules_pinned"] is True

    # Another worktree's postmortem adds a rule the untouched code violates.
    _write_rules(task_dir, [ERROR_RULE])
    assert _effective_rules_drift(task_dir, receipt) is None
    rerun = json.loads(receipt_rules_check_passed(task_dir, "all").read_text())
    assert rerun["error_violations"] == 0
    assert pinned_rules(task_dir) == []


def test_unpinned_legacy_task_pins_on_first_check(task_dir: Path, monkeypatch):
    project = task_dir.parent.parent
    (project / "old.py").write_text("client = LegacyClient()\n")
    monkeypatch.setattr(rules_engine, "_resolve_files", lambda root, mode, **_kw: [project / "old.py"])
    _write_rules(task_dir, [ERROR_RULE])
    assert pinned_rules(task_dir) is None
    with pytest.raises(ValueError, match="error_violations=1"):
        receipt_rules_check_passed(task_dir, "all")
    assert pinned_rules(task_dir) == [ERROR_RULE]


def test_run_start_init_pins_rules(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    home = tmp_path / "home"
    env = {**os.environ, "DYNOS_HOME": str(home), "DYNOS_EVENT_SECRET": "s"}
    # Seed the shared rules for this project before the task starts.
    probe = subprocess.run(
        [sys.executable, "-c",
         f"import sys; sys.path.insert(0, {str(HOOKS)!r}); from lib_core import _persistent_project_dir; "
         f"print(_persistent_project_dir(__import__('pathlib').Path({str(project)!r})))"],
        capture_output=True, text=True, env=env, check=True,
    )
    rules_path = Path(probe.stdout.strip()) / "prevention-rules.json"
    rules_path.parent.mkdir(parents=True, exist_ok=True)
    rules_path.write_text(json.dumps({"rules": [ERROR_RULE]}))

    out = subprocess.run(
        [sys.executable, str(HOOKS / "ctl.py"), "run-start-init", "--root", str(project),
         "--description", "do a thing"],
        capture_output=True, text=True, env=env,
    )
    assert out.returncode == 0, out.stderr
    td = Path(json.loads(out.stdout)["task_dir"])
    snapshot = json.loads((td / "receipts" / "rules-snapshot.json").read_text())
    assert snapshot["rules"] == [ERROR_RULE]
