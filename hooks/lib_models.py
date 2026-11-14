"""lib_models — leaf module: tier/model/host constants and helpers.

Zero imports from any hooks/ or memory/ module.
"""
from __future__ import annotations

import re
from typing import Optional

# ---------------------------------------------------------------------------
# Tier constants
# ---------------------------------------------------------------------------

TIER_FAST: str = "fast"
TIER_BALANCED: str = "balanced"
TIER_DEEP: str = "deep"
TIER_FRONTIER: str = "frontier"

ALL_TIERS: list[str] = [TIER_FAST, TIER_BALANCED, TIER_DEEP, TIER_FRONTIER]

# Ordinal rank per tier — the single authority for "is tier A above tier B".
# Callers must compare ranks, never model literals, so that inserting a tier
# above TIER_DEEP does not silently invert an equality check (e.g. the
# security floor, or the circuit breaker's deep-tier zero-yield predicate).
TIER_RANK: dict[str, int] = {
    TIER_FAST: 0,
    TIER_BALANCED: 1,
    TIER_DEEP: 2,
    TIER_FRONTIER: 3,
}

# ---------------------------------------------------------------------------
# Host constants
# ---------------------------------------------------------------------------

HOST_CLAUDE: str = "claude"
HOST_CODEX: str = "codex"
HOST_CURSOR: str = "cursor"

ALL_HOSTS: frozenset[str] = frozenset({HOST_CLAUDE, HOST_CODEX, HOST_CURSOR})

# ---------------------------------------------------------------------------
# Tier → model mapping per host
# ---------------------------------------------------------------------------

# Claude Code aliases (capability tiers, not full Anthropic IDs).
_CLAUDE_TIER_MAP: dict[str, Optional[str]] = {
    TIER_FAST: "haiku",
    TIER_BALANCED: "sonnet",
    TIER_DEEP: "opus",
    TIER_FRONTIER: "fable",
}

# Cursor's included-usage pool (Composer + Grok). Claude/GPT/Gemini are
# third-party on Cursor and must not be selected when host=cursor.
_CURSOR_TIER_MAP: dict[str, Optional[str]] = {
    TIER_FAST: "composer-2.5-fast",
    TIER_BALANCED: "composer-2.5",
    TIER_DEEP: "grok-4.7",
    TIER_FRONTIER: "grok-4.7-high-fast",
}

TIER_TO_MODEL: dict[str, dict[str, Optional[str]]] = {
    HOST_CLAUDE: dict(_CLAUDE_TIER_MAP),
    HOST_CURSOR: dict(_CURSOR_TIER_MAP),
    HOST_CODEX: {
        TIER_FAST: None,
        TIER_BALANCED: None,
        TIER_DEEP: None,
        TIER_FRONTIER: None,
    },
}

# Prefixes that already identify Cursor-pool models (keep as-is).
_CURSOR_MODEL_PREFIXES: tuple[str, ...] = (
    "composer-",
    "grok-",
    "cursor-grok-",
)

# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def resolve_model_for_tier(host: str, tier: str) -> Optional[str]:
    """Return the model literal for *host* at *tier*, or None if unmapped."""
    host_map = TIER_TO_MODEL.get(host)
    if host_map is None:
        return None
    return host_map.get(tier)


_MODEL_ID_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def normalize_model(model: Optional[str]) -> Optional[str]:
    """Return the alias for a full Claude model ID, else *model* unchanged.

    Subagent transcripts record full IDs ("claude-sonnet-5-5",
    "us.anthropic.claude-opus-5-5[1m]"), and the token hook stores them raw on
    purpose. Readers that compare against aliases or tier names call this
    first. An alias or tier name comes back lowercased; an ID containing
    "claude" and exactly one known Claude alias comes back as that alias;
    anything else (None, non-strings, unknown names) comes back unchanged.
    """
    if not isinstance(model, str):
        return model
    lowered = model.strip().lower()
    aliases = valid_models_for_host(HOST_CLAUDE)
    if lowered in aliases or lowered in TIER_RANK:
        return lowered
    if "claude" in lowered:
        families = {t for t in _MODEL_ID_TOKEN_SPLIT.split(lowered) if t in aliases}
        if len(families) == 1:
            return families.pop()
    return model


_CLAUDE_ALIASES: frozenset[str] = frozenset(
    m for m in _CLAUDE_TIER_MAP.values() if m is not None
)


def model_to_tier(model: str, *, host: str | None = None) -> Optional[str]:
    """Return the tier for *model* (host alias, Claude alias, or full ID)."""
    if not isinstance(model, str):
        return None
    lowered = model.strip().lower()
    if lowered in TIER_RANK:
        return lowered
    if host:
        for tier, mapped in TIER_TO_MODEL.get(host, {}).items():
            if mapped is not None and mapped.lower() == lowered:
                return tier
    for tier, mapped in _CURSOR_TIER_MAP.items():
        if mapped is not None and mapped.lower() == lowered:
            return tier
    normalized = normalize_model(model)
    if isinstance(normalized, str) and normalized in TIER_RANK:
        return normalized
    if isinstance(normalized, str):
        for host_map in TIER_TO_MODEL.values():
            for tier, m in host_map.items():
                if m is not None and m == normalized:
                    return tier
    return None


def translate_model_for_host(model: Optional[str], host: str) -> Optional[str]:
    """Map Claude-era aliases to the active host's spawn model IDs.

    On ``host=cursor``, policy overrides, learned UCB winners, and agent
    frontmatter that still say ``haiku``/``sonnet``/``opus``/``fable`` are
    rewritten to Composer/Grok pool models. Already-Cursor IDs pass through.
    """
    if model is None or host != HOST_CURSOR:
        return model
    if not isinstance(model, str):
        return model
    lowered = model.strip().lower()
    if lowered in ("inherit", "default", ""):
        return None
    if lowered in TIER_RANK:
        return resolve_model_for_tier(host, lowered)
    _alias_to_tier = {
        "haiku": TIER_FAST,
        "sonnet": TIER_BALANCED,
        "opus": TIER_DEEP,
        "fable": TIER_FRONTIER,
    }
    if lowered in _alias_to_tier:
        return resolve_model_for_tier(host, _alias_to_tier[lowered])
    if any(lowered.startswith(p) for p in _CURSOR_MODEL_PREFIXES):
        return lowered
    # Block explicit third-party model IDs on Cursor — route to role-appropriate tier.
    if (
        "claude" in lowered
        or lowered.startswith("gpt-")
        or lowered.startswith("gemini")
        or lowered.startswith("deepseek")
    ):
        return resolve_model_for_tier(host, TIER_BALANCED)
    return lowered


def valid_models_for_host(host: str) -> frozenset[str]:
    """Return the frozenset of non-None model literals for *host*."""
    host_map = TIER_TO_MODEL.get(host)
    if host_map is None:
        return frozenset()
    return frozenset(m for m in host_map.values() if m is not None)


def tier_rank(tier: Optional[str]) -> int:
    """Return the ordinal rank of *tier*, or -1 when unknown/None."""
    if tier is None:
        return -1
    return TIER_RANK.get(tier, -1)


def model_rank(model: Optional[str], *, host: str | None = None) -> int:
    """Return the ordinal rank of *model*'s tier, or -1 when unmapped.

    Accepts either a model literal ("opus") or a tier name ("deep"), since
    model_override and policy files admit both spellings.
    """
    if model is None:
        return -1
    if host == HOST_CURSOR:
        translated = translate_model_for_host(model, host)
        tier = model_to_tier(translated or "", host=host)
        if tier is not None:
            return tier_rank(tier)
    model_norm = normalize_model(model)
    if isinstance(model_norm, str) and model_norm in TIER_RANK:
        return TIER_RANK[model_norm]
    tier = model_to_tier(model_norm if isinstance(model_norm, str) else str(model), host=host)
    return tier_rank(tier)


# ---------------------------------------------------------------------------
# Role → default tier mapping (AC-4)
# ---------------------------------------------------------------------------

ROLE_DEFAULT_TIERS: dict[str, str] = {
    # Deep by default; frontier stays reachable (ROLE_TIER_CEILINGS) through
    # explicit policy overrides and learned routing.
    "planning": TIER_DEEP,
    "spec-writer": TIER_BALANCED,
    "backend-executor": TIER_BALANCED,
    "ui-executor": TIER_BALANCED,
    "db-executor": TIER_BALANCED,
    "integration-executor": TIER_BALANCED,
    "refactor-executor": TIER_BALANCED,
    "ml-executor": TIER_BALANCED,
    "testing-executor": TIER_BALANCED,
    "docs-executor": TIER_FAST,
    "infra-executor": TIER_BALANCED,
    "security-executor": TIER_DEEP,
    "data-executor": TIER_BALANCED,
    "observability-executor": TIER_BALANCED,
    "release-executor": TIER_BALANCED,
    "spec-completion-auditor": TIER_BALANCED,
    "code-quality-auditor": TIER_BALANCED,
    "dead-code-auditor": TIER_FAST,
    "performance-auditor": TIER_FAST,
    "db-schema-auditor": TIER_FAST,
    "ui-auditor": TIER_FAST,
    "claude-md-auditor": TIER_BALANCED,
    "security-auditor": TIER_DEEP,
    "architecture-auditor": TIER_BALANCED,
    "threat-model-auditor": TIER_DEEP,
    "api-contract-auditor": TIER_BALANCED,
    "test-strategy-auditor": TIER_BALANCED,
    "accessibility-auditor": TIER_BALANCED,
    "privacy-auditor": TIER_BALANCED,
    "supply-chain-auditor": TIER_DEEP,
    "infrastructure-auditor": TIER_BALANCED,
    "observability-auditor": TIER_BALANCED,
    "release-auditor": TIER_BALANCED,
    "data-integrity-auditor": TIER_BALANCED,
    "docs-accuracy-auditor": TIER_BALANCED,
}

# ---------------------------------------------------------------------------
# Role → maximum tier ceiling
# ---------------------------------------------------------------------------

# The frontier tier is deliberately NOT reachable by executors. Execution is
# where token spend scales with diff size, and a frontier-tier executor buys
# little that a deep-tier one does not. Planning and review are where a single
# spawn's judgment quality determines the cost of the whole task, so those are
# the only roles permitted above TIER_DEEP.
#
# This is a ceiling, not a default: an auditor's *default* tier stays whatever
# ROLE_DEFAULT_TIERS says. The ceiling only governs how high explicit policy
# overrides, UCB winners, benchmark selection, and epsilon-greedy exploration
# may push a role.
DEFAULT_TIER_CEILING: str = TIER_DEEP

# Roles named here may reach a tier above DEFAULT_TIER_CEILING.
ROLE_TIER_CEILINGS: dict[str, str] = {
    "planning": TIER_FRONTIER,
}

# Any role whose name ends with this suffix inherits the frontier ceiling.
# Matching on the suffix rather than enumerating the registry means
# project-generated auditors (created by the calibration skill) are covered
# without a second place to update.
_FRONTIER_ROLE_SUFFIX: str = "-auditor"


def max_tier_for_role(role: str) -> str:
    """Return the highest tier *role* is permitted to run at."""
    explicit = ROLE_TIER_CEILINGS.get(role)
    if explicit is not None:
        return explicit
    if role.endswith(_FRONTIER_ROLE_SUFFIX):
        return TIER_FRONTIER
    return DEFAULT_TIER_CEILING


def clamp_model_to_role_ceiling(
    role: str, model: Optional[str], host: str = HOST_CLAUDE
) -> Optional[str]:
    """Return *model*, or the ceiling model for *role* when *model* is too high.

    Returns *model* unchanged when it is None, unmapped (an unknown literal
    this module cannot rank — callers validate those separately), or already
    at or below the role's ceiling. When the host has no model for the ceiling
    tier (e.g. codex), *model* is returned unchanged: there is nothing to
    clamp to, and inventing one would be worse than the passthrough.
    """
    rank = model_rank(model, host=host)
    if rank < 0:
        return model
    ceiling = max_tier_for_role(role)
    if rank <= tier_rank(ceiling):
        return model
    ceiling_model = resolve_model_for_tier(host, ceiling)
    if ceiling_model is None:
        return model
    return ceiling_model
