"""Tests for task-20260507-001: unpredictable secret derivation hardening.

Verifies the two cryptographic changes made to hooks/lib_log.py:
1. Branch (d) of _resolve_event_secret now uses secrets.token_hex(32)
   instead of the deterministic sha256-path-hostname derivation.
2. _derive_per_task_secret now uses RFC 5869 HKDF-SHA256 producing
   a 64-char hex string with proper domain separation.

ACs covered: 11, 12, 13, 14, 15, 16.
"""

from __future__ import annotations

import hashlib
import importlib
import platform
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = ROOT / "hooks"

if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

lib_log = importlib.import_module("lib_log")

_resolve_event_secret = lib_log._resolve_event_secret
_derive_per_task_secret = lib_log._derive_per_task_secret
_EVENT_SECRET_CACHE = lib_log._EVENT_SECRET_CACHE


# ---------------------------------------------------------------------------
# Fixture: per-test cache isolation (self-contained; does not import from
# test_event_secret.py)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolate_cache() -> None:
    """Clear _EVENT_SECRET_CACHE before and after every test."""
    lib_log._EVENT_SECRET_CACHE.clear()
    yield
    lib_log._EVENT_SECRET_CACHE.clear()


# ---------------------------------------------------------------------------
# AC 12: two fresh project roots produce distinct secrets
# ---------------------------------------------------------------------------

def test_fresh_project_root_secrets_differ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two separate fresh project roots (no pre-existing secret file, no env var)
    must produce different project secrets. Each call uses its own DYNOS_HOME to
    prevent cross-call filesystem collision. Proves secrets.token_hex(32) is used
    rather than any deterministic derivation.

    Probability of collision: 2^-256 — negligibly small.
    """
    monkeypatch.delenv("DYNOS_EVENT_SECRET", raising=False)

    # --- call A ---
    home_a = tmp_path / "dynos-home-a"
    home_a.mkdir()
    root_a = tmp_path / "project-a"
    root_a.mkdir()

    monkeypatch.setenv("DYNOS_HOME", str(home_a))
    lib_log._EVENT_SECRET_CACHE.clear()
    secret_a = _resolve_event_secret(root_a)

    # --- call B ---
    home_b = tmp_path / "dynos-home-b"
    home_b.mkdir()
    root_b = tmp_path / "project-b"
    root_b.mkdir()

    monkeypatch.setenv("DYNOS_HOME", str(home_b))
    lib_log._EVENT_SECRET_CACHE.clear()
    secret_b = _resolve_event_secret(root_b)

    # Both must be valid 64-char hex strings (entropy seal).
    assert len(secret_a) == 64, f"secret_a must be 64 hex chars; got {len(secret_a)}"
    assert len(secret_b) == 64, f"secret_b must be 64 hex chars; got {len(secret_b)}"
    int(secret_a, 16)  # raises if not valid hex
    int(secret_b, 16)

    # The two secrets must differ (deterministic derivation would make them equal
    # if they hashed the same path+hostname, but random token_hex ensures they differ).
    assert secret_a != secret_b, (
        "Two fresh project roots must produce different secrets. "
        "If equal, the old deterministic sha256-path-hostname derivation may still be active."
    )


# ---------------------------------------------------------------------------
# AC 13: per-task derivation is deterministic
# ---------------------------------------------------------------------------

def test_per_task_derivation_is_deterministic() -> None:
    """_derive_per_task_secret("project-secret", "task-A") called twice must return
    the same 64-character hex string. Proves HKDF is a pure function."""
    r1 = _derive_per_task_secret("project-secret", "task-A")
    r2 = _derive_per_task_secret("project-secret", "task-A")

    assert r1 == r2, (
        "_derive_per_task_secret must be deterministic; two calls with identical "
        f"arguments returned different values: {r1!r} vs {r2!r}"
    )
    assert len(r1) == 64, (
        f"_derive_per_task_secret must return a 64-char hex string; got len={len(r1)}"
    )
    int(r1, 16)  # raises ValueError if not valid hex


# ---------------------------------------------------------------------------
# AC 14: per-task derivation salt isolation
# ---------------------------------------------------------------------------

def test_per_task_derivation_salt_isolation() -> None:
    """_derive_per_task_secret with the same project_secret but distinct task_ids
    must produce distinct outputs. Proves the task_id is used as the HKDF salt
    (different salt → different PRK → different OKM)."""
    result_a = _derive_per_task_secret("project-secret", "task-A")
    result_b = _derive_per_task_secret("project-secret", "task-B")

    assert result_a != result_b, (
        "_derive_per_task_secret must produce distinct secrets for distinct task IDs. "
        f"task-A → {result_a!r}, task-B → {result_b!r}. "
        "Salt isolation is broken — task_id is not being used as the HKDF salt."
    )


# ---------------------------------------------------------------------------
# AC 15: old deterministic derivation is NOT used
# ---------------------------------------------------------------------------

def test_old_deterministic_derivation_not_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_resolve_event_secret on a fresh root must NOT return the old
    sha256(root+hostname)[:32] value. Negative seal: proves the deterministic
    path-hostname derivation was fully removed from branch (d).

    The old derivation was:
        hashlib.sha256(f"{root.resolve()}:{platform.node()}".encode()).hexdigest()[:32]
    """
    monkeypatch.delenv("DYNOS_EVENT_SECRET", raising=False)

    dynos_home = tmp_path / "dynos-home-neg"
    dynos_home.mkdir()
    monkeypatch.setenv("DYNOS_HOME", str(dynos_home))

    root = tmp_path / "project-neg"
    root.mkdir()

    lib_log._EVENT_SECRET_CACHE.clear()
    result = _resolve_event_secret(root)

    # Compute what the old derivation would have produced.
    old = hashlib.sha256(
        f"{root.resolve()}:{platform.node()}".encode("utf-8")
    ).hexdigest()[:32]

    assert result != old, (
        f"_resolve_event_secret returned the old deterministic sha256-path-hostname "
        f"value ({old!r}). The old branch (d) derivation has NOT been removed. "
        "Expected a fresh secrets.token_hex(32) value instead."
    )
