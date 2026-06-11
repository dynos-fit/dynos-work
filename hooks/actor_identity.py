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

# A grant not consumed within this window is dead — it cannot be picked up
# by a session that appears hours later (e.g. a stale ledger after a crash).
GRANT_TTL_SECONDS = 7200


def pin_path(project_root: Path) -> Path:
    return project_root / ".dynos" / PIN_FILENAME


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
    _atomic_write_json(pin_path(project_root), pin)
    return pin


def read_pin(project_root: Path) -> dict | None:
    data = _load_json(pin_path(project_root))
    if isinstance(data, dict) and data.get("session_id"):
        return data
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


def append_grant(task_dir: Path, role: str, *, write_json_fn=None) -> dict:
    """Append a pending grant. Role validity is the CALLER's responsibility
    (ctl validates against _STAMP_ROLE_ALLOWLIST before calling).

    write_json_fn: injection point for ctl's policy-checked writer
    (write_ctl_json); defaults to the module's direct atomic writer for the
    hook-side expiry path.
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
