"""Findings that say ``severity: "blocking"`` without a ``blocking`` flag block.

Regression: release-auditor and spec-completion-auditor reports wrote
``"severity": "blocking"`` and omitted the boolean ``blocking`` key. Every
counter read ``bool(finding.get("blocking"))``, so those findings were silently
treated as non-blocking and the audit could pass over them.
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

from lib_core import finding_is_blocking  # noqa: E402


@pytest.mark.parametrize("finding, expected", [
    ({"blocking": True, "severity": "minor"}, True),
    ({"blocking": False, "severity": "critical"}, False),
    ({"severity": "blocking"}, True),
    ({"severity": "Blocking", "blocking": None}, True),
    ({"severity": "critical"}, True),
    ({"severity": "non-blocking"}, False),
    ({"severity": "major"}, False),
    ({"blocking": "yes", "severity": "minor"}, False),
    ({}, False),
    ("SC-1", False),
])
def test_finding_is_blocking(finding, expected):
    assert finding_is_blocking(finding) is expected


def _run_ctl(*args: str, home: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "ctl.py"), *args],
        capture_output=True, text=True, env={**os.environ, "DYNOS_HOME": str(home)}, check=False,
    )


def test_severity_only_blocking_finding_reaches_repair_log(tmp_path):
    td = tmp_path / "project" / ".dynos" / "task-20261028-BL"
    (td / "audit-reports").mkdir(parents=True)
    (td / "manifest.json").write_text(json.dumps({
        "task_id": td.name,
        "created_at": "2026-10-28T00:00:00Z",
        "raw_input": "x",
        "stage": "REPAIR_PLANNING",
        "classification": {"type": "feature", "domains": ["infra"], "risk_level": "medium"},
    }))
    (td / "audit-reports" / "release-auditor-sonnet-attempt-1.json").write_text(json.dumps({  # noqa: model-literal
        "auditor_name": "release-auditor",
        "status": "complete",
        "findings": [
            {"id": "REL-1", "severity": "blocking", "file": "infra/docker-compose.yml"},
            {"id": "REL-3", "severity": "non-blocking", "file": "infra/backend.Dockerfile"},
        ],
    }))
    home = tmp_path / "home"
    home.mkdir()

    gate = _run_ctl("run-audit-findings-gate", str(td), home=home)
    assert gate.returncode == 0, gate.stderr
    assert json.loads(gate.stdout)["blocking_finding_ids"] == ["REL-1"]

    assert _run_ctl("run-audit-repair-cycle-plan", str(td), home=home).returncode == 0
    build = _run_ctl("run-repair-log-build", str(td), home=home)
    assert build.returncode == 0, build.stdout + build.stderr
    tasks = [t for b in json.loads((td / "repair-log.json").read_text())["batches"] for t in b["tasks"]]
    assert [(t["finding_id"], t["assigned_executor"]) for t in tasks] in (
        [("REL-1", "infra-executor")], [("REL-1", "release-executor")],
    )


def test_repair_files_split_multi_path_field(tmp_path):
    import ctl
    task_dir = tmp_path / ".dynos" / "task-20261028-MP"
    task_dir.mkdir(parents=True)
    finding = {
        "id": "SC-002",
        "file": ".gitignore, infra/docker-compose.yml; frontend/src/api/client.ts:7",
        "line": ".gitignore:1-20; docker-compose.yml:16-18,89-91",
    }
    repo_files, pipeline_files = ctl._repair_files_for_finding(task_dir, finding, None, "infra-executor")
    assert repo_files == [".gitignore", "infra/docker-compose.yml", "frontend/src/api/client.ts"]
    assert pipeline_files == []
