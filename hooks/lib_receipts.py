"""Receipt-based contract validation chain for dynos-work.

Every pipeline step writes a structured JSON receipt to
.dynos/task-{id}/receipts/{step-name}.json proving what it did.
The next step refuses to proceed unless the prior receipt exists.
"""

# Backwards-compatibility shim from task-20260505-002.
#
# The original monolithic implementation was split into the
# ``hooks/receipts/`` package (segments B–F). This module remains as a
# pure re-export surface so that callers using ``from hooks.lib_receipts
# import <name>`` (or ``import lib_receipts``) continue to work without
# modification. No logic lives here; every public name is bound from the
# corresponding submodule, and the five private symbols are re-exported
# explicitly because star-imports drop underscore-prefixed names.

from __future__ import annotations

from receipts.core import (
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
    _LOG_MESSAGES,
    _HASH_CACHE,
    _HASH_CACHE_MAX,
    _WRITE_ROLE,
    _receipts_dir,
    _atomic_write_text,
    _resolve_min_version,
    _record_tokens,
)
from receipts.stage import (
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
from receipts.planner import (
    receipt_planner_spawn,
    receipt_plan_audit,
    receipt_tdd_tests,
)
from receipts.approval import (
    receipt_human_approval,
    receipt_auto_approval,
    receipt_postmortem_generated,
    receipt_postmortem_analysis,
    receipt_postmortem_skipped,
    receipt_calibration_applied,
    receipt_calibration_noop,
    receipt_rules_check_passed,
    receipt_force_override,
    receipt_scheduler_refused,
    _POSTMORTEM_SKIP_REASONS,
)
from receipts.budget import (
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
