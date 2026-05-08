"""Tests for hooks/circuit_breaker.py.

Covers the four breaker conditions, the downgrade tier, and the
_apply_abort side-effects (escalation.md, execution-log.md, transition).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Hooks directory is also added so `from hooks.circuit_breaker` works.
from hooks import circuit_breaker as cb  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, (dict, list)):
        path.write_text(json.dumps(payload), encoding="utf-8")
    else:
        path.write_text(str(payload), encoding="utf-8")


def _make_manifest(task_dir: Path, classification_type: str = "bugfix") -> None:
    _write(
        task_dir / "manifest.json",
        {
            "task_id": "task-test-circuit-breaker",
            "stage": "EXECUTION",
            "classification": {"type": classification_type},
        },
    )


def _stub_check_spawn_budget(monkeypatch: pytest.MonkeyPatch, payload: dict) -> None:
    """Patch subprocess.run so any check-spawn-budget call returns `payload`.

    Other commands fall through to a benign success result.
    """

    def fake_run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        if any("check-spawn-budget" in str(part) for part in cmd):
            return subprocess.CompletedProcess(
                cmd, returncode=0, stdout=json.dumps(payload), stderr=""
            )
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cb.subprocess, "run", fake_run)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_no_abort_when_thresholds_unmet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    _make_manifest(task_dir, classification_type="feature")
    _write(task_dir / "token-usage.json", {"total": 100_000, "events": []})
    _write(
        task_dir / "execution-graph.json",
        {"segments": [{"files_expected": ["a.py", "b.py"]}]},
    )
    _stub_check_spawn_budget(monkeypatch, {"status": "ok", "count": 0, "threshold": 2})

    result = cb.check_circuit_breakers(task_dir, "EXECUTION")
    assert result is None


def test_wasted_spawns_abort_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    _make_manifest(task_dir, classification_type="bugfix")
    _write(task_dir / "token-usage.json", {"total": 0, "events": []})
    _stub_check_spawn_budget(
        monkeypatch, {"status": "paused", "count": 3, "threshold": 2}
    )

    result = cb.check_circuit_breakers(task_dir, "EXECUTION")
    assert isinstance(result, dict)
    assert result.get("abort") is True
    assert result.get("trigger") == "wasted_spawns_abort"
    assert result.get("limit") == cb.WASTED_SPAWN_ABORT_THRESHOLD
    assert result.get("actual") == 3
    assert "reason" in result


def test_small_task_token_overrun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    _make_manifest(task_dir, classification_type="bugfix")
    _write(task_dir / "token-usage.json", {"total": 1_500_000, "events": []})
    _write(
        task_dir / "execution-graph.json",
        {"segments": [{"files_expected": ["a.py", "b.py"]}]},
    )
    _stub_check_spawn_budget(monkeypatch, {"status": "ok", "count": 0, "threshold": 2})

    result = cb.check_circuit_breakers(task_dir, "EXECUTION")
    assert isinstance(result, dict)
    assert result.get("abort") is True
    assert result.get("trigger") == "small_task_token_overrun"
    assert result.get("limit") == cb.SMALL_TASK_TOKEN_LIMIT
    assert result.get("actual") == 1_500_000

    # Multi-segment unique-file count > 2 must NOT fire small-task arm.
    task_dir2 = tmp_path / "task_multi"
    task_dir2.mkdir()
    _make_manifest(task_dir2, classification_type="bugfix")
    _write(task_dir2 / "token-usage.json", {"total": 1_500_000, "events": []})
    _write(
        task_dir2 / "execution-graph.json",
        {
            "segments": [
                {"files_expected": ["a.py"]},
                {"files_expected": ["b.py"]},
                {"files_expected": ["c.py"]},
            ]
        },
    )
    result2 = cb.check_circuit_breakers(task_dir2, "EXECUTION")
    assert (result2 is None) or result2.get("trigger") != "small_task_token_overrun"


def test_bugfix_token_overrun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    _make_manifest(task_dir, classification_type="bugfix")
    _write(task_dir / "token-usage.json", {"total": 6_000_000, "events": []})
    _write(
        task_dir / "execution-graph.json",
        {
            "segments": [
                {"files_expected": ["a.py"]},
                {"files_expected": ["b.py"]},
                {"files_expected": ["c.py"]},
                {"files_expected": ["d.py"]},
            ]
        },
    )
    _stub_check_spawn_budget(monkeypatch, {"status": "ok", "count": 0, "threshold": 2})

    result = cb.check_circuit_breakers(task_dir, "EXECUTION")
    assert isinstance(result, dict)
    assert result.get("abort") is True
    assert result.get("trigger") == "bugfix_token_overrun"
    assert result.get("limit") == cb.BUGFIX_TOKEN_LIMIT
    assert result.get("actual") == 6_000_000


def test_opus_zero_yield(tmp_path: Path) -> None:
    """AC-10: pre-existing test updated to seed generic receipts so it
    continues to pass once the receipt-provenance cross-check is wired in.
    Seeding route_mode=generic receipts is the minimal fixture change that
    preserves test intent while satisfying AC-10's cross-check requirement.
    """
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    _make_manifest(task_dir, classification_type="feature")

    events = [
        {"phase": "audit", "type": "spawn", "model": "opus", "agent": "auditor-1"},
        {"phase": "audit", "type": "spawn", "model": "opus", "agent": "auditor-2"},
        {"phase": "audit", "type": "spawn", "model": "opus", "agent": "auditor-3"},
        # Sonnet event must not count even if findings == [].
        {"phase": "audit", "type": "spawn", "model": "sonnet", "agent": "sonnet-1"},
    ]
    _write(task_dir / "token-usage.json", {"total": 0, "events": events})
    for name in ("auditor-1", "auditor-2", "auditor-3", "sonnet-1"):
        _write(
            task_dir / "audit-reports" / f"{name}.json",
            {"auditor": name, "findings": []},
        )

    # AC-10: seed receipts so the cross-check admits the three opus events.
    # Sonnet event does not need a receipt — model != "opus" guard fires first.
    receipts_dir = task_dir / "receipts"
    receipts_dir.mkdir()
    for name in ("auditor-1", "auditor-2", "auditor-3"):
        _write(receipts_dir / f"audit-{name}.json", {"route_mode": "generic"})

    result = cb.check_circuit_breakers(task_dir, "AUDITING")
    assert isinstance(result, dict)
    assert result.get("abort") is True
    assert result.get("trigger") == "opus_auditor_zero_yield"
    assert result.get("limit") == cb.OPUS_AUDITOR_ZERO_YIELD_THRESHOLD
    assert result.get("actual") >= 3


def test_apply_abort_writes_escalation_md(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_dir = tmp_path / "task"
    task_dir.mkdir()

    monkeypatch.setattr(
        cb.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 0, "", ""),
    )

    decision = {
        "abort": True,
        "trigger": "wasted_spawns_abort",
        "reason": "first reason",
        "limit": 3,
        "actual": 3,
    }
    cb._apply_abort(task_dir, decision)

    escalation = task_dir / "escalation.md"
    assert escalation.exists()
    body = escalation.read_text(encoding="utf-8")
    assert "wasted_spawns_abort" in body
    assert "first reason" in body

    # Calling a second time with a different decision must overwrite, not
    # append (AC-18 + risk-note "escalation.md overwrite").
    decision2 = {
        "abort": True,
        "trigger": "bugfix_token_overrun",
        "reason": "second reason",
        "limit": cb.BUGFIX_TOKEN_LIMIT,
        "actual": cb.BUGFIX_TOKEN_LIMIT + 1,
    }
    cb._apply_abort(task_dir, decision2)
    body2 = escalation.read_text(encoding="utf-8")
    assert "bugfix_token_overrun" in body2
    assert "first reason" not in body2  # overwrite, not append
    assert body2.count("# Circuit Breaker Abort") == 1


def test_apply_abort_logs_budget_abort_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_dir = tmp_path / "task"
    task_dir.mkdir()

    monkeypatch.setattr(
        cb.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 0, "", ""),
    )

    decision = {
        "abort": True,
        "trigger": "small_task_token_overrun",
        "reason": "small task too costly",
        "limit": cb.SMALL_TASK_TOKEN_LIMIT,
        "actual": cb.SMALL_TASK_TOKEN_LIMIT + 1,
    }
    cb._apply_abort(task_dir, decision)

    log = (task_dir / "execution-log.md").read_text(encoding="utf-8")
    matching = [
        line
        for line in log.splitlines()
        if line.startswith("[BUDGET-ABORT]")
        and "small_task_token_overrun" in line
    ]
    assert len(matching) == 1


def test_apply_abort_calls_transition_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_dir = tmp_path / "task"
    task_dir.mkdir()

    captured: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        captured.append([str(part) for part in cmd])
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cb.subprocess, "run", fake_run)

    decision = {
        "abort": True,
        "trigger": "wasted_spawns_abort",
        "reason": "r",
        "limit": 3,
        "actual": 3,
    }
    cb._apply_abort(task_dir, decision)

    transition_calls = [
        call
        for call in captured
        if "transition" in call and "FAILED" in call
    ]
    assert len(transition_calls) == 1
    call = transition_calls[0]
    assert call[-1] == "FAILED"
    assert "--task-dir" in call
    assert str(task_dir) in call

    # Now: when subprocess.run raises, _apply_abort must NOT propagate.
    def boom(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("subprocess unavailable")

    monkeypatch.setattr(cb.subprocess, "run", boom)
    cb._apply_abort(task_dir, decision)  # must not raise


def test_small_task_token_downgrade_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    _make_manifest(task_dir, classification_type="bugfix")
    _write(task_dir / "token-usage.json", {"total": 850_000, "events": []})
    _write(
        task_dir / "execution-graph.json",
        {"segments": [{"files_expected": ["a.py", "b.py"]}]},
    )
    _stub_check_spawn_budget(monkeypatch, {"status": "ok", "count": 0, "threshold": 2})

    result = cb.check_circuit_breakers(task_dir, "EXECUTION")
    assert isinstance(result, dict)
    assert result.get("action") == "downgrade"
    assert result.get("trigger") == "small_task_token_downgrade"
    assert "abort" not in result
    assert result.get("limit_warned") == cb.SMALL_TASK_TOKEN_DOWNGRADE_THRESHOLD
    assert result.get("limit_abort") == cb.SMALL_TASK_TOKEN_LIMIT
    assert result.get("actual") == 850_000
    assert "suggestion" in result


def test_bugfix_token_downgrade_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counterpart to test_small_task_token_downgrade_warning. Bugfix
    classification at >= 4M tokens but below 5M LIMIT must produce a
    downgrade decision (not an abort, not None). Closes residual
    5de91777 from cq-002 of task-20260507-003."""
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    _make_manifest(task_dir, classification_type="bugfix")
    _write(task_dir / "token-usage.json", {"total": 4_500_000, "events": []})
    # Bugfix path: more than SMALL_TASK_FILES_THRESHOLD files so the
    # small-task arms don't shadow the bugfix arms.
    _write(
        task_dir / "execution-graph.json",
        {"segments": [{"files_expected": ["a.py", "b.py", "c.py", "d.py"]}]},
    )
    _stub_check_spawn_budget(monkeypatch, {"status": "ok", "count": 0, "threshold": 2})

    result = cb.check_circuit_breakers(task_dir, "EXECUTION")
    assert isinstance(result, dict)
    assert result.get("action") == "downgrade"
    assert result.get("trigger") == "bugfix_token_downgrade"
    assert "abort" not in result
    assert result.get("limit_warned") == cb.BUGFIX_TOKEN_DOWNGRADE_THRESHOLD
    assert result.get("limit_abort") == cb.BUGFIX_TOKEN_LIMIT
    assert result.get("actual") == 4_500_000
    assert "suggestion" in result


def test_apply_abort_skips_transition_on_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_dir = tmp_path / "task"
    task_dir.mkdir()

    captured: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        captured.append([str(part) for part in cmd])
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cb.subprocess, "run", fake_run)

    decision = {
        "action": "downgrade",
        "trigger": "small_task_token_downgrade",
        "reason": "approaching limit",
        "suggestion": "downgrade to sonnet",
        "limit_warned": cb.SMALL_TASK_TOKEN_DOWNGRADE_THRESHOLD,
        "limit_abort": cb.SMALL_TASK_TOKEN_LIMIT,
        "actual": 850_000,
    }
    cb._apply_abort(task_dir, decision)

    # escalation.md must exist
    escalation = task_dir / "escalation.md"
    assert escalation.exists()
    assert "small_task_token_downgrade" in escalation.read_text(encoding="utf-8")

    # execution-log.md must contain a [BUDGET-DOWNGRADE] line
    log = (task_dir / "execution-log.md").read_text(encoding="utf-8")
    matching = [
        line
        for line in log.splitlines()
        if line.startswith("[BUDGET-DOWNGRADE]")
        and "small_task_token_downgrade" in line
    ]
    assert len(matching) == 1

    # No transition subprocess call must have been made.
    transition_calls = [
        call for call in captured if "transition" in call and "FAILED" in call
    ]
    assert transition_calls == []


# ---------------------------------------------------------------------------
# SEC-CB-001: _validate_audit_event_receipt unit tests (RED until helper exists)
# ---------------------------------------------------------------------------


def _seed_receipt(task_dir: Path, agent: str, payload: dict) -> None:
    """Write receipts/audit-{agent}.json (primary candidate)."""
    _write(task_dir / "receipts" / f"audit-{agent}.json", payload)


def _seed_sidecar(task_dir: Path, agent: str, model: str, content: str) -> None:
    """Write receipts/_injected-auditor-prompts/{agent}-{model}.sha256."""
    sidecar_path = (
        task_dir / "receipts" / "_injected-auditor-prompts" / f"{agent}-{model}.sha256"
    )
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text(content, encoding="utf-8")


def test_validate_audit_event_receipt_admits_with_matching_sidecar(
    tmp_path: Path,
) -> None:
    """AC-4: receipt has injected_agent_sha256='abc123' and sidecar contains
    the same hash (with trailing newline) → _validate_audit_event_receipt returns True.
    """
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    _seed_receipt(
        task_dir, "auditor-1", {"injected_agent_sha256": "abc123", "route_mode": "learned"}
    )
    _seed_sidecar(task_dir, "auditor-1", "opus", "abc123\n")

    event = {"phase": "audit", "type": "spawn", "model": "opus", "agent": "auditor-1"}
    result = cb._validate_audit_event_receipt(event, task_dir)
    assert result is True


def test_validate_audit_event_receipt_admits_generic_route_mode_without_sidecar(
    tmp_path: Path,
) -> None:
    """AC-5: receipt has route_mode='generic' and no injected_agent_sha256
    field → returns True without consulting any sidecar file.
    """
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    _seed_receipt(task_dir, "myagent", {"route_mode": "generic"})
    # Deliberately no sidecar file on disk.

    event = {"phase": "audit", "type": "spawn", "model": "opus", "agent": "myagent"}
    result = cb._validate_audit_event_receipt(event, task_dir)
    assert result is True


def test_validate_audit_event_receipt_rejects_missing_receipt(
    tmp_path: Path,
) -> None:
    """AC-3: neither audit-{agent}.json nor audit-{agent}-auditor.json exists
    → returns False.
    """
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    # No receipt files created.

    event = {"phase": "audit", "type": "spawn", "model": "opus", "agent": "ghost"}
    result = cb._validate_audit_event_receipt(event, task_dir)
    assert result is False


def test_validate_audit_event_receipt_rejects_sidecar_mismatch(
    tmp_path: Path,
) -> None:
    """AC-4 + AC-9: receipt has injected_agent_sha256='correct-sha' but sidecar
    contains 'wrong-sha' → returns False (sidecar mismatch treated as forgery).
    """
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    _seed_receipt(
        task_dir, "auditor-x", {"injected_agent_sha256": "correct-sha", "route_mode": "learned"}
    )
    _seed_sidecar(task_dir, "auditor-x", "opus", "wrong-sha")

    event = {"phase": "audit", "type": "spawn", "model": "opus", "agent": "auditor-x"}
    result = cb._validate_audit_event_receipt(event, task_dir)
    assert result is False


def test_validate_audit_event_receipt_rejects_unsafe_agent_name(
    tmp_path: Path,
) -> None:
    """AC-2: agent value containing path separators fails _SAFE_AGENT_RE
    → returns False immediately without touching the filesystem.
    """
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    # No receipt files — if the function attempted filesystem access it
    # would reach a nonexistent path, not cause an error, so absence of
    # a receipt isn't a sufficient check. We verify False is returned.

    for unsafe_agent in ("../etc/passwd", "../../root", "agent/evil", "agent name"):
        event = {
            "phase": "audit",
            "type": "spawn",
            "model": "opus",
            "agent": unsafe_agent,
        }
        result = cb._validate_audit_event_receipt(event, task_dir)
        assert result is False, f"Expected False for unsafe agent {unsafe_agent!r}"


def test_validate_audit_event_receipt_rejects_malformed_receipt_json(
    tmp_path: Path,
) -> None:
    """Boundary: receipt file exists but contains malformed JSON
    → returns False without raising.
    """
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    bad_receipt = task_dir / "receipts" / "audit-badagent.json"
    bad_receipt.parent.mkdir(parents=True, exist_ok=True)
    bad_receipt.write_text("not valid json{", encoding="utf-8")

    event = {"phase": "audit", "type": "spawn", "model": "opus", "agent": "badagent"}
    result = cb._validate_audit_event_receipt(event, task_dir)
    assert result is False


def test_validate_audit_event_receipt_rejects_empty_sidecar(
    tmp_path: Path,
) -> None:
    """Boundary: sidecar file exists but contains only whitespace
    → strip() produces '' which never matches non-empty injected_agent_sha256
    → returns False.
    """
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    _seed_receipt(
        task_dir, "auditor-es", {"injected_agent_sha256": "abc123", "route_mode": "learned"}
    )
    _seed_sidecar(task_dir, "auditor-es", "opus", "   \n")

    event = {"phase": "audit", "type": "spawn", "model": "opus", "agent": "auditor-es"}
    result = cb._validate_audit_event_receipt(event, task_dir)
    assert result is False


def test_validate_audit_event_receipt_tries_both_filename_candidates(
    tmp_path: Path,
) -> None:
    """AC-3: primary audit-{agent}.json is absent but legacy
    audit-{agent}-auditor.json exists → function uses the fallback candidate
    and returns True (generic route).
    """
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    # Only write the -auditor suffix variant (legacy fallback).
    legacy = task_dir / "receipts" / "audit-myagent-auditor.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    _write(legacy, {"route_mode": "generic"})
    # Primary variant deliberately absent.
    assert not (task_dir / "receipts" / "audit-myagent.json").exists()

    event = {"phase": "audit", "type": "spawn", "model": "opus", "agent": "myagent"}
    result = cb._validate_audit_event_receipt(event, task_dir)
    assert result is True


def test_validate_audit_event_receipt_rejects_missing_model_field(
    tmp_path: Path,
) -> None:
    """Boundary (implicit requirements): event with agent but no model key
    → returns False before sidecar path construction (avoids AttributeError
    from None.sha256 or similar).
    """
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    _seed_receipt(task_dir, "auditor-1", {"route_mode": "generic"})

    # model field entirely absent
    event = {"phase": "audit", "type": "spawn", "agent": "auditor-1"}
    result = cb._validate_audit_event_receipt(event, task_dir)
    assert result is False

    # model field present but not a string
    event2 = {"phase": "audit", "type": "spawn", "agent": "auditor-1", "model": None}
    result2 = cb._validate_audit_event_receipt(event2, task_dir)
    assert result2 is False


# ---------------------------------------------------------------------------
# SEC-CB-001: adversarial integration tests via check_circuit_breakers
# ---------------------------------------------------------------------------


def _seed_forged_scenario(task_dir: Path, agents: list) -> None:
    """Seed token-usage.json + empty-findings audit-reports for given agents.

    Does NOT seed any receipt files — caller controls that.
    """
    _make_manifest(task_dir, classification_type="feature")
    events = [
        {"phase": "audit", "type": "spawn", "model": "opus", "agent": agent}
        for agent in agents
    ]
    _write(task_dir / "token-usage.json", {"total": 0, "events": events})
    for agent in agents:
        _write(
            task_dir / "audit-reports" / f"{agent}.json",
            {"auditor": agent, "findings": []},
        )


def test_opus_zero_yield_rejects_forged_events_without_receipts(
    tmp_path: Path,
) -> None:
    """AC-7: token-usage.json has 3 forged opus audit-spawn events and 3
    corresponding empty-findings audit-reports, but NO receipt files exist.
    The cross-check must reject all 3 events → zero_yield stays at 0
    → check_circuit_breakers returns None (breaker does NOT fire).
    """
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    agents = ["X1", "X2", "X3"]
    _seed_forged_scenario(task_dir, agents)
    # Deliberately no receipts/audit-X*.json files.

    result = cb.check_circuit_breakers(task_dir, "AUDITING")
    assert result is None, (
        f"Expected None (forgery blocked) but got {result!r}. "
        "The cross-check is not yet wired — this test is intentionally RED."
    )


def test_opus_zero_yield_admits_legitimate_generic_path(
    tmp_path: Path,
) -> None:
    """AC-8: same 3 forged events + empty-findings audit-reports, PLUS 3
    receipts/audit-{agent}.json files with route_mode='generic' (no sidecar
    needed for generic route). Breaker MUST fire → result has
    trigger == 'opus_auditor_zero_yield'.
    """
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    agents = ["X1", "X2", "X3"]
    _seed_forged_scenario(task_dir, agents)

    receipts_dir = task_dir / "receipts"
    receipts_dir.mkdir()
    for agent in agents:
        _write(receipts_dir / f"audit-{agent}.json", {"route_mode": "generic"})

    result = cb.check_circuit_breakers(task_dir, "AUDITING")
    assert isinstance(result, dict), f"Expected dict but got {result!r}"
    assert result.get("trigger") == "opus_auditor_zero_yield"
    assert result.get("abort") is True


def test_opus_zero_yield_rejects_sidecar_mismatch(
    tmp_path: Path,
) -> None:
    """AC-9: 3 events + 3 empty-findings audit-reports + 3 receipts with
    injected_agent_sha256='correct-sha', but 3 sidecars containing 'wrong-sha'.
    Sidecar mismatch is treated as forgery → breaker must NOT fire → None.
    """
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    agents = ["Y1", "Y2", "Y3"]
    _seed_forged_scenario(task_dir, agents)

    receipts_dir = task_dir / "receipts"
    receipts_dir.mkdir()
    for agent in agents:
        _write(
            receipts_dir / f"audit-{agent}.json",
            {"injected_agent_sha256": "correct-sha", "route_mode": "learned"},
        )
        _seed_sidecar(task_dir, agent, "opus", "wrong-sha")

    result = cb.check_circuit_breakers(task_dir, "AUDITING")
    assert result is None, (
        f"Expected None (sidecar mismatch blocked) but got {result!r}. "
        "The cross-check is not yet wired — this test is intentionally RED."
    )
