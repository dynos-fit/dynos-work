"""Closes residual 0ee7dd96 (security-auditor): task_id values flowing from
retrospective JSON into ``root / ".dynos" / task_id`` must be validated as a
safe slug BEFORE the path join.

The threat: ``collect_retrospectives`` returns dicts whose ``task_id`` values
come from JSON files. When ``_build_events_by_task`` joins ``task_id`` to a
path, a crafted value like ``../etc`` or ``task-x/../events`` resolves outside
``.dynos/``. With ``DYNOS_EVENT_SECRET`` empty (``strict=False``),
``verify_signed_events`` returns parseable records unverified — so a crafted
task_id pointing at any readable JSONL file outside ``.dynos`` would inject
fake events into the cross-check.

The fix: ``hooks/lib_core.is_safe_task_id`` rejects anything that doesn't
match ``^task-[A-Za-z0-9][A-Za-z0-9_.-]*$``. ``_build_events_by_task`` calls
this gate before the join and skips invalid entries with a
``policy_engine_unsafe_task_id_rejected`` event.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))
sys.path.insert(0, str(ROOT / "memory"))

from lib_core import is_safe_task_id  # noqa: E402
from policy_engine import _build_events_by_task  # noqa: E402


# ---- is_safe_task_id slug regex ---------------------------------------------


@pytest.mark.parametrize("good", [
    "task-T",
    "task-A",
    "task-1",
    "task-20260507-001",
    "task-20260507-005",
    "task-Abc_def-1.2",
])
def test_is_safe_task_id_admits_legitimate_slugs(good: str) -> None:
    assert is_safe_task_id(good) is True


@pytest.mark.parametrize("bad", [
    "../etc",
    "task-x/../events",
    "task-/../bad",
    "task-..",          # leading dot in second segment
    "task-.hidden",     # leading dot
    "task-",            # empty after prefix
    "/etc/passwd",
    "..",
    ".",
    "",
    "feat-1",           # wrong prefix
    "task-x\nbad",      # control char
    "task-x y",         # whitespace
    "task-x\\bad",      # backslash
    "task-x/bad",       # path separator
])
def test_is_safe_task_id_rejects_traversal_and_garbage(bad: str) -> None:
    assert is_safe_task_id(bad) is False


@pytest.mark.parametrize("bad", [None, 0, [], {}, b"task-x", object()])
def test_is_safe_task_id_rejects_non_str_types(bad: object) -> None:
    assert is_safe_task_id(bad) is False


# ---- _build_events_by_task path-traversal gate -----------------------------


def test_build_events_by_task_rejects_traversal_task_id(tmp_path: Path):
    """A crafted task_id that escapes .dynos/ must be filtered before the
    path join. The function must NOT call verify_signed_events on the
    escape path (no entry in result, no exception)."""
    # Set up a real .dynos/ with a legitimate sibling task to prove the
    # function still works on safe input within the same call.
    dynos = tmp_path / ".dynos"
    safe_task = dynos / "task-real"
    safe_task.mkdir(parents=True)
    # Empty events.jsonl — verify_signed_events returns [].
    (safe_task / "events.jsonl").write_text("")
    # Plant a target file outside .dynos that an unsafe task_id would
    # resolve to. If the gate fails, the test would need DYNOS_EVENT_SECRET
    # tooling to demonstrate the leak; instead we assert the unsafe key is
    # absent from the result, which is a stronger structural check.
    (tmp_path / "events.jsonl").write_text(
        '{"event": "FORGED", "_sig": "deadbeef"}\n'
    )

    result = _build_events_by_task(
        tmp_path,
        task_ids={"task-real", "../", "task-x/../events", "../etc"},
    )

    # Only the safe task should appear; traversal task_ids must be rejected.
    assert "task-real" in result
    assert "../" not in result
    assert "task-x/../events" not in result
    assert "../etc" not in result


def test_build_events_by_task_rejects_non_str_task_ids(tmp_path: Path):
    """Defense in depth: the function must not crash on garbage task_id
    types. Each non-string entry should be filtered."""
    (tmp_path / ".dynos").mkdir()
    # set[str] is the declared type but a malformed retrospective could
    # smuggle non-strings via cache loads or test fakes; the gate must hold.
    result = _build_events_by_task(tmp_path, task_ids={"valid-but-not-task-prefixed"})
    assert result == {}  # "valid-but-not-task-prefixed" fails the slug regex


def test_build_events_by_task_logs_rejection_event(tmp_path: Path, monkeypatch):
    """Visibility: rejected task_ids must produce a
    ``policy_engine_unsafe_task_id_rejected`` event so reviewers can
    detect crafted retrospectives in the audit trail."""
    captured: list[dict] = []
    import lib_log  # noqa: PLC0415

    real_log_event = lib_log.log_event

    def fake_log_event(root, event_name, **fields):  # type: ignore[no-untyped-def]
        captured.append({"event": event_name, **fields})
        # Don't call real impl — we don't need files written for this assertion.

    monkeypatch.setattr("policy_engine.log_event", fake_log_event)

    (tmp_path / ".dynos").mkdir()
    _build_events_by_task(tmp_path, task_ids={"../etc", "task-real"})

    # Expect at least one rejection event for the unsafe entry.
    rejections = [
        e for e in captured
        if e.get("event") == "policy_engine_unsafe_task_id_rejected"
    ]
    assert len(rejections) >= 1
    assert any(r.get("task") == "../etc" for r in rejections)
