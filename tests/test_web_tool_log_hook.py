"""TDD-first tests for the web-tool-log PostToolUse hook (task-20260507-005).

The hook captures every harness-level WebSearch/WebFetch invocation into a
per-task web-tool-log.jsonl file that the LLM cannot forge. This mirrors the
agent_spawn_log.py trust-substrate pattern.

AC coverage:
  AC 1 — hooks/web_tool_log.py exists with correct structure
  AC 3 — Each JSONL line has ts, tool, query/url fields; fallback for missing keys
  AC 4 — write_policy.decide_write denies all agent roles for web-tool-log.jsonl

These tests are RED until hooks/web_tool_log.py is created.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))


def _make_task_dir(tmp_path: Path) -> Path:
    """Create a minimal .dynos/task-* directory tree."""
    task_dir = tmp_path / ".dynos" / "task-20260507-005"
    task_dir.mkdir(parents=True)
    (task_dir / "manifest.json").write_text(json.dumps({"task_id": task_dir.name}))
    return task_dir


def _websearch_payload(query: str = "test query") -> dict:
    return {
        "tool_name": "WebSearch",
        "tool_input": {"query": query},
        "cwd": str(ROOT),
        "session_id": "sess-abc",
    }


def _webfetch_payload(url: str = "https://example.com") -> dict:
    return {
        "tool_name": "WebFetch",
        "tool_input": {"url": url},
        "cwd": str(ROOT),
        "session_id": "sess-abc",
    }


def _import_hook():
    """Import hooks.web_tool_log (fails if not yet created — RED state)."""
    import hooks.web_tool_log as m
    return m


# ---------------------------------------------------------------------------
# AC 1 + AC 3: Hook writes correct JSONL entries
# ---------------------------------------------------------------------------

def test_web_tool_log_hook_writes_websearch_entry(tmp_path: Path) -> None:
    """AC 1, 3: WebSearch payload produces a JSONL line with ts, tool, query."""
    task_dir = _make_task_dir(tmp_path)
    mod = _import_hook()
    payload = _websearch_payload("circuit breaker pattern")
    env_patch = {"DYNOS_TASK_DIR": str(task_dir)}
    with mock.patch.dict(os.environ, env_patch, clear=False):
        rc = mod.main(["web_tool_log.py"], payload=payload)
    assert rc == 0
    log_path = task_dir / "web-tool-log.jsonl"
    assert log_path.exists(), "web-tool-log.jsonl must be created"
    lines = [ln for ln in log_path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["tool"] == "WebSearch"
    assert entry["query"] == "circuit breaker pattern"
    assert isinstance(entry["ts"], str) and len(entry["ts"]) > 0


def test_web_tool_log_hook_writes_webfetch_entry(tmp_path: Path) -> None:
    """AC 1, 3: WebFetch payload produces a JSONL line with ts, tool, url."""
    task_dir = _make_task_dir(tmp_path)
    mod = _import_hook()
    payload = _webfetch_payload("https://example.com/docs")
    env_patch = {"DYNOS_TASK_DIR": str(task_dir)}
    with mock.patch.dict(os.environ, env_patch, clear=False):
        rc = mod.main(["web_tool_log.py"], payload=payload)
    assert rc == 0
    log_path = task_dir / "web-tool-log.jsonl"
    assert log_path.exists()
    entry = json.loads(log_path.read_text().strip())
    assert entry["tool"] == "WebFetch"
    assert entry["url"] == "https://example.com/docs"
    assert isinstance(entry["ts"], str) and len(entry["ts"]) > 0


def test_web_tool_log_hook_captures_raw_fallback_when_query_missing(tmp_path: Path) -> None:
    """AC 3: When tool_input lacks 'query', fallback to raw tool_input JSON string."""
    task_dir = _make_task_dir(tmp_path)
    mod = _import_hook()
    payload = {
        "tool_name": "WebSearch",
        "tool_input": {"some_other_key": "value"},
        "cwd": str(ROOT),
    }
    env_patch = {"DYNOS_TASK_DIR": str(task_dir)}
    with mock.patch.dict(os.environ, env_patch, clear=False):
        rc = mod.main(["web_tool_log.py"], payload=payload)
    assert rc == 0
    log_path = task_dir / "web-tool-log.jsonl"
    entry = json.loads(log_path.read_text().strip())
    assert entry["tool"] == "WebSearch"
    # Fallback: query field is a non-empty string (the serialized tool_input dict)
    assert "query" in entry
    assert isinstance(entry["query"], str)
    assert len(entry["query"]) > 0


def test_web_tool_log_hook_captures_raw_fallback_when_query_non_string(tmp_path: Path) -> None:
    """AC 3: When tool_input.query is not a string, fallback fires."""
    task_dir = _make_task_dir(tmp_path)
    mod = _import_hook()
    payload = {
        "tool_name": "WebSearch",
        "tool_input": {"query": 42},  # integer, not string
        "cwd": str(ROOT),
    }
    env_patch = {"DYNOS_TASK_DIR": str(task_dir)}
    with mock.patch.dict(os.environ, env_patch, clear=False):
        rc = mod.main(["web_tool_log.py"], payload=payload)
    assert rc == 0
    entry = json.loads((task_dir / "web-tool-log.jsonl").read_text().strip())
    assert entry["tool"] == "WebSearch"
    # Fallback: query is the raw tool_input serialized
    assert isinstance(entry["query"], str)
    assert len(entry["query"]) > 0
    # Should not just be "42" directly — it's the whole tool_input dict as JSON
    parsed_fallback = json.loads(entry["query"])
    assert parsed_fallback == {"query": 42}


def test_web_tool_log_hook_writes_on_tool_failure(tmp_path: Path) -> None:
    """AC 3 (implicit): PostToolUse fires on tool error; entry still written."""
    task_dir = _make_task_dir(tmp_path)
    mod = _import_hook()
    payload = {
        "tool_name": "WebSearch",
        "tool_input": {"query": "error query"},
        "cwd": str(ROOT),
        "tool_response": {"error": "network timeout", "type": "error"},
    }
    env_patch = {"DYNOS_TASK_DIR": str(task_dir)}
    with mock.patch.dict(os.environ, env_patch, clear=False):
        rc = mod.main(["web_tool_log.py"], payload=payload)
    assert rc == 0
    log_path = task_dir / "web-tool-log.jsonl"
    assert log_path.exists(), "Entry must be written even when tool errored"
    entry = json.loads(log_path.read_text().strip())
    assert entry["tool"] == "WebSearch"
    assert entry["query"] == "error query"


def test_web_tool_log_hook_ignores_non_web_tool(tmp_path: Path) -> None:
    """AC 1: Non-WebSearch/WebFetch tool_name produces no log line."""
    task_dir = _make_task_dir(tmp_path)
    mod = _import_hook()
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "cwd": str(ROOT),
    }
    env_patch = {"DYNOS_TASK_DIR": str(task_dir)}
    with mock.patch.dict(os.environ, env_patch, clear=False):
        rc = mod.main(["web_tool_log.py"], payload=payload)
    assert rc == 0
    assert not (task_dir / "web-tool-log.jsonl").exists(), (
        "web-tool-log.jsonl must not be created for non-web tool invocations"
    )


def test_web_tool_log_hook_silent_exit_when_no_task_dir(tmp_path: Path) -> None:
    """AC 1: When no task dir is discoverable, hook exits 0 silently, no file created."""
    mod = _import_hook()
    payload = {
        "tool_name": "WebSearch",
        "tool_input": {"query": "something"},
        "cwd": str(tmp_path),  # tmp_path has no .dynos subdir
    }
    # Clear DYNOS_TASK_DIR so ancestor walk is used
    env_no_task = {k: v for k, v in os.environ.items() if k != "DYNOS_TASK_DIR"}
    with mock.patch.dict(os.environ, env_no_task, clear=True):
        rc = mod.main(["web_tool_log.py"], payload=payload)
    assert rc == 0
    # No file should be created anywhere under tmp_path
    assert not list(tmp_path.rglob("web-tool-log.jsonl"))


def test_web_tool_log_hook_appends_multiple_entries(tmp_path: Path) -> None:
    """AC 3: Multiple calls append multiple lines (one per invocation)."""
    task_dir = _make_task_dir(tmp_path)
    mod = _import_hook()
    env_patch = {"DYNOS_TASK_DIR": str(task_dir)}
    with mock.patch.dict(os.environ, env_patch, clear=False):
        mod.main(["web_tool_log.py"], payload=_websearch_payload("query one"))
        mod.main(["web_tool_log.py"], payload=_webfetch_payload("https://example.com/one"))
    log_path = task_dir / "web-tool-log.jsonl"
    lines = [ln for ln in log_path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 2
    entries = [json.loads(ln) for ln in lines]
    assert entries[0]["tool"] == "WebSearch"
    assert entries[1]["tool"] == "WebFetch"


def test_web_tool_log_hook_ts_is_iso8601_utc(tmp_path: Path) -> None:
    """AC 3: ts field is ISO 8601 UTC format."""
    from datetime import datetime, timezone
    task_dir = _make_task_dir(tmp_path)
    mod = _import_hook()
    env_patch = {"DYNOS_TASK_DIR": str(task_dir)}
    with mock.patch.dict(os.environ, env_patch, clear=False):
        mod.main(["web_tool_log.py"], payload=_websearch_payload("ts check"))
    entry = json.loads((task_dir / "web-tool-log.jsonl").read_text().strip())
    ts = entry["ts"]
    # Must parse as UTC ISO 8601 — Z suffix or +00:00
    parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None


# ---------------------------------------------------------------------------
# AC 4: write_policy denies agent roles from writing web-tool-log.jsonl
# ---------------------------------------------------------------------------

def test_write_policy_denies_web_tool_log_for_executors(tmp_path: Path) -> None:
    """AC 4: execute-inline role is denied write access to web-tool-log.jsonl."""
    from write_policy import WriteAttempt, decide_write
    task_dir = _make_task_dir(tmp_path)
    decision = decide_write(
        WriteAttempt(
            role="execute-inline",
            task_dir=task_dir,
            path=task_dir / "web-tool-log.jsonl",
            operation="create",
            source="agent",
        )
    )
    assert decision.allowed is False
    assert decision.mode == "deny"
    assert "web-tool-log.jsonl" in decision.reason or "hook-owned" in decision.reason


def test_write_policy_denies_web_tool_log_for_receipt_writer(tmp_path: Path) -> None:
    """AC 4: receipt-writer role is denied write access to web-tool-log.jsonl."""
    from write_policy import WriteAttempt, decide_write
    task_dir = _make_task_dir(tmp_path)
    decision = decide_write(
        WriteAttempt(
            role="receipt-writer",
            task_dir=task_dir,
            path=task_dir / "web-tool-log.jsonl",
            operation="create",
            source="receipt-writer",
        )
    )
    assert decision.allowed is False
    assert decision.mode == "deny"


def test_write_policy_denies_web_tool_log_for_ctl(tmp_path: Path) -> None:
    """AC 4: ctl role is denied write access to web-tool-log.jsonl."""
    from write_policy import WriteAttempt, decide_write
    task_dir = _make_task_dir(tmp_path)
    decision = decide_write(
        WriteAttempt(
            role="ctl",
            task_dir=task_dir,
            path=task_dir / "web-tool-log.jsonl",
            operation="create",
            source="ctl",
        )
    )
    assert decision.allowed is False
    assert decision.mode == "deny"


def test_write_policy_denies_web_tool_log_for_planning(tmp_path: Path) -> None:
    """AC 4: planning role is denied write access to web-tool-log.jsonl."""
    from write_policy import WriteAttempt, decide_write
    task_dir = _make_task_dir(tmp_path)
    decision = decide_write(
        WriteAttempt(
            role="planning",
            task_dir=task_dir,
            path=task_dir / "web-tool-log.jsonl",
            operation="create",
            source="agent",
        )
    )
    assert decision.allowed is False
    assert decision.mode == "deny"
