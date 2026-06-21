#!/usr/bin/env python3
"""Per-actor identity resolution for write-policy roles (D3).

Replaces the single mutable ``active-segment-role`` file as the way the
PreToolUse hook decides *who* is making a tool call:

- The MAIN session is pinned at SessionStart: the hook payload's
  ``session_id`` is recorded to ``.dynos/orchestrator-session.json``
  (hook-owned — ``write_policy.decide_write`` denies every agent write to
  it). Tool calls matching the pin resolve to the ``orchestrator`` role and
  NEVER read role files, so stamping a role for a subagent can no longer
  mutate the orchestrator's own write rights mid-flow (P0-c/P1-a in
  docs/permissions-on-design.md).

- SUBAGENT sessions (any other ``session_id``) consume single-use role
  GRANTS from ``role-grants.json`` (task-scoped, wrapper-required — only
  ``ctl.py grant-role``/``stamp-role`` may append). The first tool call from
  an unknown session binds it to the oldest pending grant; the binding is
  recorded in ``role-bindings.json`` (hook-owned) and is immutable for the
  session's lifetime — a mid-task re-stamp cannot change a running
  subagent's role, and parallel auditors each consume their own grant.

Self-elevation analysis:
- roles enter the ledger only through the ctl allowlist; a session never
  names its own role,
- the orchestrator cannot consume a grant (its session is pinned first),
- no agent role can write the pin, the ledger, or the bindings file
  (policy-denied; the hook subprocess writes them via direct file I/O, the
  same precedent as spawn-log.jsonl and audit-grep-quota.json).
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

PIN_FILENAME = "orchestrator-session.json"
GRANTS_FILENAME = "role-grants.json"
BINDINGS_FILENAME = "role-bindings.json"
SESSION_TASKS_FILENAME = "session-tasks.json"

# A grant not consumed within this window is dead — it cannot be picked up
# by a session that appears hours later (e.g. a stale ledger after a crash).
GRANT_TTL_SECONDS = 7200


def pin_path(project_root: Path) -> Path:
    return project_root / ".dynos" / PIN_FILENAME


def session_tasks_path(project_root: Path) -> Path:
    return project_root / ".dynos" / SESSION_TASKS_FILENAME


def grants_path(task_dir: Path) -> Path:
    return task_dir / GRANTS_FILENAME


def bindings_path(task_dir: Path) -> Path:
    return task_dir / BINDINGS_FILENAME


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Orchestrator pin
# ---------------------------------------------------------------------------

def pin_orchestrator(project_root: Path, payload: dict) -> dict | None:
    """Record the main session's identity. Called from the SessionStart hook.

    SessionStart fires only for the main session, so a subagent can never
    re-pin this file through the harness; agent tool-call writes to it are
    denied by write_policy.
    """
    session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        return None
    pin = {
        "session_id": session_id,
        "transcript_path": str(payload.get("transcript_path") or ""),
        "source": str(payload.get("source") or ""),
        "pinned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    project_root = project_root.resolve()
    lock_file = project_root / ".dynos" / ".orchestrator-session.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_file, "w", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        try:
            existing = _load_json(pin_path(project_root))
            if isinstance(existing, dict) and isinstance(existing.get("sessions"), dict):
                data = existing
            elif isinstance(existing, dict) and existing.get("session_id"):
                data = {
                    "session_id": existing.get("session_id"),
                    "latest_session_id": existing.get("session_id"),
                    "sessions": {str(existing["session_id"]): existing},
                }
            else:
                data = {"sessions": {}}
            sessions = data.setdefault("sessions", {})
            sessions[session_id] = pin
            data["session_id"] = session_id
            data["latest_session_id"] = session_id
            _atomic_write_json(pin_path(project_root), data)
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
    return pin


def read_pin(project_root: Path, session_id: str | None = None) -> dict | None:
    data = _load_json(pin_path(project_root))
    if not isinstance(data, dict):
        return None
    if session_id and isinstance(data.get("sessions"), dict):
        entry = data["sessions"].get(session_id)
        if isinstance(entry, dict) and entry.get("session_id"):
            return entry
        return None
    if session_id:
        if data.get("session_id") == session_id:
            return data
        return None
    if isinstance(data.get("sessions"), dict):
        latest = str(data.get("latest_session_id") or data.get("session_id") or "")
        entry = data["sessions"].get(latest)
        if isinstance(entry, dict) and entry.get("session_id"):
            return entry
    if data.get("session_id"):
        return data
    return None


def _task_is_non_terminal(task_dir: Path) -> bool:
    try:
        manifest = json.loads((task_dir / "manifest.json").read_text(encoding="utf-8"))
        return isinstance(manifest, dict) and manifest.get("stage") not in {
            "DONE", "FAILED", "CANCELLED", "CALIBRATED",
        }
    except Exception:
        return False


def bind_session_task(project_root: Path, session_id: str, task_dir: Path) -> None:
    """Persist the task currently associated with a host session.

    This is hook-owned state used to avoid the old project-global
    ``active-task.json`` singleton when multiple dynos-work sessions run at
    the same time.
    """
    if not session_id:
        return
    project_root = project_root.resolve()
    task_dir = task_dir.resolve()
    try:
        task_dir.relative_to(project_root / ".dynos")
    except ValueError:
        return
    lock_file = project_root / ".dynos" / ".session-tasks.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_file, "w", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        try:
            data = _load_json(session_tasks_path(project_root))
            if not isinstance(data, dict) or not isinstance(data.get("sessions"), dict):
                data = {"sessions": {}}
            data["sessions"][session_id] = {
                "task_dir": str(task_dir),
                "task_id": task_dir.name,
                "bound_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            _atomic_write_json(session_tasks_path(project_root), data)
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)


def lookup_session_task(project_root: Path, session_id: str) -> Path | None:
    if not session_id:
        return None
    data = _load_json(session_tasks_path(project_root.resolve()))
    if not isinstance(data, dict) or not isinstance(data.get("sessions"), dict):
        return None
    entry = data["sessions"].get(session_id)
    if not isinstance(entry, dict) or not isinstance(entry.get("task_dir"), str):
        return None
    task_dir = Path(entry["task_dir"]).resolve()
    try:
        task_dir.relative_to(project_root.resolve() / ".dynos")
    except ValueError:
        return None
    if task_dir.exists() and _task_is_non_terminal(task_dir):
        return task_dir
    return None


# ---------------------------------------------------------------------------
# Grant ledger (ctl-written via wrapper; hook consumes)
# ---------------------------------------------------------------------------

def _empty_ledger() -> dict:
    return {"grants": []}


def load_grants(task_dir: Path) -> dict:
    data = _load_json(grants_path(task_dir))
    if isinstance(data, dict) and isinstance(data.get("grants"), list):
        return data
    return _empty_ledger()


def append_grant(
    task_dir: Path,
    role: str,
    *,
    write_json_fn=None,
    expected_artifact: str | None = None,
    attempt: int | None = None,
    budget: int | None = None,
) -> dict:
    """Append a pending grant. Role validity is the CALLER's responsibility
    (ctl validates against _STAMP_ROLE_ALLOWLIST before calling).

    write_json_fn: injection point for ctl's policy-checked writer
    (write_ctl_json); defaults to the module's direct atomic writer for the
    hook-side expiry path.

    expected_artifact, attempt, budget: optional fields written into the grant
    dict only when non-None. Old callers passing no new kwargs receive a
    byte-identical grant dict.
    """
    ledger = load_grants(task_dir)
    now = time.time()
    grant = {
        "role": role,
        "granted_at": now,
        "expires_at": now + GRANT_TTL_SECONDS,
        "consumed_by": None,
        "consumed_at": None,
    }
    if expected_artifact is not None:
        grant["expected_artifact"] = expected_artifact
    if attempt is not None:
        grant["attempt"] = attempt
    if budget is not None:
        grant["budget"] = budget
    ledger["grants"].append(grant)
    writer = write_json_fn or (lambda p, d: _atomic_write_json(p, d))
    writer(grants_path(task_dir), ledger)
    return grant


def expire_grants(task_dir: Path, *, write_json_fn=None) -> int:
    """Mark every unconsumed grant expired. Clearing only reduces privilege."""
    ledger = load_grants(task_dir)
    now = time.time()
    expired = 0
    for grant in ledger["grants"]:
        if grant.get("consumed_by") is None and grant.get("expires_at", 0) > now:
            grant["expires_at"] = now
            expired += 1
    writer = write_json_fn or (lambda p, d: _atomic_write_json(p, d))
    writer(grants_path(task_dir), ledger)
    return expired


def pending_grants(task_dir: Path) -> list[dict]:
    now = time.time()
    return [
        g for g in load_grants(task_dir)["grants"]
        if g.get("consumed_by") is None and g.get("expires_at", 0) >= now
    ]


# ---------------------------------------------------------------------------
# Session -> role bindings (hook-owned)
# ---------------------------------------------------------------------------

def lookup_binding(task_dir: Path, session_id: str) -> str | None:
    data = _load_json(bindings_path(task_dir))
    if not isinstance(data, dict):
        return None
    entry = (data.get("bindings") or {}).get(session_id)
    if isinstance(entry, dict) and isinstance(entry.get("role"), str):
        return entry["role"]
    return None


def consume_grant(task_dir: Path, session_id: str) -> str | None:
    """Bind an unknown session to the oldest pending grant, atomically.

    Returns the bound role, or None when no pending grant exists. The flock
    serializes parallel subagents' first tool calls so two sessions cannot
    consume the same grant.
    """
    if not session_id:
        return None
    lock_file = task_dir / ".role-grants.lock"
    try:
        task_dir.mkdir(parents=True, exist_ok=True)
        with open(lock_file, "w", encoding="utf-8") as lock_fh:
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
            try:
                existing = lookup_binding(task_dir, session_id)
                if existing:
                    return existing
                ledger = load_grants(task_dir)
                now = time.time()
                chosen: dict | None = None
                for grant in sorted(
                    ledger["grants"], key=lambda g: g.get("granted_at", 0)
                ):
                    if grant.get("consumed_by") is None and grant.get("expires_at", 0) >= now:
                        chosen = grant
                        break
                if chosen is None:
                    return None
                chosen["consumed_by"] = session_id
                chosen["consumed_at"] = now
                _atomic_write_json(grants_path(task_dir), ledger)

                bindings = _load_json(bindings_path(task_dir))
                if not isinstance(bindings, dict) or not isinstance(
                    bindings.get("bindings"), dict
                ):
                    bindings = {"bindings": {}}
                bindings["bindings"][session_id] = {
                    "role": chosen["role"],
                    "bound_at": now,
                }
                _atomic_write_json(bindings_path(task_dir), bindings)
                return str(chosen["role"])
            finally:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)
    except OSError:
        return None


# ---------------------------------------------------------------------------
# CLI (used by the session-start bash hook)
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "pin":
        root = Path(argv[argv.index("--root") + 1]) if "--root" in argv else Path.cwd()
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except json.JSONDecodeError:
            return 0  # malformed payload: skip pinning, legacy behavior applies
        if not isinstance(payload, dict):
            return 0
        pin_orchestrator(root.resolve(), payload)
        return 0
    print("actor_identity: unknown command", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
