"""Self-proof: when task-20260418-004 has reached CALIBRATED, all
expected receipts are present (AC self-proof)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

TASK_DIR = ROOT / ".dynos" / "task-20260418-004"
CALIBRATION_RECEIPT = TASK_DIR / "receipts" / "calibration-applied.json"


pytestmark = pytest.mark.integration


def _integration_requested(config) -> bool:
    if os.environ.get("RUN_INTEGRATION") == "1":
        return True
    markexpr = ""
    try:
        markexpr = config.getoption("-m") or ""
    except Exception:
        markexpr = ""
    return "integration" in markexpr


@pytest.fixture(autouse=True)
def _skip_unless_explicit_integration(request):
    if not _integration_requested(request.config):
        pytest.skip(
            "integration: set RUN_INTEGRATION=1 or pass `-m integration` to run"
        )


@pytest.fixture(autouse=True)
def _skip_if_calibration_missing():
    if not CALIBRATION_RECEIPT.exists():
        pytest.skip(
            f"prerequisite missing: {CALIBRATION_RECEIPT} not present yet"
        )


def test_manifest_stage_is_calibrated():
    manifest_path = TASK_DIR / "manifest.json"
    assert manifest_path.exists(), f"missing manifest at {manifest_path}"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["stage"] == "CALIBRATED"


def test_calibration_receipt_valid():
    payload = json.loads(CALIBRATION_RECEIPT.read_text())
    assert payload.get("valid") is True
    assert payload.get("step") == "calibration-applied"
    for key in ("retros_consumed", "scores_updated",
                "policy_sha256_before", "policy_sha256_after"):
        assert key in payload, f"missing key {key} in calibration receipt"


def test_audit_routing_receipt_exists():
    p = TASK_DIR / "receipts" / "audit-routing.json"
    assert p.exists(), f"missing audit-routing receipt at {p}"


def test_plan_audit_check_receipt_exists():
    p = TASK_DIR / "receipts" / "plan-audit-check.json"
    assert p.exists(), f"missing plan-audit-check receipt at {p}"
