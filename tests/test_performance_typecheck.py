"""Tests for performance auditor, typecheck/lint hooks, and ADR plan section.

Covers:
  - Performance check hook detects patterns (N+1, missing timeout, unbounded query, quadratic, connection leak)
  - Typecheck/lint hook auto-detects ecosystems
  - Performance auditor agent exists with correct frontmatter
  - ADR section conditional on risk_level
  - Router includes performance-auditor for backend/db domains
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Performance check hook
# ---------------------------------------------------------------------------

class TestPerformanceCheck:
    def test_detects_n_plus_one(self, tmp_path: Path):
        from performance_check import scan_files
        (tmp_path / "app.py").write_text(
            "for user in users:\n    db.query(f'SELECT * FROM orders WHERE user_id={user.id}')\n"
        )
        result = scan_files(tmp_path)
        patterns = [f["pattern"] for f in result["findings"]]
        assert "n_plus_one" in patterns

    def test_detects_missing_timeout(self, tmp_path: Path):
        from performance_check import scan_files
        (tmp_path / "client.py").write_text("response = requests.get('https://api.example.com/data')\n")
        result = scan_files(tmp_path)
        patterns = [f["pattern"] for f in result["findings"]]
        assert "missing_timeout" in patterns

    def test_no_false_positive_with_timeout(self, tmp_path: Path):
        from performance_check import scan_files
        (tmp_path / "client.py").write_text("response = requests.get('https://api.example.com/data', timeout=30)\n")
        result = scan_files(tmp_path)
        timeout_findings = [f for f in result["findings"] if f["pattern"] == "missing_timeout"]
        assert len(timeout_findings) == 0

    def test_detects_unbounded_query(self, tmp_path: Path):
        from performance_check import scan_files
        (tmp_path / "repo.py").write_text("rows = db.execute('SELECT * FROM users')\n")
        result = scan_files(tmp_path)
        patterns = [f["pattern"] for f in result["findings"]]
        assert "unbounded_query" in patterns

    def test_detects_quadratic_loop(self, tmp_path: Path):
        from performance_check import scan_files
        (tmp_path / "algo.py").write_text("for i in items:\n    for j in items:\n        compare(i, j)\n")
        result = scan_files(tmp_path)
        patterns = [f["pattern"] for f in result["findings"]]
        assert "quadratic_loop" in patterns

    def test_detects_connection_leak(self, tmp_path: Path):
        from performance_check import scan_files
        (tmp_path / "db.py").write_text("conn = psycopg2.connect(db_url)\ncursor = conn.cursor()\ncursor.execute(query)\n")
        result = scan_files(tmp_path)
        patterns = [f["pattern"] for f in result["findings"]]
        assert "missing_connection_cleanup" in patterns

    def test_clean_code_no_findings(self, tmp_path: Path):
        from performance_check import scan_files
        (tmp_path / "clean.py").write_text("def add(a, b):\n    return a + b\n")
        result = scan_files(tmp_path)
        assert len(result["findings"]) == 0

    def test_cli_json_output(self, tmp_path: Path):
        (tmp_path / "app.py").write_text("x = 1\n")
        result = subprocess.run(
            [sys.executable, str(ROOT / "hooks" / "performance_check.py"),
             "--root", str(tmp_path)],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "findings" in data
        assert "files_scanned" in data

    def test_changed_files_filter(self, tmp_path: Path):
        from performance_check import scan_files
        (tmp_path / "a.py").write_text("for x in items:\n    db.query(x)\n")
        (tmp_path / "b.py").write_text("clean = True\n")
        result = scan_files(tmp_path, changed_files=["b.py"])
        assert len(result["findings"]) == 0


# ---------------------------------------------------------------------------
# Typecheck/lint hook
# ---------------------------------------------------------------------------

class TestTypecheckLint:
    def test_detects_no_ecosystems_in_empty_dir(self, tmp_path: Path):
        from typecheck_lint import run_checks
        result = run_checks(tmp_path)
        assert result["summary"]["ecosystems_detected"] == 0

    def test_cli_json_output(self, tmp_path: Path):
        result = subprocess.run(
            [sys.executable, str(ROOT / "hooks" / "typecheck_lint.py"),
             "--root", str(tmp_path)],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "typechecks" in data
        assert "linters" in data
        assert "summary" in data

    def test_detects_python_ecosystem(self, tmp_path: Path):
        from typecheck_lint import run_checks
        (tmp_path / "requirements.txt").write_text("flask\n")
        result = run_checks(tmp_path)
        tools = [c["tool"] for c in result["typechecks"] + result["linters"]]
        assert any("mypy" in t or "ruff" in t or "flake8" in t for t in tools)


# ---------------------------------------------------------------------------
# Performance auditor agent
# ---------------------------------------------------------------------------

class TestPerformanceAuditorAgent:
    @pytest.fixture
    def agent_text(self) -> str:
        return (ROOT / "agents" / "performance-auditor.md").read_text()

    def test_exists(self):
        assert (ROOT / "agents" / "performance-auditor.md").is_file()

    def test_has_tools_frontmatter(self, agent_text: str):
        assert "tools:" in agent_text
        assert "Read" in agent_text
        assert "Write" not in agent_text.split("---")[1]  # read-only

    def test_has_performance_hook_invocation(self, agent_text: str):
        assert "performance_check.py" in agent_text

    def test_uses_perf_prefix(self, agent_text: str):
        assert "perf-" in agent_text.lower() or '"performance"' in agent_text

    def test_severity_definitions(self, agent_text: str):
        assert "critical" in agent_text
        assert "major" in agent_text
        assert "minor" in agent_text


# ---------------------------------------------------------------------------
# ADR section in planning — conditional on risk_level
# ---------------------------------------------------------------------------

class TestADRConditionalSection:
    def test_high_risk_requires_adrs(self):
        from lib_validate import conditional_plan_headings
        assert "Architecture Decisions" in conditional_plan_headings(["backend"], risk_level="high")

    def test_critical_risk_requires_adrs(self):
        from lib_validate import conditional_plan_headings
        assert "Architecture Decisions" in conditional_plan_headings(["backend"], risk_level="critical")

    def test_medium_risk_no_adrs(self):
        from lib_validate import conditional_plan_headings
        assert "Architecture Decisions" not in conditional_plan_headings(["backend"], risk_level="medium")

    def test_low_risk_no_adrs(self):
        from lib_validate import conditional_plan_headings
        assert "Architecture Decisions" not in conditional_plan_headings(["backend"], risk_level="low")

    def test_planner_template_has_adr_section(self):
        text = (ROOT / "agents" / "planning.md").read_text()
        assert "## Architecture Decisions" in text

    def test_planner_template_adr_conditional_note(self):
        text = (ROOT / "agents" / "planning.md").read_text()
        assert "risk_level is high or critical" in text


# ---------------------------------------------------------------------------
# Router includes performance-auditor
# ---------------------------------------------------------------------------

class TestRouterPerformanceAuditor:
    def test_backend_domain_includes_perf_auditor(self):
        from unittest import mock
        with mock.patch("router.is_learning_enabled", return_value=True), \
             mock.patch("router.resolve_skip", return_value={"skip": False, "reason": "no skip", "streak": 0, "threshold": 0}), \
             mock.patch("router.resolve_model", return_value={"model": None, "source": "default"}), \
             mock.patch("router.resolve_route", return_value={"mode": "generic", "agent_path": None, "agent_name": None, "composite_score": 0.0, "source": "no learned agent"}), \
             mock.patch("router.log_event"):
            from router import build_audit_plan
            plan = build_audit_plan(Path("."), "feature", ["backend"])
            names = [a["name"] for a in plan["auditors"]]
            assert "performance-auditor" in names

    def test_db_domain_includes_perf_auditor(self):
        from unittest import mock
        with mock.patch("router.is_learning_enabled", return_value=True), \
             mock.patch("router.resolve_skip", return_value={"skip": False, "reason": "no skip", "streak": 0, "threshold": 0}), \
             mock.patch("router.resolve_model", return_value={"model": None, "source": "default"}), \
             mock.patch("router.resolve_route", return_value={"mode": "generic", "agent_path": None, "agent_name": None, "composite_score": 0.0, "source": "no learned agent"}), \
             mock.patch("router.log_event"):
            from router import build_audit_plan
            plan = build_audit_plan(Path("."), "feature", ["db"])
            names = [a["name"] for a in plan["auditors"]]
            assert "performance-auditor" in names

    def test_ui_only_no_perf_auditor(self):
        from unittest import mock
        with mock.patch("router.is_learning_enabled", return_value=True), \
             mock.patch("router.resolve_skip", return_value={"skip": False, "reason": "no skip", "streak": 0, "threshold": 0}), \
             mock.patch("router.resolve_model", return_value={"model": None, "source": "default"}), \
             mock.patch("router.resolve_route", return_value={"mode": "generic", "agent_path": None, "agent_name": None, "composite_score": 0.0, "source": "no learned agent"}), \
             mock.patch("router.log_event"):
            from router import build_audit_plan
            plan = build_audit_plan(Path("."), "feature", ["ui"])
            names = [a["name"] for a in plan["auditors"]]
            assert "performance-auditor" not in names


# ---------------------------------------------------------------------------
# Audit report schema
# ---------------------------------------------------------------------------

class TestAuditReportSchema:
    def test_perf_prefix_documented(self):
        text = (ROOT / "agents" / "_shared" / "audit-report.md").read_text()
        assert "perf-" in text

    def test_performance_auditor_documented(self):
        text = (ROOT / "agents" / "_shared" / "audit-report.md").read_text()
        assert "performance-auditor" in text

    def test_typecheck_lint_documented(self):
        text = (ROOT / "agents" / "_shared" / "audit-report.md").read_text()
        assert "typecheck-lint" in text
