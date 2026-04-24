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

# Allowlist of valid executor role names that may appear in active-segment-role.
# Privileged internal roles (ctl, receipt-writer, eventbus, scheduler, system)
# are intentionally excluded to prevent role-file injection attacks.
_EXECUTOR_ROLE_ALLOWLIST: frozenset[str] = frozenset({
    "backend-executor", "ui-executor", "testing-executor", "integration-executor",
    "ml-executor", "db-executor", "refactor-executor", "docs-executor",
    "planning", "execute-inline", "repair-coordinator",
    "audit-spec-completion", "audit-security", "audit-code-quality",
    "audit-performance", "audit-dead-code", "audit-db-schema", "audit-ui",
})

# ---------------------------------------------------------------------------
# Module-level imports for write_policy and lib_log.
# These are resolved lazily at first-use inside main() so the hook can still
# run when the hooks directory is not yet on sys.path (the path is added at
# the start of main()). The module-level names are set to None initially and
# replaced with real objects after the import succeeds. Tests can patch these
# names to intercept calls.
# ---------------------------------------------------------------------------
decide_write = None  # type: ignore[assignment]
_emit_policy_event = None  # type: ignore[assignment]
WriteAttempt = None  # type: ignore[assignment]
log_event = None  # type: ignore[assignment]


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
    global decide_write, _emit_policy_event, WriteAttempt, log_event

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
    # decide_write may be pre-populated by tests via module-level patching;
    # WriteAttempt and _emit_policy_event are always resolved from write_policy
    # because they are not independently patchable by the test suite.
    try:
        import write_policy as _wp
        if decide_write is None:
            decide_write = _wp.decide_write
        if _emit_policy_event is None:
            _emit_policy_event = _wp._emit_policy_event
        if WriteAttempt is None:
            WriteAttempt = _wp.WriteAttempt
    except ImportError as exc:
        # Only fatal if decide_write itself is not yet available
        if decide_write is None:
            print(f"pre-tool-use: cannot import write_policy: {exc}", file=sys.stderr)
            return 1
        # If decide_write was pre-populated (test context), proceed without
        # write_policy — WriteAttempt and _emit_policy_event may be None

    # Import lib_log -- failure is non-fatal (event logging is optional)
    if log_event is None:
        try:
            import lib_log as _ll
            log_event = _ll.log_event
        except ImportError:
            pass

    # Resolve role from environment
    role_from_env = os.environ.get("DYNOS_ROLE", "").strip()

    # Resolve task_dir from environment, falling back to ancestor discovery
    task_dir_from_env = os.environ.get("DYNOS_TASK_DIR", "").strip()
    if task_dir_from_env:
        task_dir: Path | None = Path(task_dir_from_env).resolve()
    else:
        task_dir = _find_task_dir_from_ancestors(cwd)

    # 3-step role resolution chain:
    # (a) env var -- highest priority
    # (b) role file in task dir -- searched when DYNOS_TASK_DIR is set
    # (c) default "execute-inline"
    role_file_searched: bool = False
    role_file_path: Path | None = None
    role_file_reason: str | None = None  # "absent" | "empty" when falling to (c)

    if role_from_env:
        # Step (a): env var wins
        role = role_from_env
        role_missing = False
    else:
        # Step (b): check role file when DYNOS_TASK_DIR is set
        role = "execute-inline"
        role_missing = True

        if task_dir_from_env:
            resolved_task_dir = Path(task_dir_from_env).resolve()
            role_file_path = resolved_task_dir / "active-segment-role"
            role_file_searched = True
            try:
                if role_file_path.is_file():
                    file_contents = role_file_path.read_text(encoding="utf-8").strip()
                    if file_contents and file_contents in _EXECUTOR_ROLE_ALLOWLIST:
                        # Step (b) resolved: use file contents, suppress role_missing
                        role = file_contents
                        role_missing = False
                    elif file_contents and file_contents not in _EXECUTOR_ROLE_ALLOWLIST:
                        # File has contents but value is not an allowed executor role --
                        # reject and fall to (c) to prevent privilege escalation via role file.
                        role_file_reason = "invalid"
                    else:
                        # File exists but empty/whitespace -- fall to (c)
                        role_file_reason = "empty"
                else:
                    # File absent -- fall to (c)
                    role_file_reason = "absent"
            except Exception:
                # File read failure is non-fatal -- treat as absent
                role_file_reason = "absent"

    # Emit pre_tool_use_role_missing event if role was not set in the environment
    # AND role was not resolved from the role file (step b)
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
            if log_event is not None and task_dir is not None:
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

    # Emit pre_tool_use_role_file_missing when resolution landed on (c) and
    # a role file was searched at (b) but was absent or empty
    if role_missing and role_file_searched and role_file_reason is not None:
        try:
            if log_event is not None:
                resolved_task_dir_str = str(Path(task_dir_from_env).resolve())
                log_event(
                    Path(task_dir_from_env).resolve().parent.parent,
                    "pre_tool_use_role_file_missing",
                    task_dir=resolved_task_dir_str,
                    path=str(role_file_path),
                    reason=role_file_reason,
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

        # Role authorization probe: when a task context is known, verify the
        # resolved role is authorized to operate in the cwd. This runs for ALL
        # Bash commands (not only ones with write destinations) so that:
        # (a) forbidden roles are caught before any side effects occur, and
        # (b) the resolved role is always observable via the decide_write call.
        # Note: _emit_policy_event is intentionally NOT called for this probe
        # to avoid duplicate events — the probe is a role-auth check, not a
        # write-destination check.
        if task_dir is not None and decide_write is not None and WriteAttempt is not None:
            probe_attempt = WriteAttempt(
                role=role,
                task_dir=task_dir,
                path=cwd,
                operation="modify",
                source="agent",
            )
            probe_decision = decide_write(probe_attempt)
            if not probe_decision.allowed:
                print(f"write-policy: {probe_decision.reason}", file=sys.stderr)
                return 2

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
                if log_event is not None and task_dir is not None:
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
