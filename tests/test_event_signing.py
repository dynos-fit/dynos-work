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
