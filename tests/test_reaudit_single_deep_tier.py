"""Re-audit runs one escalation-model spawn per ensemble auditor.

After a repair cycle (``repair-log.json`` exists) the fast -> balanced -> deep
cascade does not restart. Each ensemble auditor is re-audited once at its
escalation model, and that verdict is binding. Covers every enforcement point
that reads ``lib_ensemble``: ``ensemble_next``, the DONE gate, the receipt
writer, ``audit-inject-prompt``, repair-finding selection and
``ctl run-audit-reaudit-plan``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import lib_ensemble  # noqa: E402
from lib_core import require_receipts_for_done  # noqa: E402
from lib_validate import collect_repair_findings  # noqa: E402
from test_ensemble_cascade_enforcement import (  # noqa: E402
    AUDITOR,
    BALANCED,
    DEEP,
    FAST,
    _ctl,
    _entry,
    _inject,
    _mock_empty_registry,
    _setup_task,
    _shard,
    _spawn_log,
    _write_shard_receipt,
)


def _repair_log(td: Path, cycle: int = 1) -> None:
    (td / "repair-log.json").write_text(json.dumps({
        "task_id": td.name,
        "repair_cycle": cycle,
        "batches": [],
    }))


def _receipt(td: Path, model: str) -> dict:
    return json.loads((td / "receipts" / f"audit-{AUDITOR}-{model}.json").read_text())


# ---------------------------------------------------------------------------
# ensemble_next
# ---------------------------------------------------------------------------

def test_first_audit_is_unchanged(tmp_path):
    td = _setup_task(tmp_path)
    r = lib_ensemble.ensemble_next(td, AUDITOR)
    assert r["status"] == "spawn" and r["model"] == FAST
    assert "reaudit_model" not in r


def test_reaudit_after_pass_keeps_history_complete_and_offers_deep_spawn(tmp_path):
    td = _setup_task(tmp_path)
    _shard(td, FAST, findings=0, ts="2026-10-31T10:00:00Z")
    _shard(td, BALANCED, findings=0, ts="2026-10-31T10:05:00Z")
    _repair_log(td)
    r = lib_ensemble.ensemble_next(td, AUDITOR)
    assert r["status"] == "complete" and r["verdict"] == "pass"
    assert r["reaudit_model"] == DEEP and r["reaudit_cycle"] == 1
    assert r["reaudit_done"] is False and r["gaps"] == []


def test_reaudit_finishes_an_incomplete_cascade_with_one_deep_spawn(tmp_path):
    td = _setup_task(tmp_path)
    _shard(td, FAST, findings=0, ts="2026-10-31T10:00:00Z")  # balanced never ran
    _repair_log(td)
    r = lib_ensemble.ensemble_next(td, AUDITOR)
    assert r["status"] == "spawn" and r["model"] == DEEP
    assert r["reason"] == lib_ensemble.REASON_REAUDIT
    assert r["shard_step_name"] == f"{AUDITOR}-{DEEP}"
    assert r["gaps"]


def test_reaudit_deep_receipt_stamped_with_current_cycle_is_done(tmp_path):
    td = _setup_task(tmp_path)
    _spawn_log(td)
    _write_shard_receipt(td, FAST, findings=1, blocking=1)
    _write_shard_receipt(td, DEEP, findings=1, blocking=1)
    assert "repair_cycle" not in _receipt(td, DEEP)  # first audit: no stamp
    _repair_log(td, cycle=1)
    _write_shard_receipt(td, DEEP, findings=2, blocking=2)
    assert _receipt(td, DEEP)["repair_cycle"] == 1
    r = lib_ensemble.ensemble_next(td, AUDITOR)
    assert r["status"] == "complete" and r["verdict"] == "escalated"
    assert r["reaudit_done"] is True and r["gaps"] == []

    # The next repair cycle: the earlier re-audit stays binding, a new
    # deep spawn is offered and stamped with the new cycle.
    _repair_log(td, cycle=2)
    r = lib_ensemble.ensemble_next(td, AUDITOR)
    assert r["status"] == "complete" and r["verdict"] == "escalated"
    assert r["reaudit_done"] is False and r["reaudit_cycle"] == 2
    _write_shard_receipt(td, DEEP, findings=0)
    assert _receipt(td, DEEP)["repair_cycle"] == 2
    assert lib_ensemble.ensemble_next(td, AUDITOR)["reaudit_done"] is True


def test_reaudit_needs_an_escalation_model_above_the_voting_tiers(tmp_path):
    td = _setup_task(tmp_path, [_entry(escalation=BALANCED)])
    _repair_log(td)
    r = lib_ensemble.ensemble_next(td, AUDITOR)
    assert "reaudit_model" not in r and r["model"] == FAST


def test_unreadable_repair_log_keeps_first_audit_cascade(tmp_path):
    td = _setup_task(tmp_path)
    (td / "repair-log.json").write_text("{not json")
    assert lib_ensemble.current_repair_cycle(td) is None
    assert lib_ensemble.ensemble_next(td, AUDITOR)["model"] == FAST


# ---------------------------------------------------------------------------
# Receipt writer
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("model", [FAST, BALANCED])
def test_receipt_refuses_voting_tiers_on_reaudit(tmp_path, model):
    td = _setup_task(tmp_path)
    _spawn_log(td)
    _write_shard_receipt(td, FAST)
    _write_shard_receipt(td, BALANCED)
    _repair_log(td)
    with pytest.raises(ValueError, match="does not restart"):
        _write_shard_receipt(td, model)


def test_receipt_accepts_single_deep_spawn_on_reaudit(tmp_path):
    td = _setup_task(tmp_path)
    _spawn_log(td)
    _write_shard_receipt(td, FAST)
    _write_shard_receipt(td, BALANCED)
    _repair_log(td, cycle=3)
    _write_shard_receipt(td, DEEP, findings=1, blocking=1)
    assert _receipt(td, DEEP)["repair_cycle"] == 3


# ---------------------------------------------------------------------------
# DONE gate / ensemble-next CLI
# ---------------------------------------------------------------------------

def test_gate_accepts_reaudit_deep_only(tmp_path, monkeypatch):
    _mock_empty_registry(monkeypatch)
    td = _setup_task(tmp_path)
    _spawn_log(td)
    _write_shard_receipt(td, FAST, findings=1, blocking=1)
    _write_shard_receipt(td, DEEP, findings=1, blocking=1)
    _repair_log(td)
    _write_shard_receipt(td, DEEP, findings=0)
    gaps = require_receipts_for_done(td)
    assert not any(AUDITOR in g for g in gaps), gaps


def test_gate_does_not_require_a_reaudit_of_a_complete_cascade(tmp_path, monkeypatch):
    """Parity with the old behavior: an ensemble auditor outside the re-audit
    set keeps its first-audit verdict."""
    _mock_empty_registry(monkeypatch)
    td = _setup_task(tmp_path)
    _shard(td, FAST, findings=0, ts="2026-10-31T10:00:00Z")
    _shard(td, BALANCED, findings=0, ts="2026-10-31T10:05:00Z")
    _repair_log(td)
    gaps = require_receipts_for_done(td)
    assert not any(AUDITOR in g for g in gaps), gaps


def test_gate_refuses_incomplete_cascade_until_deep_reaudit(tmp_path, monkeypatch):
    _mock_empty_registry(monkeypatch)
    td = _setup_task(tmp_path)
    _spawn_log(td)
    _write_shard_receipt(td, FAST)
    _repair_log(td)
    gaps = require_receipts_for_done(td)
    assert any(AUDITOR in g and DEEP in g for g in gaps), gaps
    _write_shard_receipt(td, DEEP)
    assert not any(AUDITOR in g for g in require_receipts_for_done(td))


def test_ctl_ensemble_next_reports_reaudit_fields(tmp_path):
    td = _setup_task(tmp_path)
    _shard(td, FAST, findings=0, ts="2026-10-31T10:00:00Z")
    _shard(td, BALANCED, findings=0, ts="2026-10-31T10:05:00Z")
    _repair_log(td, cycle=2)
    res = _ctl("ensemble-next", str(td))
    assert res.returncode == 0, res.stdout + res.stderr
    payload = json.loads(res.stdout)
    assert payload["complete"] is True
    row = payload["auditors"][0]
    assert row["reaudit_model"] == DEEP and row["reaudit_cycle"] == 2
    assert row["reaudit_shard_step_name"] == f"{AUDITOR}-{DEEP}"


# ---------------------------------------------------------------------------
# audit-inject-prompt
# ---------------------------------------------------------------------------

def test_inject_prompt_on_reaudit_only_allows_the_deep_tier(tmp_path):
    td = _setup_task(tmp_path)
    _shard(td, FAST, findings=0, ts="2026-10-31T10:00:00Z")
    _shard(td, BALANCED, findings=0, ts="2026-10-31T10:05:00Z")
    _repair_log(td)
    for model in (FAST, BALANCED):
        res = _inject(td, model)
        assert res.returncode == 1, res.stdout + res.stderr
        assert "re-audit" in json.loads(res.stdout)["error"]
    res = _inject(td, DEEP)
    assert res.returncode == 0, res.stdout + res.stderr


# ---------------------------------------------------------------------------
# Repair-finding selection
# ---------------------------------------------------------------------------

def _report_attempt(td: Path, model: str, findings: list[dict], attempt: int) -> None:
    (td / "audit-reports" / f"{AUDITOR}-{model}-attempt-{attempt}.json").write_text(json.dumps({
        "auditor_name": AUDITOR,
        "status": "complete",
        "findings": findings,
    }))


def test_reaudit_deep_report_is_binding_over_first_audit_voting_reports(tmp_path):
    td = _setup_task(tmp_path)
    _spawn_log(td)
    # First audit passed at the voting tiers with a non-blocking finding.
    _report_attempt(td, FAST, [{"id": "S-1", "blocking": False, "location": "a.py:1"}], 1)
    _report_attempt(td, BALANCED, [{"id": "S-2", "blocking": False, "location": "b.py:1"}], 1)
    _shard(td, FAST, findings=0, ts="2026-10-31T10:00:00Z")
    _shard(td, BALANCED, findings=0, ts="2026-10-31T10:05:00Z")
    _repair_log(td)
    # Re-audit: one deep spawn, which finds a blocking issue.
    _report_attempt(td, DEEP, [{"id": "S-9", "blocking": True, "location": "c.py:1"}], 1)
    _write_shard_receipt(td, DEEP, findings=1, blocking=1)
    # The shared receipt helper also writes an unversioned report of its own.
    (td / "audit-reports" / f"{AUDITOR}-{DEEP}.json").unlink()
    collected = collect_repair_findings(td)
    assert [f["id"] for f in collected["findings"]] == ["S-9"]
    superseded = {Path(r["report_path"]).name for r in collected["reports"] if r["superseded_by"]}
    assert superseded == {
        f"{AUDITOR}-{FAST}-attempt-1.json",
        f"{AUDITOR}-{BALANCED}-attempt-1.json",
    }


# ---------------------------------------------------------------------------
# ctl run-audit-reaudit-plan
# ---------------------------------------------------------------------------

def test_reaudit_plan_names_one_deep_spawn_for_ensemble_auditors(tmp_path):
    td = _setup_task(tmp_path, [
        _entry("security-auditor"),
        _entry("spec-completion-auditor", ensemble=False, model=BALANCED),
    ])
    (td / "repair-log.json").write_text(json.dumps({
        "task_id": td.name,
        "repair_cycle": 1,
        "batches": [],
    }))
    res = _ctl("run-audit-reaudit-plan", str(td))
    assert res.returncode == 0, res.stdout + res.stderr
    payload = json.loads(res.stdout)
    spawns = payload["spawns"]
    assert spawns["security-auditor"] == {
        "model": DEEP,
        "ensemble": True,
        "shard_step_name": f"security-auditor-{DEEP}",
        "in_plan": True,
    }
    assert spawns["spec-completion-auditor"]["model"] == BALANCED
    assert spawns["spec-completion-auditor"]["ensemble"] is False
