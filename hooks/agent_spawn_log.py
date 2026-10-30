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
from collections import Counter
import subprocess
from pathlib import Path
from typing import Any

_VALID_PHASES = ("pre", "post")
_EXCERPT_MAX_CHARS = 500


def _task_is_non_terminal(task_dir: Path) -> bool:
    try:
        manifest = json.loads((task_dir / "manifest.json").read_text(encoding="utf-8"))
        return isinstance(manifest, dict) and manifest.get("stage") not in {
            "DONE", "FAILED", "CANCELLED", "CALIBRATED",
        }
    except Exception:
        return False


def _task_dir_from_path(path: Path) -> Path | None:
    try:
        current = path.resolve()
    except Exception:
        return None
    for ancestor in [current, *current.parents]:
        if ancestor.parent.name == ".dynos" and ancestor.name.startswith("task-"):
            return ancestor if _task_is_non_terminal(ancestor) else None
    return None


def _project_root_from_ancestors(cwd: Path) -> Path | None:
    current = cwd.resolve()
    for ancestor in [current, *current.parents]:
        if (ancestor / ".dynos").is_dir():
            return ancestor
    return None


def _find_task_dir_from_ancestors(cwd: Path) -> Path | None:
    """Walk upward from cwd, returning only an unambiguous active task."""
    direct = _task_dir_from_path(cwd)
    if direct is not None:
        return direct
    project_root = _project_root_from_ancestors(cwd)
    if project_root is None:
        return None
    try:
        candidates = [
            task for task in sorted((project_root / ".dynos").glob("task-*"), reverse=True)
            if _task_is_non_terminal(task)
        ]
    except OSError:
        return None
    if len(candidates) == 1:
        return candidates[0]
    return None


def _find_task_dir_from_agent_payload(payload: dict[str, Any], cwd: Path) -> Path | None:
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return None
    for key in ("prompt", "description"):
        value = tool_input.get(key)
        if not isinstance(value, str):
            continue
        for token in value.replace("\n", " ").split():
            if ".dynos/" not in token or "/task-" not in token:
                continue
            cleaned = token.strip("`'\"(),[]{}<>")
            path = Path(cleaned)
            if not path.is_absolute():
                path = cwd / path
            task_dir = _task_dir_from_path(path)
            if task_dir is not None:
                return task_dir
    return None


def _resolve_task_dir(cwd: Path, payload: dict[str, Any]) -> Path | None:
    """Resolve task dir from explicit/session context or unambiguous cwd."""
    env_task = os.environ.get("DYNOS_TASK_DIR", "").strip()
    if env_task:
        try:
            return Path(env_task).resolve()
        except OSError:
            return None
    task_from_payload = _find_task_dir_from_agent_payload(payload, cwd)
    if task_from_payload is not None:
        return task_from_payload
    session_id = str(payload.get("session_id") or "").strip()
    project_root = _project_root_from_ancestors(cwd)
    if session_id and project_root is not None:
        try:
            import actor_identity
            bound = actor_identity.lookup_session_task(project_root, session_id)
            if bound is not None:
                return bound
        except Exception:
            pass
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
    launch_id = _async_launch_agent_id(payload.get("tool_response"), response_content)
    if launch_id is not None:
        entry["async_launch"] = True
        entry["agent_id"] = launch_id
    return entry


def _async_launch_agent_id(tool_response: Any, content_text: str) -> str | None:
    """Return the agent id when this post marks a background launch, else None.

    A background (async) spawn's Agent call returns as soon as the subagent
    starts, with no result content, so its post row says nothing about the
    subagent having finished. The id may be ``""`` when the host omits it.
    """
    if isinstance(tool_response, dict):
        agent_id = tool_response.get("agentId") or tool_response.get("agent_id") or ""
        if tool_response.get("status") == "async_launched" or not content_text.strip():
            return str(agent_id)
        return None
    if not content_text.strip() or "Async agent launched" in content_text:
        return ""
    return None


def _utc_iso8601() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _append_jsonl(path: Path, entry: dict[str, Any]) -> None:
    """Atomically append a JSONL line. Creates parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)


# A `pre` row older than this cannot belong to a live spawn; ignoring it keeps
# one crashed spawn (pre without post) from pinning its role stamp for the
# rest of the task.
_SPAWN_INFLIGHT_WINDOW_SECONDS = 2 * 60 * 60


def _planner_spawns_in_flight(log_path: Path, pending: dict[str, Any] | None = None) -> int:
    """Count planner spawns that have not finished yet (see ``spawns_in_flight``)."""
    return spawns_in_flight(log_path, pending).get("planning", 0)


def spawns_in_flight(log_path: Path, pending: dict[str, Any] | None = None) -> Counter[str]:
    """Count unfinished spawns per write-policy role.

    For each role, pairs ``pre`` rows with ``post`` rows by ``prompt_sha256``
    (a multiset, so identical prompts pair one-to-one) and counts recent
    ``pre`` rows left unmatched. A foreground post means the subagent
    returned; an ``async_launch`` post only means it started, so that spawn
    stays in flight until a SubagentStop ``stop`` row with its ``agent_id``
    appears (or, failing an id match, any stop row of that role not claimed
    by id). ``pending`` is a post row not yet in the log. Any read problem
    returns an empty count, which preserves the clear-everything behaviour.
    """
    from lib_core import role_for_subagent_type

    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return Counter()
    rows_by_role: dict[str, list[dict[str, Any]]] = {}
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("tool") == "Agent":
            role = role_for_subagent_type(str(row.get("subagent_type", "")))
            if role:
                rows_by_role.setdefault(role, []).append(row)
    if pending is not None:
        role = role_for_subagent_type(str(pending.get("subagent_type", "")))
        if role:
            rows_by_role.setdefault(role, []).append(pending)
    counts: Counter[str] = Counter()
    for role, rows in rows_by_role.items():
        outstanding = _unfinished_spawns(rows)
        if outstanding:
            counts[role] = outstanding
    return counts


def _unfinished_spawns(rows: list[dict[str, Any]]) -> int:
    import datetime as _dt

    now = _dt.datetime.now(_dt.timezone.utc)
    pres: list[str] = []
    posts: dict[str, list[dict[str, Any]]] = {}
    stop_ids: Counter[str] = Counter()
    for row in rows:
        digest = str(row.get("prompt_sha256", ""))
        if row.get("phase") == "post":
            posts.setdefault(digest, []).append(row)
        elif row.get("phase") == "stop":
            stop_ids[str(row.get("agent_id") or "")] += 1
        elif row.get("phase") == "pre":
            try:
                ts = _dt.datetime.fromisoformat(str(row.get("timestamp", "")).replace("Z", "+00:00"))
            except ValueError:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=_dt.timezone.utc)
            if (now - ts).total_seconds() <= _SPAWN_INFLIGHT_WINDOW_SECONDS:
                pres.append(digest)

    outstanding = 0
    unmatched_launches = 0
    for digest in pres:
        matched = posts.get(digest)
        if not matched:
            outstanding += 1
            continue
        post = matched.pop(0)
        if not post.get("async_launch"):
            continue
        agent_id = str(post.get("agent_id") or "")
        if agent_id and stop_ids[agent_id] > 0:
            stop_ids[agent_id] -= 1
        else:
            unmatched_launches += 1
    spare_stops = sum(stop_ids.values())
    return outstanding + max(0, unmatched_launches - spare_stops)


def release_planning_stamp(task_dir: Path, pending: dict[str, Any] | None = None) -> int:
    """Clear the planning stamp once no planner spawn is still running.

    Returns the number of planner spawns still in flight (0 means the stamp
    was cleared, or there was none to clear). Raises when ``ctl clear-role``
    fails, so callers can report it without failing the hook.
    """
    from lib_core import clear_stamped_role

    in_flight = _planner_spawns_in_flight(task_dir / "spawn-log.jsonl", pending)
    if not in_flight and clear_stamped_role(task_dir, "planning"):
        ctl = Path(__file__).with_name("ctl.py")
        cleared = subprocess.run(
            [sys.executable, str(ctl), "clear-role", str(task_dir)],
            capture_output=True, text=True,
        )
        if cleared.returncode:
            raise RuntimeError(cleared.stderr.strip() or "clear-role failed")
        if pending is not None:
            pending["cleared_role"] = "planning"
    return in_flight


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

    task_dir = _resolve_task_dir(cwd, payload)
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
    if phase == "pre":
        from lib_core import role_for_subagent_type, stamp_role
        subagent_type = str(entry.get("subagent_type", ""))
        role = role_for_subagent_type(subagent_type)
        if role is None:
            entry["stamped_role"] = None
            try:
                from lib_log import log_event
                log_event(task_dir.parent.parent, "spawn_role_unmapped", task=task_dir.name, subagent_type=subagent_type)
            except Exception:
                pass
        else:
            try:
                # Parallel spawns share the role file: keep the roles of
                # siblings still running instead of overwriting them.
                stamp_role(task_dir, role, keep=spawns_in_flight(log_path))
                # Keep the one-shot actor grant path used by pinned sessions.
                ctl = Path(__file__).with_name("ctl.py")
                grant = subprocess.run([sys.executable, str(ctl), "grant-role", str(task_dir), "--role", role], capture_output=True, text=True)
                if grant.returncode:
                    raise RuntimeError(grant.stderr.strip() or "grant-role failed")
                entry["stamped_role"] = role
            except Exception as exc:
                print(f"agent-spawn-log: role stamp failed: {exc}", file=sys.stderr)
                return 1
    if phase == "post":
        # The planning stamp lives exactly as long as the planner spawn:
        # once the planner returns, drop the stamp and expire its grant so
        # the orchestrator stops adopting the planner's restricted role. A
        # background launch has not returned yet; SubagentStop clears it.
        from lib_core import role_for_subagent_type
        if role_for_subagent_type(str(entry.get("subagent_type", ""))) == "planning" \
                and not entry.get("async_launch"):
            # Hierarchical planning runs worker planners in parallel and they
            # share one stamp: only the last one to return may clear it.
            try:
                in_flight = release_planning_stamp(task_dir, entry)
                if in_flight:
                    entry["planning_clear_deferred"] = in_flight
            except Exception as exc:
                print(f"agent-spawn-log: role clear failed: {exc}", file=sys.stderr)
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
