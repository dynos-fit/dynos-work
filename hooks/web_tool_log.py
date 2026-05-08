#!/usr/bin/env python3
"""PostToolUse hook for the WebSearch and WebFetch tools.

Captures harness-level evidence of every WebSearch/WebFetch invocation to a
per-task web-tool-log.jsonl file. Because this hook runs as a subprocess and
writes via Python file I/O (not through any harness tool), the LLM cannot
forge entries — write_policy.decide_write explicitly denies all agent roles
from writing web-tool-log.jsonl.

This mirrors the agent_spawn_log.py trust-substrate pattern.

Invocation (two modes):

  1. CLI mode (shell dispatcher):
         python3 hooks/web_tool_log.py
     Reads JSON payload from stdin.

  2. Direct call (tests):
         mod.main(argv, payload=<dict>)
     Accepts payload directly; stdin is not read when payload is provided.

Stdin: standard Claude Code hook payload — JSON with ``tool_name``,
``tool_input``, ``cwd``, and (post only) ``tool_response``.

Output: appends a JSONL line to ``<task_dir>/web-tool-log.jsonl`` where
``<task_dir>`` is resolved from ``DYNOS_TASK_DIR`` env or by walking ``cwd``
upward to the nearest ``.dynos/task-*`` directory. If no task dir is
discoverable, the hook exits 0 silently.

Each JSONL line contains:
  ts   — ISO 8601 UTC timestamp
  tool — "WebSearch" or "WebFetch"
  query — (WebSearch) the search query string, or raw fallback
  url   — (WebFetch) the URL string, or raw fallback

Exit codes:
    0 — success, or no active task (silent skip), or non-web tool
    1 — internal error (malformed stdin, write failure)
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

_WEB_TOOLS = frozenset({"WebSearch", "WebFetch"})


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


def _utc_iso8601() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _append_jsonl(path: Path, entry: dict[str, Any]) -> None:
    """Atomically append a JSONL line. Creates parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)


def _build_entry(tool_name: str, tool_input: Any) -> dict[str, Any]:
    """Build a JSONL entry for a WebSearch or WebFetch invocation.

    AC 3: Each line has ts (ISO8601 UTC), tool, and either query (WebSearch)
    or url (WebFetch). Fallback: if tool_input.query/.url is absent or
    non-string, serialize tool_input as JSON string and store under the
    expected key.
    """
    if not isinstance(tool_input, dict):
        tool_input = {}

    entry: dict[str, Any] = {
        "ts": _utc_iso8601(),
        "tool": tool_name,
    }

    if tool_name == "WebSearch":
        raw = tool_input.get("query")
        if isinstance(raw, str):
            entry["query"] = raw
        else:
            # Fallback: serialize the entire tool_input as a JSON string
            entry["query"] = json.dumps(tool_input, sort_keys=True, ensure_ascii=False)
    elif tool_name == "WebFetch":
        raw = tool_input.get("url")
        if isinstance(raw, str):
            entry["url"] = raw
        else:
            # Fallback: serialize the entire tool_input as a JSON string
            entry["url"] = json.dumps(tool_input, sort_keys=True, ensure_ascii=False)

    return entry


def main(argv: list[str], *, payload: dict[str, Any] | None = None) -> int:
    """Main entry point.

    Args:
        argv: Command-line arguments (argv[0] is the script name).
        payload: If provided, use this dict directly instead of reading stdin.
                 This path is used by tests.

    Returns:
        0 on success or silent skip, 1 on internal error.
    """
    if payload is None:
        # CLI mode: read payload from stdin
        try:
            raw = sys.stdin.read()
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as exc:
            print(f"web-tool-log: malformed stdin: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:
            print(f"web-tool-log: failed to read stdin: {exc}", file=sys.stderr)
            return 1

    if not isinstance(payload, dict):
        print("web-tool-log: stdin payload must be a JSON object", file=sys.stderr)
        return 1

    tool_name = payload.get("tool_name")
    if tool_name not in _WEB_TOOLS:
        # Not a web tool — exit 0 silently (AC 1)
        return 0

    cwd_str = payload.get("cwd") or os.getcwd()
    try:
        cwd = Path(cwd_str).resolve()
    except OSError:
        return 0

    task_dir = _resolve_task_dir(cwd)
    if task_dir is None or not task_dir.is_dir():
        # No active task — exit 0 silently (AC 1)
        return 0

    tool_input = payload.get("tool_input") or {}
    try:
        entry = _build_entry(tool_name, tool_input)
    except Exception as exc:
        print(f"web-tool-log: failed to build entry: {exc}", file=sys.stderr)
        return 1

    log_path = task_dir / "web-tool-log.jsonl"
    try:
        _append_jsonl(log_path, entry)
    except OSError as exc:
        print(f"web-tool-log: failed to append {log_path}: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except SystemExit:
        raise
    except Exception as exc:
        print(f"web-tool-log: internal error: {exc}", file=sys.stderr)
        sys.exit(1)
