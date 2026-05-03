"""Tests for task-20260423-001 AC21: HMAC event signing + verification.

Covers the new ``sign_event`` and ``verify_signed_events`` helpers in
``hooks/lib_log.py`` plus the ``DYNOS_EVENT_SECRET`` integration in
``log_event``:

  (a) ``sign_event({"event":"x","ts":"t"}, "s")`` produces a deterministic
      64-char hex digest (HMAC-SHA256).
  (b) ``sign_event`` is insensitive to key order in the payload.
  (c) ``log_event`` with ``DYNOS_EVENT_SECRET`` set writes a JSONL line
      whose ``_sig`` field verifies under ``verify_signed_events``.
  (d) A tampered line (manually rewritten field after write) is excluded
      from the result list in default mode and raises ``ValueError`` in
      ``strict=True``.
  (e) A record missing ``_sig`` is excluded by default and raises in
      ``strict=True``.
  (f) Empty secret returns empty list in ``strict=True`` and all parseable
      records plus a logged ``verify_signed_events_no_secret`` event in
      non-strict mode.

Tests are TDD-first: if the helpers have not been exported yet, the
module-level skip activates and the suite is a no-op. Once the
implementation lands, every test must pass.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = ROOT / "hooks"

if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))


# Import the module fresh so each test run sees the current on-disk code.
lib_log = importlib.import_module("lib_log")


_MISSING_HELPERS = [
    name for name in ("sign_event", "verify_signed_events")
    if not hasattr(lib_log, name)
]

pytestmark = pytest.mark.skipif(
    bool(_MISSING_HELPERS),
    reason=(
        "lib_log is missing required helpers for TDD-first event signing: "
        + ", ".join(_MISSING_HELPERS)
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_project(tmp_path: Path) -> tuple[Path, Path, str]:
    """Build a project root with a task dir + manifest. Returns
    (root, task_dir, task_id)."""
    root = tmp_path / "project"
    root.mkdir()
    (root / ".dynos").mkdir()
    task_id = "task-20260423-999"
    task_dir = root / ".dynos" / task_id
    task_dir.mkdir()
    (task_dir / "manifest.json").write_text(json.dumps({"task_id": task_id}))
    return root, task_dir, task_id


def _read_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        out.append(json.loads(line))
    return out


# ---------------------------------------------------------------------------
# (a) sign_event produces deterministic 64-char hex digest
# ---------------------------------------------------------------------------
def test_sign_event_deterministic_hex64() -> None:
    digest_1 = lib_log.sign_event({"event": "x", "ts": "t"}, "s")
    digest_2 = lib_log.sign_event({"event": "x", "ts": "t"}, "s")
    assert isinstance(digest_1, str)
    assert len(digest_1) == 64, f"expected 64-char hex digest, got {len(digest_1)}"
    # Hex characters only
    int(digest_1, 16)  # will raise ValueError if not valid hex
    assert digest_1 == digest_2, "sign_event must be deterministic"

    # Extra guard: the digest must match an HMAC-SHA256 over the canonical
    # JSON serialization of the payload with `_sig` removed. If an
    # implementation silently switches the algorithm or the canonicalization
    # this test fails.
    expected = hmac.new(
        b"s",
        json.dumps(
            {"event": "x", "ts": "t"},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert digest_1 == expected, (
        "sign_event digest does not match the canonical HMAC-SHA256 over "
        "sorted-key compact JSON; see spec AC10."
    )


# ---------------------------------------------------------------------------
# (b) sign_event is insensitive to key order
# ---------------------------------------------------------------------------
def test_sign_event_key_order_insensitive() -> None:
    d_asc = lib_log.sign_event({"a": 1, "b": 2, "c": 3}, "s")
    d_desc = lib_log.sign_event({"c": 3, "b": 2, "a": 1}, "s")
    assert d_asc == d_desc, "sign_event must be insensitive to key order"


# ---------------------------------------------------------------------------
# (b-extra) sign_event does not mutate the input payload and strips any
#           existing ``_sig`` key before signing.
# ---------------------------------------------------------------------------
def test_sign_event_does_not_mutate_input_and_ignores_existing_sig() -> None:
    payload = {"event": "x", "ts": "t", "_sig": "stale"}
    before = dict(payload)
    digest = lib_log.sign_event(payload, "s")
    assert payload == before, "sign_event must not mutate the input payload"
    # A different stale _sig must not change the digest — the function
    # must strip _sig before hashing.
    other = {"event": "x", "ts": "t", "_sig": "different"}
    assert lib_log.sign_event(other, "s") == digest, (
        "sign_event must strip _sig before hashing (key-independent of stale sigs)"
    )


# ---------------------------------------------------------------------------
# (c) log_event with DYNOS_EVENT_SECRET set writes a JSONL line whose _sig
#     verifies under verify_signed_events
# ---------------------------------------------------------------------------
def test_log_event_signs_when_secret_set_and_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, task_dir, task_id = _make_project(tmp_path)
    secret = "test-secret"
    monkeypatch.setenv("DYNOS_EVENT_SECRET", secret)

    lib_log.log_event(root, "unit_test_event", task=task_id, payload_key="value")

    events = task_dir / "events.jsonl"
    assert events.exists(), "events.jsonl must be written by log_event"
    records = _read_jsonl(events)
    assert len(records) == 1, f"expected exactly 1 event, got {len(records)}"
    rec = records[0]
    assert "_sig" in rec, "log_event must attach _sig when DYNOS_EVENT_SECRET is set"
    assert rec.get("event") == "unit_test_event"
    assert rec.get("payload_key") == "value"

    # verify_signed_events must return the record, with its signature intact.
    verified = lib_log.verify_signed_events(task_dir, secret)
    assert len(verified) == 1, (
        f"expected verify_signed_events to return the single signed record, "
        f"got {len(verified)}"
    )
    assert verified[0].get("event") == "unit_test_event"


# ---------------------------------------------------------------------------
# (c-extra) log_event with DYNOS_EVENT_SECRET unset still writes _sig because
#           _resolve_event_secret auto-derives a secret (AC 11/12).
# ---------------------------------------------------------------------------
def test_log_event_signs_unconditionally_without_env_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, task_dir, task_id = _make_project(tmp_path)
    monkeypatch.delenv("DYNOS_EVENT_SECRET", raising=False)
    monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "dynos-home"))
    # Clear the in-process cache so the test does not inherit a cached secret
    # from a previous test run in the same process.
    lib_log._EVENT_SECRET_CACHE.clear()

    lib_log.log_event(root, "unit_no_secret_event", task=task_id)

    records = _read_jsonl(task_dir / "events.jsonl")
    assert len(records) == 1
    assert "_sig" in records[0], (
        "log_event must attach _sig even when DYNOS_EVENT_SECRET is unset "
        "(auto-derived secret via _resolve_event_secret, AC 12)"
    )


# ---------------------------------------------------------------------------
# (d) A tampered line is excluded in default mode and raises in strict
# ---------------------------------------------------------------------------
def test_tampered_line_excluded_in_default_and_raises_strict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, task_dir, task_id = _make_project(tmp_path)
    secret = "tamper-test"
    monkeypatch.setenv("DYNOS_EVENT_SECRET", secret)

    lib_log.log_event(root, "original_event", task=task_id, name="alpha")
    lib_log.log_event(root, "second_event", task=task_id, name="beta")

    events = task_dir / "events.jsonl"
    lines = events.read_text().splitlines()
    assert len(lines) == 2

    # Tamper with line 0: rewrite `name` from "alpha" to "forged".
    rec = json.loads(lines[0])
    assert "_sig" in rec, "precondition: signing must be on"
    rec["name"] = "forged"
    # Keep _sig as-is so verification will now mismatch.
    lines[0] = json.dumps(rec)
    events.write_text("\n".join(lines) + "\n")

    # Default mode: tampered line excluded, second line survives.
    verified = lib_log.verify_signed_events(task_dir, secret)
    names = [r.get("name") for r in verified]
    assert "forged" not in names, (
        "tampered event must NOT appear in default-mode verify output"
    )
    assert "beta" in names, (
        "untampered event must still be returned in default mode"
    )

    # Strict mode: must raise a ValueError naming the bad line.
    with pytest.raises(ValueError):
        lib_log.verify_signed_events(task_dir, secret, strict=True)


# ---------------------------------------------------------------------------
# (e) A record missing _sig is excluded by default and raises in strict
# ---------------------------------------------------------------------------
def test_missing_sig_excluded_default_raises_strict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, task_dir, task_id = _make_project(tmp_path)
    secret = "sig-missing-test"
    monkeypatch.setenv("DYNOS_EVENT_SECRET", secret)

    # First event signed normally.
    lib_log.log_event(root, "signed_event", task=task_id, name="signed")

    # Append a record without _sig directly to the file.
    events = task_dir / "events.jsonl"
    with events.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"event": "unsigned_event", "name": "dangling"}) + "\n")

    # Default: signed record included, unsigned excluded.
    verified = lib_log.verify_signed_events(task_dir, secret)
    names = [r.get("name") for r in verified]
    assert "signed" in names
    assert "dangling" not in names, (
        "a record missing _sig must be excluded in default mode"
    )

    # Strict: raise.
    with pytest.raises(ValueError):
        lib_log.verify_signed_events(task_dir, secret, strict=True)


# ---------------------------------------------------------------------------
# (f) Empty secret: strict mode returns empty list; non-strict returns all
#     parseable records + logs a verify_signed_events_no_secret event.
# ---------------------------------------------------------------------------
def test_empty_secret_strict_returns_empty_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, task_dir, task_id = _make_project(tmp_path)
    # Write one event with signing ON so the file is non-empty.
    monkeypatch.setenv("DYNOS_EVENT_SECRET", "outer-secret")
    lib_log.log_event(root, "any_event", task=task_id)

    # Now verify with an EMPTY secret in strict mode.
    result_strict = lib_log.verify_signed_events(task_dir, "", strict=True)
    assert result_strict == [], (
        "verify_signed_events(strict=True) with empty secret must return []; "
        f"got {result_strict!r}"
    )


def test_empty_secret_nonstrict_returns_records_and_logs_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, task_dir, task_id = _make_project(tmp_path)
    # Populate two events with signing ON.
    monkeypatch.setenv("DYNOS_EVENT_SECRET", "outer-secret")
    lib_log.log_event(root, "a", task=task_id)
    lib_log.log_event(root, "b", task=task_id)

    pre_count = len(_read_jsonl(task_dir / "events.jsonl"))
    assert pre_count == 2

    # Non-strict + empty secret: return parseable records + log a
    # verify_signed_events_no_secret event.
    result = lib_log.verify_signed_events(task_dir, "", strict=False)
    assert len(result) == pre_count, (
        "non-strict + empty secret must return all parseable records "
        f"({pre_count}); got {len(result)}"
    )

    all_events = _read_jsonl(task_dir / "events.jsonl")
    event_names = [r.get("event") for r in all_events]
    assert "verify_signed_events_no_secret" in event_names, (
        "non-strict + empty secret must emit a verify_signed_events_no_secret "
        f"event; observed event names={event_names!r}"
    )


# ===========================================================================
# NEW TESTS — task-20260501-003: per-task HMAC key derivation
# ===========================================================================

# ---------------------------------------------------------------------------
# AC 1: _derive_per_task_secret — pure derivation helper
# ---------------------------------------------------------------------------
def test_derive_per_task_secret_deterministic() -> None:
    """_derive_per_task_secret(project_secret, task_id) returns a stable 32-char
    hex string. A hardcoded expected value catches wrong HMAC argument order."""
    fn = getattr(lib_log, "_derive_per_task_secret", None)
    assert fn is not None, (
        "_derive_per_task_secret must be exported from lib_log; "
        "production code not yet implemented"
    )

    result1 = fn("project-secret", "task-A")
    result2 = fn("project-secret", "task-A")

    # Must be a 32-character lowercase hex string.
    assert isinstance(result1, str), "return type must be str"
    assert len(result1) == 32, f"expected 32-char hex string, got len={len(result1)}"
    int(result1, 16)  # raises ValueError if not valid hex

    # Must be stable across two calls (pure function, no randomness).
    assert result1 == result2, "_derive_per_task_secret must be deterministic"

    # Must match a manually computed HMAC-SHA256[:32] — catches wrong arg order.
    # If project_secret and task_id are swapped the digest differs.
    expected = hmac.new(
        b"project-secret",
        b"task-A",
        hashlib.sha256,
    ).hexdigest()[:32]
    assert result1 == expected, (
        f"_derive_per_task_secret returned wrong digest; "
        f"got {result1!r}, expected {expected!r}. "
        "This likely indicates swapped hmac.new arguments (key vs message)."
    )

    # Sanity: different task_id produces a different secret.
    result_b = fn("project-secret", "task-B")
    assert result1 != result_b, (
        "_derive_per_task_secret must produce distinct secrets for distinct task IDs"
    )


# ---------------------------------------------------------------------------
# AC 2: _resolve_event_secret with and without task_id
# ---------------------------------------------------------------------------
def test_resolve_event_secret_with_task_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When task_id is a non-empty string, _resolve_event_secret returns the
    per-task derivation (not the raw project secret)."""
    fn = getattr(lib_log, "_resolve_event_secret", None)
    derive = getattr(lib_log, "_derive_per_task_secret", None)
    assert fn is not None, "_resolve_event_secret must exist"
    assert derive is not None, "_derive_per_task_secret must exist"

    proj_secret = "proj-secret-ac2"
    monkeypatch.setenv("DYNOS_EVENT_SECRET", proj_secret)

    root = tmp_path / "proj"
    root.mkdir()

    result = fn(root, task_id="task-X")
    expected = derive(proj_secret, "task-X")

    assert result == expected, (
        f"_resolve_event_secret with task_id='task-X' must return the per-task "
        f"derivation. Got {result!r}, expected {expected!r}"
    )
    assert result != proj_secret, (
        "_resolve_event_secret with task_id must NOT return the raw project secret"
    )
    assert len(result) == 32, "per-task secret must be 32 hex chars"


def test_resolve_event_secret_no_task_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When task_id is None, _resolve_event_secret returns the project secret
    unchanged (identical to pre-change behaviour)."""
    fn = getattr(lib_log, "_resolve_event_secret", None)
    assert fn is not None, "_resolve_event_secret must exist"

    proj_secret = "proj-secret-no-task"
    monkeypatch.setenv("DYNOS_EVENT_SECRET", proj_secret)

    root = tmp_path / "proj"
    root.mkdir()

    result = fn(root)  # no task_id kwarg — should use default (None)
    assert result == proj_secret, (
        f"_resolve_event_secret with no task_id must return the project secret "
        f"unchanged. Got {result!r}"
    )


# ---------------------------------------------------------------------------
# AC 3: sign_event with task_id differs from without
# ---------------------------------------------------------------------------
def test_sign_event_with_task_id_differs_from_without() -> None:
    """sign_event(payload, secret, task_id='task-A') must produce a different
    digest than sign_event(payload, secret). Pure unit test — no I/O."""
    record = {"event": "test_evt", "ts": "2026-01-01T00:00:00Z"}
    secret = "some-project-secret"

    sig_with_task = lib_log.sign_event(record, secret, task_id="task-A")
    sig_without_task = lib_log.sign_event(record, secret)

    assert sig_with_task != sig_without_task, (
        "sign_event with task_id='task-A' must produce a digest distinct from "
        "sign_event without task_id — per-task namespace isolation broken"
    )

    # Also verify different task IDs produce different sigs.
    sig_task_b = lib_log.sign_event(record, secret, task_id="task-B")
    assert sig_with_task != sig_task_b, (
        "sign_event must produce distinct signatures for distinct task IDs"
    )


# ---------------------------------------------------------------------------
# AC 4: log_event uses per-task secret when task kwarg is provided
# ---------------------------------------------------------------------------
def test_log_event_task_scoped_signature_uses_per_task_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Events written by log_event(task=task_id) must be signed with the
    per-task HMAC secret, not the raw project secret."""
    derive = getattr(lib_log, "_derive_per_task_secret", None)
    assert derive is not None, "_derive_per_task_secret must exist"

    root, task_dir, task_id = _make_project(tmp_path)
    proj_secret = "proj-secret-ac4"
    monkeypatch.setenv("DYNOS_EVENT_SECRET", proj_secret)

    lib_log.log_event(root, "ac4_event", task=task_id, payload_key="ac4_value")

    events_path = task_dir / "events.jsonl"
    assert events_path.exists()
    records = _read_jsonl(events_path)
    assert len(records) == 1
    rec = records[0]
    assert "_sig" in rec

    stored_sig = rec["_sig"]

    # Recompute: per-task secret is derived from proj_secret + task_id.
    per_task_secret = derive(proj_secret, task_id)
    # sign_event strips _sig before hashing, so pass the full record.
    expected_sig = lib_log.sign_event(rec, per_task_secret)

    assert stored_sig == expected_sig, (
        f"log_event must sign with the per-task secret (derived from "
        f"project_secret + task_id). "
        f"Stored: {stored_sig!r}, expected: {expected_sig!r}. "
        "This means log_event is still using the raw project secret."
    )

    # Confirm it does NOT match a signature made with the raw project secret.
    project_sig = lib_log.sign_event(rec, proj_secret)
    assert stored_sig != project_sig, (
        "The stored _sig must NOT equal a signature made with the raw project "
        "secret — cross-task namespace isolation requires per-task derivation"
    )


# ---------------------------------------------------------------------------
# AC 5: verify_signed_events accepts per-task-signed records; rejects
#        project-signed records in strict mode
# ---------------------------------------------------------------------------
def test_verify_accepts_per_task_signed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record signed with the per-task secret must pass verify_signed_events."""
    derive = getattr(lib_log, "_derive_per_task_secret", None)
    assert derive is not None

    # Build task_dir with name "task-ac5"
    task_dir = tmp_path / "task-ac5"
    task_dir.mkdir()

    proj_secret = "proj-secret-ac5"
    per_task_secret = derive(proj_secret, "task-ac5")

    record = {"event": "ac5_event", "ts": "2026-01-01T00:00:00Z", "data": "x"}
    record["_sig"] = lib_log.sign_event(record, per_task_secret)

    events_path = task_dir / "events.jsonl"
    events_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    verified = lib_log.verify_signed_events(task_dir, proj_secret)
    assert len(verified) == 1, (
        f"verify_signed_events must accept per-task-signed records; got {verified!r}"
    )
    assert verified[0].get("event") == "ac5_event"


def test_verify_rejects_project_signed_in_strict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record signed with the raw project secret (not per-task) must be
    rejected with ValueError in strict mode."""
    task_dir = tmp_path / "task-ac5-strict"
    task_dir.mkdir()

    proj_secret = "proj-secret-ac5-strict"

    record = {"event": "old_style_event", "ts": "2026-01-01T00:00:00Z"}
    # Sign with the raw project secret only (legacy, pre-derivation)
    record["_sig"] = lib_log.sign_event(record, proj_secret)

    events_path = task_dir / "events.jsonl"
    events_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    with pytest.raises(ValueError) as exc_info:
        lib_log.verify_signed_events(task_dir, proj_secret, strict=True)

    assert "legacy_project_secret_in_strict_mode" in str(exc_info.value), (
        f"strict mode must raise ValueError with 'legacy_project_secret_in_strict_mode' "
        f"for project-signed records; got {exc_info.value!r}"
    )


# ---------------------------------------------------------------------------
# AC 6: migration rewrites events.jsonl atomically; failure emits event
# ---------------------------------------------------------------------------
def test_migration_rewrites_events_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After verify_signed_events in non-strict mode finds a project-secret-signed
    record, it must atomically rewrite events.jsonl so the record is now signed
    with the per-task secret."""
    derive = getattr(lib_log, "_derive_per_task_secret", None)
    assert derive is not None

    # task_dir must have a non-empty name so per-task derivation applies.
    task_dir = tmp_path / "task-migrate"
    task_dir.mkdir()

    proj_secret = "proj-secret-migrate"
    per_task_secret = derive(proj_secret, "task-migrate")

    # Write a record signed with the raw project secret (legacy format).
    record = {"event": "legacy_event", "ts": "2026-01-01T00:00:00Z", "x": 1}
    record["_sig"] = lib_log.sign_event(record, proj_secret)

    events_path = task_dir / "events.jsonl"
    events_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    # Verify in non-strict mode — should accept AND trigger migration.
    verified = lib_log.verify_signed_events(task_dir, proj_secret, strict=False)
    assert len(verified) == 1, (
        "non-strict verify must return the project-secret-signed record"
    )

    # Read back the file — it must now be signed with the per-task secret.
    rewritten = _read_jsonl(events_path)
    assert len(rewritten) == 1, (
        f"migration must not change the number of records; got {len(rewritten)}"
    )
    migrated_rec = rewritten[0]
    assert "_sig" in migrated_rec

    # Recompute the expected per-task signature.
    expected_sig = lib_log.sign_event(migrated_rec, per_task_secret)
    assert migrated_rec["_sig"] == expected_sig, (
        f"migrated record _sig must equal per-task-signed digest. "
        f"Got {migrated_rec['_sig']!r}, expected {expected_sig!r}. "
        "Migration rewrote with wrong secret."
    )

    # Confirm the project-secret signature is gone.
    project_sig = lib_log.sign_event(migrated_rec, proj_secret)
    assert migrated_rec["_sig"] != project_sig, (
        "migrated record must not still be signed with the raw project secret"
    )


def test_migration_failure_emits_event_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the atomic rewrite (os.replace) raises, verify_signed_events must:
    - emit verify_signed_events_migration_failed to the event log
    - return the accepted records without raising
    """
    derive = getattr(lib_log, "_derive_per_task_secret", None)
    assert derive is not None

    # We need a proper project structure so log_event can emit the failure event.
    root = tmp_path / "proj-migfail"
    root.mkdir()
    (root / ".dynos").mkdir()
    task_dir = root / ".dynos" / "task-migfail"
    task_dir.mkdir()

    proj_secret = "proj-secret-migfail"
    monkeypatch.setenv("DYNOS_EVENT_SECRET", proj_secret)

    # Write a project-secret-signed record to trigger migration.
    record = {"event": "legacy_for_fail", "ts": "2026-01-01T00:00:00Z"}
    record["_sig"] = lib_log.sign_event(record, proj_secret)
    events_path = task_dir / "events.jsonl"
    events_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    # Monkeypatch os.replace to raise OSError so the atomic write fails.
    import os as _os
    monkeypatch.setattr(_os, "replace", lambda *a, **kw: (_ for _ in ()).throw(OSError("injected failure")))

    # verify_signed_events must NOT raise — soft failure only.
    result = lib_log.verify_signed_events(task_dir, proj_secret, strict=False)
    assert len(result) >= 1, (
        "verify_signed_events must return the accepted records even when migration fails"
    )

    # The failure event must have been emitted somewhere — either the global log
    # or an ancestor log. Since log_event uses task_dir.parent.parent = root, it
    # falls back to the global .dynos/events.jsonl.
    global_log = root / ".dynos" / "events.jsonl"
    # Also check the task's own log as a fallback path.
    all_logged_events: list[dict] = []
    if global_log.exists():
        all_logged_events.extend(_read_jsonl(global_log))
    if events_path.exists():
        all_logged_events.extend(_read_jsonl(events_path))

    event_names = [e.get("event") for e in all_logged_events]
    assert "verify_signed_events_migration_failed" in event_names, (
        f"verify_signed_events_migration_failed must be emitted when the atomic "
        f"rewrite fails. Observed event names: {event_names!r}"
    )


# ---------------------------------------------------------------------------
# AC 7: cross-task replay is rejected (load-bearing test)
# ---------------------------------------------------------------------------
def test_cross_task_replay_rejected_non_strict(tmp_path: Path) -> None:
    """An event signed for task-A, injected into task-B's log, must be EXCLUDED
    (return []) by verify_signed_events in non-strict mode.

    This is the core cross-task isolation property. Both the per-task secret
    and the raw project secret must fail — task-A-derived secret != task-B secret
    and task-A-derived secret != raw project secret."""
    derive = getattr(lib_log, "_derive_per_task_secret", None)
    assert derive is not None

    proj_secret = "proj-secret-replay"
    task_a_secret = derive(proj_secret, "task-A")

    # Create task-B's directory.
    task_b_dir = tmp_path / "task-B"
    task_b_dir.mkdir()

    # Sign the record as if it belongs to task-A.
    record = {"event": "task_a_event", "ts": "2026-01-01T00:00:00Z", "owner": "A"}
    record["_sig"] = lib_log.sign_event(record, task_a_secret)

    # Inject task-A's signed record into task-B's events.jsonl.
    events_path = task_b_dir / "events.jsonl"
    events_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    # task-B's verify must reject it — both per-task-B secret and project secret
    # do not match the task-A-derived signature.
    result = lib_log.verify_signed_events(task_b_dir, proj_secret, strict=False)
    assert result == [], (
        f"Cross-task replay must be rejected: task-A-signed record injected "
        f"into task-B's log must not appear in verify_signed_events output. "
        f"Got: {result!r}. "
        "The per-task isolation is broken — verify is accepting wrong-task records."
    )


def test_cross_task_replay_rejected_strict(tmp_path: Path) -> None:
    """An event signed for task-A, injected into task-B's log, must cause
    verify_signed_events to raise ValueError in strict mode."""
    derive = getattr(lib_log, "_derive_per_task_secret", None)
    assert derive is not None

    proj_secret = "proj-secret-replay-strict"
    task_a_secret = derive(proj_secret, "task-A")

    task_b_dir = tmp_path / "task-B"
    task_b_dir.mkdir()

    record = {"event": "task_a_event_strict", "ts": "2026-01-01T00:00:00Z"}
    record["_sig"] = lib_log.sign_event(record, task_a_secret)

    events_path = task_b_dir / "events.jsonl"
    events_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    with pytest.raises(ValueError) as exc_info:
        lib_log.verify_signed_events(task_b_dir, proj_secret, strict=True)

    # The error must name the mismatch — "signature_mismatch" since neither
    # per-task-B nor project secret matches the task-A-derived signature.
    assert "signature_mismatch" in str(exc_info.value) or "invalid" in str(exc_info.value), (
        f"strict mode must raise ValueError for cross-task replay; "
        f"got {exc_info.value!r}"
    )


# ---------------------------------------------------------------------------
# AC 8: global fallback (no task kwarg) signs with raw project secret
# ---------------------------------------------------------------------------
def test_global_fallback_signs_with_project_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """log_event without a task kwarg must sign with the project secret (not
    a per-task derivation). The global .dynos/events.jsonl is verified via
    a task_dir whose .name is '.dynos' — falling back to the project secret."""
    root = tmp_path / "proj-global"
    root.mkdir()
    (root / ".dynos").mkdir()

    proj_secret = "proj-secret-global"
    monkeypatch.setenv("DYNOS_EVENT_SECRET", proj_secret)

    # log_event with no task — goes to .dynos/events.jsonl.
    lib_log.log_event(root, "global_event", some_field="global_val")

    global_events_path = root / ".dynos" / "events.jsonl"
    assert global_events_path.exists(), ".dynos/events.jsonl must be created"

    records = _read_jsonl(global_events_path)
    assert len(records) >= 1
    rec = [r for r in records if r.get("event") == "global_event"]
    assert len(rec) == 1
    assert "_sig" in rec[0]

    # Verify the global event is signed with the project secret.
    # The global path is .dynos/ which has .name == ".dynos" — per spec,
    # verify_signed_events must use task_dir.name as task_id, but ".dynos"
    # is a non-empty string so it will derive a per-task secret for ".dynos".
    # However, log_event without task uses the project secret directly.
    # So we verify using the project secret directly by checking the sig field.
    stored_sig = rec[0]["_sig"]
    expected_project_sig = lib_log.sign_event(rec[0], proj_secret)

    assert stored_sig == expected_project_sig, (
        f"Global log_event (no task) must sign with the raw project secret. "
        f"Got {stored_sig!r}, expected {expected_project_sig!r}. "
        "log_event without task is using per-task derivation (regression)."
    )


# ---------------------------------------------------------------------------
# AC 9: empty string and None task_id both fall back to project-secret behavior
# ---------------------------------------------------------------------------
def test_empty_task_id_falls_back_to_project_secret() -> None:
    """sign_event(r, s, task_id='') and sign_event(r, s, task_id=None) must
    both produce the same digest as sign_event(r, s). Pure unit test."""
    record = {"event": "ac9_event", "ts": "2026-01-01T00:00:00Z"}
    secret = "project-secret-ac9"

    sig_baseline = lib_log.sign_event(record, secret)
    sig_empty_str = lib_log.sign_event(record, secret, task_id="")
    sig_none = lib_log.sign_event(record, secret, task_id=None)

    assert sig_empty_str == sig_baseline, (
        f"sign_event with task_id='' must equal baseline (no task_id). "
        f"Got {sig_empty_str!r} vs {sig_baseline!r}. "
        "Empty string task_id must fall back to project-secret (falsy check required)."
    )
    assert sig_none == sig_baseline, (
        f"sign_event with task_id=None must equal baseline (no task_id). "
        f"Got {sig_none!r} vs {sig_baseline!r}."
    )

    # Confirm these are NOT equal to a non-empty task_id signature
    # (ensures the empty/None fallback is not accidentally the same as derivation).
    sig_task_a = lib_log.sign_event(record, secret, task_id="task-A")
    assert sig_baseline != sig_task_a, (
        "Sanity: the baseline must differ from task_id='task-A'"
    )


# ---------------------------------------------------------------------------
# AC 10: verify_signed_events two-positional-args caller compatibility
# ---------------------------------------------------------------------------
def test_verify_signed_events_caller_compat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """verify_signed_events(task_dir, secret) with only two positional args
    must work correctly — simulating the call shape at lib_receipts.py:1661."""
    derive = getattr(lib_log, "_derive_per_task_secret", None)
    assert derive is not None

    task_dir = tmp_path / "task-compat"
    task_dir.mkdir()

    proj_secret = "proj-secret-compat"
    per_task_secret = derive(proj_secret, "task-compat")

    record = {"event": "compat_event", "ts": "2026-01-01T00:00:00Z"}
    record["_sig"] = lib_log.sign_event(record, per_task_secret)

    events_path = task_dir / "events.jsonl"
    events_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    # Call with only two positional arguments — exactly as lib_receipts.py does.
    result = lib_log.verify_signed_events(task_dir, proj_secret)

    assert len(result) == 1, (
        f"Two-positional-arg call must verify per-task-signed records correctly. "
        f"Got {result!r}"
    )
    assert result[0].get("event") == "compat_event"


# ---------------------------------------------------------------------------
# AC 12: migration event names present in DIAGNOSTIC_ONLY_EVENTS
# ---------------------------------------------------------------------------
def test_migration_event_names_in_diagnostic_only() -> None:
    """Both new migration event names must be in DIAGNOSTIC_ONLY_EVENTS.
    Existing entries must not have been removed."""
    doe = lib_log.DIAGNOSTIC_ONLY_EVENTS

    assert "verify_signed_events_migration_attempted" in doe, (
        "'verify_signed_events_migration_attempted' must be in DIAGNOSTIC_ONLY_EVENTS"
    )
    assert "verify_signed_events_migration_failed" in doe, (
        "'verify_signed_events_migration_failed' must be in DIAGNOSTIC_ONLY_EVENTS"
    )

    # Spot-check that no pre-existing entries were removed.
    pre_existing_sample = {
        "verify_signed_events_mismatch",
        "receipt_written",
        "stage_transition",
        "gate_refused",
        "audit_receipt_content_paired",
    }
    for entry in pre_existing_sample:
        assert entry in doe, (
            f"Pre-existing DIAGNOSTIC_ONLY_EVENTS entry {entry!r} was removed — "
            "AC 12 forbids removing existing entries"
        )
