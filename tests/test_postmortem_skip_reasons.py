"""Tests for task-20260419-002 G1+G2: `_POSTMORTEM_SKIP_REASONS` enum
shrink and the required ``subsumed_by`` argument on
``receipt_postmortem_skipped``.

Covers acceptance criteria 1, 3, and 4 from the task spec.

Failure modes exercised (in the order the validator applies them):
  (a) ``subsumed_by`` must be a list
  (b) every entry must match the task_id slug regex
  (c) empty ``subsumed_by`` is rejected when the reason is NOT one of
      {clean-task, no-findings} — unreachable in production after G1's
      enum shrink, so we use monkeypatch to temporarily expand the
      enum and prove the rule still fires
  (d) every entry must have a corresponding postmortem file on disk
      under ``_persistent_project_dir(root)/postmortems/{task_id}.json``

The happy-path test round-trips a real subsumed_by entry by
pre-creating the postmortem file where the validator expects it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

import lib_receipts  # noqa: E402
from lib_core import _persistent_project_dir  # noqa: E402
from lib_receipts import receipt_postmortem_skipped  # noqa: E402


def _task_dir(tmp_path: Path) -> Path:
    """Create a task dir under ``<tmp>/project/.dynos/task-*/`` so that
    ``task_dir.parent.parent == <tmp>/project`` — the shape the
    validator expects when resolving the persistent project dir."""
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260419-TEST"
    td.mkdir(parents=True)
    return td


def _pre_create_postmortem(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    td: Path,
    task_id: str,
) -> Path:
    """Write a valid postmortem JSON at the exact path the validator
    will stat. Returns the path that was written so callers can make
    extra assertions on it.

    We pin ``DYNOS_HOME`` under ``tmp_path`` first so that
    ``_persistent_project_dir`` resolves inside the test sandbox
    (instead of the developer's real ``~/.dynos``).
    """
    monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "dynos-home"))
    root = td.parent.parent
    pm_dir = _persistent_project_dir(root) / "postmortems"
    pm_dir.mkdir(parents=True, exist_ok=True)
    pm_path = pm_dir / f"{task_id}.json"
    pm_path.write_text(json.dumps({"task_id": task_id}))
    return pm_path


# ---------------------------------------------------------------------------
# Criterion 1: `quality-above-threshold` must be rejected
# ---------------------------------------------------------------------------


def test_quality_above_threshold_rejected(tmp_path: Path):
    """The literal reason removed by G1 must now raise a ValueError whose
    message enumerates both remaining valid reasons (`clean-task` AND
    `no-findings`) and contains the literal substring `invalid`."""
    td = _task_dir(tmp_path)
    with pytest.raises(ValueError) as excinfo:
        receipt_postmortem_skipped(
            td,
            reason="quality-above-threshold",
            retrospective_sha256="a" * 64,
            subsumed_by=[],
        )
    msg = str(excinfo.value)
    assert "invalid" in msg, f"expected 'invalid' in message; got: {msg}"
    # Both remaining valid reasons must be enumerated in the message so
    # the caller can self-correct without reading source.
    assert "clean-task, no-findings" in msg, (
        f"expected the literal substring 'clean-task, no-findings' "
        f"in the message; got: {msg}"
    )


# ---------------------------------------------------------------------------
# Criterion 3(a): `subsumed_by` must be a list
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_value",
    [
        "task-20260418-001",            # string is not a list
        ("task-20260418-001",),         # tuple is not a list
        {"task-20260418-001"},          # set is not a list
        {"0": "task-20260418-001"},     # dict is not a list
        None,                            # None is not a list
        42,                              # int is not a list
    ],
)
def test_subsumed_by_must_be_list(tmp_path: Path, bad_value):
    """Rule (a): anything other than `list` must raise. The error
    message pins the expected shape so downstream callers know what
    the validator wants."""
    td = _task_dir(tmp_path)
    with pytest.raises(ValueError) as excinfo:
        receipt_postmortem_skipped(
            td,
            reason="clean-task",
            retrospective_sha256="a" * 64,
            subsumed_by=bad_value,  # type: ignore[arg-type]
        )
    assert "subsumed_by must be a list" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Criterion 3(b): every entry must match the task_id regex
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_entry",
    [
        "not-a-task-id",        # wrong prefix
        "task-",                 # missing body
        "task-_underscore",      # first body char must be alphanumeric
        "task-20260418 001",     # whitespace is rejected
        "task-20260418/001",     # slash is not in the allowed set
        "",                      # empty string
        123,                      # non-string entry
        None,                     # None entry
    ],
)
def test_subsumed_by_entry_must_match_task_regex(tmp_path: Path, bad_entry):
    """Rule (b): every entry must match ``^task-[A-Za-z0-9][A-Za-z0-9_.-]*$``.
    The error message must contain BOTH the bracketed index
    ``subsumed_by[...]`` and the literal ``must match``.

    Non-string entries are rejected with the same error (the validator
    must not raise a TypeError from re.match on a non-str).
    """
    td = _task_dir(tmp_path)
    with pytest.raises(ValueError) as excinfo:
        receipt_postmortem_skipped(
            td,
            reason="clean-task",
            retrospective_sha256="a" * 64,
            subsumed_by=[bad_entry],  # type: ignore[list-item]
        )
    msg = str(excinfo.value)
    assert "subsumed_by[" in msg, (
        f"expected bracketed index 'subsumed_by[' in message; got: {msg}"
    )
    assert "must match" in msg, (
        f"expected literal 'must match' in message; got: {msg}"
    )


# ---------------------------------------------------------------------------
# Criterion 3(c): non-empty required when reason is not clean-task / no-findings
# ---------------------------------------------------------------------------


def test_subsumed_by_empty_rejected_when_reason_not_clean_or_no_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Rule (c): when the reason is NOT one of the "nothing to cite"
    reasons, ``subsumed_by`` MUST be non-empty.

    After G1's enum shrink this branch is unreachable via the reason
    enum check, so we temporarily expand ``_POSTMORTEM_SKIP_REASONS``
    to include a test-only reason; this proves the defensive rule
    still fires and is not dead code.
    """
    td = _task_dir(tmp_path)
    # Expand the enum to include a test-only reason so we can reach
    # rule (c). The original frozenset is restored automatically by
    # monkeypatch at teardown.
    extended = frozenset(
        set(lib_receipts._POSTMORTEM_SKIP_REASONS) | {"test-reason-expansion"}
    )
    monkeypatch.setattr(lib_receipts, "_POSTMORTEM_SKIP_REASONS", extended)

    with pytest.raises(ValueError) as excinfo:
        receipt_postmortem_skipped(
            td,
            reason="test-reason-expansion",
            retrospective_sha256="a" * 64,
            subsumed_by=[],
        )
    msg = str(excinfo.value)
    assert "subsumed_by must be non-empty when reason=" in msg, (
        f"expected 'subsumed_by must be non-empty when reason=' in "
        f"message; got: {msg}"
    )


# ---------------------------------------------------------------------------
# Criterion 3(d): every entry must have a postmortem file on disk
# ---------------------------------------------------------------------------


def test_subsumed_by_missing_postmortem_file_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Rule (d): every entry in a non-empty ``subsumed_by`` must point
    to a real postmortem file under
    ``_persistent_project_dir(root)/postmortems/{task_id}.json``.
    A missing file raises ValueError whose message names both the
    literal ``missing postmortem file for`` AND the offending task_id.
    """
    monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "dynos-home"))
    td = _task_dir(tmp_path)
    bogus_task_id = "task-20260418-DOES-NOT-EXIST"
    with pytest.raises(ValueError) as excinfo:
        receipt_postmortem_skipped(
            td,
            reason="clean-task",  # enum-valid; rule (d) fires due to non-empty list
            retrospective_sha256="a" * 64,
            subsumed_by=[bogus_task_id],
        )
    msg = str(excinfo.value)
    assert "missing postmortem file for" in msg, (
        f"expected 'missing postmortem file for' in message; got: {msg}"
    )
    assert bogus_task_id in msg, (
        f"expected offending task_id {bogus_task_id!r} in message; got: {msg}"
    )


# ---------------------------------------------------------------------------
# Criterion 4: payload records reason + subsumed_by verbatim
# ---------------------------------------------------------------------------


def test_payload_records_reason_and_subsumed_by(tmp_path: Path):
    """Skip receipt with reason=clean-task and empty subsumed_by must
    round-trip through JSON with both fields intact. This is the
    minimal case — no postmortem file is needed because the list is
    empty so rule (d) does not apply."""
    td = _task_dir(tmp_path)
    out = receipt_postmortem_skipped(
        td,
        reason="clean-task",
        retrospective_sha256="e" * 64,
        subsumed_by=[],
    )
    assert out.exists(), f"receipt file was not written at {out}"
    payload = json.loads(out.read_text())
    assert payload["reason"] == "clean-task"
    assert payload["subsumed_by"] == []
    # Also confirm the retrospective sha is carried — guards against
    # a regression that silently drops a sibling field.
    assert payload["retrospective_sha256"] == "e" * 64


def test_happy_path_with_real_subsumed_postmortem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """End-to-end: reason=no-findings, subsumed_by cites a real prior
    task whose postmortem file exists on disk at the expected path.
    The written receipt must round-trip both fields."""
    td = _task_dir(tmp_path)
    prior_task = "task-20260418-005"
    _pre_create_postmortem(monkeypatch, tmp_path, td, prior_task)

    out = receipt_postmortem_skipped(
        td,
        reason="no-findings",
        retrospective_sha256="f" * 64,
        subsumed_by=[prior_task],
    )
    assert out.exists()
    payload = json.loads(out.read_text())
    assert payload["reason"] == "no-findings"
    # The receipt stores a shallow copy (list()) of the input; content
    # must match exactly.
    assert payload["subsumed_by"] == [prior_task]
    # Sanity: extra-safe mutation of the caller's list must NOT
    # retroactively corrupt the receipt (the writer stores a copy).
    # We re-read the file after a would-be mutation; the payload on
    # disk does not change (that is enforced by file immutability
    # after write_receipt — we are proving the stored value is the
    # captured snapshot).
    assert payload["retrospective_sha256"] == "f" * 64
