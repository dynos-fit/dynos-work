"""dynos-work receipt-writer package — split from lib_receipts.py.

Re-exports the full public surface that was previously defined in the
monolithic ``hooks/lib_receipts.py``. The names listed in ``__all__``
match the original module's ``__all__`` exactly so downstream callers
that imported from ``lib_receipts`` continue to work via the shim.
"""

from .core import (
    write_receipt,
    read_receipt,
    require_receipt,
    validate_chain,
    hash_file,
    validate_receipt_model_field,
    RECEIPT_CONTRACT_VERSION,
    MIN_VERSION_PER_STEP,
    CALIBRATION_POLICY_FILES,
    INJECTED_PROMPTS_DIR,
    INJECTED_AUDITOR_PROMPTS_DIR,
    INJECTED_PLANNER_PROMPTS_DIR,
)
from .stage import (
    receipt_search_conducted,
    receipt_spec_validated,
    receipt_plan_validated,
    receipt_executor_routing,
    receipt_executor_done,
    receipt_audit_routing,
    receipt_audit_done,
    receipt_retrospective,
    receipt_post_completion,
    plan_validated_receipt_matches,
    plan_audit_matches,
)
from .planner import (
    receipt_planner_spawn,
    receipt_plan_audit,
    receipt_tdd_tests,
)
from .approval import (
    receipt_human_approval,
    receipt_auto_approval,
    receipt_postmortem_generated,
    receipt_postmortem_analysis,
    receipt_postmortem_skipped,
    receipt_calibration_applied,
    receipt_calibration_noop,
    receipt_rules_check_passed,
    receipt_force_override,
)
from .budget import (
    receipt_spawn_budget_paused,
    receipt_spawn_budget_resumed,
)

__all__ = [
    "write_receipt",
    "read_receipt",
    "require_receipt",
    "validate_chain",
    "hash_file",
    "plan_validated_receipt_matches",
    "plan_audit_matches",
    "receipt_search_conducted",
    "receipt_spec_validated",
    "receipt_plan_validated",
    "receipt_executor_routing",
    "receipt_executor_done",
    "receipt_audit_routing",
    "receipt_audit_done",
    "receipt_retrospective",
    "receipt_post_completion",
    "receipt_planner_spawn",
    "receipt_plan_audit",
    "receipt_tdd_tests",
    "receipt_human_approval",
    "receipt_auto_approval",
    "receipt_postmortem_generated",
    "receipt_postmortem_analysis",
    "receipt_postmortem_skipped",
    "receipt_calibration_applied",
    "receipt_calibration_noop",
    "receipt_rules_check_passed",
    "receipt_force_override",
    "RECEIPT_CONTRACT_VERSION",
    "MIN_VERSION_PER_STEP",
    "CALIBRATION_POLICY_FILES",
    "INJECTED_PROMPTS_DIR",
    "INJECTED_AUDITOR_PROMPTS_DIR",
    "INJECTED_PLANNER_PROMPTS_DIR",
    "receipt_spawn_budget_paused",
    "receipt_spawn_budget_resumed",
    "validate_receipt_model_field",
]
