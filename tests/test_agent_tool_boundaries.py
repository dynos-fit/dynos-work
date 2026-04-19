"""Tests for PR #13 — per-agent tool boundaries via frontmatter.

Validates:
  - Every agent .md file has a tools: field in frontmatter
  - Executors have write tools (Write, Edit)
  - Auditors do NOT have write tools
  - Read-only agents (planner, coordinator, encoder) don't have write tools
  - All agents have at minimum Read and Grep
  - Tools field parses as a valid YAML list
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

AGENTS_DIR = Path(__file__).resolve().parent.parent / "agents"

# All agent files (excluding _shared)
AGENT_FILES = sorted(
    f for f in AGENTS_DIR.glob("*.md")
    if f.is_file() and not f.name.startswith("_")
)

# Classification of agents
EXECUTORS = {
    "backend-executor",
    "db-executor",
    "docs-executor",
    "integration-executor",
    "ml-executor",
    "refactor-executor",
    "testing-executor",
    "ui-executor",
}

READ_ONLY_AUDITORS = {
    "code-quality-auditor",
    "db-schema-auditor",
    "dead-code-auditor",
    "performance-auditor",
    "security-auditor",
    "spec-completion-auditor",
    "ui-auditor",
}

READ_ONLY_OTHER = {
    "repair-coordinator",
    "investigator",
}

# Planner is a writer: it produces spec.md, plan.md, execution-graph.json
# under .dynos/task-{id}/ and validates them via Bash-invoked hooks scripts.
WRITERS_OTHER = {
    "planning",
}

WRITE_TOOLS = {"Write", "Edit"}
READ_TOOLS = {"Read", "Grep"}


def _parse_frontmatter(path: Path) -> dict:
    """Extract YAML frontmatter from a markdown file (no PyYAML dependency)."""
    text = path.read_text()
    match = re.match(r"^---\n(.*?\n)---", text, re.DOTALL)
    assert match, f"No frontmatter in {path.name}"
    fm: dict = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if val.startswith("[") and val.endswith("]"):
            # Parse YAML-style list: [Read, Write, Edit]
            items = [item.strip().strip('"').strip("'") for item in val[1:-1].split(",")]
            fm[key] = [i for i in items if i]
        elif val.startswith('"') and val.endswith('"'):
            fm[key] = val[1:-1]
        else:
            fm[key] = val
    return fm


# ---------------------------------------------------------------------------
# Every agent has a tools: field
# ---------------------------------------------------------------------------

class TestAllAgentsHaveTools:
    @pytest.mark.parametrize("agent_file", AGENT_FILES, ids=lambda f: f.stem)
    def test_tools_field_exists(self, agent_file: Path):
        fm = _parse_frontmatter(agent_file)
        assert "tools" in fm, f"{agent_file.name} missing tools: field"

    @pytest.mark.parametrize("agent_file", AGENT_FILES, ids=lambda f: f.stem)
    def test_tools_is_list(self, agent_file: Path):
        fm = _parse_frontmatter(agent_file)
        assert isinstance(fm["tools"], list), f"{agent_file.name} tools: must be a list"

    @pytest.mark.parametrize("agent_file", AGENT_FILES, ids=lambda f: f.stem)
    def test_tools_not_empty(self, agent_file: Path):
        fm = _parse_frontmatter(agent_file)
        assert len(fm["tools"]) > 0, f"{agent_file.name} tools: must not be empty"

    def test_all_17_agents_found(self):
        assert len(AGENT_FILES) == 18, f"Expected 19 agents, found {len(AGENT_FILES)}"


# ---------------------------------------------------------------------------
# Minimum tools: every agent needs Read + Grep
# ---------------------------------------------------------------------------

class TestMinimumTools:
    @pytest.mark.parametrize("agent_file", AGENT_FILES, ids=lambda f: f.stem)
    def test_has_read(self, agent_file: Path):
        fm = _parse_frontmatter(agent_file)
        assert "Read" in fm["tools"], f"{agent_file.name} missing Read"

    @pytest.mark.parametrize("agent_file", AGENT_FILES, ids=lambda f: f.stem)
    def test_has_grep(self, agent_file: Path):
        fm = _parse_frontmatter(agent_file)
        assert "Grep" in fm["tools"], f"{agent_file.name} missing Grep"


# ---------------------------------------------------------------------------
# Executors MUST have write tools
# ---------------------------------------------------------------------------

class TestExecutorsHaveWriteTools:
    @pytest.fixture(params=sorted(EXECUTORS))
    def executor_file(self, request) -> Path:
        return AGENTS_DIR / f"{request.param}.md"

    def test_has_write(self, executor_file: Path):
        fm = _parse_frontmatter(executor_file)
        assert "Write" in fm["tools"], f"{executor_file.name} executor missing Write"

    def test_has_edit(self, executor_file: Path):
        fm = _parse_frontmatter(executor_file)
        assert "Edit" in fm["tools"], f"{executor_file.name} executor missing Edit"

    def test_has_bash(self, executor_file: Path):
        fm = _parse_frontmatter(executor_file)
        assert "Bash" in fm["tools"], f"{executor_file.name} executor missing Bash"

    def test_has_glob(self, executor_file: Path):
        fm = _parse_frontmatter(executor_file)
        assert "Glob" in fm["tools"], f"{executor_file.name} executor missing Glob"


# ---------------------------------------------------------------------------
# Auditors must NOT have write tools
# ---------------------------------------------------------------------------

class TestAuditorsNoWriteTools:
    @pytest.fixture(params=sorted(READ_ONLY_AUDITORS))
    def auditor_file(self, request) -> Path:
        return AGENTS_DIR / f"{request.param}.md"

    def test_no_write(self, auditor_file: Path):
        fm = _parse_frontmatter(auditor_file)
        assert "Write" not in fm["tools"], f"{auditor_file.name} auditor must not have Write"

    def test_no_edit(self, auditor_file: Path):
        fm = _parse_frontmatter(auditor_file)
        assert "Edit" not in fm["tools"], f"{auditor_file.name} auditor must not have Edit"


# ---------------------------------------------------------------------------
# Other read-only agents must NOT have write tools
# ---------------------------------------------------------------------------

class TestReadOnlyOtherNoWriteTools:
    @pytest.fixture(params=sorted(READ_ONLY_OTHER))
    def agent_file(self, request) -> Path:
        return AGENTS_DIR / f"{request.param}.md"

    def test_no_write(self, agent_file: Path):
        fm = _parse_frontmatter(agent_file)
        assert "Write" not in fm["tools"], f"{agent_file.name} must not have Write"

    def test_no_edit(self, agent_file: Path):
        fm = _parse_frontmatter(agent_file)
        assert "Edit" not in fm["tools"], f"{agent_file.name} must not have Edit"


# ---------------------------------------------------------------------------
# Specific tool assignments
# ---------------------------------------------------------------------------

class TestSpecificAssignments:
    def test_planner_has_write_edit_bash(self):
        """Planner writes spec.md/plan.md/execution-graph.json and runs Bash validators."""
        fm = _parse_frontmatter(AGENTS_DIR / "planning.md")
        assert "Write" in fm["tools"], "planning must have Write to produce spec.md/plan.md"
        assert "Edit" in fm["tools"], "planning must have Edit to iterate on its artifacts"
        assert "Bash" in fm["tools"], "planning must have Bash for validate_task_artifacts etc."

    def test_spec_completion_auditor_no_bash(self):
        """Spec-completion auditor only reads specs and code."""
        fm = _parse_frontmatter(AGENTS_DIR / "spec-completion-auditor.md")
        assert "Bash" not in fm["tools"]

    def test_repair_coordinator_no_bash(self):
        """Repair coordinator reads reports and plans, doesn't execute."""
        fm = _parse_frontmatter(AGENTS_DIR / "repair-coordinator.md")
        assert "Bash" not in fm["tools"]

    # state-encoder moved to sandbox/ — no longer in agents/

    def test_investigator_has_bash(self):
        """Investigator needs Bash for diagnostic commands."""
        fm = _parse_frontmatter(AGENTS_DIR / "investigator.md")
        assert "Bash" in fm["tools"]

    def test_security_auditor_has_bash(self):
        """Security auditor needs Bash for compliance hook."""
        fm = _parse_frontmatter(AGENTS_DIR / "security-auditor.md")
        assert "Bash" in fm["tools"]

    def test_code_quality_auditor_has_bash(self):
        """Code-quality auditor needs Bash for doc-accuracy hook."""
        fm = _parse_frontmatter(AGENTS_DIR / "code-quality-auditor.md")
        assert "Bash" in fm["tools"]


# ---------------------------------------------------------------------------
# Coverage: every agent is classified
# ---------------------------------------------------------------------------

class TestClassificationCoverage:
    def test_all_agents_classified(self):
        """Every agent file is in exactly one category."""
        all_names = {f.stem for f in AGENT_FILES}
        classified = EXECUTORS | READ_ONLY_AUDITORS | READ_ONLY_OTHER | WRITERS_OTHER
        unclassified = all_names - classified
        assert not unclassified, f"Unclassified agents: {unclassified}"

    def test_no_duplicate_classification(self):
        overlap = (EXECUTORS & READ_ONLY_AUDITORS) | (EXECUTORS & READ_ONLY_OTHER) | (READ_ONLY_AUDITORS & READ_ONLY_OTHER)
        assert not overlap, f"Agents in multiple categories: {overlap}"
