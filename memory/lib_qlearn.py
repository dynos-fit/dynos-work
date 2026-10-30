#!/usr/bin/env python3
"""Tabular Q-learning for the dynos-work repair loop.

Two Q-tables:
  - executor assignment: which executor handles which finding type
  - model selection: which model to use for each executor/finding combo

Toggle via policy.json: "repair_qlearning": true/false
Depends only on lib_core.
"""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent)); _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "hooks"))

import json
import random
from pathlib import Path

from lib_core import _persistent_project_dir, load_json, now_iso, project_policy, write_json
from lib_usage_telemetry import record_usage as _record_usage
_record_usage("lib_qlearn")
from lib_defaults import (
    EFFICIENCY_CLAMP,
    EFFICIENCY_WEIGHT,
    ESCALATION_RETRY_THRESHOLD,
    QLEARN_ALPHA,
    QLEARN_EPSILON,
    QLEARN_GAMMA,
    QVALUE_PRECISION,
    REWARD_FAILURE,
    REWARD_MAX,
    REWARD_MIN,
    REWARD_PARTIAL,
    REWARD_PENALTY_PER_NEW_FINDING,
    REWARD_SUCCESS,
    TOKEN_BUDGETS,
)

# Lazy import to avoid circular dependency — router imports lib_core too
_router = None

def _get_router():
    global _router
    if _router is None:
        import router as _r
        _router = _r
    return _router

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_ALPHA = QLEARN_ALPHA      # learning rate
DEFAULT_GAMMA = QLEARN_GAMMA      # discount factor
DEFAULT_EPSILON = QLEARN_EPSILON  # exploration rate

from lib_core import VALID_EXECUTORS as _DISCOVERED_EXECUTORS

ALL_EXECUTORS = sorted(_DISCOVERED_EXECUTORS)

# Default per-category action spaces — overridable via .dynos/config/action-spaces.json
_DEFAULT_ACTION_SPACE: dict[str, list[str]] = {
    "sec":  ["security-executor", "backend-executor", "integration-executor"],
    "comp": ["release-executor", "integration-executor", "backend-executor"],
    "cq":   ["backend-executor", "refactor-executor", "testing-executor"],
    "dc":   ["refactor-executor"],
    "db":   ["db-executor", "data-executor"],
    "ui":   ["ui-executor"],
    "perf": ["backend-executor", "db-executor", "observability-executor"],
    "sc":   ["backend-executor", "ui-executor", "db-executor", "integration-executor"],
    "infra": ["infra-executor", "release-executor"],
    "data": ["data-executor", "db-executor"],
    "obs": ["observability-executor", "backend-executor"],
    "test": ["testing-executor"],
    "docs": ["docs-executor"],
}

# The reporting auditor is the reliable category signal; finding IDs are
# free-form auditor output (``SC-`` is both spec-completion's prefix and what
# supply-chain auditors emit). An ID prefix that matches no action space used
# to fall through to every executor, so an unseen state picked one at random
# (a data-executor assigned to a receipt file).
_AUDITOR_CATEGORY: dict[str, str] = {
    "security-auditor": "sec",
    "privacy-auditor": "sec",
    "threat-model-auditor": "sec",
    "code-quality-auditor": "cq",
    "architecture-auditor": "cq",
    "dead-code-auditor": "dc",
    "db-schema-auditor": "db",
    "data-integrity-auditor": "data",
    "ui-auditor": "ui",
    "accessibility-auditor": "ui",
    "performance-auditor": "perf",
    "spec-completion-auditor": "sc",
    "infrastructure-auditor": "infra",
    "supply-chain-auditor": "infra",
    "release-auditor": "infra",
    "observability-auditor": "obs",
    "test-strategy-auditor": "test",
    "docs-accuracy-auditor": "docs",
}

EXECUTOR_ACTION_SPACE = _DEFAULT_ACTION_SPACE  # module-level default

# ---------------------------------------------------------------------------
# File domains
# ---------------------------------------------------------------------------
# The auditor says what kind of defect a finding is; the files say who can
# fix it. A code-quality finding on a .tsx file is still UI work, so the
# learner may only pick among executors that fit the files to modify.
# Ordered most-preferred first: the head of the list is the deterministic
# default when the learner has nothing to go on.
FILE_DOMAIN_EXECUTORS: dict[str, list[str]] = {
    "test": ["testing-executor"],
    "db": ["db-executor", "data-executor"],
    "docs": ["docs-executor", "release-executor"],
    "ui": ["ui-executor"],
    "infra": ["infra-executor", "release-executor", "security-executor"],
    "code": [
        "backend-executor", "refactor-executor", "security-executor", "integration-executor",
        "db-executor", "data-executor", "observability-executor", "ml-executor", "ui-executor",
    ],
}

_UI_EXTS = {".tsx", ".jsx", ".vue", ".svelte", ".astro", ".css", ".scss", ".sass", ".less",
            ".styl", ".html", ".htm"}
_DOC_EXTS = {".md", ".mdx", ".rst", ".adoc", ".txt"}
_DB_EXTS = {".sql", ".prisma"}
_CODE_EXTS = {".py", ".pyi", ".go", ".rs", ".rb", ".java", ".kt", ".kts", ".scala", ".ts", ".mts",
              ".cts", ".js", ".mjs", ".cjs", ".php", ".cs", ".ex", ".exs", ".dart", ".swift",
              ".c", ".h", ".cc", ".cpp", ".hpp", ".m", ".mm", ".lua", ".sh", ".bash", ".zsh",
              ".ipynb", ".r", ".jl", ".clj", ".erl", ".hs", ".ml", ".fs", ".graphql", ".proto"}
_INFRA_EXTS = {".tf", ".tfvars", ".hcl", ".nix"}
_INFRA_NAMES = {"dockerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yml",
                "compose.yaml", "makefile", "procfile", "jenkinsfile", "uv.lock", "poetry.lock",
                "pipfile", "pipfile.lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
                "bun.lockb", "cargo.lock", "go.sum", "gemfile.lock", "composer.lock",
                "requirements.txt", ".gitignore", ".dockerignore", ".gitlab-ci.yml"}
_INFRA_DIRS = {".github", ".circleci", ".gitlab", "infra", "infrastructure", "terraform", "deploy",
               "deployment", "k8s", "kubernetes", "helm", "charts", "ansible", ".buildkite"}
_TEST_DIRS = {"tests", "test", "__tests__", "spec", "specs", "e2e", "cypress", "playwright"}
_DB_DIRS = {"migrations", "migration", "alembic", "db/migrate"}
_DOC_DIRS = {"docs", "doc", "documentation"}


def file_domain(path: str) -> str:
    """Repair domain of one repo path: test, db, docs, ui, infra, code, other.

    Order matters: a migration written in Python is db work, a test of a
    React component is test work, and a docs/ page is docs whatever its
    extension.
    """
    rel = path.strip().replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    lowered = rel.lower()
    parts = lowered.split("/")
    dirs, name = parts[:-1], parts[-1]
    ext = "." + name.rsplit(".", 1)[1] if "." in name.lstrip(".") else ""
    stem = name[: -len(ext)] if ext else name
    if (
        any(d in _TEST_DIRS for d in dirs)
        or stem.startswith("test_") or stem.endswith("_test")
        or stem.endswith((".test", ".spec")) or name == "conftest.py"
    ):
        return "test"
    if any(d in _DB_DIRS for d in dirs) or "db/migrate" in lowered or ext in _DB_EXTS:
        return "db"
    if any(d in _DOC_DIRS for d in dirs) or (ext in _DOC_EXTS and name not in _INFRA_NAMES):
        return "docs"
    if ext in _UI_EXTS:
        return "ui"
    if name in _INFRA_NAMES or name.startswith("dockerfile") or any(d in _INFRA_DIRS for d in dirs) or ext in _INFRA_EXTS:
        return "infra"
    if ext in _CODE_EXTS:
        return "code"
    return "other"


def files_domain(files: list[str] | None) -> str | None:
    """Sorted '+'-joined domains of *files* ('code+ui'), or None for no files."""
    domains = sorted({file_domain(f) for f in files or [] if isinstance(f, str) and f.strip()})
    return "+".join(domains) if domains else None


def executors_for_files(files: list[str] | None) -> list[str]:
    """Executors fit to edit *files*, most-preferred first.

    The union over each file's domain, in file order. A file of unknown
    domain ('other') constrains nothing, so it contributes no executors; a
    file list of only unknown files returns ``[]`` (no constraint).
    """
    out: list[str] = []
    for path in files or []:
        if not isinstance(path, str) or not path.strip():
            continue
        for executor in FILE_DOMAIN_EXECUTORS.get(file_domain(path), []):
            if executor in _DISCOVERED_EXECUTORS and executor not in out:
                out.append(executor)
    return out


def allowed_executors(category: str, files: list[str] | None, root: Path | None = None) -> list[str]:
    """The learner's action space for one repair task.

    The category's action space intersected with the executors that fit the
    files, in the category's order; when they share nothing, the files win
    (a code-quality defect in a .tsx file goes to ui-executor). Without files
    the category's action space applies unchanged.
    """
    by_category = _executors_for_category(category, root)
    by_files = executors_for_files(files)
    if not by_files:
        return by_category
    both = [e for e in by_category if e in by_files]
    return both or by_files


def default_executor(category: str, files: list[str] | None, root: Path | None = None) -> str:
    """Deterministic executor for a repair task when the learner is off or
    returns nothing: the head of ``allowed_executors``."""
    allowed = allowed_executors(category, files, root)
    return allowed[0] if allowed else "backend-executor"


def severity_floor_tier(executor: str | None, severity: str, blocking: bool = False) -> str | None:
    """Tier to force when no model was chosen but *executor*'s role default
    is the fast tier and the finding is serious; else None."""
    from lib_models import ROLE_DEFAULT_TIERS, TIER_BALANCED, TIER_FAST  # noqa: PLC0415

    serious = blocking or severity in _SERIOUS_SEVERITIES or _normalize_severity(severity) in _SERIOUS_SEVERITIES
    if serious and ROLE_DEFAULT_TIERS.get(executor or "") == TIER_FAST:
        return TIER_BALANCED
    return None


# Severities that never get the fast tier.
_SERIOUS_SEVERITIES = {"critical", "high", "major", "blocking"}
_SEVERITY_ALIASES = {"major": "high", "minor": "low", "blocking": "high", "non-blocking": "low"}


def _normalize_severity(severity: str) -> str:
    return _SEVERITY_ALIASES.get(severity, severity)


def _is_blocking(finding: dict) -> bool:
    return finding.get("blocking") is True or finding.get("severity") == "blocking"

VALID_ROUTE_MODES = ["generic", "learned"]
from lib_models import ALL_TIERS as ALL_TIERS, resolve_model_for_tier as _resolve_model_for_tier  # re-export; replaces VALID_MODELS
from lib_host import detect_host as _detect_host, get_persisted_host as _get_persisted_host

# Backwards compat
VALID_EXECUTORS = ALL_EXECUTORS

# TOKEN_BUDGETS imported from lib_defaults


# ---------------------------------------------------------------------------
# Q-table storage
# ---------------------------------------------------------------------------

def _q_table_path(root: Path, table_name: str) -> Path:
    return _persistent_project_dir(root) / f"q-repair-{table_name}.json"


def load_q_table(root: Path, table_name: str) -> dict:
    """Load a Q-table from persistent storage. Returns empty table if not found."""
    path = _q_table_path(root, table_name)
    if not path.exists():
        return {"version": 1, "updated_at": now_iso(), "entries": {}}
    try:
        data = load_json(path)
        if not isinstance(data, dict) or "entries" not in data:
            return {"version": 1, "updated_at": now_iso(), "entries": {}}
        return data
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "updated_at": now_iso(), "entries": {}}


def save_q_table(root: Path, table_name: str, table: dict) -> None:
    """Save a Q-table to persistent storage."""
    table["updated_at"] = now_iso()
    write_json(_q_table_path(root, table_name), table)



# ---------------------------------------------------------------------------
# State encoding
# ---------------------------------------------------------------------------


def encode_repair_state(
    finding_category: str,
    severity: str,
    task_type: str,
    retry_count: int,
    domain: str | None = None,
) -> str:
    """Encode repair state as a compact string key.

    Example: "sec:critical:feature:0", or with the files' domain
    "cq:high:feature:0:ui". The domain is appended (severity stays the
    second field, which ``update_from_outcomes`` reads), so values learned
    for one kind of file never steer another. Keys written before the domain
    existed are never matched by a domain-tagged state: those states start
    fresh rather than inherit Q-values learned across unrelated files.
    """
    base = f"{finding_category}:{severity}:{task_type}:{retry_count}"
    return f"{base}:{domain}" if domain else base


def finding_category(finding_id: str, auditor: str = "") -> str:
    """Action-space category for a finding: from the auditor when known,
    else the lowercased ID prefix. 'SEC-001' → 'sec'."""
    if auditor in _AUDITOR_CATEGORY:
        return _AUDITOR_CATEGORY[auditor]
    prefix = finding_id.split("-")[0] if "-" in finding_id else finding_id
    return prefix.lower()


# ---------------------------------------------------------------------------
# Action selection (epsilon-greedy)
# ---------------------------------------------------------------------------

def select_action(
    q_table: dict,
    state: str,
    valid_actions: list[str],
    epsilon: float,
    default: str | None = None,
) -> tuple[str, str]:
    """Select an action using epsilon-greedy policy.

    Returns (action, source) where source is "q-learning", "q-explore", or
    "q-default". With ``default`` set (a deterministic fallback), an unseen
    state returns it instead of a random pick; pass ``epsilon=0`` with it
    for a fully deterministic choice.
    """
    if not valid_actions:
        raise ValueError("No valid actions")

    entries = q_table.get("entries", {})
    state_q = entries.get(state, {})

    # Epsilon-greedy: explore with probability epsilon
    if epsilon > 0 and random.random() < epsilon:
        action = random.choice(valid_actions)
        return action, "q-explore"

    # Exploit: pick action with highest Q-value
    best_action = None
    best_value = float("-inf")
    for action in valid_actions:
        value = float(state_q.get(action, 0.0))
        if value > best_value:
            best_value = value
            best_action = action

    # If all Q-values are 0 (unseen state), pick randomly — or the default.
    if best_value == 0.0 and all(float(state_q.get(a, 0.0)) == 0.0 for a in valid_actions):
        if default is not None:
            return (default if default in valid_actions else valid_actions[0]), "q-default"
        return random.choice(valid_actions), "q-explore"

    return best_action, "q-learning"


def get_q_value(q_table: dict, state: str, action: str) -> float:
    """Get the Q-value for a state-action pair."""
    return float(q_table.get("entries", {}).get(state, {}).get(action, 0.0))


# ---------------------------------------------------------------------------
# Q-value update
# ---------------------------------------------------------------------------

def update_q_value(
    q_table: dict,
    state: str,
    action: str,
    reward: float,
    next_state: str | None,
    alpha: float = DEFAULT_ALPHA,
    gamma: float = DEFAULT_GAMMA,
) -> float:
    """Update Q-value using the standard Q-learning rule.

    Q(s,a) = Q(s,a) + alpha * (reward + gamma * max_a'(Q(s',a')) - Q(s,a))

    If next_state is None (terminal), the future value term is 0.
    Returns the new Q-value.
    """
    entries = q_table.setdefault("entries", {})
    state_q = entries.setdefault(state, {})
    old_value = float(state_q.get(action, 0.0))

    # Future value: max Q(s', a') over all actions in next state
    future_value = 0.0
    if next_state is not None:
        next_q = entries.get(next_state, {})
        if next_q:
            future_value = max(float(v) for v in next_q.values())

    # Q-learning update
    new_value = old_value + alpha * (reward + gamma * future_value - old_value)
    state_q[action] = round(new_value, QVALUE_PRECISION)

    return new_value


# ---------------------------------------------------------------------------
# Reward computation
# ---------------------------------------------------------------------------

def compute_repair_reward(
    finding_resolved: bool,
    new_findings_introduced: int,
    tokens_used: int,
    token_budget: int,
) -> float:
    """Compute reward for a single repair action.

    Returns a float in [-1.0, +1.0].
    """
    # Base reward
    if finding_resolved and new_findings_introduced == 0:
        reward = REWARD_SUCCESS
    elif finding_resolved:
        reward = max(0.0, REWARD_PARTIAL - REWARD_PENALTY_PER_NEW_FINDING * new_findings_introduced)
    else:
        reward = REWARD_FAILURE

    # Token efficiency bonus/penalty
    if token_budget > 0 and tokens_used > 0:
        efficiency = EFFICIENCY_WEIGHT * (1.0 - tokens_used / token_budget)
        efficiency = max(-EFFICIENCY_CLAMP, min(EFFICIENCY_CLAMP, efficiency))
        reward += efficiency

    return max(REWARD_MIN, min(REWARD_MAX, reward))


# ---------------------------------------------------------------------------
# Repair plan builder
# ---------------------------------------------------------------------------

def _load_action_spaces(root: Path) -> dict[str, list[str]]:
    """Load action spaces from .dynos/config/action-spaces.json with fallback to defaults."""
    config_path = root / ".dynos" / "config" / "action-spaces.json"
    try:
        data = load_json(config_path)
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return _DEFAULT_ACTION_SPACE


def _executors_for_category(category: str, root: Path | None = None) -> list[str]:
    """Return the valid executor action space for a finding category."""
    spaces = _load_action_spaces(root) if root else _DEFAULT_ACTION_SPACE
    return spaces.get(category, ALL_EXECUTORS)


def _has_learned_agent(root: Path, executor: str, task_type: str) -> tuple[bool, str | None, str | None]:
    """Check if a learned agent exists for this executor + task_type.

    Returns (exists, agent_path, agent_name).
    """
    try:
        router = _get_router()
        route = router.resolve_route(root, executor, task_type)
        if route.get("mode") in ("replace", "alongside") and route.get("agent_path"):
            return True, route.get("agent_path"), route.get("agent_name")
    except Exception:
        pass
    return False, None, None


def _resolve_host(root: Path) -> str:
    """Return the active host: persisted value if available, else detect from environment."""
    cp_path = _persistent_project_dir(root) / "control-plane.json"
    return _get_persisted_host(cp_path) or _detect_host()


def _tier_to_model_override(host: str, tier: str | None) -> tuple[str | None, bool]:
    """Resolve a tier name to the emitted model_override value for *host*.

    Returns (resolved_model_or_None, should_include_key).
    - Under claude: resolves deep/balanced/fast to their vendor model literal (True)
    - Under a host where tier resolves to None: returns (None, False) — caller omits key
    - tier is None (default/disabled path): returns (None, False)
    """
    if tier is None:
        return None, False
    resolved = _resolve_model_for_tier(host, tier)
    if resolved is None:
        return None, False
    return resolved, True


def build_repair_plan(
    root: Path,
    findings: list[dict],
    task_type: str,
) -> dict:
    """Build executor, route-mode, and model assignments using hierarchical Q-learning.

    Three sequential decisions per finding, each conditioned on the prior:
      1. Executor: the finding category's action space intersected with the
         executors that fit the finding's ``files`` (see ``allowed_executors``)
      2. Route mode: generic vs learned (only if learned agent exists)
      3. Model tier: fast/balanced/deep/frontier (hard constraints override;
         never fast for critical/high/major/blocking severity)

    A finding may carry ``files`` (the repair's files_to_modify); their
    domain joins the Q state key. A blocking finding is routed without
    exploration: epsilon is 0 and an unseen state takes the deterministic
    default (the head of the allowed executors; the executor's role-default
    tier, raised to the severity floor) instead of a random pick.

    If repair_qlearning is disabled in policy.json, returns default assignments.
    """
    from lib_models import (  # noqa: PLC0415
        ROLE_DEFAULT_TIERS as _ROLE_DEFAULT_TIERS,
        TIER_BALANCED as _TIER_BALANCED,
        TIER_DEEP as _TIER_DEEP_QK,
        TIER_FAST as _TIER_FAST,
    )

    policy = project_policy(root)
    enabled = bool(policy.get("repair_qlearning", True))
    epsilon = float(policy.get("repair_epsilon", DEFAULT_EPSILON))

    host = _resolve_host(root)

    executor_q = load_q_table(root, "executor") if enabled else {"entries": {}}
    route_q = load_q_table(root, "route") if enabled else {"entries": {}}
    model_q = load_q_table(root, "model") if enabled else {"entries": {}}

    assignments = []
    for finding in findings:
        finding_id = finding.get("id", finding.get("finding_id", "unknown"))
        raw_severity = str(finding.get("severity", "medium") or "medium")
        severity = _normalize_severity(raw_severity)
        auditor = finding.get("auditor", finding.get("auditor_name", ""))
        retry_count = int(finding.get("retry_count", 0))
        category = finding_category(finding_id, auditor)
        files = [f for f in finding.get("files") or [] if isinstance(f, str) and f.strip()]
        domain = files_domain(files)
        blocking = _is_blocking(finding)
        serious = raw_severity in _SERIOUS_SEVERITIES or severity in _SERIOUS_SEVERITIES or blocking
        # Blocking findings are not an exploration budget.
        finding_epsilon = 0.0 if blocking else epsilon

        # Base state: category:severity:task_type:retry[:domain]
        base_state = encode_repair_state(category, severity, task_type, retry_count, domain)

        # --- Step 1: Executor (restricted by category and files) ---
        valid_executors = allowed_executors(category, files, root)
        if enabled:
            executor, executor_source = select_action(
                executor_q, base_state, valid_executors, finding_epsilon,
                default=valid_executors[0] if blocking else None,
            )
            executor_q_val = get_q_value(executor_q, base_state, executor)
        else:
            executor = None
            executor_source = "default"
            executor_q_val = None

        # --- Step 2: Route mode (conditioned on executor) ---
        # Only offer "learned" if a learned agent actually exists
        has_learned, agent_path, agent_name = (
            _has_learned_agent(root, executor, task_type)
            if executor and enabled else (False, None, None)
        )

        if enabled and executor and has_learned:
            route_state = f"{base_state}:{executor}"
            route_mode, route_source = select_action(
                route_q, route_state, VALID_ROUTE_MODES, finding_epsilon,
                default="generic" if blocking else None,
            )
            route_q_val = get_q_value(route_q, route_state, route_mode)
        elif has_learned:
            route_mode = "generic"
            route_source = "default"
            route_q_val = None
        else:
            route_mode = "generic"
            route_source = "no_learned_agent"
            route_q_val = None
            agent_path = None
            agent_name = None

        # --- Step 3: Model (conditioned on executor + route mode) ---
        # Hard constraints first
        valid_tiers = [t for t in ALL_TIERS if not (serious and t == _TIER_FAST)]
        if retry_count >= ESCALATION_RETRY_THRESHOLD:
            model = _TIER_DEEP_QK
            model_source = "escalation"
            model_q_val = None
        elif auditor.startswith("security"):
            model = _TIER_DEEP_QK
            model_source = "security_floor"
            model_q_val = None
        elif enabled and executor:
            model_state = f"{base_state}:{executor}:{route_mode}"
            default_tier = _ROLE_DEFAULT_TIERS.get(executor, _TIER_BALANCED)
            if default_tier not in valid_tiers:
                default_tier = _TIER_BALANCED
            model, model_source = select_action(
                model_q, model_state, valid_tiers, finding_epsilon,
                default=default_tier if blocking else None,
            )
            model_q_val = get_q_value(model_q, model_state, model)
        else:
            model = None
            model_source = "default"
            model_q_val = None

        resolved_model, include_model_override = _tier_to_model_override(host, model)
        assignment: dict = {
            "finding_id": finding_id,
            "state": base_state,
            "serious": serious,
            "file_domain": domain,
            "allowed_executors": valid_executors,
            "assigned_executor": executor,
            "executor_source": executor_source,
            "executor_q_value": executor_q_val,
            "route_mode": route_mode,
            "route_source": route_source,
            "route_q_value": route_q_val if enabled else None,
            "agent_path": agent_path if route_mode == "learned" else None,
            "agent_name": agent_name if route_mode == "learned" else None,
            "model_source": model_source,
            "model_q_value": model_q_val,
        }
        if include_model_override:
            assignment["model_override"] = resolved_model
        assignments.append(assignment)

    return {
        "generated_at": now_iso(),
        "source": "q-learning" if enabled else "default",
        "enabled": enabled,
        "epsilon": epsilon if enabled else None,
        "assignments": assignments,
    }


# ---------------------------------------------------------------------------
# Repair outcome update
# ---------------------------------------------------------------------------

def update_from_outcomes(root: Path, outcomes: list[dict], task_type: str) -> dict:
    """Update all three Q-tables from repair outcomes.

    Each outcome has: finding_id, state, executor, route_mode, model,
    resolved, new_findings, tokens_used, next_state.

    The same reward feeds all three tables — the executor, route-mode,
    and model all contributed to the outcome jointly.

    Returns summary of updates applied.
    """
    policy = project_policy(root)
    enabled = bool(policy.get("repair_qlearning", True))
    if not enabled:
        return {"updated": False, "reason": "repair_qlearning disabled"}

    alpha = float(policy.get("repair_alpha", DEFAULT_ALPHA))
    gamma = float(policy.get("repair_gamma", DEFAULT_GAMMA))

    executor_q = load_q_table(root, "executor")
    route_q = load_q_table(root, "route")
    model_q = load_q_table(root, "model")

    updates = []
    for outcome in outcomes:
        finding_id = outcome.get("finding_id", "unknown")
        state = outcome.get("state", "")
        executor = outcome.get("executor", "")
        route_mode = outcome.get("route_mode", "generic")
        model = outcome.get("model")
        resolved = bool(outcome.get("resolved", False))
        new_findings = int(outcome.get("new_findings", 0))
        tokens_used = int(outcome.get("tokens_used", 0))
        next_state = outcome.get("next_state")

        severity = state.split(":")[1] if ":" in state else "medium"
        token_budget = TOKEN_BUDGETS.get(severity, 12000)

        reward = compute_repair_reward(resolved, new_findings, tokens_used, token_budget)

        # Update executor Q-table: state → executor
        new_eq = None
        if executor and state:
            new_eq = update_q_value(executor_q, state, executor, reward, next_state, alpha, gamma)

        # Update route Q-table: state:executor → route_mode
        new_rq = None
        if executor and state and route_mode:
            route_state = f"{state}:{executor}"
            new_rq = update_q_value(route_q, route_state, route_mode, reward, next_state, alpha, gamma)

        # Update model Q-table: state:executor:route_mode → model
        new_mq = None
        if model and executor:
            model_state = f"{state}:{executor}:{route_mode}"
            new_mq = update_q_value(model_q, model_state, model, reward, next_state, alpha, gamma)

        updates.append({
            "finding_id": finding_id,
            "reward": round(reward, 4),
            "executor_q_new": round(new_eq, 4) if new_eq is not None else None,
            "route_q_new": round(new_rq, 4) if new_rq is not None else None,
            "model_q_new": round(new_mq, 4) if new_mq is not None else None,
        })

    save_q_table(root, "executor", executor_q)
    save_q_table(root, "route", route_q)
    save_q_table(root, "model", model_q)

    return {
        "updated": True,
        "alpha": alpha,
        "gamma": gamma,
        "updates": updates,
    }
