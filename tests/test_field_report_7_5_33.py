"""Field report 7.5.33: the user summary must agree with the findings gate.

``completion.json::user_summary`` read "FAILED — 20 blocking findings" on a
task whose ``run-audit-findings-gate`` returned ``clear``. ``run-audit-summary``
and ``derive_user_summary`` summed ``blocking_count`` over every report file,
including attempts a re-audit replaced and voting-tier reports a binding
deep-tier verdict overruled. Both now use the gate's report selection
(``collect_repair_findings``).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

import lib_residuals  # noqa: E402
from lib_receipts import receipt_audit_routing, write_receipt  # noqa: E402
from lib_validate import derive_user_summary  # noqa: E402

FAST = "haiku"  # noqa: model-literal
DEEP = "fable"  # noqa: model-literal


def _ctl(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(ROOT / "hooks" / "ctl.py"), *args],
        cwd=ROOT, text=True, capture_output=True, env={**os.environ, **env}, check=False,
    )


def _entry(name: str, *, ensemble: bool) -> dict:
    entry = {
        "name": name, "action": "spawn", "model": DEEP, "ensemble": ensemble,
        "route_mode": "generic", "agent_path": None, "injected_agent_sha256": None,
    }
    if ensemble:
        entry["ensemble_voting_models"] = [FAST]
        entry["ensemble_escalation_model"] = DEEP
    return entry


def _report(td: Path, auditor: str, model: str, attempt: int, findings: list[dict]) -> None:
    (td / "audit-reports" / f"{auditor}-{model}-attempt-{attempt}.json").write_text(json.dumps({
        "auditor_name": auditor, "status": "complete", "findings": findings,
    }))


def _shard(td: Path, auditor: str, model: str, blocking: int, ts: str) -> None:
    path = write_receipt(
        td, f"audit-{auditor}-{model}",
        auditor_name=auditor, model_used=model, finding_count=blocking, blocking_count=blocking,
        report_path=None, report_sha256=None, tokens_used=1, route_mode="generic",
        agent_path=None, injected_agent_sha256=None,
    )
    data = json.loads(path.read_text())
    data["ts"] = ts
    path.write_text(json.dumps(data))


def _task(tmp_path: Path) -> Path:
    """One escalated cascade the deep tier cleared, one attempt a re-audit replaced."""
    td = tmp_path / "project" / ".dynos" / "task-20261029-001"
    (td / "receipts").mkdir(parents=True)
    (td / "audit-reports").mkdir()
    (td / "manifest.json").write_text(json.dumps({
        "task_id": td.name, "created_at": "2026-10-29T00:00:00Z", "raw_input": "x",
        "stage": "CHECKPOINT_AUDIT",
        "classification": {"type": "feature", "risk_level": "high", "domains": ["backend", "ui"]},
    }))
    entries = [_entry("security-auditor", ensemble=True), _entry("ui-auditor", ensemble=False)]
    (td / "audit-plan.json").write_text(json.dumps({"auditors": entries}))
    receipt_audit_routing(td, entries)

    sec = "security-auditor"
    _report(td, sec, FAST, 1, [
        {"id": "SEC-1", "severity": "high", "blocking": True, "location": "src/auth.py:10", "category": "authz"},
        {"id": "SEC-2", "severity": "high", "blocking": True, "location": "src/auth.py:20", "category": "authz"},
    ])
    _report(td, sec, DEEP, 1, [])
    _shard(td, sec, FAST, blocking=2, ts="2026-10-29T10:00:00Z")
    _shard(td, sec, DEEP, blocking=0, ts="2026-10-29T10:05:00Z")

    _report(td, "ui-auditor", DEEP, 1, [
        {"id": "UI-1", "severity": "major", "blocking": True, "location": "web/app.tsx:7", "category": "a11y"},
    ])
    _report(td, "ui-auditor", DEEP, 2, [])
    return td


def test_audit_summary_agrees_with_findings_gate(tmp_path):
    td = _task(tmp_path)
    env = {"DYNOS_HOME": str(tmp_path / "dynos-home")}

    gate = _ctl("run-audit-findings-gate", str(td), env=env)
    assert gate.returncode == 0, gate.stdout + gate.stderr
    assert json.loads(gate.stdout)["status"] == "clear"

    res = _ctl("run-audit-summary", str(td), env=env)
    assert res.returncode == 0, res.stdout + res.stderr
    summary = json.loads((td / "audit-summary.json").read_text())
    assert summary["total_blocking"] == 0
    assert summary["audit_result"] == "pass"
    assert summary["user_summary"].startswith("Audit complete — ALL PASSED\n")
    assert "  security-auditor: PASS" in summary["user_summary"]
    assert "  ui-auditor: PASS" in summary["user_summary"]
    assert "FAIL" not in summary["user_summary"]

    # The superseded reports stay listed, marked, with their raw counts.
    superseded = {Path(r["report_path"]).name: r for r in summary["reports"] if r["superseded_by"]}
    assert set(superseded) == {f"security-auditor-{FAST}-attempt-1.json", f"ui-auditor-{DEEP}-attempt-1.json"}
    assert superseded[f"security-auditor-{FAST}-attempt-1.json"]["blocking_count"] == 2

    # verify-audit-summary-text re-derives from audit-summary.json byte-exactly.
    assert derive_user_summary(summary) == summary["user_summary"]


def test_derive_user_summary_skips_superseded_reports():
    summary = {
        "task_id": "task-X",
        "reports": [
            {"auditor_name": "security-auditor", "blocking_count": 3, "superseded_by": "attempt-2"},
            {"auditor_name": "security-auditor", "blocking_count": 0, "superseded_by": None},
            {"auditor_name": "ui-auditor", "blocking_count": 1, "superseded_by": None},
        ],
    }
    result = derive_user_summary(summary)
    assert result.startswith("Audit complete — FAILED — 1 blocking findings\n")
    assert "  security-auditor: PASS" in result
    assert "  ui-auditor: FAIL (1 blocking)" in result


def test_derive_user_summary_prefers_gate_counts():
    """Two binding findings at one location merge into one in the gate."""
    summary = {
        "task_id": "task-X",
        "reports": [{"auditor_name": "ui-auditor", "blocking_count": 2, "superseded_by": None}],
        "blocking_by_auditor": {"ui-auditor": 1},
    }
    assert derive_user_summary(summary).startswith("Audit complete — FAILED — 1 blocking findings\n")


def test_residual_ingest_skips_superseded_reports(tmp_path):
    root = tmp_path / "project"
    report = tmp_path / "old.json"
    report.write_text(json.dumps({"findings": [
        {"id": "CQ-1", "blocking": False, "severity": "major", "description": "d",
         "location": "a.py:1", "category": "maintainability", "remediation": "Extract the helper."},
    ]}))
    entry = {"auditor_name": "code-quality-auditor", "report_path": str(report), "superseded_by": "attempt-2"}
    assert lib_residuals.ingest_findings(tmp_path, root, {"task_id": "task-X", "reports": [entry]}) == 0
    entry["superseded_by"] = None
    assert lib_residuals.ingest_findings(tmp_path, root, {"task_id": "task-X", "reports": [entry]}) == 1
