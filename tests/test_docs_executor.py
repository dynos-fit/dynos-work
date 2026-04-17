"""Tests for docs-executor agent.

Validates:
  - Agent exists with correct frontmatter
  - Registered as valid executor in lib_core
  - Planner knows about docs-executor
  - Execute skill lists docs-executor
  - Repair-coordinator routes doc findings to docs-executor
  - Agent references validate_docs_accuracy hook
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
ROOT = Path(__file__).resolve().parent.parent


class TestDocsExecutorAgent:
    @pytest.fixture
    def agent_text(self) -> str:
        return (ROOT / "agents" / "docs-executor.md").read_text()

    def test_exists(self):
        assert (ROOT / "agents" / "docs-executor.md").is_file()

    def test_has_write_tools(self, agent_text: str):
        frontmatter = agent_text.split("---")[1]
        assert "Write" in frontmatter
        assert "Edit" in frontmatter
        assert "Bash" in frontmatter

    def test_references_validate_docs_accuracy(self, agent_text: str):
        assert "validate_docs_accuracy" in agent_text

    def test_has_evidence_format(self, agent_text: str):
        assert "evidence" in agent_text.lower()
        assert "segment-id" in agent_text

    def test_hard_rules_verify_before_done(self, agent_text: str):
        assert "Never document features that don't exist" in agent_text

    def test_covers_readme(self, agent_text: str):
        assert "README" in agent_text

    def test_covers_api_docs(self, agent_text: str):
        assert "API" in agent_text

    def test_covers_setup_guide(self, agent_text: str):
        assert "setup" in agent_text.lower() or "Setup" in agent_text

    def test_covers_architecture_docs(self, agent_text: str):
        assert "Architecture" in agent_text or "architecture" in agent_text


class TestDocsExecutorRegistered:
    def test_in_valid_executors(self):
        from lib_core import VALID_EXECUTORS
        assert "docs-executor" in VALID_EXECUTORS

    def test_planner_lists_docs_executor(self):
        text = (ROOT / "agents" / "planning.md").read_text()
        assert "docs-executor" in text

    def test_execute_skill_lists_docs_executor(self):
        text = (ROOT / "skills" / "execute" / "SKILL.md").read_text()
        assert "docs-executor" in text


class TestRepairCoordinatorRouting:
    def test_doc_findings_route_to_docs_executor(self):
        text = (ROOT / "agents" / "repair-coordinator.md").read_text()
        assert "docs-executor" in text
        # Verify doc-accuracy findings go to docs-executor specifically
        lines = text.splitlines()
        for line in lines:
            if "doc-accuracy" in line.lower():
                assert "docs-executor" in line
                break
        else:
            pytest.fail("No doc-accuracy routing line found")
