#!/usr/bin/env python3
"""PreToolUse + PostToolUse hook for the `Agent` tool.

Captures harness-level evidence of every subagent spawn and return, so the
audit-receipt write step can mechanically reconcile orchestrator-claimed
spawns against actual spawns. Without this, the orchestrator can claim
``model: "haiku"`` for an auditor that never spawned and the receipt chain  # noqa: model-literal
has no way to detect the forgery (this is the audit-chain forgery incident
documented at memory/project_audit_forgery_incident.md).

Invocation:
    python3 hooks/agent_spawn_log.py <phase>

  where ``<phase>`` is ``pre`` (PreToolUse) or ``post`` (PostToolUse).

Stdin: standard Claude Code hook payload — JSON with ``tool_name``,
``tool_input``, ``cwd``, and (post only) ``tool_response``.

Output: appends a JSONL line to
``<task_dir>/spawn-log.jsonl`` where ``<task_dir>`` is resolved from
``DYNOS_TASK_DIR`` env or by walking ``cwd`` upward to the nearest
``.dynos/task-*`` directory. If no task dir is discoverable, the hook
exits 0 silently (Agent calls outside dynos-work tasks are not audited).

The spawn-log itself is hook-owned: ``write_policy.decide_write`` denies
direct orchestrator writes to it, so the only path that creates entries
is this hook subprocess. The orchestrator can read the log but cannot
forge entries.

Exit codes:
    0 — success, or no active task (silent skip)
    1 — internal error (malformed stdin, unknown phase, write failure)
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

_VALID_PHASES = ("pre", "post")
_EXCERPT_MAX_CHARS = 500


def _find_task_dir_from_ancestors(cwd: Path) -> Path | None:
    """Walk upward from cwd to the nearest .dynos/task-* directory."""
    current = cwd.resolve()
    for ancestor in [current, *current.parents]:
        dynos = ancestor / ".dynos"
        if dynos.is_dir():
            try:
                candidates = sorted(dynos.glob("task-*"), reverse=True)
                if candidates:
                    return candidates[0]
            except OSError:
                pass
    return None


def _resolve_task_dir(cwd: Path) -> Path | None:
    """Resolve task dir from DYNOS_TASK_DIR env or by ancestor walk."""
    env_task = os.environ.get("DYNOS_TASK_DIR", "").strip()
    if env_task:
        try:
            return Path(env_task).resolve()
        except OSError:
            return None
    return _find_task_dir_from_ancestors(cwd)


def _sha256_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _excerpt(text: str, max_chars: int = _EXCERPT_MAX_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "...[truncated]"


def _extract_prompt(tool_input: dict[str, Any]) -> str:
    """Best-effort prompt extraction. Agent tool puts it under 'prompt'."""
    val = tool_input.get("prompt", "")
    return val if isinstance(val, str) else json.dumps(val, sort_keys=True)


def _extract_response_content(tool_response: Any) -> tuple[str, str | None]:
    """Return (content_text, stop_reason). Tolerates string or dict shapes."""
    if isinstance(tool_response, str):
        return tool_response, None
    if not isinstance(tool_response, dict):
        return "", None
    content = tool_response.get("content", "")
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                t = block.get("text", "")
                if isinstance(t, str):
                    parts.append(t)
            elif isinstance(block, str):
                parts.append(block)
        content_text = "\n".join(parts)
    elif isinstance(content, str):
        content_text = content
    else:
        content_text = json.dumps(content, sort_keys=True)
    stop_reason = tool_response.get("stop_reason")
    if not isinstance(stop_reason, str):
        stop_reason = None
    return content_text, stop_reason


def _build_pre_entry(payload: dict[str, Any]) -> dict[str, Any]:
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    prompt_text = _extract_prompt(tool_input)
    return {
        "phase": "pre",
        "tool": "Agent",
        "subagent_type": tool_input.get("subagent_type") or "unknown",
        "description": (tool_input.get("description") or "")[:200],
        "prompt_sha256": _sha256_of(prompt_text),
        "prompt_length": len(prompt_text),
        "timestamp": _utc_iso8601(),
        "session_id": payload.get("session_id") or "",
    }


def _build_post_entry(payload: dict[str, Any]) -> dict[str, Any]:
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    prompt_text = _extract_prompt(tool_input)
    response_content, stop_reason = _extract_response_content(payload.get("tool_response"))
    entry: dict[str, Any] = {
        "phase": "post",
        "tool": "Agent",
        "subagent_type": tool_input.get("subagent_type") or "unknown",
        "description": (tool_input.get("description") or "")[:200],
        "prompt_sha256": _sha256_of(prompt_text),
        "result_sha256": _sha256_of(response_content),
        "result_length": len(response_content),
        "result_excerpt": _excerpt(response_content),
        "stop_reason": stop_reason,
        "timestamp": _utc_iso8601(),
        "session_id": payload.get("session_id") or "",
    }
    if stop_reason == "max_tokens":
        entry["truncated"] = True
    return entry


def _utc_iso8601() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _append_jsonl(path: Path, entry: dict[str, Any]) -> None:
    """Atomically append a JSONL line. Creates parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in _VALID_PHASES:
        print(
            f"agent-spawn-log: phase must be one of {_VALID_PHASES}; got {argv[1:]!r}",
            file=sys.stderr,
        )
        return 1
    phase = argv[1]

    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        print(f"agent-spawn-log: malformed stdin: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"agent-spawn-log: failed to read stdin: {exc}", file=sys.stderr)
        return 1

    if not isinstance(payload, dict):
        print("agent-spawn-log: stdin payload must be a JSON object", file=sys.stderr)
        return 1

    if payload.get("tool_name") != "Agent":
        return 0

    cwd_str = payload.get("cwd") or os.getcwd()
    try:
        cwd = Path(cwd_str).resolve()
    except OSError:
        return 0

    task_dir = _resolve_task_dir(cwd)
    if task_dir is None or not task_dir.is_dir():
        return 0

    try:
        if phase == "pre":
            entry = _build_pre_entry(payload)
        else:
            entry = _build_post_entry(payload)
    except Exception as exc:
        print(f"agent-spawn-log: failed to build entry: {exc}", file=sys.stderr)
        return 1

    log_path = task_dir / "spawn-log.jsonl"
    try:
        _append_jsonl(log_path, entry)
    except OSError as exc:
        print(f"agent-spawn-log: failed to append {log_path}: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except SystemExit:
        raise
    except Exception as exc:
        print(f"agent-spawn-log: internal error: {exc}", file=sys.stderr)
        sys.exit(1)
