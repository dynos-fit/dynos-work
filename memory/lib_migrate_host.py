#!/usr/bin/env python3
"""Host migration utility for dynos-work effectiveness-scores records.

Backfills legacy records (those lacking a ``host`` key) with:
  - ``host``            — always "claude" for historical records
  - ``model_tier``      — derived from the record's ``model`` field
  - ``migration_stamp`` — "task-20260611-001"

Writes a receipt file to ``data_dir/receipts/host-backfill-v1`` AFTER all
records have been processed, so an interrupted run leaves no partial receipt.

Idempotent: records already containing a ``host`` key are skipped. A second
call to ``run_backfill`` with the same data_dir is a no-op.
"""

from __future__ import annotations

import json
import sys as _sys
from pathlib import Path

_sys.path.insert(0, str(Path(__file__).resolve().parent))
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_models import model_to_tier, HOST_CLAUDE

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

MIGRATION_RECEIPT_NAME: str = "host-backfill-v1"
MIGRATION_STAMP: str = "task-20260611-001"

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_EFFECTIVENESS_SCORES_FILE = "effectiveness-scores.json"


def _read_records(data_dir: Path) -> list[dict]:
    """Read records from effectiveness-scores.json. Returns [] on any error."""
    path = data_dir / _EFFECTIVENESS_SCORES_FILE
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, list):
            return data
        return []
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return []


def _write_records(data_dir: Path, records: list[dict]) -> None:
    """Atomically write records to effectiveness-scores.json."""
    path = data_dir / _EFFECTIVENESS_SCORES_FILE
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(records, indent=2), encoding="utf-8")
    tmp.replace(path)


def _receipt_exists(data_dir: Path) -> bool:
    """Return True if the migration receipt already exists."""
    return (data_dir / "receipts" / MIGRATION_RECEIPT_NAME).exists()


def _write_receipt(data_dir: Path, records_processed: int) -> None:
    """Write the migration receipt to data_dir/receipts/MIGRATION_RECEIPT_NAME."""
    receipts_dir = data_dir / "receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = receipts_dir / MIGRATION_RECEIPT_NAME
    receipt = {
        "receipt": MIGRATION_RECEIPT_NAME,
        "migration_stamp": MIGRATION_STAMP,
        "records_processed": records_processed,
    }
    receipt_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_backfill(data_dir: Path) -> None:
    """Backfill host/model_tier/migration_stamp fields on legacy records.

    Idempotent: if the receipt already exists, returns immediately without
    reading or modifying any records.

    Records already containing a ``host`` key are skipped — they are written
    back unchanged so the file is left in a consistent state.

    The receipt file is written AFTER all records have been processed.
    """
    data_dir = Path(data_dir)

    # Idempotency guard: if the receipt exists, nothing to do.
    if _receipt_exists(data_dir):
        return

    records = _read_records(data_dir)

    modified_count = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        # Skip records that already have a host key.
        if "host" in record:
            continue
        # Derive model_tier from existing model field.
        model = record.get("model", "")
        tier = model_to_tier(model) if isinstance(model, str) and model else None

        record["host"] = HOST_CLAUDE
        record["model_tier"] = tier if tier is not None else "unknown"
        record["migration_stamp"] = MIGRATION_STAMP
        modified_count += 1

    # Write records back (even if no records were modified, to be safe).
    _write_records(data_dir, records)

    # Receipt is written AFTER all records are processed.
    _write_receipt(data_dir, records_processed=modified_count)
