"""Task-007 self-proof smoke.

Asserts the structural invariants task-007 added hold at import time.
These are the quick gates that catch a regression before the deeper
adversarial tests even run — import the library and check the
expected state.

Runs on every pytest invocation.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "hooks"))

import lib_receipts  # noqa: E402


def test_receipt_contract_version_is_five():
    """Task-007 hard constraint migrated for task-009 AC 24:
    RECEIPT_CONTRACT_VERSION bumped 4 → 5 for the force-override
    reason/approver contract tightening.

    Any future bump must coordinate writer migrations + test
    fixtures; asserting the exact value here is the pin that catches
    an out-of-band bump. Rename _is_four → _is_five follows the
    task-009 policy of making the current floor visible in the test name."""
    assert lib_receipts.RECEIPT_CONTRACT_VERSION == 5


def test_plan_routing_writer_fully_deleted():
    """Task-007 A-001: receipt_plan_routing and its log message are
    deleted end-to-end — no ALLOWLIST / kept-for-reinstatement hack."""
    assert "receipt_plan_routing" not in lib_receipts.__all__
    assert not hasattr(lib_receipts, "receipt_plan_routing")
    assert "plan-routing" not in lib_receipts._LOG_MESSAGES


def test_self_compute_writers_present():
    """Task-007 B-class: the 6 writers migrated to self-compute are
    present with the new signatures."""
    import inspect

    def params(fn):
        return list(inspect.signature(fn).parameters.keys())

    # B-001 receipt_postmortem_generated (td, postmortem_json_path, **_legacy)
    assert params(lib_receipts.receipt_postmortem_generated)[:2] == [
        "task_dir", "postmortem_json_path"
    ]
    # B-002 receipt_retrospective (td, **_legacy)
    retro_params = params(lib_receipts.receipt_retrospective)
    assert retro_params[0] == "task_dir"
    assert "_legacy" in retro_params[-1] or retro_params[-1].endswith("_legacy")
    # B-003 receipt_spec_validated (td, **_legacy)
    assert params(lib_receipts.receipt_spec_validated)[0] == "task_dir"
    # B-004 receipt_plan_validated (td, validation_passed_override, **_legacy)
    pv_params = params(lib_receipts.receipt_plan_validated)
    assert pv_params[0] == "task_dir"
    assert "validation_passed_override" in pv_params
    # B-006 receipt_plan_audit (td, tokens_used, **_legacy) — finding_count removed
    pa_params = params(lib_receipts.receipt_plan_audit)
    assert "finding_count" not in pa_params


def test_diagnostic_only_events_allowlist_defined():
    """Task-007 A-007: DIAGNOSTIC_ONLY_EVENTS is a non-empty frozenset."""
    from lib_log import DIAGNOSTIC_ONLY_EVENTS
    assert isinstance(DIAGNOSTIC_ONLY_EVENTS, frozenset)
    assert len(DIAGNOSTIC_ONLY_EVENTS) >= 18


def test_validate_chain_calls_require_receipts_for_done():
    """Task-007 A-008: diagnostic bridges to gate so the two cannot
    drift — source must reference require_receipts_for_done."""
    import inspect
    src = inspect.getsource(lib_receipts.validate_chain)
    assert "require_receipts_for_done" in src


def test_daemon_has_calibration_recovery_sweep():
    """Task-007 A-003: daemon sweep is the recovery belt for tasks
    that hit SIGKILL/OOM between DONE and calibration receipt."""
    import daemon
    assert hasattr(daemon, "calibration_recovery_sweep")
    assert callable(daemon.calibration_recovery_sweep)


def test_bin_dynos_has_rules_check_subcommand():
    """Task-007 A-004: bin/dynos rules-check <task_dir> is the
    in-tree production caller that closes the ALLOWLIST exemption."""
    bin_dynos = REPO_ROOT / "bin" / "dynos"
    text = bin_dynos.read_text(encoding="utf-8")
    assert "rules-check)" in text
    assert "receipt_rules_check_passed" in text
