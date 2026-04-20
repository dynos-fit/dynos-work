"""TDD-first CI lint for AC5 (task-20260419-009).

Walks every ``.dynos/task-*/receipts/force-override-*.json`` file in the
repository (or a caller-supplied root) and asserts that for every receipt
written at ``contract_version >= 5`` the ``reason`` and ``approver``
top-level fields are present, are ``str``, and are non-empty.

Pre-v5 receipts (``contract_version < 5``) are explicitly exempt per AC5
— the contract bump in AC23 is additive and must not retroactively
invalidate receipts written under the v4 shape.

Also validates AC24 indirectly: imports ``MIN_VERSION_PER_STEP`` from
``hooks/lib_receipts`` and asserts ``"force-override-*"`` is pinned at
floor 5 so new writes cannot slip through at a lower contract version.

Also validates AC23 indirectly: imports ``RECEIPT_CONTRACT_VERSION`` and
asserts it is at least 5 (the bump taken by this task).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_receipts import MIN_VERSION_PER_STEP, RECEIPT_CONTRACT_VERSION  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parent.parent


def _walk_force_override_receipts(root: Path) -> list[Path]:
    """Return every force-override-*.json path under root/.dynos/task-*/receipts/."""
    dynos = root / ".dynos"
    if not dynos.exists():
        return []
    return sorted(dynos.glob("task-*/receipts/force-override-*.json"))


def _validate_receipt(path: Path) -> list[str]:
    """Return a list of human-readable failure strings for the given receipt.

    Empty list => valid. Receipts at contract_version < 5 are always valid
    for the purposes of this lint.
    """
    failures: list[str] = []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"{path}: cannot parse receipt: {exc}"]
    if not isinstance(payload, dict):
        return [f"{path}: receipt is not a dict (got {type(payload).__name__})"]

    ver = payload.get("contract_version", 1)
    if not isinstance(ver, int) or ver < 5:
        # Pre-v5: exempt.
        return []

    for field in ("reason", "approver"):
        val = payload.get(field)
        if val is None:
            failures.append(f"{path}: v{ver} receipt missing required field {field!r}")
        elif not isinstance(val, str):
            failures.append(
                f"{path}: v{ver} receipt field {field!r} must be str, "
                f"got {type(val).__name__}"
            )
        elif not val:
            failures.append(f"{path}: v{ver} receipt field {field!r} is empty string")
    return failures


# --- AC23: contract version bumped ---------------------------------------

def test_contract_version_is_at_least_5():
    assert RECEIPT_CONTRACT_VERSION >= 5, (
        f"RECEIPT_CONTRACT_VERSION must be bumped to 5 (AC23); "
        f"currently {RECEIPT_CONTRACT_VERSION}"
    )


# --- AC24: MIN_VERSION_PER_STEP floor for force-override-* ---------------

def test_min_version_per_step_has_force_override_floor_5():
    floor = MIN_VERSION_PER_STEP.get("force-override-*")
    assert floor == 5, (
        f"MIN_VERSION_PER_STEP must include 'force-override-*': 5 (AC24); "
        f"currently {floor!r}"
    )


# --- AC5: on-disk receipts carry reason+approver on v5+ ------------------

def test_all_v5_plus_force_override_receipts_carry_reason_and_approver():
    """Walk the live repo tree and assert every v5+ receipt is valid."""
    failures: list[str] = []
    for path in _walk_force_override_receipts(REPO_ROOT):
        failures.extend(_validate_receipt(path))

    assert not failures, (
        "Found v5+ force-override receipts without required reason/approver "
        "fields:\n" + "\n".join(f"  - {f}" for f in failures)
    )


def test_walker_detects_malformed_v5_receipt(tmp_path):
    """Fixture-driven regression: a v5 receipt missing ``reason`` must be
    flagged by the walker. Proves the lint has teeth.
    """
    rcpt_dir = tmp_path / ".dynos" / "task-XXXX" / "receipts"
    rcpt_dir.mkdir(parents=True)
    bad = rcpt_dir / "force-override-A-B.json"
    bad.write_text(
        json.dumps(
            {
                "step": "force-override-A-B",
                "contract_version": 5,
                "valid": True,
                "from_stage": "A",
                "to_stage": "B",
                "bypassed_gates": [],
                "approver": "alice",
                # reason deliberately missing
            }
        )
    )

    walked = _walk_force_override_receipts(tmp_path)
    assert walked, "walker must find the malformed receipt"
    failures: list[str] = []
    for p in walked:
        failures.extend(_validate_receipt(p))
    assert failures, "lint must flag a v5 receipt missing 'reason'"
    assert any("reason" in f for f in failures)


def test_walker_detects_empty_string_approver_v5(tmp_path):
    rcpt_dir = tmp_path / ".dynos" / "task-YYYY" / "receipts"
    rcpt_dir.mkdir(parents=True)
    bad = rcpt_dir / "force-override-A-B.json"
    bad.write_text(
        json.dumps(
            {
                "step": "force-override-A-B",
                "contract_version": 5,
                "valid": True,
                "from_stage": "A",
                "to_stage": "B",
                "bypassed_gates": [],
                "reason": "because",
                "approver": "",  # empty
            }
        )
    )
    failures: list[str] = []
    for p in _walk_force_override_receipts(tmp_path):
        failures.extend(_validate_receipt(p))
    assert any("approver" in f for f in failures)


def test_walker_accepts_valid_v5_receipt(tmp_path):
    rcpt_dir = tmp_path / ".dynos" / "task-OK" / "receipts"
    rcpt_dir.mkdir(parents=True)
    ok = rcpt_dir / "force-override-A-B.json"
    ok.write_text(
        json.dumps(
            {
                "step": "force-override-A-B",
                "contract_version": 5,
                "valid": True,
                "from_stage": "A",
                "to_stage": "B",
                "bypassed_gates": [],
                "reason": "deferred-findings stale",
                "approver": "alice",
            }
        )
    )
    failures: list[str] = []
    for p in _walk_force_override_receipts(tmp_path):
        failures.extend(_validate_receipt(p))
    assert not failures, f"clean v5 receipt must not fail lint: {failures!r}"


def test_walker_exempts_pre_v5_receipt_missing_reason(tmp_path):
    """A pre-v5 receipt without reason/approver is explicitly NOT a failure."""
    rcpt_dir = tmp_path / ".dynos" / "task-PREV" / "receipts"
    rcpt_dir.mkdir(parents=True)
    pre = rcpt_dir / "force-override-A-B.json"
    pre.write_text(
        json.dumps(
            {
                "step": "force-override-A-B",
                "contract_version": 4,  # pre-v5
                "valid": True,
                "from_stage": "A",
                "to_stage": "B",
                "bypassed_gates": [],
                # no reason / no approver — acceptable at v4
            }
        )
    )
    failures: list[str] = []
    for p in _walk_force_override_receipts(tmp_path):
        failures.extend(_validate_receipt(p))
    assert not failures, (
        f"pre-v5 receipts must be exempt from reason/approver lint: {failures!r}"
    )


def test_receipt_force_override_writer_validates_reason_and_approver(tmp_path):
    """Direct writer test (AC2): ``receipt_force_override`` is kw-only for
    ``reason`` and ``approver`` and rejects non-str / empty values.
    """
    from lib_receipts import receipt_force_override  # noqa: PLC0415

    td = tmp_path / ".dynos" / "task-WRITER"
    td.mkdir(parents=True)

    # Missing reason entirely — TypeError (required kw-only) or ValueError.
    with pytest.raises((TypeError, ValueError)):
        receipt_force_override(
            td, "A", "B", [], approver="alice",
        )  # type: ignore[call-arg]

    # Empty reason
    with pytest.raises(ValueError, match="reason"):
        receipt_force_override(
            td, "A", "B", [], reason="", approver="alice",
        )

    # Empty approver
    with pytest.raises(ValueError, match="approver"):
        receipt_force_override(
            td, "A", "B", [], reason="because", approver="",
        )

    # Non-string reason
    with pytest.raises(ValueError, match="reason"):
        receipt_force_override(
            td, "A", "B", [], reason=123, approver="alice",  # type: ignore[arg-type]
        )

    # Valid call writes a receipt whose top-level payload carries both.
    out = receipt_force_override(
        td, "A", "B", [], reason="because we needed it", approver="alice",
    )
    payload = json.loads(out.read_text())
    assert payload["reason"] == "because we needed it"
    assert payload["approver"] == "alice"
    assert payload.get("contract_version", 0) >= 5
