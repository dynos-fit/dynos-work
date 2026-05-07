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
