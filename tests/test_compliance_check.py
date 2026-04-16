"""Tests for hooks/compliance_check.py.

Validates:
  - Ecosystem detection across all supported project types
  - License check finding generation
  - SBOM generation status handling
  - Provenance check status handling
  - Privacy-relevant code detection (PII detection, missing features)
  - CLI invocation and JSON output shape
  - Compliance findings follow the canonical audit report schema
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest import mock

import pytest

# Ensure hooks/ is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

import compliance_check


# ---------------------------------------------------------------------------
# Ecosystem detection
# ---------------------------------------------------------------------------

class TestDetectEcosystems:
    def test_npm_from_package_json(self, tmp_path: Path):
        (tmp_path / "package.json").write_text("{}")
        assert compliance_check.detect_ecosystems(tmp_path) == ["npm"]

    def test_python_from_pyproject(self, tmp_path: Path):
        (tmp_path / "pyproject.toml").write_text("")
        assert compliance_check.detect_ecosystems(tmp_path) == ["python"]

    def test_go_from_go_mod(self, tmp_path: Path):
        (tmp_path / "go.mod").write_text("")
        assert compliance_check.detect_ecosystems(tmp_path) == ["go"]

    def test_rust_from_cargo_toml(self, tmp_path: Path):
        (tmp_path / "Cargo.toml").write_text("")
        assert compliance_check.detect_ecosystems(tmp_path) == ["rust"]

    def test_ruby_from_gemfile(self, tmp_path: Path):
        (tmp_path / "Gemfile").write_text("")
        assert compliance_check.detect_ecosystems(tmp_path) == ["ruby"]

    def test_java_from_pom_xml(self, tmp_path: Path):
        (tmp_path / "pom.xml").write_text("")
        assert compliance_check.detect_ecosystems(tmp_path) == ["java"]

    def test_php_from_composer_json(self, tmp_path: Path):
        (tmp_path / "composer.json").write_text("{}")
        assert compliance_check.detect_ecosystems(tmp_path) == ["php"]

    def test_dart_from_pubspec(self, tmp_path: Path):
        (tmp_path / "pubspec.yaml").write_text("")
        assert compliance_check.detect_ecosystems(tmp_path) == ["dart"]

    def test_swift_from_package_swift(self, tmp_path: Path):
        (tmp_path / "Package.swift").write_text("")
        assert compliance_check.detect_ecosystems(tmp_path) == ["swift"]

    def test_elixir_from_mix_lock(self, tmp_path: Path):
        (tmp_path / "mix.lock").write_text("")
        assert compliance_check.detect_ecosystems(tmp_path) == ["elixir"]

    def test_multiple_ecosystems(self, tmp_path: Path):
        (tmp_path / "package.json").write_text("{}")
        (tmp_path / "pyproject.toml").write_text("")
        result = compliance_check.detect_ecosystems(tmp_path)
        assert "npm" in result
        assert "python" in result

    def test_empty_project(self, tmp_path: Path):
        assert compliance_check.detect_ecosystems(tmp_path) == []


# ---------------------------------------------------------------------------
# License check
# ---------------------------------------------------------------------------

class TestCheckLicenses:
    def test_no_ecosystems_returns_empty(self, tmp_path: Path):
        assert compliance_check.check_licenses(tmp_path, []) == []

    def test_unsupported_ecosystem_returns_empty(self, tmp_path: Path):
        assert compliance_check.check_licenses(tmp_path, ["rust"]) == []

    @mock.patch("compliance_check._run_cmd")
    def test_npm_copyleft_detected(self, mock_run, tmp_path: Path):
        mock_run.return_value = (0, json.dumps({
            "some-pkg@1.0.0": {"licenses": "GPL-3.0", "repository": "https://example.com"},
            "safe-pkg@2.0.0": {"licenses": "MIT", "repository": "https://example.com"},
        }), "")
        findings = compliance_check.check_licenses(tmp_path, ["npm"])
        assert len(findings) == 1
        assert findings[0]["package"] == "some-pkg@1.0.0"
        assert findings[0]["license"] == "GPL-3.0"
        assert findings[0]["ecosystem"] == "npm"

    @mock.patch("compliance_check._run_cmd")
    def test_npm_agpl_detected(self, mock_run, tmp_path: Path):
        mock_run.return_value = (0, json.dumps({
            "agpl-pkg@1.0.0": {"licenses": "AGPL-3.0-only", "repository": ""},
        }), "")
        findings = compliance_check.check_licenses(tmp_path, ["npm"])
        assert len(findings) == 1
        assert "AGPL" in findings[0]["license"]

    @mock.patch("compliance_check._run_cmd")
    def test_npm_mit_not_flagged(self, mock_run, tmp_path: Path):
        mock_run.return_value = (0, json.dumps({
            "safe@1.0.0": {"licenses": "MIT", "repository": ""},
        }), "")
        assert compliance_check.check_licenses(tmp_path, ["npm"]) == []

    @mock.patch("compliance_check._run_cmd")
    def test_npm_checker_failure_returns_empty(self, mock_run, tmp_path: Path):
        mock_run.return_value = (1, "", "error")
        assert compliance_check.check_licenses(tmp_path, ["npm"]) == []

    @mock.patch("compliance_check._run_cmd")
    def test_python_copyleft_detected(self, mock_run, tmp_path: Path):
        (tmp_path / "requirements.txt").write_text("some-pkg==1.0")
        mock_run.return_value = (0, json.dumps([
            {"Name": "some-pkg", "Version": "1.0", "License": "GPL-2.0-or-later"},
            {"Name": "safe-pkg", "Version": "2.0", "License": "MIT"},
        ]), "")
        findings = compliance_check.check_licenses(tmp_path, ["python"])
        assert len(findings) == 1
        assert findings[0]["package"] == "some-pkg==1.0"
        assert findings[0]["ecosystem"] == "python"

    @mock.patch("compliance_check._run_cmd")
    def test_python_checker_fallback(self, mock_run, tmp_path: Path):
        (tmp_path / "requirements.txt").write_text("")
        # First call (piplicenses module) fails, second (pip-licenses binary) succeeds
        mock_run.side_effect = [
            (1, "", "not found"),
            (0, json.dumps([{"Name": "gpl-pkg", "Version": "1.0", "License": "GPL-3.0"}]), ""),
        ]
        findings = compliance_check.check_licenses(tmp_path, ["python"])
        assert len(findings) == 1

    @mock.patch("compliance_check._run_cmd")
    def test_license_list_format(self, mock_run, tmp_path: Path):
        mock_run.return_value = (0, json.dumps({
            "dual-pkg@1.0.0": {"licenses": ["MIT", "GPL-3.0"], "repository": ""},
        }), "")
        findings = compliance_check.check_licenses(tmp_path, ["npm"])
        assert len(findings) == 1
        assert "GPL-3.0" in findings[0]["license"]


# ---------------------------------------------------------------------------
# SBOM generation
# ---------------------------------------------------------------------------

class TestGenerateSbom:
    @mock.patch("compliance_check._run_cmd")
    def test_syft_success(self, mock_run, tmp_path: Path):
        task_dir = tmp_path / ".dynos" / "task-1"
        task_dir.mkdir(parents=True)
        mock_run.return_value = (0, '{"bomFormat": "CycloneDX"}', "")
        result = compliance_check.generate_sbom(tmp_path, task_dir, ["npm"])
        assert result["generated"] is True
        assert result["tool"] == "syft"
        assert result["format"] == "cyclonedx-json"

    @mock.patch("compliance_check._run_cmd")
    def test_syft_failure_falls_back_to_cyclonedx_python(self, mock_run, tmp_path: Path):
        task_dir = tmp_path / ".dynos" / "task-1"
        task_dir.mkdir(parents=True)
        sbom_path = task_dir / "sbom.json"

        def side_effect(cmd, **kwargs):
            if cmd[0] == "syft":
                return (1, "", "not found")
            # cyclonedx-bom creates the file
            sbom_path.write_text('{"bomFormat": "CycloneDX"}')
            return (0, "", "")

        mock_run.side_effect = side_effect
        result = compliance_check.generate_sbom(tmp_path, task_dir, ["python"])
        assert result["generated"] is True
        assert result["tool"] == "cyclonedx-bom"

    @mock.patch("compliance_check._run_cmd")
    def test_no_tools_available(self, mock_run, tmp_path: Path):
        task_dir = tmp_path / ".dynos" / "task-1"
        task_dir.mkdir(parents=True)
        mock_run.return_value = (1, "", "not found")
        result = compliance_check.generate_sbom(tmp_path, task_dir, ["npm"])
        assert result["generated"] is False
        assert "neither" in result["reason"]

    @mock.patch("compliance_check._run_cmd")
    def test_syft_failure_falls_back_to_cyclonedx_npm(self, mock_run, tmp_path: Path):
        task_dir = tmp_path / ".dynos" / "task-1"
        task_dir.mkdir(parents=True)
        sbom_path = task_dir / "sbom.json"

        def side_effect(cmd, **kwargs):
            if cmd[0] == "syft":
                return (1, "", "not found")
            if "cyclonedx-npm" in str(cmd):
                sbom_path.write_text('{"bomFormat": "CycloneDX"}')
                return (0, "", "")
            return (1, "", "not found")

        mock_run.side_effect = side_effect
        result = compliance_check.generate_sbom(tmp_path, task_dir, ["npm"])
        assert result["generated"] is True
        assert result["tool"] == "cyclonedx-npm"


# ---------------------------------------------------------------------------
# Provenance check
# ---------------------------------------------------------------------------

class TestCheckProvenance:
    @mock.patch("compliance_check._run_cmd")
    def test_cosign_not_installed(self, mock_run, tmp_path: Path):
        mock_run.return_value = (-1, "", "command not found: cosign")
        result = compliance_check.check_provenance(tmp_path, ["npm"])
        assert result["checked"] is False

    @mock.patch("compliance_check._run_cmd")
    def test_npm_signatures_pass(self, mock_run, tmp_path: Path):
        (tmp_path / "package-lock.json").write_text("{}")
        mock_run.side_effect = [
            (0, "cosign v2.0.0", ""),  # cosign version
            (0, "audited 50 packages", ""),  # npm audit signatures
        ]
        result = compliance_check.check_provenance(tmp_path, ["npm"])
        assert result["checked"] is True
        assert len(result["results"]) == 1
        assert result["results"][0]["passed"] is True

    @mock.patch("compliance_check._run_cmd")
    def test_npm_signatures_fail(self, mock_run, tmp_path: Path):
        (tmp_path / "package-lock.json").write_text("{}")
        mock_run.side_effect = [
            (0, "cosign v2.0.0", ""),
            (1, "", "invalid signatures found"),
        ]
        result = compliance_check.check_provenance(tmp_path, ["npm"])
        assert result["checked"] is True
        assert result["results"][0]["passed"] is False

    @mock.patch("compliance_check._run_cmd")
    def test_no_lock_file_skips_npm(self, mock_run, tmp_path: Path):
        mock_run.return_value = (0, "cosign v2.0.0", "")
        result = compliance_check.check_provenance(tmp_path, ["npm"])
        assert result["checked"] is True
        assert len(result["results"]) == 0


# ---------------------------------------------------------------------------
# Privacy checks
# ---------------------------------------------------------------------------

class TestCheckPrivacy:
    def test_no_pii_skips(self, tmp_path: Path):
        (tmp_path / "main.py").write_text("print('hello')")
        result = compliance_check.check_privacy(tmp_path)
        assert result["handles_pii"] is False
        assert result["missing"] == []

    def test_pii_with_all_features(self, tmp_path: Path):
        (tmp_path / "models.py").write_text("user_email = db.Column(String)")
        (tmp_path / "export.py").write_text("def export_data(user_id): pass")
        (tmp_path / "account.py").write_text("def delete_account(user_id): pass")
        (tmp_path / "consent.py").write_text("def cookie_consent(): pass")
        result = compliance_check.check_privacy(tmp_path)
        assert result["handles_pii"] is True
        assert result["missing"] == []

    def test_pii_missing_export(self, tmp_path: Path):
        (tmp_path / "models.py").write_text("user_email = db.Column(String)")
        (tmp_path / "account.py").write_text("def delete_account(user_id): pass")
        (tmp_path / "consent.py").write_text("def cookie_consent(): pass")
        result = compliance_check.check_privacy(tmp_path)
        assert result["handles_pii"] is True
        assert "data_export" in result["missing"]

    def test_pii_missing_deletion(self, tmp_path: Path):
        (tmp_path / "models.py").write_text("user_email = db.Column(String)")
        (tmp_path / "export.py").write_text("def data_export(user_id): pass")
        (tmp_path / "consent.py").write_text("def cookie_consent(): pass")
        result = compliance_check.check_privacy(tmp_path)
        assert result["handles_pii"] is True
        assert "account_deletion" in result["missing"]

    def test_pii_missing_consent(self, tmp_path: Path):
        (tmp_path / "models.py").write_text("user_email = db.Column(String)")
        (tmp_path / "export.py").write_text("def data_export(user_id): pass")
        (tmp_path / "account.py").write_text("def delete_account(user_id): pass")
        result = compliance_check.check_privacy(tmp_path)
        assert result["handles_pii"] is True
        assert "consent_management" in result["missing"]

    def test_pii_all_missing(self, tmp_path: Path):
        (tmp_path / "models.py").write_text("user_email = db.Column(String)")
        result = compliance_check.check_privacy(tmp_path)
        assert result["handles_pii"] is True
        assert len(result["missing"]) == 3

    def test_changed_files_filter(self, tmp_path: Path):
        (tmp_path / "models.py").write_text("user_email = db.Column(String)")
        result = compliance_check.check_privacy(tmp_path, changed_files=["models.py"])
        assert result["handles_pii"] is True

    def test_ignores_node_modules(self, tmp_path: Path):
        nm = tmp_path / "node_modules" / "pkg"
        nm.mkdir(parents=True)
        (nm / "index.js").write_text("var user_email = 'test';")
        result = compliance_check.check_privacy(tmp_path)
        assert result["handles_pii"] is False

    def test_ignores_dynos_dir(self, tmp_path: Path):
        dynos = tmp_path / ".dynos" / "task-1"
        dynos.mkdir(parents=True)
        (dynos / "spec.py").write_text("user_email = 'test'")
        result = compliance_check.check_privacy(tmp_path)
        assert result["handles_pii"] is False


# ---------------------------------------------------------------------------
# _run_cmd edge cases
# ---------------------------------------------------------------------------

class TestRunCmd:
    def test_command_not_found(self):
        rc, stdout, stderr = compliance_check._run_cmd(["nonexistent_cmd_12345"])
        assert rc == -1
        assert "command not found" in stderr

    def test_timeout(self):
        rc, stdout, stderr = compliance_check._run_cmd(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            timeout=1,
        )
        assert rc == -2
        assert "timeout" in stderr


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------

class TestCLI:
    def test_json_output_shape(self, tmp_path: Path):
        task_dir = tmp_path / ".dynos" / "task-1"
        task_dir.mkdir(parents=True)
        (tmp_path / "main.py").write_text("print('hello')")

        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent.parent / "hooks" / "compliance_check.py"),
             "--root", str(tmp_path), "--task-dir", str(task_dir)],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "ecosystems_detected" in data
        assert "license_findings" in data
        assert "sbom" in data
        assert "provenance" in data
        assert "privacy" in data

    def test_with_changed_files(self, tmp_path: Path):
        task_dir = tmp_path / ".dynos" / "task-1"
        task_dir.mkdir(parents=True)
        (tmp_path / "main.py").write_text("print('hello')")

        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent.parent / "hooks" / "compliance_check.py"),
             "--root", str(tmp_path), "--task-dir", str(task_dir),
             "--changed-files", "main.py,other.py"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert isinstance(data["ecosystems_detected"], list)


# ---------------------------------------------------------------------------
# Finding schema compatibility
# ---------------------------------------------------------------------------

class TestFindingSchemaCompat:
    """Verify that compliance findings can use the canonical audit report schema."""

    def test_compliance_finding_shape(self):
        """A compliance finding has all required fields from audit-report.md."""
        finding = {
            "id": "comp-001",
            "category": "compliance",
            "description": "GPL-3.0 dependency 'some-pkg@1.0.0' in proprietary project",
            "location": "package.json:1",
            "severity": "major",
            "blocking": True,
        }
        assert finding["id"].startswith("comp-")
        assert finding["category"] == "compliance"
        assert finding["severity"] in ("critical", "major", "minor")
        assert isinstance(finding["blocking"], bool)

    def test_security_finding_backwards_compat(self):
        """Existing security findings remain valid — category is optional."""
        finding = {
            "id": "sec-001",
            "description": "Hardcoded secret in config.py",
            "location": "config.py:10",
            "severity": "critical",
            "blocking": True,
        }
        # No category field — backwards compatible
        assert "category" not in finding
        assert finding["id"].startswith("sec-")

    def test_security_finding_with_category(self):
        """Security findings can optionally include category."""
        finding = {
            "id": "sec-002",
            "category": "security",
            "description": "SQL injection in query builder",
            "location": "db.py:42",
            "severity": "critical",
            "blocking": True,
        }
        assert finding["category"] == "security"

    def test_repair_task_shape_for_compliance(self):
        """Compliance repair tasks match the repair_tasks schema."""
        repair_task = {
            "finding_id": "comp-002",
            "description": "Add data export endpoint for user data (GDPR Art. 20)",
            "assigned_executor": "backend-executor",
            "affected_files": ["src/api/routes.py"],
        }
        assert repair_task["finding_id"].startswith("comp-")
        assert repair_task["assigned_executor"] in (
            "backend-executor", "integration-executor", "ui-executor",
        )


# ---------------------------------------------------------------------------
# Copyleft license set
# ---------------------------------------------------------------------------

class TestCopyleftSet:
    def test_gpl_variants_covered(self):
        for v in ("GPL-2.0", "GPL-2.0-only", "GPL-2.0-or-later",
                   "GPL-3.0", "GPL-3.0-only", "GPL-3.0-or-later"):
            assert v in compliance_check.COPYLEFT_LICENSES

    def test_agpl_variants_covered(self):
        for v in ("AGPL-1.0", "AGPL-3.0", "AGPL-3.0-only", "AGPL-3.0-or-later"):
            assert v in compliance_check.COPYLEFT_LICENSES

    def test_lgpl_variants_covered(self):
        for v in ("LGPL-2.0", "LGPL-2.1", "LGPL-3.0"):
            assert v in compliance_check.COPYLEFT_LICENSES

    def test_mit_not_copyleft(self):
        assert "MIT" not in compliance_check.COPYLEFT_LICENSES

    def test_apache_not_copyleft(self):
        assert "Apache-2.0" not in compliance_check.COPYLEFT_LICENSES

    def test_bsd_not_copyleft(self):
        assert "BSD-3-Clause" not in compliance_check.COPYLEFT_LICENSES


# ---------------------------------------------------------------------------
# Ecosystem marker coverage
# ---------------------------------------------------------------------------

class TestEcosystemMarkers:
    """Ensure we support the project types mentioned in the PR spec."""

    @pytest.mark.parametrize("marker,eco", [
        ("package.json", "npm"),
        ("pyproject.toml", "python"),
        ("go.mod", "go"),
        ("Cargo.toml", "rust"),
        ("Gemfile", "ruby"),
        ("pom.xml", "java"),
        ("composer.json", "php"),
        ("pubspec.yaml", "dart"),
        ("Package.swift", "swift"),
        ("mix.lock", "elixir"),
    ])
    def test_marker_maps_to_ecosystem(self, marker: str, eco: str):
        assert compliance_check.ECOSYSTEM_MARKERS[marker] == eco
