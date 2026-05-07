"""Tests for the two residual-cleanup tools added to drain queue bloat:

1. ``_accept_finding`` filter: drops minor findings whose ``remediation``
   says "no change required" / "no action required" / "no fix required".
   These were leaking into the residual queue from auditors that record
   their reviews of subsystems where nothing actionable was found.

2. ``ctl residual-close``: closes a queue row to ``done`` or ``wontfix``
   without invoking ``/dynos-work:start``. For one-liners, registry-only
   edits, or informational notes that don't need the full lifecycle.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

import lib_residuals  # noqa: E402


def _make_finding(remediation: str, **overrides: object) -> dict:
    base = {
        "id": "X-001",
        "category": "security",
        "severity": "minor",
        "blocking": False,
        "title": "Reviewed X; no exploit",
        "description": "Detailed review of X surface; no actionable issue.",
        "location": "hooks/foo.py",
        "remediation": remediation,
    }
    base.update(overrides)
    return base


# ---- _accept_finding remediation filter ------------------------------------


def test_accept_finding_drops_no_change_required():
    f = _make_finding("No change required.")
    assert lib_residuals._accept_finding(f, "security-auditor") is False


def test_accept_finding_drops_no_action_required():
    f = _make_finding("No action required. Optionally consider...")
    assert lib_residuals._accept_finding(f, "security-auditor") is False


def test_accept_finding_drops_no_fix_required():
    f = _make_finding("No fix required.")
    assert lib_residuals._accept_finding(f, "security-auditor") is False


def test_accept_finding_drops_case_insensitive():
    f = _make_finding("NO CHANGE REQUIRED")
    assert lib_residuals._accept_finding(f, "security-auditor") is False


def test_accept_finding_admits_real_remediation():
    f = _make_finding(
        "Validate agent against allowlist regex r'^[A-Za-z0-9_-]+$' "
        "before path/glob join."
    )
    assert lib_residuals._accept_finding(f, "security-auditor") is True


def test_accept_finding_drops_minor_without_remediation():
    """severity:minor with no remediation field is a review note, not a
    real finding. Drop. (task-005 schema-shape filter.)"""
    f = _make_finding("")  # empty remediation
    f.pop("remediation")
    f["severity"] = "minor"
    assert lib_residuals._accept_finding(f, "security-auditor") is False


def test_accept_finding_drops_minor_with_empty_remediation():
    f = _make_finding("")  # empty string remediation
    f["severity"] = "minor"
    assert lib_residuals._accept_finding(f, "security-auditor") is False


def test_accept_finding_drops_minor_with_null_remediation():
    f = _make_finding("")
    f["severity"] = "minor"
    f["remediation"] = None
    assert lib_residuals._accept_finding(f, "security-auditor") is False


def test_accept_finding_admits_major_without_remediation():
    """severity:major / severity:critical findings are admitted even
    without remediation — the severity alone justifies a queue row.
    Only severity:minor needs the remediation gate."""
    f = _make_finding("")
    f.pop("remediation")
    f["severity"] = "major"
    assert lib_residuals._accept_finding(f, "security-auditor") is True


def test_accept_finding_admits_critical_without_remediation():
    f = _make_finding("")
    f.pop("remediation")
    f["severity"] = "critical"
    assert lib_residuals._accept_finding(f, "security-auditor") is True


def test_accept_finding_admits_minor_with_real_remediation():
    """severity:minor + concrete remediation is a legitimate small bug."""
    f = _make_finding(
        "Add input validation: reject any value containing path separators."
    )
    f["severity"] = "minor"
    assert lib_residuals._accept_finding(f, "security-auditor") is True


# ---- ctl residual-close ----------------------------------------------------


def _write_queue(root: Path, rows: list[dict]) -> Path:
    qd = root / ".dynos"
    qd.mkdir(parents=True, exist_ok=True)
    p = qd / "proactive-findings.json"
    p.write_text(json.dumps({"findings": rows}, indent=2))
    return p


def _row(rid: str, status: str = "pending") -> dict:
    return {
        "id": rid,
        "kind": "residual",
        "fingerprint": f"fp-{rid}",
        "created_at": "2026-05-07T18:00:00Z",
        "source_task_id": "manual",
        "source_auditor": "security-auditor",
        "title": f"row {rid}",
        "description": f"desc {rid}",
        "location": "(repo-wide)",
        "status": status,
        "attempts": 0,
        "last_attempt_at": None,
    }


def _ctl(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(ROOT / "hooks" / "ctl.py"), *args]
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def test_residual_close_marks_done(tmp_path: Path):
    qpath = _write_queue(tmp_path, [_row("R-1")])
    res = _ctl("residual-close", "R-1", "--reason", "trivial registry edit; one-liner", "--root", str(tmp_path), cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    payload = json.loads(res.stdout)
    assert payload["status"] == "closed"
    assert payload["new_status"] == "done"

    data = json.loads(qpath.read_text())
    row = next(r for r in data["findings"] if r["id"] == "R-1")
    assert row["status"] == "done"
    assert row["close_reason"] == "trivial registry edit; one-liner"
    assert "closed_at" in row


def test_residual_close_wontfix(tmp_path: Path):
    qpath = _write_queue(tmp_path, [_row("R-2")])
    res = _ctl(
        "residual-close", "R-2",
        "--reason", "duplicate of R-1",
        "--status", "wontfix",
        "--root", str(tmp_path),
        cwd=tmp_path,
    )
    assert res.returncode == 0, res.stderr
    data = json.loads(qpath.read_text())
    row = next(r for r in data["findings"] if r["id"] == "R-2")
    assert row["status"] == "wontfix"


def test_residual_close_rejects_empty_reason(tmp_path: Path):
    _write_queue(tmp_path, [_row("R-3")])
    res = _ctl("residual-close", "R-3", "--reason", "   ", "--root", str(tmp_path), cwd=tmp_path)
    assert res.returncode == 1
    assert "reason" in res.stderr.lower()


def test_residual_close_unknown_id(tmp_path: Path):
    _write_queue(tmp_path, [_row("R-1")])
    res = _ctl("residual-close", "DOES-NOT-EXIST", "--reason", "nothing to do", "--root", str(tmp_path), cwd=tmp_path)
    assert res.returncode == 1
    assert "not found" in res.stderr.lower()


def test_residual_close_idempotent_on_already_closed(tmp_path: Path):
    qpath = _write_queue(tmp_path, [_row("R-4", status="done")])
    res = _ctl("residual-close", "R-4", "--reason", "second call", "--root", str(tmp_path), cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    payload = json.loads(res.stdout)
    assert payload["status"] == "noop"
    # On-disk row not modified by the noop
    data = json.loads(qpath.read_text())
    row = next(r for r in data["findings"] if r["id"] == "R-4")
    assert "close_reason" not in row


def test_residual_close_rejects_invalid_status(tmp_path: Path):
    _write_queue(tmp_path, [_row("R-5")])
    res = _ctl(
        "residual-close", "R-5",
        "--reason", "ok",
        "--status", "garbage",
        "--root", str(tmp_path),
        cwd=tmp_path,
    )
    assert res.returncode == 1


# ---- apply_analysis no longer ingests prevention rules ---------------------


def test_apply_analysis_does_not_call_ingest_prevention_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """task-005 architectural fix: apply_analysis previously triple-counted
    work by adding rules to prevention-rules.json AND emitting a residual
    row for each rule via ingest_prevention_rules. The residual row was
    redundant — once the rule is in the registry, the engine enforces it
    on every commit. This test pins the removal: ingest_prevention_rules
    must not be called from the apply_analysis code path. The function
    itself remains importable for any other callers / tests."""
    sys.path.insert(0, str(ROOT / "memory"))
    import postmortem_analysis as pa  # noqa: PLC0415
    import lib_residuals as lr  # noqa: PLC0415

    calls: list[tuple] = []

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return 0

    # The `apply_analysis` import does `from lib_residuals import
    # ingest_prevention_rules` lazily inside the function body. Patch the
    # source attribute so any deferred import resolves to the spy.
    monkeypatch.setattr(lr, "ingest_prevention_rules", spy)

    # Seed minimal task dir + a synthetic analysis with a rule that WOULD
    # have been promoted under the old behavior (enforcement=ci-gate).
    project = tmp_path / "proj"
    (project / ".dynos").mkdir(parents=True)
    task_dir = project / ".dynos" / "task-test"
    task_dir.mkdir()
    (task_dir / "manifest.json").write_text(json.dumps({"task_id": "task-test"}))
    (task_dir / "task-retrospective.json").write_text(json.dumps({"quality_score": 0.9}))
    (project / ".dynos" / "events.jsonl").touch()

    analysis = {
        "summary": "test",
        "prevention_rules": [{
            "rule": "every breaker must read events via verify_signed_events",
            "rationale": "raw reads are forgeable",
            "executor": "backend-executor",
            "category": "sec",
            "enforcement": "ci-gate",
            "template": "advisory",
        }],
    }

    monkeypatch.chdir(project)
    pa.apply_analysis(task_dir, analysis)

    assert calls == [], (
        f"ingest_prevention_rules must NOT be called from apply_analysis "
        f"(task-005 fix); was called with: {calls}"
    )
