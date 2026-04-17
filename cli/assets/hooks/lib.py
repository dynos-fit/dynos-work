#!/usr/bin/env python3
"""Deterministic control helpers for dynos-work.

This module is a thin re-export facade. All implementation lives in the
lib_* sub-modules. Every name that was previously importable from
lib remains importable here.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# lib_core: constants, utilities, path helpers, task state, retrospectives
# ---------------------------------------------------------------------------
from lib_core import (
    ALLOWED_STAGE_TRANSITIONS,
    COMPOSITE_WEIGHTS,
    NEXT_COMMAND,
    STAGE_ORDER,
    TOKEN_ESTIMATES,
    VALID_CLASSIFICATION_TYPES,
    VALID_DOMAINS,
    VALID_EXECUTORS,
    VALID_RISK_LEVELS,
    _persistent_project_dir,
    _safe_float,
    automation_queue_path,
    benchmark_history_path,
    benchmark_index_path,
    benchmark_policy_config,
    collect_retrospectives,
    find_active_tasks,
    is_learning_enabled,
    is_pid_running,
    learned_agents_root,
    learned_registry_path,
    load_json,
    next_command_for_stage,
    now_iso,
    project_dir,
    project_policy,
    require,
    retrospective_task_ids,
    task_recency_index,
    tasks_since,
    trajectories_store_path,
    transition_task,
    write_json,
)

# ---------------------------------------------------------------------------
# lib_validate: spec/plan/graph/repair validation
# ---------------------------------------------------------------------------
from lib_validate import (
    REQUIRED_PLAN_HEADINGS,
    REQUIRED_SPEC_HEADINGS,
    apply_fast_track,
    check_segment_ownership,
    collect_headings,
    compute_fast_track,
    conditional_plan_headings,
    detect_cycle,
    parse_acceptance_criteria,
    validate_generated_html,
    validate_manifest,
    validate_repair_log,
    validate_retrospective,
    validate_task_artifacts,
)

# ---------------------------------------------------------------------------
# plan_gap_analysis: verify plan claims against codebase
# ---------------------------------------------------------------------------
from plan_gap_analysis import (
    analyze_api_contracts,
    analyze_data_model,
    extract_section,
    findings_from_report,
    parse_markdown_table,
    run_gap_analysis,
)

# ---------------------------------------------------------------------------
# lib_trajectory: trajectory store, quality scoring, similarity search
# ---------------------------------------------------------------------------
from lib_trajectory import (
    _domain_overlap,
    collect_task_summaries,
    compute_quality_score,
    ensure_trajectory_store,
    estimate_token_usage,
    load_token_usage,
    make_trajectory_entry,
    rebuild_trajectory_store,
    retrospective_benchmark_score,
    search_trajectories,
    trajectory_similarity,
    validate_retrospective_scores,
)

# ---------------------------------------------------------------------------
# lib_registry: learned agent registry
# ---------------------------------------------------------------------------
from lib_registry import (
    MAX_REGISTRY_BENCHMARKS,
    apply_evaluation_to_registry,
    ensure_learned_registry,
    entry_is_stale,
    register_learned_agent,
    resolve_registry_route,
)

# ---------------------------------------------------------------------------
# lib_benchmark: benchmark evaluation and fixtures
# ---------------------------------------------------------------------------
from lib_benchmark import (
    MAX_BENCHMARK_HISTORY_RUNS,
    _category_summaries,
    append_benchmark_run,
    benchmark_fixture_score,
    benchmark_fixtures_dir,
    compute_benchmark_summary,
    ensure_benchmark_history,
    ensure_benchmark_index,
    evaluate_candidate,
    iter_benchmark_fixtures,
    matching_fixtures_for_registry_entry,
    synthesize_fixture_for_entry,
    upsert_fixture_trace,
)

# ---------------------------------------------------------------------------
# lib_queue: automation queue management
# ---------------------------------------------------------------------------
from lib_queue import (
    enqueue_automation_item,
    ensure_automation_queue,
    queue_identity,
    replace_automation_queue,
)
