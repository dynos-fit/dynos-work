"""Tests for conditional plan sections (API Contracts + Data Model).

The sections are required only when the execution graph's ``files_expected``
actually touch an API surface (routes, controllers, handlers, endpoints) or a
persisted schema (migrations, models, schema). Domain membership alone no
longer forces either table: it produced ``N/A`` boilerplate in every
backend/ui/db plan without strengthening the gap analysis.

Validates:
  - conditional_plan_headings returns headings per planned file set
  - validate_task_artifacts enforces conditional headings when the graph requires them
  - Backwards compatibility: plans without conditional sections pass when nothing requires them
"""
from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_validate import (
    REQUIRED_PLAN_HEADINGS,
    conditional_plan_headings,
    validate_task_artifacts,
)


# ---------------------------------------------------------------------------
# Helper: create a minimal valid task directory
# ---------------------------------------------------------------------------

def _base_plan(extra_sections: str = "") -> str:
    """Return a plan.md with all required headings + optional extra sections."""
    return textwrap.dedent(f"""\
        # Implementation Plan

        ## Technical Approach
        Approach text.

        ## Reference Code
        No references needed.

        ## Components / Modules
        ### Component: Widget
        - **Purpose:** Does things
        - **Files:** src/widget.py

        ## Data Flow
        Data flows.

        ## Error Handling Strategy
        Errors handled.

        ## Test Strategy
        Tests written.

        ## Dependency Graph
        No deps.

        ## Open Questions
        None.
        {extra_sections}
    """)


def _api_contracts_section() -> str:
    return textwrap.dedent("""
        ## API Contracts
        | Endpoint | Method | Request | Response | Auth | Codes |
        |---|---|---|---|---|---|
        | /api/test | GET | — | `{ok: true}` | none | 200 |
    """)


def _data_model_section() -> str:
    return textwrap.dedent("""
        ## Data Model
        | Table | Column | Type | Nullable | Default | Index | Notes |
        |---|---|---|---|---|---|---|
        | tests | id | int | no | auto | pk | — |
    """)


def _make_task_dir(
    tmp_path: Path,
    domains: list[str],
    extra_plan: str = "",
    include_plan: bool = True,
    source_files: dict[str, str] | None = None,
    graph_files: list[str] | None = None,
) -> Path:
    """Create a minimal task directory with manifest, spec, plan, and graph."""
    task_dir = tmp_path / ".dynos" / "task-test"
    task_dir.mkdir(parents=True)
    if graph_files is not None:
        (task_dir / "execution-graph.json").write_text(json.dumps({
            "task_id": "task-test",
            "segments": [{
                "id": "seg-1", "executor": "backend-executor", "description": "d",
                "files_expected": list(graph_files), "depends_on": [],
                "parallelizable": True, "criteria_ids": [1],
            }],
        }))
    if source_files:
        for path, content in source_files.items():
            fpath = tmp_path / path
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text(content)

    manifest = {
        "stage": "PLANNING",
        "classification": {
            "type": "feature",
            "domains": domains,
            "risk_level": "medium",
            "notes": "",
        },
    }
    (task_dir / "manifest.json").write_text(json.dumps(manifest))

    spec = textwrap.dedent("""\
        # Normalized Spec

        ## Task Summary
        Test task.

        ## User Context
        Test user.

        ## Acceptance Criteria
        1. Something works.

        ## Implicit Requirements Surfaced
        None.

        ## Out of Scope
        Nothing.

        ## Assumptions
        None.

        ## Risk Notes
        None.
    """)
    (task_dir / "spec.md").write_text(spec)

    if include_plan:
        (task_dir / "plan.md").write_text(_base_plan(extra_plan))

    return task_dir


# ---------------------------------------------------------------------------
# conditional_plan_headings unit tests
# ---------------------------------------------------------------------------

class TestConditionalPlanHeadings:
    def test_route_file_requires_api_contracts(self):
        assert "API Contracts" in conditional_plan_headings(["backend"], files=["src/routes/users.js"])

    def test_controller_file_requires_api_contracts(self):
        assert "API Contracts" in conditional_plan_headings(["ui"], files=["app/controllers/orders.rb"])

    def test_api_basename_requires_api_contracts(self):
        assert "API Contracts" in conditional_plan_headings([], files=["server/api.py"])

    def test_migration_file_requires_data_model(self):
        assert "Data Model" in conditional_plan_headings(["db"], files=["migrations/001_add_users.sql"])

    def test_model_file_requires_data_model(self):
        assert "Data Model" in conditional_plan_headings([], files=["src/models/user.py"])

    def test_domains_alone_require_nothing(self):
        assert conditional_plan_headings(["backend", "ui", "security", "db"]) == []
        assert conditional_plan_headings(["backend"], files=["src/utils/format.py"]) == []

    def test_ml_requires_neither(self):
        assert conditional_plan_headings(["ml"], files=["ml/train.py"]) == []

    def test_routes_and_migrations_require_both(self):
        result = conditional_plan_headings(["backend", "db"], files=["src/routes.js", "db/migrations/2.sql"])
        assert "API Contracts" in result
        assert "Data Model" in result

    def test_empty_domains(self):
        assert conditional_plan_headings([]) == []

    def test_accepts_set_input(self):
        result = conditional_plan_headings({"backend", "db"}, files={"src/routes.js", "src/models.py"})
        assert "API Contracts" in result
        assert "Data Model" in result

    def test_architecture_decisions_only_for_critical(self):
        assert "Architecture Decisions" in conditional_plan_headings([], "critical")
        assert "Architecture Decisions" not in conditional_plan_headings([], "high")

    def test_does_not_duplicate_base_headings(self):
        result = conditional_plan_headings(["backend", "db"], "critical", files=["src/routes.js", "src/models.py"])
        for heading in result:
            assert heading not in REQUIRED_PLAN_HEADINGS


# ---------------------------------------------------------------------------
# validate_task_artifacts integration — conditional headings enforced
# ---------------------------------------------------------------------------

class TestValidateConditionalHeadings:
    def test_route_file_missing_api_contracts_fails(self, tmp_path: Path):
        task_dir = _make_task_dir(tmp_path, domains=["backend"], graph_files=["src/routes.js"])
        errors = validate_task_artifacts(task_dir)
        assert any("API Contracts" in e for e in errors)

    def test_backend_domain_without_api_files_needs_no_section(self, tmp_path: Path):
        task_dir = _make_task_dir(tmp_path, domains=["backend"], graph_files=["src/utils/format.py"])
        errors = validate_task_artifacts(task_dir)
        assert not any("API Contracts" in e for e in errors)

    def test_route_file_with_api_contracts_passes(self, tmp_path: Path):
        task_dir = _make_task_dir(
            tmp_path, domains=["backend"], extra_plan=_api_contracts_section(),
            source_files={"src/routes.js": "app.get('/api/test', handler);"},
            graph_files=["src/routes.js"],
        )
        errors = validate_task_artifacts(task_dir)
        assert not any("API Contracts" in e for e in errors)

    def test_migration_file_missing_data_model_fails(self, tmp_path: Path):
        task_dir = _make_task_dir(tmp_path, domains=["db"], graph_files=["migrations/001.sql"])
        errors = validate_task_artifacts(task_dir)
        assert any("Data Model" in e for e in errors)

    def test_db_domain_without_schema_files_needs_no_section(self, tmp_path: Path):
        task_dir = _make_task_dir(tmp_path, domains=["db"], graph_files=["scripts/report.py"])
        errors = validate_task_artifacts(task_dir)
        assert not any("Data Model" in e for e in errors)

    def test_migration_file_with_data_model_passes(self, tmp_path: Path):
        task_dir = _make_task_dir(
            tmp_path, domains=["db"], extra_plan=_data_model_section(),
            source_files={"migrations/001.sql": "CREATE TABLE tests (id INT PRIMARY KEY);"},
            graph_files=["migrations/001.sql"],
        )
        errors = validate_task_artifacts(task_dir)
        assert not any("Data Model" in e for e in errors)

    def test_routes_and_migrations_need_both(self, tmp_path: Path):
        task_dir = _make_task_dir(
            tmp_path, domains=["backend", "db"], graph_files=["src/routes.js", "migrations/001.sql"],
        )
        errors = validate_task_artifacts(task_dir)
        assert any("API Contracts" in e for e in errors)
        assert any("Data Model" in e for e in errors)

    def test_routes_and_migrations_with_both_passes(self, tmp_path: Path):
        task_dir = _make_task_dir(
            tmp_path,
            domains=["backend", "db"],
            extra_plan=_api_contracts_section() + _data_model_section(),
            source_files={
                "src/routes.js": "app.get('/api/test', handler);",
                "migrations/001.sql": "CREATE TABLE tests (id INT PRIMARY KEY);",
            },
            graph_files=["src/routes.js", "migrations/001.sql"],
        )
        errors = validate_task_artifacts(task_dir)
        assert not any("API Contracts" in e for e in errors)
        assert not any("Data Model" in e for e in errors)

    def test_no_graph_requires_no_conditional_sections(self, tmp_path: Path):
        task_dir = _make_task_dir(tmp_path, domains=["ui", "security"])
        errors = validate_task_artifacts(task_dir)
        assert not any("API Contracts" in e for e in errors)

    def test_component_subsection_without_exact_files_fails(self, tmp_path: Path):
        task_dir = _make_task_dir(tmp_path, domains=["backend"], extra_plan=_api_contracts_section())
        (task_dir / "plan.md").write_text(textwrap.dedent("""\
            # Implementation Plan

            ## Technical Approach
            Approach text.

            ## Reference Code
            No references needed.

            ## Components / Modules
            ### Component: Widget
            - **Purpose:** Does things

            ## API Contracts
            | Endpoint | Method | Request | Response | Auth | Codes |
            |---|---|---|---|---|---|
            | /api/test | GET | — | `{ok: true}` | none | 200 |

            ## Data Flow
            Data flows.

            ## Error Handling Strategy
            Errors handled.

            ## Test Strategy
            Tests written.

            ## Dependency Graph
            No deps.

            ## Open Questions
            None.
        """))
        errors = validate_task_artifacts(task_dir, run_gap=False)
        assert "plan component section missing exact files: ### Component: Widget" in errors

    def test_component_subsection_with_exact_files_passes(self, tmp_path: Path):
        task_dir = _make_task_dir(
            tmp_path,
            domains=["backend"],
            extra_plan=_api_contracts_section(),
            source_files={"src/widget.py": "def ok():\n    return True\n"},
        )
        errors = validate_task_artifacts(task_dir, run_gap=False)
        assert "plan component section missing exact files" not in "\n".join(errors)


# ---------------------------------------------------------------------------
# Backwards compatibility: domains that don't trigger conditional sections
# ---------------------------------------------------------------------------

class TestBackwardsCompat:
    def test_ml_only_no_extra_headings_needed(self, tmp_path: Path):
        task_dir = _make_task_dir(tmp_path, domains=["ml"])
        errors = validate_task_artifacts(task_dir)
        assert not any("API Contracts" in e for e in errors)
        assert not any("Data Model" in e for e in errors)

    def test_no_plan_file_no_conditional_errors(self, tmp_path: Path):
        task_dir = _make_task_dir(tmp_path, domains=["backend", "db"], include_plan=False)
        errors = validate_task_artifacts(task_dir)
        # Without a plan file (non-strict mode), no plan heading errors
        assert not any("API Contracts" in e for e in errors)
        assert not any("Data Model" in e for e in errors)

    def test_no_classification_no_conditional_errors(self, tmp_path: Path):
        task_dir = _make_task_dir(tmp_path, domains=["backend"])
        # Remove classification from manifest
        manifest = json.loads((task_dir / "manifest.json").read_text())
        del manifest["classification"]
        (task_dir / "manifest.json").write_text(json.dumps(manifest))
        errors = validate_task_artifacts(task_dir)
        # Without classification, no conditional headings enforced
        assert not any("API Contracts" in e for e in errors)

    def test_empty_domains_no_conditional_errors(self, tmp_path: Path):
        """Edge case: classification exists but domains is empty."""
        task_dir = _make_task_dir(tmp_path, domains=[])
        errors = validate_task_artifacts(task_dir)
        # Empty domains triggers a manifest validation error, but not conditional heading errors
        assert not any("API Contracts" in e for e in errors)
        assert not any("Data Model" in e for e in errors)


# ---------------------------------------------------------------------------
# Planner template validation
# ---------------------------------------------------------------------------

class TestPlannerTemplate:
    """Verify agents/planning.md includes the conditional sections."""

    @pytest.fixture
    def planner_text(self) -> str:
        path = Path(__file__).resolve().parent.parent / "agents" / "planning.md"
        return path.read_text()

    def test_api_contracts_section_in_template(self, planner_text: str):
        assert "## API Contracts" in planner_text

    def test_data_model_section_in_template(self, planner_text: str):
        assert "## Data Model" in planner_text

    def test_api_contracts_conditional_note(self, planner_text: str):
        assert "Required only when a planned file is an API surface" in planner_text
        assert "do not write an `N/A` table" in planner_text

    def test_data_model_conditional_note(self, planner_text: str):
        assert "Required only when a planned file is a migration, model, or schema module" in planner_text

    def test_api_contracts_before_data_flow(self, planner_text: str):
        api_pos = planner_text.index("## API Contracts")
        flow_pos = planner_text.index("## Data Flow")
        assert api_pos < flow_pos

    def test_data_model_before_data_flow(self, planner_text: str):
        model_pos = planner_text.index("## Data Model")
        flow_pos = planner_text.index("## Data Flow")
        assert model_pos < flow_pos
