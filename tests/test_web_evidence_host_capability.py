"""Tests for host-aware web-search evidence in the external-solution gate.

The gate's temporal cross-check reads ``web-tool-log.jsonl``, which only ever
gets written by the PostToolUse hook matching ``WebSearch|WebFetch`` — tool
names that exist on Claude Code alone. Codex researches through its own
built-in tooling, which never reaches this plugin's matcher, so on that host
the log cannot exist and the check was unsatisfiable: ``run-spec-ready``
returned 1 forever and the task could not leave SPEC_NORMALIZATION.

Coverage:
  - claude host: behaviour is byte-identical to before (strict)
  - codex host: degrades to a receipt-vs-gate ordering check, visibly
  - degraded mode still refuses a receipt that predates the gate
  - a real log entry passes at full strength on any host
  - persisted control-plane host beats env detection (bypass defense)
  - unknown host names stay strict
  - the web-tool-log hook normalises non-Claude tool spellings
  - hooks/session-start writes the persisted host anchor
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

VALID_SUMMARY = "x" * 210
VALID_URL = "https://example.com/evidence"

GATE_TS = "2026-05-07T10:00:00Z"
ENTRY_TS = "2026-05-07T10:30:00Z"
RECEIPT_TS = "2026-05-07T11:00:00Z"
EARLY_RECEIPT_TS = "2026-05-07T09:00:00Z"


def _task_dir(tmp_path: Path) -> Path:
    task_dir = tmp_path / ".dynos" / "task-20260907-001"
    task_dir.mkdir(parents=True)
    return task_dir


def _gate(written_at: str = GATE_TS) -> dict:
    return {"search_recommended": True, "written_at": written_at}


def _receipt(ts: str = RECEIPT_TS) -> dict:
    return {
        "step": "search-conducted",
        "ts": ts,
        "urls_consulted": [VALID_URL],
        "findings_summary": VALID_SUMMARY,
    }


def _write_log(task_dir: Path, entries: list[dict]) -> None:
    (task_dir / "web-tool-log.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8"
    )


def _persist_host(task_dir: Path, host: str) -> None:
    """Write the hook-owned host anchor the validator prefers over env."""
    cp = task_dir.parent / "control-plane.json"
    cp.write_text(json.dumps({"host": host}), encoding="utf-8")


# ---------------------------------------------------------------------------
# Host capability predicate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "host,expected",
    [
        ("claude", True),
        ("codex", False),
        ("CODEX", False),
        ("  codex  ", False),
        ("", True),
        ("some-future-host", True),
    ],
)
def test_host_emits_web_tool_telemetry(host: str, expected: bool) -> None:
    """Only known non-emitting hosts relax; unknown names stay strict."""
    from lib_host import host_emits_web_tool_telemetry
    assert host_emits_web_tool_telemetry(host) is expected


def test_resolve_host_prefers_persisted_over_env(tmp_path: Path, monkeypatch) -> None:
    """A persisted host wins over env detection and reports its provenance."""
    from lib_host import persist_host, resolve_host
    monkeypatch.setenv("CODEX_PLUGIN_ROOT", "/somewhere")
    cp = tmp_path / "control-plane.json"
    persist_host(cp, "claude")
    assert resolve_host(cp) == ("claude", "control-plane")


def test_resolve_host_falls_back_to_env(tmp_path: Path, monkeypatch) -> None:
    """With no persisted anchor, env detection answers and says so."""
    from lib_host import resolve_host
    monkeypatch.setenv("CODEX_PLUGIN_ROOT", "/somewhere")
    assert resolve_host(tmp_path / "absent.json") == ("codex", "env")


# ---------------------------------------------------------------------------
# Strict path (claude) — unchanged behaviour
# ---------------------------------------------------------------------------

def test_claude_host_still_refuses_missing_log(tmp_path: Path) -> None:
    """The pre-existing hard failure is untouched on a telemetry host."""
    from ctl import _check_web_tool_evidence
    task_dir = _task_dir(tmp_path)
    _persist_host(task_dir, "claude")
    err = _check_web_tool_evidence(task_dir, _gate(), _receipt())
    assert err is not None
    assert "web-tool-log.jsonl not found" in err


def test_claude_host_still_refuses_entry_outside_window(tmp_path: Path) -> None:
    """A log entry predating the gate is not evidence on a telemetry host."""
    from ctl import _check_web_tool_evidence
    task_dir = _task_dir(tmp_path)
    _persist_host(task_dir, "claude")
    _write_log(task_dir, [{"ts": "2026-05-07T09:30:00Z", "tool": "WebSearch", "query": "q"}])
    err = _check_web_tool_evidence(task_dir, _gate(), _receipt())
    assert err is not None
    assert "0 qualifying web-tool entries in window" in err


def test_unknown_host_stays_strict(tmp_path: Path) -> None:
    """An unrecognised host name does not unlock the degraded path."""
    from ctl import _check_web_tool_evidence
    task_dir = _task_dir(tmp_path)
    _persist_host(task_dir, "some-future-host")
    err = _check_web_tool_evidence(task_dir, _gate(), _receipt())
    assert err is not None
    assert "web-tool-log.jsonl not found" in err


# ---------------------------------------------------------------------------
# Degraded path (codex)
# ---------------------------------------------------------------------------

def test_codex_host_passes_without_log(tmp_path: Path, capsys) -> None:
    """The blocker case: no log, and the spec may still advance on Codex."""
    from ctl import _check_web_tool_evidence
    task_dir = _task_dir(tmp_path)
    _persist_host(task_dir, "codex")
    assert _check_web_tool_evidence(task_dir, _gate(), _receipt()) is None


def test_codex_degrade_is_announced_on_stdout(tmp_path: Path, capsys) -> None:
    """Degrading is never silent — it prints a [GATE] line."""
    from ctl import _check_web_tool_evidence
    task_dir = _task_dir(tmp_path)
    _persist_host(task_dir, "codex")
    _check_web_tool_evidence(task_dir, _gate(), _receipt())
    out = capsys.readouterr().out
    assert "[GATE] external-solution-cross-check degraded" in out
    assert "codex" in out
    assert "control-plane" in out


def test_codex_degrade_emits_event(tmp_path: Path) -> None:
    """The degrade lands in events.jsonl so an auditor can see it later."""
    from ctl import _check_web_tool_evidence
    task_dir = _task_dir(tmp_path)
    _persist_host(task_dir, "codex")
    _check_web_tool_evidence(task_dir, _gate(), _receipt())
    events_path = task_dir / "events.jsonl"
    assert events_path.exists(), "degraded cross-check must leave an event trail"
    events = [json.loads(ln) for ln in events_path.read_text().splitlines() if ln.strip()]
    degraded = [e for e in events if e.get("event") == "web_tool_evidence_degraded"]
    assert len(degraded) == 1, f"expected one degrade event, got {events}"
    assert degraded[0]["host"] == "codex"
    assert degraded[0]["host_source"] == "control-plane"


def test_codex_host_refuses_receipt_predating_gate(tmp_path: Path) -> None:
    """Degraded does not mean unconditional: ordering still has to hold."""
    from ctl import _check_web_tool_evidence
    task_dir = _task_dir(tmp_path)
    _persist_host(task_dir, "codex")
    err = _check_web_tool_evidence(task_dir, _gate(), _receipt(ts=EARLY_RECEIPT_TS))
    assert err is not None
    assert "predates gate.written_at" in err


def test_codex_host_refuses_receipt_without_timestamp(tmp_path: Path) -> None:
    """With no log and no parseable receipt ts, there is no evidence at all."""
    from ctl import _check_web_tool_evidence
    task_dir = _task_dir(tmp_path)
    _persist_host(task_dir, "codex")
    err = _check_web_tool_evidence(task_dir, _gate(), {"step": "search-conducted"})
    assert err is not None
    assert "only ordering evidence" in err


def test_codex_host_with_real_log_entry_passes_at_full_strength(
    tmp_path: Path, capsys
) -> None:
    """A qualifying entry satisfies the check outright — no degrade needed."""
    from ctl import _check_web_tool_evidence
    task_dir = _task_dir(tmp_path)
    _persist_host(task_dir, "codex")
    _write_log(task_dir, [{"ts": ENTRY_TS, "tool": "WebSearch", "query": "q"}])
    assert _check_web_tool_evidence(task_dir, _gate(), _receipt()) is None
    assert "degraded" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Bypass defense: env is not enough to relax the check
# ---------------------------------------------------------------------------

def test_env_codex_cannot_override_persisted_claude(
    tmp_path: Path, monkeypatch
) -> None:
    """CODEX_PLUGIN_ROOT on a Bash call must not unlock the degraded path
    when the hook-written anchor says this is a Claude session."""
    from ctl import _check_web_tool_evidence
    task_dir = _task_dir(tmp_path)
    _persist_host(task_dir, "claude")
    monkeypatch.setenv("CODEX_PLUGIN_ROOT", "/home/attacker/plugin")
    err = _check_web_tool_evidence(task_dir, _gate(), _receipt())
    assert err is not None, "env alone must not relax the evidence check"


def test_env_provenance_is_recorded_when_no_anchor_exists(
    tmp_path: Path, monkeypatch
) -> None:
    """Without an anchor the env signal is used, but the event says so."""
    from ctl import _check_web_tool_evidence
    task_dir = _task_dir(tmp_path)
    monkeypatch.setenv("CODEX_PLUGIN_ROOT", "/home/user/dynos-work")
    monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "dynos-home"))
    assert _check_web_tool_evidence(task_dir, _gate(), _receipt()) is None
    events = [
        json.loads(ln)
        for ln in (task_dir / "events.jsonl").read_text().splitlines()
        if ln.strip()
    ]
    degraded = [e for e in events if e.get("event") == "web_tool_evidence_degraded"]
    assert degraded and degraded[0]["host_source"] == "env"


# ---------------------------------------------------------------------------
# Hook-side: non-Claude tool spellings produce canonical entries
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "spelling,canonical",
    [
        ("WebSearch", "WebSearch"),
        ("web_search", "WebSearch"),
        ("web.search", "WebSearch"),
        ("WebFetch", "WebFetch"),
        ("web_fetch", "WebFetch"),
    ],
)
def test_web_tool_log_normalises_tool_names(
    tmp_path: Path, monkeypatch, spelling: str, canonical: str
) -> None:
    """A harness that fires this hook under its own tool name still writes the
    canonical entry the gate reads, so it is enforced at full strength."""
    import web_tool_log
    task_dir = _task_dir(tmp_path)
    (task_dir / "manifest.json").write_text(
        json.dumps({"stage": "SPEC_NORMALIZATION"}), encoding="utf-8"
    )
    monkeypatch.setenv("DYNOS_TASK_DIR", str(task_dir))
    payload = {
        "tool_name": spelling,
        "tool_input": {"query": "q", "url": "https://example.com"},
        "cwd": str(tmp_path),
    }
    assert web_tool_log.main(["web_tool_log.py"], payload=payload) == 0
    entries = [
        json.loads(ln)
        for ln in (task_dir / "web-tool-log.jsonl").read_text().splitlines()
        if ln.strip()
    ]
    assert [e["tool"] for e in entries] == [canonical]


def test_web_tool_log_ignores_unrelated_tools(tmp_path: Path, monkeypatch) -> None:
    """Normalisation does not widen what counts as a web tool."""
    import web_tool_log
    task_dir = _task_dir(tmp_path)
    (task_dir / "manifest.json").write_text(
        json.dumps({"stage": "SPEC_NORMALIZATION"}), encoding="utf-8"
    )
    monkeypatch.setenv("DYNOS_TASK_DIR", str(task_dir))
    payload = {"tool_name": "Read", "tool_input": {}, "cwd": str(tmp_path)}
    assert web_tool_log.main(["web_tool_log.py"], payload=payload) == 0
    assert not (task_dir / "web-tool-log.jsonl").exists()


# ---------------------------------------------------------------------------
# The anchor the defense depends on must actually be written
# ---------------------------------------------------------------------------

def test_session_start_persists_host_anchor() -> None:
    """lib_host.persist_host had zero callers, so get_persisted_host always
    returned None and every reader silently fell back to env. The SessionStart
    hook is the documented write path — keep it wired."""
    text = (ROOT / "hooks" / "session-start").read_text(encoding="utf-8")
    assert "persist_host" in text, (
        "hooks/session-start must persist the detected host to "
        "control-plane.json; the gate's bypass defense reads that anchor"
    )
