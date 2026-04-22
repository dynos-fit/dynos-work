"""TDD-first tests for task-20260420-001 D3 part 3 — reader verification.

Covers acceptance criteria 16 and 17:

    AC16 readers verify HMAC on task-scoped events; mismatches/missing-sig
         are dropped and a discriminated `event_signature_invalid` event is
         emitted with `reason` in {missing_sig_field, sig_mismatch,
         key_unreadable, line_malformed}.
         Migrated readers: receipt_post_completion (lib_receipts:1296-1328),
         lib_core:2137 (shared retrospective_flushed scan — shared log stays
         unsigned read-only, but any per-task read switches), lib_core:2208
         fingerprint, collect_retrospectives downstream.
    AC17 receipt_post_completion NO LONGER scans the shared .dynos/events.jsonl
         for handler attribution — only the per-task signed log is consulted.

TODAY these tests FAIL because the reader migration has not happened:
    receipt_post_completion STILL reads both repo_events and task_events.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "hooks"))

from lib_receipts import receipt_post_completion  # noqa: E402


def _canonical(record_without_sig: dict) -> bytes:
    return json.dumps(
        record_without_sig,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        ensure_ascii=False,
    ).encode("utf-8")


def _prepare_task(tmp_path: Path, task_id: str = "task-RDR") -> tuple[Path, Path, bytes]:
    """Returns (root, task_dir, key_bytes)."""
    root = tmp_path / "project"
    (root / ".dynos").mkdir(parents=True)
    td = root / ".dynos" / task_id
    td.mkdir()
    # Minimal manifest so receipt_post_completion doesn't fall over on
    # load_json of a missing manifest — create the receipts dir too.
    (td / "receipts").mkdir()
    key_bytes = secrets.token_bytes(32)
    (td / ".events-key").write_text(
        base64.b64encode(key_bytes).decode("ascii"), encoding="utf-8"
    )
    try:
        (td / ".events-key").chmod(0o600)
    except OSError:
        pass
    return root, td, key_bytes


def _append_signed(path: Path, record: dict, key_bytes: bytes) -> None:
    record_no_sig = dict(record)
    record_no_sig.pop("sig", None)
    sig = hmac.new(key_bytes, _canonical(record_no_sig), hashlib.sha256).hexdigest()
    full = {**record_no_sig, "sig": sig}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(full, default=str, ensure_ascii=False) + "\n")


def _append_unsigned(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# AC17: shared log is NEVER consulted by receipt_post_completion
# ---------------------------------------------------------------------------


def test_receipt_post_completion_ignores_shared_log(tmp_path: Path):
    """Forge a handler event in the SHARED log with the correct task
    attribution. The migrated reader must ignore it — the per-task signed
    log is the only source of truth."""
    root, td, _key = _prepare_task(tmp_path)
    task_id = td.name

    shared = root / ".dynos" / "events.jsonl"
    _append_unsigned(
        shared,
        {
            "ts": "2026-04-20T00:00:00Z",
            "event": "eventbus_handler",
            "task": task_id,
            "handler": "h",
            "name": "h",
        },
    )
    # No record in the per-task log. Reader MUST fail to match handler 'h'.
    with pytest.raises(ValueError) as exc_info:
        receipt_post_completion(td, [{"name": "h"}])
    assert "post-completion handler not in events" in str(exc_info.value)


# ---------------------------------------------------------------------------
# AC16: signed per-task event passes verification
# ---------------------------------------------------------------------------


def test_receipt_post_completion_accepts_signed_per_task_event(tmp_path: Path):
    root, td, key = _prepare_task(tmp_path)
    task_id = td.name

    _append_signed(
        td / "events.jsonl",
        {
            "ts": "2026-04-20T00:00:00Z",
            "event": "eventbus_handler",
            "task": task_id,
            "handler": "h",
            "name": "h",
        },
        key,
    )

    receipt_path = receipt_post_completion(td, [{"name": "h"}])
    assert receipt_path.exists()
    rec = json.loads(receipt_path.read_text())
    assert rec.get("self_verify") == "passed"


# ---------------------------------------------------------------------------
# AC16: unsigned per-task event rejected with discriminated reason
# ---------------------------------------------------------------------------


def test_receipt_post_completion_rejects_unsigned_per_task_event(tmp_path: Path):
    root, td, _key = _prepare_task(tmp_path)
    task_id = td.name

    _append_unsigned(
        td / "events.jsonl",
        {
            "ts": "2026-04-20T00:00:00Z",
            "event": "eventbus_handler",
            "task": task_id,
            "handler": "h",
            "name": "h",
        },
    )

    with pytest.raises(ValueError):
        receipt_post_completion(td, [{"name": "h"}])

    # And the discriminated signature-invalid event fires somewhere — either
    # in the shared log or the per-task log.
    found = _find_signature_invalid_reason(root, td, expected="missing_sig_field")
    assert found, (
        "Expected event_signature_invalid(reason=missing_sig_field) after "
        "reading an unsigned per-task event"
    )


def test_receipt_post_completion_rejects_mismatched_signature(tmp_path: Path):
    root, td, key = _prepare_task(tmp_path)
    task_id = td.name

    rec = {
        "ts": "2026-04-20T00:00:00Z",
        "event": "eventbus_handler",
        "task": task_id,
        "handler": "h",
        "name": "h",
    }
    # Compute a BAD sig deliberately.
    bad_sig = hmac.new(b"WRONG_KEY_BYTES_32" * 2, _canonical(rec), hashlib.sha256).hexdigest()
    path = td / "events.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({**rec, "sig": bad_sig}) + "\n")

    with pytest.raises(ValueError):
        receipt_post_completion(td, [{"name": "h"}])

    found = _find_signature_invalid_reason(root, td, expected="sig_mismatch")
    assert found, "Expected event_signature_invalid(reason=sig_mismatch)"


# ---------------------------------------------------------------------------
# Helper: grep both logs for the discriminated event
# ---------------------------------------------------------------------------


def _find_signature_invalid_reason(root: Path, td: Path, expected: str) -> bool:
    for path in (root / ".dynos" / "events.jsonl", td / "events.jsonl"):
        if not path.exists():
            continue
        for ln in path.read_text().splitlines():
            if not ln.strip():
                continue
            try:
                rec = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if rec.get("event") == "event_signature_invalid":
                if rec.get("reason") == expected:
                    return True
    return False


# ---------------------------------------------------------------------------
# AC16: collect_retrospectives / other readers verify per-task events
# ---------------------------------------------------------------------------


def test_collect_retrospectives_verifies_tampered_per_task_log(tmp_path: Path):
    """Tampered per-task log ought to be noticed by whatever downstream
    reader consumes it. The migrated readers (lib_core:2137, :2208) emit
    event_signature_invalid with the discriminated reason."""
    from lib_core import collect_retrospectives  # noqa: PLC0415

    root, td, key = _prepare_task(tmp_path, task_id="task-TAMPER")
    task_id = td.name

    # Place a minimal retrospective so collect_retrospectives has something
    # to ingest — the key invariant we are testing is that per-task event
    # reads do the signature check.
    retro_path = td / "task-retrospective.json"
    retro_path.write_text(
        json.dumps({"task_id": task_id, "quality_score": 0.5}), encoding="utf-8"
    )

    # Write a valid signed event first, then a tampered one.
    _append_signed(
        td / "events.jsonl",
        {"ts": "2026-04-20T00:00:00Z", "event": "foo", "task": task_id},
        key,
    )
    # Now a tampered line (valid shape + bad sig).
    tampered = {
        "ts": "2026-04-20T00:00:01Z",
        "event": "foo",
        "task": task_id,
        "sig": "deadbeef" * 8,
    }
    (td / "events.jsonl").open("a").write(json.dumps(tampered) + "\n")

    # Call collect_retrospectives — should not crash. If any migrated
    # reader consults the per-task log during collection, it must emit
    # event_signature_invalid; an unmigrated reader silently accepts and
    # the test fails to find the discriminated event.
    collect_retrospectives(root)
    # Clean the cache so we do not poison later calls.
    from lib_core import _COLLECT_RETRO_CACHE  # noqa: PLC0415

    _COLLECT_RETRO_CACHE.clear()

    # Policy per spec D3 AC16: reject with named event. Accept either
    # per-task or shared-log emission.
    assert _find_signature_invalid_reason(root, td, expected="sig_mismatch"), (
        "event_signature_invalid(reason=sig_mismatch) MUST fire when a "
        "migrated reader encounters a tampered per-task event"
    )
