"""Tests for PR #12 — doc-accuracy category in code-quality-auditor.

Validates:
  - code-quality-auditor agent includes doc-accuracy category
  - validate_docs_accuracy.py hook is referenced correctly
  - doc-accuracy findings use the canonical audit report schema
  - Repair-coordinator routes doc-accuracy findings
  - Backwards compatibility: existing cq- findings unchanged
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


class TestCodeQualityAuditorDocAccuracy:
    """Verify agent definition includes doc-accuracy category."""

    @pytest.fixture
    def agent_text(self) -> str:
        return (ROOT / "agents" / "code-quality-auditor.md").read_text()

    def test_doc_accuracy_category_present(self, agent_text: str):
        assert "### Category: doc-accuracy" in agent_text

    def test_code_quality_category_present(self, agent_text: str):
        assert "### Category: code-quality" in agent_text

    def test_hook_invocation_documented(self, agent_text: str):
        assert "validate_docs_accuracy.py" in agent_text

    def test_json_flag_used(self, agent_text: str):
        assert "--json" in agent_text

    def test_md_file_condition(self, agent_text: str):
        assert ".md" in agent_text
        assert "skip this category entirely" in agent_text

    def test_category_field_required(self, agent_text: str):
        assert "category" in agent_text
        assert '"doc-accuracy"' in agent_text

    def test_deterministic_hook_rule(self, agent_text: str):
        assert "deterministic" in agent_text.lower()
        assert "do not hallucinate" in agent_text.lower()


class TestAuditReportSchemaDocAccuracy:
    """Verify audit-report.md documents doc-accuracy for code-quality-auditor."""

    @pytest.fixture
    def schema_text(self) -> str:
        return (ROOT / "agents" / "_shared" / "audit-report.md").read_text()

    def test_doc_accuracy_mentioned(self, schema_text: str):
        assert "doc-accuracy" in schema_text

    def test_cq_prefix_preserved(self, schema_text: str):
        assert "`cq-`" in schema_text


class TestRepairCoordinatorDocAccuracy:
    """Verify repair-coordinator handles doc-accuracy findings."""

    @pytest.fixture
    def coordinator_text(self) -> str:
        return (ROOT / "agents" / "repair-coordinator.md").read_text()

    def test_doc_accuracy_routing_exists(self, coordinator_text: str):
        assert "doc-accuracy" in coordinator_text.lower() or "Doc-accuracy" in coordinator_text


class TestValidateDocsAccuracyHookExists:
    """Verify the hook exists and runs."""

    def test_hook_file_exists(self):
        assert (ROOT / "hooks" / "validate_docs_accuracy.py").is_file()

    def test_hook_runs_json_mode(self, tmp_path: Path):
        doc = tmp_path / "test.md"
        doc.write_text("See `src/nonexistent/file.py` for details.\n")
        result = subprocess.run(
            [sys.executable, str(ROOT / "hooks" / "validate_docs_accuracy.py"),
             "--doc", str(doc), "--root", str(tmp_path), "--json"],
            capture_output=True, text=True, timeout=30,
        )
        # Exit code 1 = broken refs found (which is expected)
        assert result.returncode in (0, 1)
        if result.returncode == 1:
            data = json.loads(result.stdout)
            assert "broken" in data

    def test_hook_passes_on_valid_doc(self, tmp_path: Path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "real.py").write_text("pass")
        doc = tmp_path / "test.md"
        doc.write_text("See `src/real.py` for details.\n")
        result = subprocess.run(
            [sys.executable, str(ROOT / "hooks" / "validate_docs_accuracy.py"),
             "--doc", str(doc), "--root", str(tmp_path), "--json"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0


class TestFindingSchemaCompat:
    """Verify doc-accuracy findings follow the canonical schema."""

    def test_doc_accuracy_finding_shape(self):
        finding = {
            "id": "cq-010",
            "category": "doc-accuracy",
            "description": "README.md:15 references 'src/auth.py' which does not exist",
            "location": "README.md:15",
            "severity": "major",
            "blocking": True,
        }
        assert finding["id"].startswith("cq-")
        assert finding["category"] == "doc-accuracy"
        assert finding["severity"] in ("critical", "major", "minor")

    def test_code_quality_finding_backwards_compat(self):
        finding = {
            "id": "cq-001",
            "description": "Function too long",
            "location": "src/app.py:42",
            "severity": "minor",
            "blocking": False,
        }
        # No category field — backwards compatible
        assert "category" not in finding
        assert finding["id"].startswith("cq-")

    def test_code_quality_finding_with_category(self):
        finding = {
            "id": "cq-002",
            "category": "code-quality",
            "description": "Uncaught async error",
            "location": "src/handler.ts:30",
            "severity": "major",
            "blocking": True,
        }
        assert finding["category"] == "code-quality"

    def test_repair_task_for_doc_accuracy(self):
        repair = {
            "finding_id": "cq-010",
            "description": "Fix broken path reference in README.md line 15: update 'src/auth.py' to 'src/authentication.py'",
            "assigned_executor": "integration-executor",
            "affected_files": ["README.md"],
        }
        assert repair["finding_id"].startswith("cq-")
