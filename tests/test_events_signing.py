"""TDD-first tests for task-20260420-001 D3 part 2 — log_event signing.

Covers acceptance criteria 13, 14, 15, 18:

    AC13 log_event(root, evt, task=X, ...) signs per-task:
         - writes to .dynos/task-{X}/events.jsonl (NOT shared log)
         - `sig` field is HMAC-SHA256(key, canonical_json_bytes_of_record_without_sig)
         - canonical: sort_keys=True, separators=(',',':'), default=str, ensure_ascii=False
    AC14 missing key + task-attributed emission raises RuntimeError.
         No silent fallback to the shared log.
    AC15 log_event with no task= still writes unsigned to shared log
         (maintenance / cold-start escape hatch).
    AC18 DYNOS_EVENT_SIGNING_DISABLED truthy:
         - signing skipped
         - `event_signing_bypassed` event emitted once (per process / per task)

Plus implicit-requirement: _canonical_sig_bytes is stable across calls.

TODAY these tests FAIL because the signing path has not been added to lib_log.py.
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

from lib_log import log_event  # noqa: E402


def _prepare_task(root: Path, task_id: str = "task-X") -> tuple[Path, bytes]:
    """Create .dynos/task-{id}/ and a valid .events-key. Returns (task_dir, key_bytes)."""
    td = root / ".dynos" / task_id
    td.mkdir(parents=True, exist_ok=True)
    key_bytes = secrets.token_bytes(32)
    b64 = base64.b64encode(key_bytes).decode("ascii")
    (td / ".events-key").write_text(b64, encoding="utf-8")
    try:
        (td / ".events-key").chmod(0o600)
    except OSError:
        pass
    return td, key_bytes


def _canonical(record_without_sig: dict) -> bytes:
    return json.dumps(
        record_without_sig,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        ensure_ascii=False,
    ).encode("utf-8")


def _make_root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    (root / ".dynos").mkdir(exist_ok=True)
    return root


# ---------------------------------------------------------------------------
# AC13 — signing: per-task + correct HMAC
# ---------------------------------------------------------------------------


def test_log_event_with_task_writes_to_per_task_jsonl(tmp_path: Path):
    root = _make_root(tmp_path)
    td, _ = _prepare_task(root, "task-X")

    log_event(root, "foo", task="task-X", bar=1)

    per_task = td / "events.jsonl"
    shared = root / ".dynos" / "events.jsonl"
    assert per_task.exists(), "task-attributed event must land in per-task log"
    assert not shared.exists() or shared.stat().st_size == 0, (
        "task-attributed event MUST NOT duplicate to shared log"
    )


def test_log_event_signature_present_and_valid(tmp_path: Path):
    root = _make_root(tmp_path)
    td, key_bytes = _prepare_task(root, "task-Y")

    log_event(root, "foo", task="task-Y", bar=1, nested={"z": 3, "a": 1})

    lines = [
        ln for ln in (td / "events.jsonl").read_text().splitlines() if ln.strip()
    ]
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert "sig" in rec, "signed events MUST carry a 'sig' field"
    expected_sig = hmac.new(
        key_bytes,
        _canonical({k: v for k, v in rec.items() if k != "sig"}),
        hashlib.sha256,
    ).hexdigest()
    assert hmac.compare_digest(rec["sig"], expected_sig), (
        f"sig mismatch: recomputed {expected_sig!r} vs stored {rec['sig']!r}"
    )


# ---------------------------------------------------------------------------
# AC15 — maintenance events without task kwarg still write unsigned to shared
# ---------------------------------------------------------------------------


def test_log_event_without_task_writes_to_shared_log_unsigned(tmp_path: Path):
    root = _make_root(tmp_path)

    log_event(root, "maintenance_cycle", note="daemon_tick")

    shared = root / ".dynos" / "events.jsonl"
    assert shared.exists()
    lines = [ln for ln in shared.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert "sig" not in rec, "shared-log events MUST be unsigned"
    assert rec["event"] == "maintenance_cycle"


# ---------------------------------------------------------------------------
# AC14 — missing key + task-attributed → RuntimeError
# ---------------------------------------------------------------------------


def test_log_event_missing_key_raises(tmp_path: Path):
    root = _make_root(tmp_path)
    td = root / ".dynos" / "task-noKEY"
    td.mkdir(parents=True)  # dir exists, but NO .events-key file

    with pytest.raises(RuntimeError) as exc_info:
        log_event(root, "foo", task="task-noKEY", a=1)

    msg = str(exc_info.value)
    assert ".events-key" in msg, (
        "RuntimeError must name the missing .events-key path for forensic triage"
    )


def test_log_event_missing_key_does_not_silently_fall_back_to_shared(tmp_path: Path):
    root = _make_root(tmp_path)
    td = root / ".dynos" / "task-A"
    td.mkdir(parents=True)
    with pytest.raises(RuntimeError):
        log_event(root, "foo", task="task-A")
    shared = root / ".dynos" / "events.jsonl"
    assert not shared.exists() or shared.stat().st_size == 0, (
        "silent fallback to the shared log recreates the forgery window"
    )


# ---------------------------------------------------------------------------
# AC18 — DYNOS_EVENT_SIGNING_DISABLED bypass
# ---------------------------------------------------------------------------


def test_env_var_bypasses_signing_and_emits_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _make_root(tmp_path)
    td, _ = _prepare_task(root, "task-BP")

    monkeypatch.setenv("DYNOS_EVENT_SIGNING_DISABLED", "1")

    # Reset any cached per-process bypass state so the test is deterministic.
    import lib_log  # noqa: PLC0415

    for name in (
        "_BYPASS_EVENT_EMITTED",
        "_BYPASS_EMITTED_PER_TASK",
    ):
        if hasattr(lib_log, name):
            try:
                if isinstance(getattr(lib_log, name), set):
                    getattr(lib_log, name).clear()
                else:
                    setattr(lib_log, name, False)
            except Exception:
                pass

    log_event(root, "foo", task="task-BP", a=1)
    log_event(root, "foo", task="task-BP", a=2)

    lines = [
        ln for ln in (td / "events.jsonl").read_text().splitlines() if ln.strip()
    ]
    bypass_events = [
        json.loads(ln)
        for ln in lines
        if json.loads(ln).get("event") == "event_signing_bypassed"
    ]
    assert len(bypass_events) == 1, (
        "event_signing_bypassed MUST be emitted exactly once per task per process "
        f"(got {len(bypass_events)})"
    )
    # And the foo records are unsigned.
    foo_events = [
        json.loads(ln) for ln in lines if json.loads(ln).get("event") == "foo"
    ]
    assert len(foo_events) == 2
    for rec in foo_events:
        assert "sig" not in rec, "bypass mode must not sign the record"


def test_env_var_false_value_does_not_bypass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """ADR-7: values in {'', '0', 'false', 'no'} (case-insensitive) are NOT truthy."""
    root = _make_root(tmp_path)
    td, _ = _prepare_task(root, "task-NB")

    monkeypatch.setenv("DYNOS_EVENT_SIGNING_DISABLED", "false")

    log_event(root, "foo", task="task-NB", a=1)

    lines = [
        ln for ln in (td / "events.jsonl").read_text().splitlines() if ln.strip()
    ]
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert "sig" in rec, "env var='false' must NOT disable signing"


# ---------------------------------------------------------------------------
# Implicit requirement: canonical-bytes helper is stable
# ---------------------------------------------------------------------------


def test_canonical_bytes_helper_is_stable():
    """Repeated canonical-byte compute over the same logical record MUST
    produce identical bytes — any ordering drift breaks verification."""
    rec1 = {"b": 2, "a": 1, "z": [3, 2, 1]}
    rec2 = {"z": [3, 2, 1], "a": 1, "b": 2}
    b1 = _canonical(rec1)
    b2 = _canonical(rec2)
    assert b1 == b2
    # And the module-level producer (if importable) must match this spec.
    try:
        from lib_log import _canonical_sig_bytes as lib_canonical  # noqa: PLC0415
    except ImportError:
        try:
            from lib_signed_events import _canonical_sig_bytes as lib_canonical  # noqa: PLC0415
        except ImportError:
            pytest.fail(
                "Shared _canonical_sig_bytes helper must exist in lib_log "
                "or lib_signed_events (single source of truth per ADR-4)"
            )
    assert lib_canonical(rec1) == b1
    assert lib_canonical(rec2) == b1
