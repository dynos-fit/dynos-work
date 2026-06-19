from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "hooks"))
sys.path.insert(0, str(ROOT / "memory"))


NEW_EXECUTORS = {
    "infra-executor",
    "security-executor",
    "data-executor",
    "observability-executor",
    "release-executor",
}

NEW_AUDITORS = {
    "architecture-auditor",
    "threat-model-auditor",
    "api-contract-auditor",
    "test-strategy-auditor",
    "accessibility-auditor",
    "privacy-auditor",
    "supply-chain-auditor",
    "infrastructure-auditor",
    "observability-auditor",
    "release-auditor",
    "data-integrity-auditor",
    "docs-accuracy-auditor",
}


def test_new_agent_files_and_templates_exist() -> None:
    for role in NEW_EXECUTORS | NEW_AUDITORS:
        assert (ROOT / "agents" / f"{role}.md").is_file()
        assert (ROOT / "cli" / "assets" / "templates" / "base" / "agents" / f"{role}.md").is_file()


def test_new_executors_are_runtime_discovered_and_stampable() -> None:
    from lib_core import VALID_EXECUTORS
    import ctl
    import pre_tool_use

    assert NEW_EXECUTORS <= VALID_EXECUTORS
    assert NEW_EXECUTORS <= ctl._STAMP_ROLE_ALLOWLIST
    assert NEW_EXECUTORS <= pre_tool_use._EXECUTOR_ROLE_ALLOWLIST


def test_new_auditors_are_stampable_and_model_routed() -> None:
    import ctl
    import pre_tool_use
    import lib_models

    audit_roles = {f"audit-{name.removesuffix('-auditor')}" for name in NEW_AUDITORS}
    assert audit_roles <= ctl._STAMP_ROLE_ALLOWLIST
    assert audit_roles <= pre_tool_use._EXECUTOR_ROLE_ALLOWLIST
    assert NEW_AUDITORS <= set(lib_models.ROLE_DEFAULT_TIERS)


def test_router_default_registry_uses_expanded_auditors() -> None:
    from router import _DEFAULT_AUDITOR_REGISTRY

    registry = _DEFAULT_AUDITOR_REGISTRY
    always = set(registry["always"])
    domains = registry["domain_conditional"]

    assert {"architecture-auditor", "test-strategy-auditor", "docs-accuracy-auditor"} <= always
    assert "accessibility-auditor" in domains["ui"]
    assert "api-contract-auditor" in domains["backend"]
    assert "observability-auditor" in domains["backend"]
    assert "data-integrity-auditor" in domains["db"]
    assert "supply-chain-auditor" in domains["infra"]
    assert "threat-model-auditor" in domains["security"]
    assert "release-auditor" in domains["migration"]


def test_execution_skill_lists_expanded_executors() -> None:
    text = (ROOT / "skills" / "execution" / "SKILL.md").read_text()
    execute_text = (ROOT / "skills" / "execute" / "SKILL.md").read_text()
    for role in NEW_EXECUTORS:
        assert role in text
        assert role in execute_text

