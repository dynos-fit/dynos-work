"""Tests for PR #17 — usage telemetry for dormancy detection.

Validates:
  - Telemetry recording and reading
  - Monitored modules are instrumented
  - CLI stats-usage command works
  - Static analysis findings (3 of 4 modules are active)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# lib_usage_telemetry core
# ---------------------------------------------------------------------------

class TestRecordUsage:
    def test_records_entry(self, tmp_path: Path):
        with mock.patch.dict(os.environ, {"DYNOS_HOME": str(tmp_path)}):
            from lib_usage_telemetry import record_usage, read_telemetry
            record_usage("test_module")
            entries = read_telemetry()
            assert len(entries) == 1
            assert entries[0]["module"] == "test_module"
            assert "ts" in entries[0]
            assert "pid" in entries[0]

    def test_records_function(self, tmp_path: Path):
        with mock.patch.dict(os.environ, {"DYNOS_HOME": str(tmp_path)}):
            from lib_usage_telemetry import record_usage, read_telemetry
            record_usage("test_module", function="my_func")
            entries = read_telemetry()
            assert entries[0]["function"] == "my_func"

    def test_appends_multiple(self, tmp_path: Path):
        with mock.patch.dict(os.environ, {"DYNOS_HOME": str(tmp_path)}):
            from lib_usage_telemetry import record_usage, read_telemetry
            record_usage("mod_a")
            record_usage("mod_b")
            record_usage("mod_a")
            entries = read_telemetry()
            assert len(entries) == 3

    def test_never_raises(self, tmp_path: Path):
        with mock.patch.dict(os.environ, {"DYNOS_HOME": "/nonexistent/path/that/cannot/be/created/xxxxxxx"}):
            from lib_usage_telemetry import record_usage
            # Should not raise even with bad path
            record_usage("test")


class TestSummarizeTelemetry:
    def test_counts_per_module(self, tmp_path: Path):
        with mock.patch.dict(os.environ, {"DYNOS_HOME": str(tmp_path)}):
            from lib_usage_telemetry import record_usage, summarize_telemetry
            record_usage("mod_a")
            record_usage("mod_b")
            record_usage("mod_a")
            counts = summarize_telemetry()
            assert counts["mod_a"] == 2
            assert counts["mod_b"] == 1

    def test_empty_when_no_data(self, tmp_path: Path):
        with mock.patch.dict(os.environ, {"DYNOS_HOME": str(tmp_path)}):
            from lib_usage_telemetry import summarize_telemetry
            assert summarize_telemetry() == {}


# ---------------------------------------------------------------------------
# Instrumentation verification
# ---------------------------------------------------------------------------

class TestModulesInstrumented:
    """Verify that each monitored module calls record_usage at import time."""

    def test_dream_instrumented(self):
        text = (ROOT / "sandbox" / "dream.py").read_text()
        assert "record_usage" in text
        assert '"dream"' in text

    def test_postmortem_improve_instrumented(self):
        text = (ROOT / "memory" / "postmortem_improve.py").read_text()
        assert "record_usage" in text
        assert '"postmortem_improve"' in text

    def test_lib_qlearn_instrumented(self):
        text = (ROOT / "memory" / "lib_qlearn.py").read_text()
        assert "record_usage" in text
        assert '"lib_qlearn"' in text

    def test_cli_base_instrumented(self):
        text = (ROOT / "hooks" / "cli_base.py").read_text()
        assert "record_usage" in text
        assert '"cli_base"' in text


# ---------------------------------------------------------------------------
# Static analysis findings (verified in the exploration)
# ---------------------------------------------------------------------------

class TestStaticAnalysisFindings:
    """Codify the static analysis results so they don't regress."""

    def test_cli_base_has_importers(self):
        """cli_base is imported by 24+ modules — NOT dormant."""
        from pathlib import Path
        hooks = ROOT / "hooks"
        importers = []
        for f in hooks.glob("*.py"):
            if f.name == "cli_base.py":
                continue
            try:
                if "from cli_base import" in f.read_text():
                    importers.append(f.name)
            except OSError:
                continue
        assert len(importers) >= 20, f"cli_base should have 20+ importers, found {len(importers)}"

    def test_lib_qlearn_has_importers(self):
        """lib_qlearn is imported by ctl.py and proactive.py — NOT dormant."""
        hooks = ROOT / "hooks"
        importers = []
        for f in hooks.glob("*.py"):
            if f.name == "lib_qlearn.py":
                continue
            try:
                if "from lib_qlearn import" in f.read_text():
                    importers.append(f.name)
            except OSError:
                continue
        assert len(importers) >= 1

    def test_postmortem_improve_has_importers(self):
        """postmortem_improve is imported by postmortem.py — NOT dormant."""
        text = (ROOT / "memory" / "postmortem.py").read_text()
        assert "from postmortem_improve import" in text

    def test_dream_has_no_python_importers(self):
        """dream.py has zero Python importers (only subprocess call from founder skill)."""
        hooks = ROOT / "hooks"
        importers = []
        for f in hooks.glob("*.py"):
            if f.name == "dream.py":
                continue
            try:
                content = f.read_text()
                if "from dream import" in content or "import dream" in content:
                    importers.append(f.name)
            except OSError:
                continue
        assert len(importers) == 0, f"dream.py should have 0 Python importers, found {importers}"


# ---------------------------------------------------------------------------
# CLI: stats-usage
# ---------------------------------------------------------------------------

class TestStatsUsageCli:
    def test_runs_successfully(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "hooks" / "ctl.py"), "stats-usage"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        assert "Module Usage Telemetry" in result.stdout

    def test_json_output(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "hooks" / "ctl.py"), "stats-usage", "--json"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "dream" in data
        assert "postmortem_improve" in data
        assert "lib_qlearn" in data
        assert "cli_base" in data
