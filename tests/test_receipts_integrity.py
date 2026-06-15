"""TDD-first regression tests for findings #56 and #57 — receipt comment correctness
and write_receipt integrity-field override protection.

AC 9: The misleading comment 'spawn receipts now carry' must be absent from
hooks/receipts/core.py; the spawn-* floor value 7 must remain unchanged.

AC 10: write_receipt must NOT allow callers to override step, ts, valid, or
contract_version via **payload. After the dict-reorder fix these keys are
placed AFTER **payload so canonical values always win.

These tests are RED by design until the production fixes are applied.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

from lib_receipts import write_receipt, RECEIPT_CONTRACT_VERSION  # noqa: E402


def _task_dir(tmp_path: Path) -> Path:
    """Create a minimal task dir that write_receipt can use."""
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260615-002-integrity"
    td.mkdir(parents=True)
    return td


# ---------------------------------------------------------------------------
# AC 9: Receipt comment correctness
# ---------------------------------------------------------------------------


def test_spawn_floor_comment_corrected() -> None:
    """AC 9: The misleading old comment must be absent; spawn-budget must appear;
    the spawn-* floor value must remain 7.

    This is a documentation-correctness test: reads the raw source text of
    hooks/receipts/core.py and asserts on comment content.
    """
    core_source = (ROOT / "hooks" / "receipts" / "core.py").read_text(encoding="utf-8")

    # The old misleading claim must be GONE
    assert "spawn receipts now carry" not in core_source, (
        "The misleading claim 'spawn receipts now carry' must be removed from core.py. "
        "It falsely attributes host/tier/resolved_model v7 protection to the spawn-* "
        "floor entry when in fact the floor only matches spawn-budget-paused/resumed."
    )

    # A corrected reference to spawn-budget must appear in its place
    assert "spawn-budget" in core_source, (
        "A corrected reference to 'spawn-budget' (or 'spawn-budget-paused') must appear "
        "in the comment replacing the misleading old text."
    )

    # The floor value itself must remain 7 — do not accidentally change it
    assert '"spawn-*": 7' in core_source, (
        "The spawn-* floor value must remain 7 (unchanged by the comment-only fix)."
    )


# ---------------------------------------------------------------------------
# AC 10: write_receipt integrity-field override protection
# ---------------------------------------------------------------------------


def test_write_receipt_contract_version_not_overridden_by_payload(
    tmp_path: Path,
) -> None:
    """AC 10: Passing contract_version=999 in payload must NOT override the canonical value.

    After the dict reorder fix:
        receipt = {**payload, "step": ..., "contract_version": RECEIPT_CONTRACT_VERSION}
    the canonical RECEIPT_CONTRACT_VERSION wins over any value in payload.
    """
    td = _task_dir(tmp_path)
    receipt_path = write_receipt(td, "executor-seg1", contract_version=999)

    on_disk = json.loads(receipt_path.read_text())
    assert on_disk["contract_version"] == RECEIPT_CONTRACT_VERSION, (
        f"contract_version in receipt must equal RECEIPT_CONTRACT_VERSION "
        f"({RECEIPT_CONTRACT_VERSION}), not a payload-supplied 999. "
        f"Got: {on_disk['contract_version']!r}. "
        f"The dict-reorder fix is missing — payload is currently overriding the "
        f"canonical integrity field."
    )


def test_write_receipt_valid_not_overridden_by_payload(tmp_path: Path) -> None:
    """AC 10: Passing valid=False in payload must NOT override the canonical True value."""
    td = _task_dir(tmp_path)
    receipt_path = write_receipt(td, "executor-seg1", valid=False)

    on_disk = json.loads(receipt_path.read_text())
    assert on_disk["valid"] is True, (
        f"valid in receipt must always be True (canonical); "
        f"got {on_disk['valid']!r}. "
        f"The dict-reorder fix is missing — payload valid=False is currently winning."
    )


def test_write_receipt_step_not_overridden_by_payload(tmp_path: Path) -> None:
    """AC 10: Passing step='injected-step' in payload must NOT override the step_name argument."""
    td = _task_dir(tmp_path)
    receipt_path = write_receipt(td, "executor-seg1", step="injected-step")

    on_disk = json.loads(receipt_path.read_text())
    assert on_disk["step"] == "executor-seg1", (
        f"step in receipt must equal the step_name argument ('executor-seg1'), "
        f"not a payload-supplied 'injected-step'. Got: {on_disk['step']!r}. "
        f"The dict-reorder fix is missing — payload step is currently overriding."
    )
    # Also confirm that other non-conflicting payload fields ARE preserved
    # (the **payload spread must still be present after the fix)
    # We pass step= in payload above; since step conflicts it should be overridden.
    # A separate non-conflicting key (not passed here) would be preserved.
