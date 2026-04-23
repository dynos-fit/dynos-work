#!/usr/bin/env python3
"""PreToolUse hook for dynos-work write-policy enforcement.

Reads a JSON payload from stdin, extracts tool_name, tool_input, and cwd,
resolves the DYNOS_ROLE and DYNOS_TASK_DIR from the environment, then
calls decide_write() to enforce the write boundary.

Exit codes:
    0 -- write allowed
    1 -- internal error (never a silent allow)
    2 -- write denied (deny-and-continue semantics)

Stderr on denial is prefixed with "write-policy: ".
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


def _find_task_dir_from_ancestors(cwd: Path) -> Path | None:
    """Walk upward from cwd, looking for the nearest .dynos/task-* directory."""
    current = cwd.resolve()
    for ancestor in [current, *current.parents]:
        dynos = ancestor / ".dynos"
        if dynos.is_dir():
            try:
                candidates = sorted(dynos.glob("task-*"), reverse=True)
                if candidates:
                    return candidates[0]
            except Exception:
                pass
    return None


# ---------------------------------------------------------------------------
# Bash pre-filter: commands that could write files
# ---------------------------------------------------------------------------
# These patterns detect shell commands that write to the filesystem.
# Group index indicates which capture group holds the destination path.
_BASH_WRITE_PATTERNS: list[tuple[re.Pattern[str], int]] = [
    # redirection: > file or >> file  (looks for > after optional command text)
    (re.compile(r">>?\s*([^\s;|&>]+)"), 1),
    # tee: tee [-a] file
    (re.compile(r"\btee\s+(?:-a\s+)?([^\s;|&]+)"), 1),
    # mv: mv src dst
    (re.compile(r"\bmv\s+\S+\s+([^\s;|&]+)"), 1),
    # cp: cp [-rRp...] src dst
    (re.compile(r"\bcp\s+(?:-[a-zA-Z]+\s+)?(?:\S+\s+)+([^\s;|&]+)"), 1),
    # rsync: rsync [opts...] src dst
    (re.compile(r"\brsync\s+(?:\S+\s+)+([^\s;|&]+)"), 1),
    # install: install [-m MODE] [opts] src dst
    (re.compile(r"\binstall\s+(?:-[a-zA-Z]+\s+(?:\S+\s+)?)?(?:\S+\s+)+([^\s;|&]+)"), 1),
]


def _extract_bash_destinations(command: str) -> list[str]:
    """Return a list of write destination paths detected in a Bash command string."""
    destinations: list[str] = []
    seen: set[str] = set()
    for pattern, group in _BASH_WRITE_PATTERNS:
        for match in pattern.finditer(command):
            dest = match.group(group).strip().rstrip(";|&>")
            if dest and dest not in seen:
                seen.add(dest)
                destinations.append(dest)
    return destinations


def _resolve_path(raw: str, cwd: Path) -> Path:
    """Resolve a path string relative to cwd."""
    p = Path(raw)
    if not p.is_absolute():
        p = cwd / p
    return p.resolve()


def main() -> int:
    """Main entry point. Returns exit code."""
    # Add hooks directory to sys.path so we can import write_policy and lib_log
    script_dir = Path(__file__).resolve().parent
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))

    # Parse stdin JSON -- any failure here is an internal error (exit 1)
    try:
        raw_stdin = sys.stdin.read()
        payload = json.loads(raw_stdin)
    except json.JSONDecodeError as exc:
        print(f"pre-tool-use: malformed stdin (not valid JSON): {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"pre-tool-use: failed to read stdin: {exc}", file=sys.stderr)
        return 1

    # Extract required fields -- missing fields are an internal error (exit 1)
    try:
        tool_name = payload["tool_name"]
        tool_input = payload["tool_input"]
        cwd_str = payload.get("cwd", os.getcwd())
        cwd = Path(cwd_str).resolve()
    except KeyError as exc:
        print(f"pre-tool-use: missing required field in payload: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"pre-tool-use: failed to parse payload fields: {exc}", file=sys.stderr)
        return 1

    # Import write_policy -- failure is an internal error (exit 1)
    try:
        from write_policy import WriteAttempt, WriteDecision, decide_write, _emit_policy_event  # noqa: F401
    except ImportError as exc:
        print(f"pre-tool-use: cannot import write_policy: {exc}", file=sys.stderr)
        return 1

    # Resolve role from environment
    role_from_env = os.environ.get("DYNOS_ROLE", "").strip()

    # Resolve task_dir from environment, falling back to ancestor discovery
    task_dir_from_env = os.environ.get("DYNOS_TASK_DIR", "").strip()
    if task_dir_from_env:
        task_dir: Path | None = Path(task_dir_from_env).resolve()
    else:
        task_dir = _find_task_dir_from_ancestors(cwd)

    role_missing = not role_from_env
    if role_missing:
        role = "execute-inline"
    else:
        role = role_from_env

    # Emit pre_tool_use_role_missing event if role was not set in the environment
    if role_missing:
        if tool_name == "Bash":
            raw_cmd = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
            missing_event_path = str(raw_cmd)[:120]
        else:
            if isinstance(tool_input, dict):
                missing_event_path = tool_input.get("file_path", tool_input.get("path", ""))
            else:
                missing_event_path = ""
        try:
            from lib_log import log_event
            if task_dir is not None:
                root = task_dir.parent.parent
                log_event(
                    root,
                    "pre_tool_use_role_missing",
                    task=task_dir.name,
                    tool_name=tool_name,
                    resolved_path=str(missing_event_path),
                    fallback_role=role,
                )
        except Exception:
            # Event logging failure is NOT a fatal internal error -- proceed
            pass

    # -----------------------------------------------------------------
    # Handle Bash tool: apply regex pre-filter on command
    # -----------------------------------------------------------------
    if tool_name == "Bash":
        command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
        if not isinstance(command, str):
            print("pre-tool-use: Bash tool_input.command is not a string", file=sys.stderr)
            return 1

        destinations = _extract_bash_destinations(command)

        if not destinations:
            # No write pattern matched -- pass without policy check
            return 0

        # Check each destination through decide_write
        for raw_dest in destinations:
            dest_path = _resolve_path(raw_dest, cwd)
            attempt = WriteAttempt(
                role=role,
                task_dir=task_dir,
                path=dest_path,
                operation="modify",
                source="agent",
            )
            decision = decide_write(attempt)
            _emit_policy_event(attempt, decision)

            # Emit structured bash_check event
            try:
                from lib_log import log_event
                if task_dir is not None:
                    root = task_dir.parent.parent
                    log_event(
                        root,
                        "pre_tool_use_bash_check",
                        task=task_dir.name,
                        command=command[:120],
                        destination=str(dest_path),
                        role=role,
                        allowed=decision.allowed,
                        reason=decision.reason,
                    )
            except Exception:
                pass

            if not decision.allowed:
                print(f"write-policy: {decision.reason}", file=sys.stderr)
                return 2

        return 0

    # -----------------------------------------------------------------
    # Handle Write, Edit, MultiEdit tools: extract the target file path
    # -----------------------------------------------------------------
    if tool_name in ("Write", "Edit", "MultiEdit"):
        if tool_name == "MultiEdit":
            edits = tool_input.get("edits", []) if isinstance(tool_input, dict) else []
            if not isinstance(edits, list):
                print("pre-tool-use: MultiEdit edits is not a list", file=sys.stderr)
                return 1
            paths_to_check: list[Path] = []
            for edit in edits:
                fp = edit.get("file_path", "") if isinstance(edit, dict) else ""
                if fp:
                    paths_to_check.append(_resolve_path(fp, cwd))
            if not paths_to_check:
                print("pre-tool-use: MultiEdit has no edits with file_path", file=sys.stderr)
                return 1
        else:
            if not isinstance(tool_input, dict):
                print("pre-tool-use: tool_input is not a dict", file=sys.stderr)
                return 1
            raw_path = tool_input.get("file_path", tool_input.get("path", ""))
            if not raw_path:
                print("pre-tool-use: no file_path found in tool_input", file=sys.stderr)
                return 1
            paths_to_check = [_resolve_path(raw_path, cwd)]

        for target_path in paths_to_check:
            attempt = WriteAttempt(
                role=role,
                task_dir=task_dir,
                path=target_path,
                operation="modify",
                source="agent",
            )
            decision = decide_write(attempt)
            _emit_policy_event(attempt, decision)

            if not decision.allowed:
                print(f"write-policy: {decision.reason}", file=sys.stderr)
                return 2

        return 0

    # Unknown tool name -- pass without policy check
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:
        print(f"pre-tool-use: internal error: {exc}", file=sys.stderr)
        sys.exit(1)
