"""End-to-end task-receipt chain — tamper-detection over per-task receipts.

The chain is a JSONL append-only log at .dynos/task-{id}/task-receipt-chain.jsonl.
Each entry hashes a single receipt or artifact file plus the canonical-JSON
hash of the prior entry, so a rewrite or deletion anywhere in the chain
breaks the linkage. Each entry is HMAC-signed with the per-task secret
(from lib_log) so even the chain itself cannot be silently rewritten.

Public API:
    extend_chain_for_receipt(task_dir, step, receipt_path)
    extend_chain_for_artifact(task_dir, file_path)
    validate_chain(task_dir) -> ChainValidationResult

Lock protocol (mandatory order):
    open("a") → fcntl.LOCK_EX → read tail (compute prev_sha256) → build
    entry → sign → write line → flush → fsync → LOCK_UN → close.

Computing prev_sha256 BEFORE the lock is a TOCTOU bug — concurrent
writers would each see the same tail and produce duplicate prev_sha256.
"""
from __future__ import annotations

import dataclasses
import fcntl
import hashlib
import hmac
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from lib_core import now_iso
from lib_log import _derive_per_task_secret, _resolve_event_secret, log_event


__all__ = [
    "extend_chain_for_receipt",
    "extend_chain_for_artifact",
    "validate_chain",
    "ChainValidationResult",
]


# Module-level genesis constant. MUST be sha256 of empty BYTES (b"") not
# empty string. Computed at import time so any future Python crypto change
# propagates here without manual update.
_GENESIS_PREV_SHA256: str = hashlib.sha256(b"").hexdigest()

_CHAIN_FILENAME = "task-receipt-chain.jsonl"


@dataclass(frozen=True)
class ChainValidationResult:
    """Structured result of `validate_chain()`.

    Fields:
        status: One of {"valid", "content_mismatch", "chain_corrupt",
                "chain_missing", "chain_truncated"}.
        first_failed_index: 0-based index of the first failing entry, or
                None when status == "valid" or "chain_missing".
        first_failed_field: Which field broke ("sha256", "prev_sha256",
                "_sig"), or None when status is "valid"/"chain_missing".
        error_reason: Human-readable detail for the CLI/logs.
    """
    status: str
    first_failed_index: int | None
    first_failed_field: str | None
    error_reason: str | None


def _canonical_json(entry: dict) -> str:
    """Canonical serialization for HMAC input. Mirrors lib_log.sign_event.

    Excludes `_sig` from the input so signing is reproducible.
    """
    without_sig = {k: v for k, v in entry.items() if k != "_sig"}
    return json.dumps(
        without_sig,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _sign_entry(entry: dict, project_secret: str, task_id: str) -> str:
    """Compute the HMAC-SHA256 _sig for a chain entry.

    Falls back to project_secret directly when task_id is falsy
    (matches lib_log convention so the global .dynos chain — if any —
    works the same way).
    """
    canonical = _canonical_json(entry).encode("utf-8")
    if task_id:
        key = _derive_per_task_secret(project_secret, task_id).encode("utf-8")
    else:
        key = project_secret.encode("utf-8")
    return hmac.new(key, canonical, hashlib.sha256).hexdigest()


def _read_tail_prev_sha(chain_path: Path) -> str:
    """Compute prev_sha256 for the next entry: hash of last line's
    canonical_json (sans _sig), or the genesis constant if file is
    empty/absent. Caller must hold LOCK_EX before invoking.
    """
    if not chain_path.exists():
        return _GENESIS_PREV_SHA256
    try:
        text = chain_path.read_text(encoding="utf-8")
    except OSError:
        return _GENESIS_PREV_SHA256
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return _GENESIS_PREV_SHA256
    last = lines[-1]
    try:
        record = json.loads(last)
    except json.JSONDecodeError:
        # Malformed last line — return genesis so a new chain can recover.
        # validate_chain will surface the corruption separately.
        return _GENESIS_PREV_SHA256
    return hashlib.sha256(_canonical_json(record).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    """Hash raw file bytes."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _task_relative(task_dir: Path, file_path: Path) -> str:
    """Convert file_path to task_dir-relative posix path."""
    try:
        return file_path.resolve().relative_to(task_dir.resolve()).as_posix()
    except ValueError:
        # File lives outside task_dir — store an absolute path. validate_chain
        # will recompute against the same path on validate.
        return str(file_path)


def _append_entry(task_dir: Path, step: str, kind: str, file_path: Path) -> None:
    """Append a chain entry under fcntl.LOCK_EX. Internal helper.

    file_path must exist; caller's responsibility.
    """
    chain_path = task_dir / _CHAIN_FILENAME
    chain_path.parent.mkdir(parents=True, exist_ok=True)
    root = task_dir.parent.parent
    project_secret = _resolve_event_secret(root)
    task_id = task_dir.name if task_dir.name else None

    # Touch the file first so we can open in r+ later if needed
    if not chain_path.exists():
        chain_path.touch()

    # Mandatory lock-then-compute-then-write order. Computing prev_sha256
    # OUTSIDE the lock is a TOCTOU bug.
    with chain_path.open("a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            prev_sha = _read_tail_prev_sha(chain_path)
            entry = {
                "step": step,
                "kind": kind,
                "file_path": _task_relative(task_dir, file_path),
                "sha256": _file_sha256(file_path),
                "prev_sha256": prev_sha,
                "ts": now_iso(),
            }
            entry["_sig"] = _sign_entry(entry, project_secret, task_id or "")
            line = json.dumps(entry, default=str, ensure_ascii=False) + "\n"
            f.write(line)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass  # fsync best-effort; LOCK + write durability is the contract
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def extend_chain_for_receipt(task_dir: Path, step: str, receipt_path: Path) -> None:
    """Append a chain entry for a newly-written receipt.

    Idempotency note: callers that may invoke this multiple times for the
    same receipt (e.g. the run-task-receipt-chain CLI) should de-dup before
    calling. Direct callers from write_receipt always append a new entry.
    """
    _append_entry(task_dir, step, "receipt", receipt_path)


def extend_chain_for_artifact(task_dir: Path, file_path: Path) -> None:
    """Append a chain entry for a newly-written artifact (spec.md,
    plan.md, execution-graph.json, etc).
    """
    step = file_path.name
    _append_entry(task_dir, step, "artifact", file_path)


def validate_chain(task_dir: Path) -> ChainValidationResult:
    """Re-walk the chain and verify every entry's integrity.

    Three independent checks per entry:
      1. content sha256: re-hash the file referenced by file_path,
         compare to stored sha256 → mismatch is content_mismatch.
      2. prev_sha256: recompute from prior entry's canonical_json,
         compare to stored prev_sha256 → mismatch is chain_corrupt.
      3. _sig: HMAC over canonical_json(entry without _sig),
         compare to stored _sig → mismatch is chain_corrupt.
    """
    chain_path = task_dir / _CHAIN_FILENAME
    if not chain_path.exists():
        return ChainValidationResult(
            status="chain_missing",
            first_failed_index=None,
            first_failed_field=None,
            error_reason=f"chain file absent: {chain_path}",
        )

    try:
        text = chain_path.read_text(encoding="utf-8")
    except OSError as exc:
        return ChainValidationResult(
            status="chain_corrupt",
            first_failed_index=None,
            first_failed_field=None,
            error_reason=f"chain file unreadable: {exc}",
        )

    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ChainValidationResult(
            status="valid",
            first_failed_index=None,
            first_failed_field=None,
            error_reason=None,
        )

    root = task_dir.parent.parent
    project_secret = _resolve_event_secret(root)
    task_id = task_dir.name if task_dir.name else None

    expected_prev = _GENESIS_PREV_SHA256
    for idx, line in enumerate(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            return ChainValidationResult(
                status="chain_corrupt",
                first_failed_index=idx,
                first_failed_field=None,
                error_reason=f"line {idx} unparseable JSON: {exc}",
            )
        if not isinstance(entry, dict):
            return ChainValidationResult(
                status="chain_corrupt",
                first_failed_index=idx,
                first_failed_field=None,
                error_reason=f"line {idx} not a JSON object",
            )

        # Check 1: content sha256
        rel = entry.get("file_path", "")
        artifact_path = task_dir / rel if not Path(rel).is_absolute() else Path(rel)
        if not artifact_path.exists():
            return ChainValidationResult(
                status="content_mismatch",
                first_failed_index=idx,
                first_failed_field="sha256",
                error_reason=f"line {idx} references missing file: {rel}",
            )
        try:
            actual_sha = _file_sha256(artifact_path)
        except OSError as exc:
            return ChainValidationResult(
                status="content_mismatch",
                first_failed_index=idx,
                first_failed_field="sha256",
                error_reason=f"line {idx} file unreadable: {exc}",
            )
        if actual_sha != entry.get("sha256"):
            return ChainValidationResult(
                status="content_mismatch",
                first_failed_index=idx,
                first_failed_field="sha256",
                error_reason=f"line {idx}: file content does not match stored sha256",
            )

        # Check 2: prev_sha256 linkage
        if entry.get("prev_sha256") != expected_prev:
            return ChainValidationResult(
                status="chain_corrupt",
                first_failed_index=idx,
                first_failed_field="prev_sha256",
                error_reason=(
                    f"line {idx}: prev_sha256 broken "
                    f"(stored={entry.get('prev_sha256')!r}, expected={expected_prev!r})"
                ),
            )

        # Check 3: HMAC _sig
        expected_sig = _sign_entry(entry, project_secret, task_id or "")
        if not hmac.compare_digest(expected_sig, str(entry.get("_sig", ""))):
            return ChainValidationResult(
                status="chain_corrupt",
                first_failed_index=idx,
                first_failed_field="_sig",
                error_reason=f"line {idx}: HMAC _sig invalid",
            )

        # Advance expected_prev for the next entry.
        expected_prev = hashlib.sha256(_canonical_json(entry).encode("utf-8")).hexdigest()

    return ChainValidationResult(
        status="valid",
        first_failed_index=None,
        first_failed_field=None,
        error_reason=None,
    )
