"""Per-task prevention-rule waivers (field report 7.5.32, item 5).

The rules file is shared across tasks and branches. A rule another task
added for a symbol this branch lacks used to block the task, and removing
then restoring the rule tripped the drift gate. ``ctl waive-rule`` exempts
the task instead, and the drift gate compares the rules minus the waivers.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "hooks"))

import rules_engine  # noqa: E402
from lib_core import _effective_rules_drift, _persistent_project_dir  # noqa: E402
from lib_receipts import receipt_rules_check_passed  # noqa: E402

RULE = {
    "rule_id": "no-legacy-client",
    "template": "pattern_must_not_appear",
    "params": {"regex": "LegacyClient", "scope": "**/*.py"},
    "severity": "error",
}
OTHER = {"rule_id": "advice", "template": "advisory", "params": {}, "severity": "warn"}


@pytest.fixture
def task(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "dynos-home"))
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20261028-001"
    td.mkdir(parents=True)
    (project / "app.py").write_text("client = LegacyClient()\n")
    monkeypatch.setattr(rules_engine, "_resolve_files", lambda root, mode, **_kw: [project / "app.py"])
    return td


def _write_rules(td: Path, rules: list[dict]) -> None:
    path = _persistent_project_dir(td.parent.parent) / "prevention-rules.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"rules": rules}, indent=2))


def _ctl(*args: str, env_home: Path) -> subprocess.CompletedProcess[str]:
    import os
    return subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "ctl.py"), *args],
        capture_output=True, text=True, env={**os.environ, "DYNOS_HOME": str(env_home)},
    )


def test_waiver_exempts_the_task_and_is_recorded(task: Path, tmp_path: Path):
    _write_rules(task, [RULE, OTHER])
    with pytest.raises(ValueError, match="error_violations=1"):
        receipt_rules_check_passed(task, "all")

    home = tmp_path / "dynos-home"
    unknown = _ctl("waive-rule", str(task), "no-such-rule", "--reason", "x", env_home=home)
    assert unknown.returncode == 1 and "no prevention rule" in unknown.stderr
    no_reason = _ctl("waive-rule", str(task), "no-legacy-client", "--reason", " ", env_home=home)
    assert no_reason.returncode == 1
    ok = _ctl("waive-rule", str(task), "no-legacy-client", "--reason", "symbol absent on this branch",
              env_home=home)
    assert ok.returncode == 0, ok.stdout + ok.stderr

    receipt = json.loads(receipt_rules_check_passed(task, "all").read_text())
    assert receipt["error_violations"] == 0
    assert receipt["waived_rules"] == ["no-legacy-client"]
    assert receipt["rules_loaded"] == 1
    assert _effective_rules_drift(task, receipt) is None


def test_drift_ignores_waived_rule_edits_and_reordering(task: Path, tmp_path: Path):
    _write_rules(task, [RULE, OTHER])
    _ctl("waive-rule", str(task), "no-legacy-client", "--reason", "n/a", env_home=tmp_path / "dynos-home")
    receipt = json.loads(receipt_rules_check_passed(task, "all").read_text())

    # Removing and restoring the waived rule, or reordering, is not drift.
    _write_rules(task, [OTHER])
    assert _effective_rules_drift(task, receipt) is None
    _write_rules(task, [OTHER, {**RULE, "rationale": "edited"}])
    assert _effective_rules_drift(task, receipt) is None

    # A rule another task adds to the shared file mid-task is not drift:
    # the task is held to the rules pinned at its first check.
    _write_rules(task, [OTHER, RULE, {"rule_id": "new", "template": "advisory", "params": {}}])
    assert _effective_rules_drift(task, receipt) is None

    # A legacy task with no pinned snapshot still compares the live file,
    # where a new rule the task has not been checked against is drift.
    (task / "receipts" / "rules-snapshot.json").unlink()
    drift = _effective_rules_drift(task, receipt)
    assert drift is not None and "waive-rule" in drift


def test_agents_cannot_write_the_waiver_file(task: Path):
    from write_policy import WriteAttempt, decide_write
    attempt = WriteAttempt(role="backend-executor", task_dir=task, path=task / "rules-waivers.json",
                           operation="create", source="agent")
    decision = decide_write(attempt)
    assert decision.allowed is False
    assert "waive-rule" in (decision.wrapper_command or decision.reason)
