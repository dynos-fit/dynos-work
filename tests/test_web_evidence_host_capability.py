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
        ("", True),
        ("some-future-host", True),
    ],
)
def test_host_emits_web_tool_telemetry(host: str, expected: bool) -> None:
    """Claude Code emits WebSearch/WebFetch telemetry."""
    from lib_host import host_emits_web_tool_telemetry
    assert host_emits_web_tool_telemetry(host) is expected


def test_resolve_host_prefers_persisted_over_env(tmp_path: Path, monkeypatch) -> None:
    """A persisted host wins and reports its provenance."""
    from lib_host import persist_host, resolve_host
    cp = tmp_path / "control-plane.json"
    persist_host(cp, "claude")
    assert resolve_host(cp) == ("claude", "control-plane")


def test_resolve_host_falls_back_to_env(tmp_path: Path, monkeypatch) -> None:
    """With no persisted anchor, env detection answers claude and says so."""
    from lib_host import resolve_host
    assert resolve_host(tmp_path / "absent.json") == ("claude", "env")


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
    """SessionStart invokes session_start_hook.py which persists control-plane.json."""
    hook_text = (ROOT / "hooks" / "session-start").read_text(encoding="utf-8")
    py_text = (ROOT / "hooks" / "session_start_hook.py").read_text(encoding="utf-8")
    assert "session_start_hook.py" in hook_text
    assert "persist_host" in py_text
