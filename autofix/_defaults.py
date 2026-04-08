#!/usr/bin/env python3
"""Centralized constants for the dynos-work runtime.

Every tunable numeric literal lives here. Import what you need:

    from dynoslib_defaults import PAGERANK_DAMPING, SCAN_TARGET_WEIGHTS

To override at runtime, modify policy.json — the constants here are
compile-time defaults that policy.json values take precedence over.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# PageRank (dynoslib_crawler.py)
# ---------------------------------------------------------------------------

PAGERANK_DAMPING: float = 0.85
PAGERANK_ITERATIONS: int = 20

# ---------------------------------------------------------------------------
# Scan target scoring (dynoslib_crawler.py)
#
#   score = WEIGHT_PAGERANK * pagerank
#         + WEIGHT_FRESHNESS * freshness
#         + WEIGHT_FINDING_HISTORY * finding_history
#         + WEIGHT_CHANGE_VELOCITY * change_velocity
#         + WEIGHT_COMPLEXITY * complexity
#         - cooldown
# ---------------------------------------------------------------------------

WEIGHT_PAGERANK: float = 30.0
WEIGHT_FRESHNESS: float = 25.0
WEIGHT_FINDING_HISTORY: float = 20.0
WEIGHT_CHANGE_VELOCITY: float = 15.0
WEIGHT_COMPLEXITY: float = 10.0

MAX_SCAN_TARGET_FILES: int = 10
MAX_NEIGHBOR_FILES: int = 5
MAX_NEIGHBOR_LINES: int = 100

FRESHNESS_NORM_DAYS: float = 365.0
COMPLEXITY_LINE_CAP: float = 1000.0

# Cooldown penalties (subtracted from composite score)
COOLDOWN_THRESHOLD_1DAY: int = 1
COOLDOWN_PENALTY_1DAY: float = 100.0
COOLDOWN_THRESHOLD_3DAY: int = 3
COOLDOWN_PENALTY_3DAY: float = 30.0
COOLDOWN_THRESHOLD_7DAY: int = 7
COOLDOWN_PENALTY_7DAY: float = 10.0
COOLDOWN_BONUS_CLEAN: float = 5.0

# Git subprocess timeouts (seconds)
GIT_REVPARSE_TIMEOUT: int = 10
GIT_LSFILES_TIMEOUT: int = 15
GIT_LOG_CHURN_TIMEOUT: int = 15
GIT_LOG_PERFILE_TIMEOUT: int = 5
CHURN_WINDOW_DAYS: int = 30

# ---------------------------------------------------------------------------
# Q-learning (dynoslib_qlearn.py)
# ---------------------------------------------------------------------------

QLEARN_ALPHA: float = 0.1       # learning rate
QLEARN_GAMMA: float = 0.9       # discount factor
QLEARN_EPSILON: float = 0.15    # exploration rate
QVALUE_PRECISION: int = 6       # decimal places when storing Q-values

# Token budgets by severity (repair Q-learning)
TOKEN_BUDGET_LOW: int = 8_000
TOKEN_BUDGET_MEDIUM: int = 12_000
TOKEN_BUDGET_HIGH: int = 18_000
TOKEN_BUDGET_CRITICAL: int = 25_000

TOKEN_BUDGETS: dict[str, int] = {
    "low": TOKEN_BUDGET_LOW,
    "medium": TOKEN_BUDGET_MEDIUM,
    "high": TOKEN_BUDGET_HIGH,
    "critical": TOKEN_BUDGET_CRITICAL,
}

# Reward values (repair Q-learning)
REWARD_SUCCESS: float = 1.0            # finding resolved, no new findings
REWARD_PARTIAL: float = 0.5            # finding resolved, some new findings
REWARD_PENALTY_PER_NEW_FINDING: float = 0.1
REWARD_FAILURE: float = -0.5           # finding not resolved
REWARD_MIN: float = -1.0
REWARD_MAX: float = 1.0
EFFICIENCY_WEIGHT: float = 0.2
EFFICIENCY_CLAMP: float = 0.2

ESCALATION_RETRY_THRESHOLD: int = 2   # use opus after N retries

# ---------------------------------------------------------------------------
# Autofix Q-learning rewards (dynoproactive.py)
# ---------------------------------------------------------------------------

AUTOFIX_REWARD_PR_MERGED: float = 1.0
AUTOFIX_REWARD_PR_OPENED: float = 0.5
AUTOFIX_REWARD_NO_CHANGES: float = -0.3
AUTOFIX_REWARD_VERIFICATION_FAILED: float = -0.5
AUTOFIX_REWARD_GIT_COMMIT_FAILED: float = -0.2
AUTOFIX_REWARD_ISSUE_OPENED: float = 0.3
AUTOFIX_REWARD_SKIP: float = 0.0

# PR feedback loop rewards
PR_FEEDBACK_REWARD_MERGED: float = 0.8
PR_FEEDBACK_REWARD_CLOSED: float = -0.5

# ---------------------------------------------------------------------------
# Fix templates (dynoslib_templates.py)
# ---------------------------------------------------------------------------

MAX_FIX_TEMPLATES: int = 50
MAX_TEMPLATE_DIFF_LINES: int = 100

# ---------------------------------------------------------------------------
# Batch fixes (dynoproactive.py)
# ---------------------------------------------------------------------------

BATCH_MIN_GROUP_SIZE: int = 3   # minimum findings to form a batch

# ---------------------------------------------------------------------------
# Confidence & category thresholds (dynoproactive.py)
# ---------------------------------------------------------------------------

CONF_AUTOFIX_BASE: float = 0.75
CONF_SYNTAX_ERROR: float = 0.95
CONF_DEAD_CODE: float = 0.88
CONF_LLM_REVIEW: float = 0.70
CONF_ISSUE_ONLY: float = 0.35      # dependency-vuln, architectural-drift
MIN_CONF_AUTOFIX: float = 0.65
MIN_FINDING_CONFIDENCE: float = 0.7
HIGH_CONFIDENCE_THRESHOLD: float = 0.9   # degeneration warning

# ---------------------------------------------------------------------------
# LLM review file scoring (dynoproactive.py — _compute_file_scores)
# ---------------------------------------------------------------------------

FILE_SCORE_CHURN_MAX: int = 10
FILE_SCORE_CHURN_WEIGHT: int = 3
FILE_SCORE_COMPLEXITY_DIVISOR: int = 50
FILE_SCORE_COMPLEXITY_MAX: int = 10
FILE_SCORE_COMPLEXITY_WEIGHT: int = 2
FILE_SCORE_NO_TEST_BOOST: int = 5
FILE_SCORE_PENALTY_SCANNED_TODAY: int = 100
FILE_SCORE_PENALTY_SCANNED_3DAYS: int = 30
FILE_SCORE_PENALTY_SCANNED_7DAYS: int = 10
FILE_SCORE_PENALTY_CLEAN_SCAN: int = 5

LLM_REVIEW_MAX_FILES: int = 10
LLM_REVIEW_FILE_TRUNCATION: int = 200   # max lines sent to Haiku

# ---------------------------------------------------------------------------
# Verification scoring (dynoproactive.py — _verify_fix)
# ---------------------------------------------------------------------------

VERIFY_DIFF_PENALTY_DIVISOR: float = 1000.0
VERIFY_DIFF_PENALTY_CAP: float = 0.25
VERIFY_FILE_PENALTY: float = 0.03
VERIFY_FILE_PENALTY_CAP: float = 0.15
VERIFY_LARGE_DIFF_PENALTY: float = 0.15

# ---------------------------------------------------------------------------
# PR throttling & limits (dynoproactive.py)
# ---------------------------------------------------------------------------

MAX_PRS_PER_DAY: int = 100
MAX_OPEN_PRS: int = 100
COOLDOWN_AFTER_FAILURES: int = 100

FINDING_MAX_AGE_DAYS: int = 30
MAX_FINDINGS_ENTRIES: int = 500
RECENT_PRS_COUNT: int = 10
MAX_ATTEMPTS: int = 3

# ---------------------------------------------------------------------------
# Timeouts — dynoproactive.py
# ---------------------------------------------------------------------------

SCAN_TIMEOUT_SECONDS: int = 600
LLM_INVOCATION_TIMEOUT: int = 600
RESCAN_TIMEOUT: int = 120
GH_API_TIMEOUT: int = 30
GIT_BRANCH_TIMEOUT: int = 15
GIT_PUSH_TIMEOUT: int = 15
GIT_DELETE_TIMEOUT: int = 10
PIP_AUDIT_TIMEOUT: int = 120
NPM_AUDIT_TIMEOUT: int = 120

# ---------------------------------------------------------------------------
# Cross-project priority queue (dynoglobal.py)
# ---------------------------------------------------------------------------

SEVERITY_WEIGHTS: dict[str, int] = {
    "critical": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
}

DEFAULT_CENTRALITY_SCORE: float = 0.5   # when file not in PageRank graph

# ---------------------------------------------------------------------------
# Backoff (dynoglobal.py)
# ---------------------------------------------------------------------------

BACKOFF_HOURS_7DAY: int = 168
BACKOFF_SKIP_RATIO_7DAY: int = 8
BACKOFF_HOURS_3DAY: int = 72
BACKOFF_SKIP_RATIO_3DAY: int = 4
BACKOFF_HOURS_1DAY: int = 24
BACKOFF_SKIP_RATIO_1DAY: int = 2

# ---------------------------------------------------------------------------
# Daemon (dynoglobal.py)
# ---------------------------------------------------------------------------

DEFAULT_POLL_SECONDS: int = 3600
MAINTENANCE_CYCLE_TIMEOUT: int = 300
LOG_MAX_AGE_DAYS: int = 30
RECENT_TASKS_WINDOW: int = 10
RECENT_SWEEPS_COUNT: int = 5

# Task-level token budgets (by risk_level x task_type)
TASK_TOKEN_BUDGETS: dict[str, dict[str, int]] = {
    "low":      {"feature": 30_000,  "bugfix": 15_000,  "refactor": 20_000},
    "medium":   {"feature": 80_000,  "bugfix": 40_000,  "refactor": 50_000},
    "high":     {"feature": 200_000, "bugfix": 80_000,  "refactor": 100_000},
    "critical": {"feature": 400_000, "bugfix": 200_000, "refactor": 200_000},
}

DEFAULT_TOKEN_BUDGET: int = 60_000

# ---------------------------------------------------------------------------
# Design option scoring (dynosdream.py)
# ---------------------------------------------------------------------------

DESIGN_WEIGHT_COMPLEXITY: float = 0.35
DESIGN_WEIGHT_MAINTAINABILITY: float = 0.20
DESIGN_WEIGHT_SECURITY: float = 0.25
DESIGN_WEIGHT_TRAJECTORY: float = 0.20

DESIGN_FILE_PENALTY_PER_FILE: float = 0.03
DESIGN_FILE_PENALTY_CAP: float = 0.35
DESIGN_SECURITY_PENALTY_PER_HIT: float = 0.03
DESIGN_SECURITY_PENALTY_CAP: float = 0.20
DESIGN_MAINTAINABILITY_KEYWORD_WEIGHT: float = 0.04
DESIGN_ARCH_COMPLEXITY_DIVISOR: int = 40

DESIGN_COMPLEXITY_PENALTIES: dict[str, float] = {
    "easy": 0.05,
    "medium": 0.12,
    "hard": 0.22,
}

DESIGN_RISK_PENALTIES: dict[str, float] = {
    "low": 0.05,
    "medium": 0.10,
    "high": 0.18,
    "critical": 0.28,
}

# MCTS simulation
MCTS_DEFAULT_ITERATIONS: int = 12
MCTS_ROLLOUT_NOISE_MIN: float = -0.05
MCTS_ROLLOUT_NOISE_MAX: float = 0.05

# Score thresholds for design option certificates
SCORE_THRESHOLD_PREFERRED: float = 0.78
SCORE_THRESHOLD_ACCEPTABLE: float = 0.62
COMPONENT_MIN_ACCEPTABLE: float = 0.70    # complexity, maintainability, security
TRAJECTORY_MIN_ACCEPTABLE: float = 0.40
SECURITY_FINDINGS_THRESHOLD_LOW: float = 0.75
SECURITY_FINDINGS_THRESHOLD_CRITICAL: float = 0.55

RELATED_TRAJECTORIES_LIMIT: int = 3

# ---------------------------------------------------------------------------
# Postmortem analysis (dynopostmortem.py)
# ---------------------------------------------------------------------------

OVERRUN_RATIO_MEDIUM: float = 1.5
OVERRUN_RATIO_HIGH: float = 3.0
QUALITY_THRESHOLD_ANOMALY: float = 0.5
REPAIR_CYCLES_THRESHOLD: int = 2
WASTED_SPAWN_RATIO_THRESHOLD: float = 0.5
QUALITY_REGRESSION_THRESHOLD: float = 0.3

PATTERN_DETECTION_WINDOW: int = 5
GENERIC_ROUTING_PATTERN_THRESHOLD: int = 3
OVERRUN_PATTERN_THRESHOLD: int = 2
REPAIR_PATTERN_THRESHOLD: int = 3
ZERO_QUALITY_PATTERN_THRESHOLD: int = 2

SIMILAR_TASKS_LIMIT: int = 3
SIMILARITY_WEIGHT_TASKTYPE: float = 0.5
SIMILARITY_WEIGHT_DOMAIN: float = 0.3
SIMILARITY_WEIGHT_RISK: float = 0.2

# ---------------------------------------------------------------------------
# Router (dynorouter.py)
# ---------------------------------------------------------------------------

ROUTER_WEIGHT_QUALITY: float = 0.5
ROUTER_WEIGHT_COST: float = 0.3
ROUTER_WEIGHT_EFFICIENCY: float = 0.2

UCB_EXPLORATION_CONSTANT: float = 0.5
UCB_COLD_START_MINIMUM: int = 5
DEFAULT_SKIP_THRESHOLD: int = 3

# Recurring pattern detection
RECURRING_PATTERN_MIN_TASKS: int = 3
RECURRING_PATTERN_THRESHOLD: float = 0.5  # 50% of task count
