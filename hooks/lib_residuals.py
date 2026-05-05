#!/usr/bin/env python3
"""Proactive residual-findings queue (task-20260504-007).

Producer/consumer queue for non-blocking audit findings. The queue lives at
``<root>/.dynos/proactive-findings.json`` with shape ``{"findings": [...]}``.

This module is intentionally independent of ``ctl.py``. It depends only on
``lib_core`` (for ``load_json``/``write_json``) and the Python standard
library so the producer hook can use it without importing the full ctl
machinery, and so the run-next skill can use it from a separate process.

Concurrency model
-----------------
``ingest_findings`` and ``update_row_status`` both perform the read-modify-
write under a single ``fcntl.LOCK_EX`` held on the queue file FD itself
(not on a temp file). Writes go through a same-directory temp file followed
by ``os.rename`` for crash safety; the rename happens while the lock is
still held so concurrent callers serialize cleanly.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from lib_core import load_json


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALLOWED_AUDITORS: frozenset[str] = frozenset({
    "claude-md-auditor",
    "dead-code-auditor",
})

REJECTED_CATEGORIES: frozenset[str] = frozenset({
    "tool-error",
    "no-rules-found",
    "no-files-changed",
})

VALID_STATUSES: frozenset[str] = frozenset({
    "pending",
    "in_progress",
    "done",
    "failed",
})

DEDUP_BLOCKING_STATUSES: frozenset[str] = frozenset({
    "pending",
    "in_progress",
})

# Anchored HTML-comment regex. Both the ``<!-- residual-id: `` prefix and
# the closing `` -->`` suffix must be present; a bare ``residual-id:``
# substring must NOT match (spec.md Risk Note 4).
#
# The ID payload is one or more characters that are NOT whitespace and do
# NOT contain ``-->``. We use a greedy-but-bounded character class that
# rejects whitespace and the ``>`` character to keep the pattern linear-
# time and ReDoS-free (no nested quantifiers, no alternation over the
# same prefix).
_RESIDUAL_ID_RE = re.compile(r"<!-- residual-id: ([^\s>]+) -->")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def queue_path(root: Path) -> Path:
    """Return the canonical queue-file path for a project root."""
    return Path(root) / ".dynos" / "proactive-findings.json"


def load_queue(qpath: Path) -> dict:
    """Read the queue file and return ``{"findings": [...]}``.

    Returns ``{"findings": []}`` if the file is absent. Does NOT create the
    file as a side-effect of reading.
    """
    p = Path(qpath)
    if not p.exists():
        return {"findings": []}
    try:
        data = load_json(p)
    except (json.JSONDecodeError, OSError):
        # Corrupt or unreadable queue is treated as empty rather than
        # surfaced — the producer must never crash the audit pipeline.
        return {"findings": []}
    if not isinstance(data, dict):
        return {"findings": []}
    findings = data.get("findings")
    if not isinstance(findings, list):
        return {"findings": []}
    return {"findings": findings}


def compute_fingerprint(auditor_name: str, finding_id: str, description: str) -> str:
    """Lowercase hex SHA-256 of ``auditor_name + ':' + finding_id + ':' + description``.

    No salt; deterministic across processes so dedup works between the
    producer and any future replays.
    """
    payload = f"{auditor_name}:{finding_id}:{description}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def extract_residual_id(raw_input_text: str) -> Optional[str]:
    """Return the first residual-id from a fully-formed HTML comment.

    Matches ``<!-- residual-id: <id> -->`` exactly. A bare
    ``residual-id:`` substring without both delimiters does not match.
    Returns ``None`` if the pattern is absent or input is not a string.
    """
    if not isinstance(raw_input_text, str):
        return None
    m = _RESIDUAL_ID_RE.search(raw_input_text)
    if m is None:
        return None
    return m.group(1)


def select_next_pending(root: Path) -> Optional[dict]:
    """Return the oldest eligible pending row, or ``None``.

    Eligibility: ``status == "pending"`` AND ``attempts < 3``. Ordering is
    by ``created_at`` ascending (lexicographic on the ISO-8601 string).
    Missing queue file returns ``None`` without raising.
    """
    qp = queue_path(root)
    q = load_queue(qp)
    eligible = [
        row
        for row in q.get("findings", [])
        if isinstance(row, dict)
        and row.get("status") == "pending"
        and isinstance(row.get("attempts"), int)
        and row.get("attempts", 0) < 3
    ]
    if not eligible:
        return None
    eligible.sort(key=lambda r: r.get("created_at", ""))
    return eligible[0]


def update_row_status(root: Path, row_id: str, new_status: str) -> None:
    """Update ``status`` of the single row whose ``id`` matches ``row_id``.

    Raises ``ValueError`` if ``new_status`` is not one of the four valid
    statuses, or if ``row_id`` is not present in the queue. Performs a
    LOCK_EX read-modify-write on the queue file FD.

    Does NOT increment ``attempts``. The attempts counter is owned by the
    consumer (run-next), not by status updates.
    """
    if new_status not in VALID_STATUSES:
        raise ValueError(
            f"invalid status {new_status!r}; must be one of {sorted(VALID_STATUSES)}"
        )

    qp = queue_path(root)
    qp.parent.mkdir(parents=True, exist_ok=True)

    # Open with O_CREAT|O_RDWR so the FD exists for LOCK_EX even if the
    # file was just created. The lock is held for the entire RMW window.
    fd: int = -1
    try:
        fd = os.open(str(qp), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            current = _read_locked_queue(fd)
            findings = current.get("findings", [])

            target_index = None
            for i, row in enumerate(findings):
                if isinstance(row, dict) and row.get("id") == row_id:
                    target_index = i
                    break
            if target_index is None:
                raise ValueError(f"row_id {row_id!r} not found in queue")

            findings[target_index]["status"] = new_status
            if new_status == "in_progress":
                findings[target_index]["last_attempt_at"] = _now_iso_z()

            _atomic_write_under_lock(qp, {"findings": findings})
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        if fd >= 0:
            os.close(fd)


def ingest_findings(task_dir: Path, root: Path, summary: dict) -> int:
    """Producer entry point. Append admitted residuals to the queue.

    Returns the number of rows actually appended (after filter and dedup).
    Concurrency-safe: dedup check + append happen under a single
    ``fcntl.LOCK_EX`` held on the queue file FD.

    Never raises on a filtered or malformed finding — those are silently
    skipped. May raise on irrecoverable IO (caller in cmd_run_audit_finish
    is expected to catch and log).
    """
    if not isinstance(summary, dict):
        return 0

    task_id = summary.get("task_id", "")
    if not isinstance(task_id, str):
        task_id = ""

    reports = summary.get("reports", [])
    if not isinstance(reports, list):
        return 0

    # Stage 1: collect candidate rows from per-auditor reports (no IO into
    # the queue yet). Each candidate is a fully-formed row dict ready for
    # dedup and append.
    candidates: list[dict] = []
    for report_entry in reports:
        if not isinstance(report_entry, dict):
            continue
        auditor_name = report_entry.get("auditor_name")
        report_path = report_entry.get("report_path")
        if not isinstance(auditor_name, str) or not isinstance(report_path, str):
            continue
        if auditor_name not in ALLOWED_AUDITORS:
            # Whole report skipped — no need to read it.
            continue

        try:
            report_data = load_json(Path(report_path))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            # Missing or corrupt per-auditor report is a soft failure for
            # the residual pipeline; the audit-summary itself is the
            # authoritative pipeline output.
            continue
        if not isinstance(report_data, dict):
            continue
        findings = report_data.get("findings", [])
        if not isinstance(findings, list):
            continue

        for finding in findings:
            if not _accept_finding(finding, auditor_name):
                continue
            row = _build_row(finding, auditor_name, task_id)
            candidates.append(row)

    if not candidates:
        return 0

    # Stage 2: critical section — open queue, lock, dedup, append, atomic
    # rename. The lock is on the queue file FD (not on the temp file) so
    # concurrent ingest calls serialize correctly.
    qp = queue_path(root)
    qp.parent.mkdir(parents=True, exist_ok=True)
    fd: int = -1
    try:
        fd = os.open(str(qp), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            current = _read_locked_queue(fd)
            existing = current.get("findings", [])

            # Build a set of fingerprints that BLOCK new appends —
            # i.e. fingerprints of rows currently pending or in_progress.
            # Fingerprints of done/failed rows do NOT block (resurfaced).
            blocking_fps: set[str] = set()
            for row in existing:
                if not isinstance(row, dict):
                    continue
                if row.get("status") in DEDUP_BLOCKING_STATUSES:
                    fp = row.get("fingerprint")
                    if isinstance(fp, str):
                        blocking_fps.add(fp)

            # Apply dedup against blocking_fps; also dedup within this
            # batch so two identical findings in one summary do not both
            # get appended.
            appended = 0
            for cand in candidates:
                fp = cand["fingerprint"]
                if fp in blocking_fps:
                    continue
                existing.append(cand)
                blocking_fps.add(fp)
                appended += 1

            if appended > 0:
                _atomic_write_under_lock(qp, {"findings": existing})

            return appended
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        if fd >= 0:
            os.close(fd)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso_z() -> str:
    """Return current UTC time as ISO-8601 with trailing ``Z``."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _accept_finding(finding: Any, auditor_name: str) -> bool:
    """Apply the four-condition residual filter (AC-3).

    Returns True iff the finding is admitted as a residual. All four
    conditions are case-sensitive; ``blocking`` must be the boolean
    ``False`` (not a falsy int / string / None).
    """
    if not isinstance(finding, dict):
        return False
    if auditor_name not in ALLOWED_AUDITORS:
        return False

    blocking = finding.get("blocking")
    # Strict identity check: must be the boolean False, not 0/""/None.
    if blocking is not False:
        return False
    # Belt-and-suspenders: bool is a subclass of int in Python; the check
    # above already excludes 0 because ``0 is False`` is False.

    severity = finding.get("severity")
    if not isinstance(severity, str):
        return False
    if severity == "info":
        return False

    category = finding.get("category")
    if not isinstance(category, str):
        return False
    if category in REJECTED_CATEGORIES:
        return False

    # Required fields for row construction.
    if not isinstance(finding.get("id"), str):
        return False
    if not isinstance(finding.get("description"), str):
        return False
    if not isinstance(finding.get("location"), str):
        return False

    return True


def _build_row(finding: dict, auditor_name: str, task_id: str) -> dict:
    """Construct a queue row from an admitted finding (AC-2 shape)."""
    description = finding["description"]
    finding_id = finding["id"]
    location = finding["location"]
    fp = compute_fingerprint(auditor_name, finding_id, description)
    return {
        "id": str(uuid.uuid4()),
        "kind": "residual",
        "fingerprint": fp,
        "created_at": _now_iso_z(),
        "source_task_id": task_id,
        "source_auditor": auditor_name,
        "title": description[:120],
        "description": description,
        "location": location,
        "status": "pending",
        "attempts": 0,
        "last_attempt_at": None,
    }


def _read_locked_queue(fd: int) -> dict:
    """Read queue contents from an already-locked FD.

    The caller holds LOCK_EX on ``fd``. We seek to 0 and read all bytes.
    An empty file (just-created) is treated as ``{"findings": []}``.
    """
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        try:
            chunk = os.read(fd, 65536)
        except OSError as exc:  # pragma: no cover - defensive
            if exc.errno in (errno.EINTR,):
                continue
            raise
        if not chunk:
            break
        chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw.strip():
        return {"findings": []}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"findings": []}
    if not isinstance(data, dict):
        return {"findings": []}
    findings = data.get("findings")
    if not isinstance(findings, list):
        return {"findings": []}
    return {"findings": findings}


def _atomic_write_under_lock(qpath: Path, data: dict) -> None:
    """Write ``data`` atomically to ``qpath`` while the queue lock is held.

    The temp file lives in the same directory as ``qpath`` so ``os.rename``
    is atomic on POSIX. The temp file is NOT locked — locking is on the
    queue file FD owned by the caller.
    """
    parent = qpath.parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(parent), prefix=".pf-", suffix=".tmp")
    try:
        try:
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            # fdopen took ownership of tmp_fd; if writing failed we still
            # need to remove the temp file.
            raise
        os.rename(tmp_name, str(qpath))
        tmp_name = None  # rename consumed it
    finally:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


__all__ = [
    "queue_path",
    "load_queue",
    "ingest_findings",
    "select_next_pending",
    "update_row_status",
    "extract_residual_id",
    "compute_fingerprint",
]
