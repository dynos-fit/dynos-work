"""Adversarial test: forged events in the project-global events.jsonl must
be invisible to _build_events_by_task when reading per-task event files.

This test replicates the exact HMAC derivation used by lib_log._derive_per_task_secret
and lib_log.sign_event so that the legitimately signed event passes verification while
the forged global event is dropped.

Signing format (must match lib_log.sign_event exactly):
  1. Filter the payload dict to exclude the "_sig" key.
  2. Serialize with json.dumps(..., sort_keys=True, separators=(",", ":"),
     ensure_ascii=False, default=str).encode("utf-8").
  3. HMAC-SHA256 over the canonical bytes using the per-task-derived secret.
  4. Store hexdigest as "_sig" in the event dict.

Per-task key derivation (must match lib_log._derive_per_task_secret exactly):
  1. prk = hmac(key=task_id.encode("utf-8"), msg=project_secret.encode("utf-8"),
               digestmod=sha256).digest()
  2. okm = hmac(key=prk, msg=b"dynos-work/v1/per-task-event-secret" + b"\\x01",
               digestmod=sha256).digest()
  3. Return okm.hex()
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "memory"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from policy_engine import _build_events_by_task  # noqa: E402


TEST_SECRET = "test-secret-not-empty"
TASK_TARGET = "task-target"
TASK_ATTACKER = "task-attacker"

_HKDF_INFO = b"dynos-work/v1/per-task-event-secret" + b"\x01"


def _derive_per_task_secret(project_secret: str, task_id: str) -> str:
    """Replicate lib_log._derive_per_task_secret (HKDF-SHA256 single expand block).

    Salt = task_id (key), IKM = project_secret (msg), then one HKDF expand
    step using the versioned info label.
    """
    prk = hmac.new(
        task_id.encode("utf-8"),
        project_secret.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    okm = hmac.new(
        prk,
        _HKDF_INFO,
        hashlib.sha256,
    ).digest()
    return okm.hex()


def _sign_event(payload: dict, task_id: str, project_secret: str) -> dict:
    """Sign event under the per-task derived key, exactly as lib_log.sign_event does.

    Excludes "_sig" from canonical payload, uses compact separators and sort_keys=True.
    Stores result as "_sig".
    """
    per_task_secret = _derive_per_task_secret(project_secret, task_id)
    without_sig = {k: v for k, v in payload.items() if k != "_sig"}
    canonical = json.dumps(
        without_sig,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    sig = hmac.new(
        per_task_secret.encode("utf-8"),
        canonical,
        hashlib.sha256,
    ).hexdigest()
    return {**without_sig, "_sig": sig}


def test_cross_task_forgery_invisible_to_per_task_read(tmp_path, monkeypatch):
    """A forged event appended to the project-global events.jsonl claiming
    task_id=task-target must not appear in the result of
    _build_events_by_task(root, task_ids={"task-target"}).

    Verifies AC 7 and AC 8: the per-task read path is HMAC-gated so that
    an attacker who writes handler-Y to the global file cannot inflate
    effectiveness scores for task-target.
    """
    monkeypatch.setenv("DYNOS_EVENT_SECRET", TEST_SECRET)

    dynos = tmp_path / ".dynos"
    dynos.mkdir()

    # --- task-target: legitimate signed event for handler-X ---
    target_dir = dynos / TASK_TARGET
    target_dir.mkdir()
    legit_event = {
        "event": "learned_agent_applied",
        "task_id": TASK_TARGET,
        "agent_name": "handler-X",
        "segment_id": "backend-executor",
    }
    signed_legit = _sign_event(legit_event, TASK_TARGET, TEST_SECRET)
    (target_dir / "events.jsonl").write_text(
        json.dumps(signed_legit) + "\n", encoding="utf-8"
    )

    # --- task-attacker: separate dir (exists but is not queried) ---
    attacker_dir = dynos / TASK_ATTACKER
    attacker_dir.mkdir()
    (attacker_dir / "events.jsonl").write_text("", encoding="utf-8")

    # --- project-global events.jsonl: forged event claiming task-target ---
    # Uses an intentionally invalid _sig to simulate a forgery attempt.
    forged_event = {
        "event": "learned_agent_applied",
        "task_id": TASK_TARGET,
        "agent_name": "handler-Y",
        "segment_id": "backend-executor",
        "_sig": "deadbeef" * 8,  # 64 hex chars, invalid HMAC
    }
    (dynos / "events.jsonl").write_text(
        json.dumps(forged_event) + "\n", encoding="utf-8"
    )

    result = _build_events_by_task(tmp_path, task_ids={TASK_TARGET})

    assert TASK_TARGET in result, "task-target must appear in result"
    agent_names = [e.get("agent_name") for e in result[TASK_TARGET]]
    assert "handler-X" in agent_names, (
        "legitimately signed event for handler-X must be present in per-task read"
    )
    assert "handler-Y" not in agent_names, (
        "forged global event for handler-Y must NOT appear in per-task verified result"
    )


def test_task_ids_none_returns_empty_without_file_io(tmp_path, monkeypatch):
    """When task_ids is None, _build_events_by_task must return {} immediately
    without reading any file — even if events.jsonl exists.
    """
    monkeypatch.setenv("DYNOS_EVENT_SECRET", TEST_SECRET)

    dynos = tmp_path / ".dynos"
    dynos.mkdir()
    # Write a global events.jsonl so we can detect if it was read.
    (dynos / "events.jsonl").write_text(
        json.dumps({"event": "learned_agent_applied", "task_id": "task-x"}) + "\n",
        encoding="utf-8",
    )

    result = _build_events_by_task(tmp_path, task_ids=None)

    assert result == {}, (
        "task_ids=None must return {} immediately (AC 3: conservative/secure behavior)"
    )


def test_empty_task_ids_returns_empty_without_file_io(tmp_path, monkeypatch):
    """When task_ids is an empty set, _build_events_by_task must return {}
    without reading any file.
    """
    monkeypatch.setenv("DYNOS_EVENT_SECRET", TEST_SECRET)

    dynos = tmp_path / ".dynos"
    dynos.mkdir()
    (dynos / "events.jsonl").write_text(
        json.dumps({"event": "learned_agent_applied", "task_id": "task-x"}) + "\n",
        encoding="utf-8",
    )

    result = _build_events_by_task(tmp_path, task_ids=set())

    assert result == {}, (
        "empty task_ids set must return {} (AC 3: no global read on empty input)"
    )


def test_missing_task_dir_omitted_no_global_fallback(tmp_path, monkeypatch):
    """When task_ids contains a task_id whose directory does not exist,
    that task_id must be omitted from the result — no fallback to
    the project-global events.jsonl must occur (AC 5, AC 6).
    """
    monkeypatch.setenv("DYNOS_EVENT_SECRET", TEST_SECRET)

    dynos = tmp_path / ".dynos"
    dynos.mkdir()

    # Global file contains a valid-looking event for the missing task.
    global_event = {
        "event": "learned_agent_applied",
        "task_id": "task-missing",
        "agent_name": "handler-global",
        "segment_id": "backend-executor",
    }
    (dynos / "events.jsonl").write_text(
        json.dumps(global_event) + "\n", encoding="utf-8"
    )

    # task-missing directory does NOT exist.
    result = _build_events_by_task(tmp_path, task_ids={"task-missing"})

    assert "task-missing" not in result, (
        "missing task dir must be omitted from result — no global fallback (AC 5)"
    )
    # Verify the global file was not read: if it was, handler-global would appear.
    all_agent_names = [
        e.get("agent_name")
        for events in result.values()
        for e in events
    ]
    assert "handler-global" not in all_agent_names, (
        "global fallback read must not occur for missing task directory (AC 6)"
    )


def test_per_task_read_uses_hmac_verification(tmp_path, monkeypatch):
    """An event written to task-target/events.jsonl with a wrong HMAC must
    be excluded from the result, even when task_ids={"task-target"}.
    This confirms verify_signed_events is actually being called with the secret.
    """
    monkeypatch.setenv("DYNOS_EVENT_SECRET", TEST_SECRET)

    dynos = tmp_path / ".dynos"
    dynos.mkdir()

    target_dir = dynos / TASK_TARGET
    target_dir.mkdir()

    # Write an event with an invalid _sig directly to the per-task file.
    bad_event = {
        "event": "learned_agent_applied",
        "task_id": TASK_TARGET,
        "agent_name": "handler-bad",
        "segment_id": "backend-executor",
        "_sig": "badbadba" * 8,
    }
    (target_dir / "events.jsonl").write_text(
        json.dumps(bad_event) + "\n", encoding="utf-8"
    )

    result = _build_events_by_task(tmp_path, task_ids={TASK_TARGET})

    assert TASK_TARGET in result, "task-target dir exists so key must appear in result"
    agent_names = [e.get("agent_name") for e in result[TASK_TARGET]]
    assert "handler-bad" not in agent_names, (
        "event with invalid HMAC in per-task file must be excluded by verify_signed_events"
    )


def test_legitimate_event_survives_verification(tmp_path, monkeypatch):
    """A correctly signed event in task-target/events.jsonl must survive
    verify_signed_events and appear in the result. This closes the false-negative
    window: if signing derivation is wrong, handler-X will be absent and
    this test will fail.
    """
    monkeypatch.setenv("DYNOS_EVENT_SECRET", TEST_SECRET)

    dynos = tmp_path / ".dynos"
    dynos.mkdir()

    target_dir = dynos / TASK_TARGET
    target_dir.mkdir()

    legit_event = {
        "event": "learned_agent_applied",
        "task_id": TASK_TARGET,
        "agent_name": "handler-X",
        "segment_id": "backend-executor",
    }
    signed_legit = _sign_event(legit_event, TASK_TARGET, TEST_SECRET)
    (target_dir / "events.jsonl").write_text(
        json.dumps(signed_legit) + "\n", encoding="utf-8"
    )

    result = _build_events_by_task(tmp_path, task_ids={TASK_TARGET})

    assert TASK_TARGET in result
    agent_names = [e.get("agent_name") for e in result[TASK_TARGET]]
    assert "handler-X" in agent_names, (
        "correctly signed handler-X event must survive HMAC verification"
    )


def test_multiple_task_ids_only_reads_requested_tasks(tmp_path, monkeypatch):
    """When task_ids={"task-A"}, only task-A's per-task file is read.
    task-B's events must not appear in the result even if task-B's dir exists.
    """
    monkeypatch.setenv("DYNOS_EVENT_SECRET", TEST_SECRET)

    dynos = tmp_path / ".dynos"
    dynos.mkdir()

    # task-A: properly signed event
    task_a_dir = dynos / "task-A"
    task_a_dir.mkdir()
    event_a = {"event": "learned_agent_applied", "task_id": "task-A",
                "agent_name": "handler-A", "segment_id": "backend-executor"}
    signed_a = _sign_event(event_a, "task-A", TEST_SECRET)
    (task_a_dir / "events.jsonl").write_text(
        json.dumps(signed_a) + "\n", encoding="utf-8"
    )

    # task-B: properly signed event (but not queried)
    task_b_dir = dynos / "task-B"
    task_b_dir.mkdir()
    event_b = {"event": "learned_agent_applied", "task_id": "task-B",
                "agent_name": "handler-B", "segment_id": "backend-executor"}
    signed_b = _sign_event(event_b, "task-B", TEST_SECRET)
    (task_b_dir / "events.jsonl").write_text(
        json.dumps(signed_b) + "\n", encoding="utf-8"
    )

    result = _build_events_by_task(tmp_path, task_ids={"task-A"})

    assert "task-A" in result
    assert "task-B" not in result, (
        "task-B must not appear in result when not in task_ids"
    )
    all_names = [e.get("agent_name") for e in result.get("task-A", [])]
    assert "handler-A" in all_names
    assert "handler-B" not in all_names
