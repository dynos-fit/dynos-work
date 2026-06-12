"""lib_models — leaf module: tier/model/host constants and helpers.

Zero imports from any hooks/ or memory/ module.
"""
from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# Tier constants
# ---------------------------------------------------------------------------

TIER_FAST: str = "fast"
TIER_BALANCED: str = "balanced"
TIER_DEEP: str = "deep"

ALL_TIERS: list[str] = [TIER_FAST, TIER_BALANCED, TIER_DEEP]

# ---------------------------------------------------------------------------
# Host constants
# ---------------------------------------------------------------------------

HOST_CLAUDE: str = "claude"
HOST_CODEX: str = "codex"

ALL_HOSTS: frozenset[str] = frozenset({HOST_CLAUDE, HOST_CODEX})

# ---------------------------------------------------------------------------
# Tier → model mapping per host
# ---------------------------------------------------------------------------

TIER_TO_MODEL: dict[str, dict[str, Optional[str]]] = {
    HOST_CLAUDE: {
        TIER_FAST: "haiku",
        TIER_BALANCED: "sonnet",
        TIER_DEEP: "opus",
    },
    HOST_CODEX: {
        TIER_FAST: None,
        TIER_BALANCED: None,
        TIER_DEEP: None,
    },
}

# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def resolve_model_for_tier(host: str, tier: str) -> Optional[str]:
    """Return the model literal for *host* at *tier*, or None if unmapped."""
    host_map = TIER_TO_MODEL.get(host)
    if host_map is None:
        return None
    return host_map.get(tier)


def model_to_tier(model: str) -> Optional[str]:
    """Return the tier for *model*, or None if unknown."""
    for host_map in TIER_TO_MODEL.values():
        for tier, m in host_map.items():
            if m is not None and m == model:
                return tier
    return None


def valid_models_for_host(host: str) -> frozenset[str]:
    """Return the frozenset of non-None model literals for *host*."""
    host_map = TIER_TO_MODEL.get(host)
    if host_map is None:
        return frozenset()
    return frozenset(m for m in host_map.values() if m is not None)


# ---------------------------------------------------------------------------
# Role → default tier mapping (AC-4)
# ---------------------------------------------------------------------------

ROLE_DEFAULT_TIERS: dict[str, str] = {
    "planning": TIER_BALANCED,
    "spec-writer": TIER_BALANCED,
    "backend-executor": TIER_BALANCED,
    "ui-executor": TIER_BALANCED,
    "db-executor": TIER_BALANCED,
    "integration-executor": TIER_BALANCED,
    "refactor-executor": TIER_BALANCED,
    "ml-executor": TIER_BALANCED,
    "testing-executor": TIER_BALANCED,
    "docs-executor": TIER_FAST,
    "spec-completion-auditor": TIER_BALANCED,
    "code-quality-auditor": TIER_BALANCED,
    "dead-code-auditor": TIER_FAST,
    "performance-auditor": TIER_FAST,
    "db-schema-auditor": TIER_FAST,
    "ui-auditor": TIER_FAST,
    "claude-md-auditor": TIER_BALANCED,
    "security-auditor": TIER_DEEP,
}
