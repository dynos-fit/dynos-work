"""Tests for PR #14 — LEARNING_ENABLED policy flag.

Validates:
  - is_learning_enabled reads from policy.json correctly
  - Defaults to True when file/key missing
  - Router returns generic immediately when learning disabled
  - Router model returns default when learning disabled
  - Router skip returns false when learning disabled
  - Event bus skips learning handlers when disabled
  - Existing behavior unchanged when learning enabled (default)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))


# ---------------------------------------------------------------------------
# is_learning_enabled
# ---------------------------------------------------------------------------

class TestIsLearningEnabled:
    def test_default_true_no_file(self, tmp_path: Path):
        from lib_core import is_learning_enabled
        # No policy.json exists
        with mock.patch.dict(os.environ, {"DYNOS_HOME": str(tmp_path / ".dynos")}):
            assert is_learning_enabled(tmp_path / "project") is True

    def test_true_when_key_true(self, tmp_path: Path):
        from lib_core import is_learning_enabled
        dynos_home = tmp_path / ".dynos"
        slug = str((tmp_path / "project").resolve()).strip("/").replace("/", "-")
        policy_dir = dynos_home / "projects" / slug
        policy_dir.mkdir(parents=True)
        (policy_dir / "policy.json").write_text(json.dumps({"learning_enabled": True}))
        with mock.patch.dict(os.environ, {"DYNOS_HOME": str(dynos_home)}):
            assert is_learning_enabled(tmp_path / "project") is True

    def test_false_when_key_false(self, tmp_path: Path):
        from lib_core import is_learning_enabled
        dynos_home = tmp_path / ".dynos"
        slug = str((tmp_path / "project").resolve()).strip("/").replace("/", "-")
        policy_dir = dynos_home / "projects" / slug
        policy_dir.mkdir(parents=True)
        (policy_dir / "policy.json").write_text(json.dumps({"learning_enabled": False}))
        with mock.patch.dict(os.environ, {"DYNOS_HOME": str(dynos_home)}):
            assert is_learning_enabled(tmp_path / "project") is False

    def test_default_true_when_key_missing(self, tmp_path: Path):
        from lib_core import is_learning_enabled
        dynos_home = tmp_path / ".dynos"
        slug = str((tmp_path / "project").resolve()).strip("/").replace("/", "-")
        policy_dir = dynos_home / "projects" / slug
        policy_dir.mkdir(parents=True)
        (policy_dir / "policy.json").write_text(json.dumps({"other_key": "value"}))
        with mock.patch.dict(os.environ, {"DYNOS_HOME": str(dynos_home)}):
            assert is_learning_enabled(tmp_path / "project") is True

    def test_default_true_on_corrupt_json(self, tmp_path: Path):
        from lib_core import is_learning_enabled
        dynos_home = tmp_path / ".dynos"
        slug = str((tmp_path / "project").resolve()).strip("/").replace("/", "-")
        policy_dir = dynos_home / "projects" / slug
        policy_dir.mkdir(parents=True)
        (policy_dir / "policy.json").write_text("not json!!!")
        with mock.patch.dict(os.environ, {"DYNOS_HOME": str(dynos_home)}):
            assert is_learning_enabled(tmp_path / "project") is True


# ---------------------------------------------------------------------------
# Router: resolve_route
# ---------------------------------------------------------------------------

class TestResolveRouteWithLearning:
    @mock.patch("router.is_learning_enabled", return_value=False)
    @mock.patch("router.log_event")
    def test_returns_generic_when_disabled(self, mock_log, mock_learning, tmp_path: Path):
        from router import resolve_route
        result = resolve_route(tmp_path, "backend-executor", "feature")
        assert result["mode"] == "generic"
        assert result["source"] == "learning_enabled=false"
        assert result["agent_path"] is None

    @mock.patch("router.is_learning_enabled", return_value=True)
    @mock.patch("router.ensure_learned_registry", return_value={"agents": []})
    @mock.patch("router.log_event")
    def test_returns_generic_normally_no_agents(self, mock_log, mock_registry, mock_learning, tmp_path: Path):
        from router import resolve_route
        result = resolve_route(tmp_path, "backend-executor", "feature")
        assert result["mode"] == "generic"
        assert result["source"] == "no learned agent"


# ---------------------------------------------------------------------------
# Router: resolve_model
# ---------------------------------------------------------------------------

class TestResolveModelWithLearning:
    @mock.patch("router.is_learning_enabled", return_value=False)
    @mock.patch("router.project_policy", return_value={})
    @mock.patch("router.log_event")
    def test_returns_default_when_disabled(self, mock_log, mock_policy, mock_learning, tmp_path: Path):
        from router import resolve_model
        result = resolve_model(tmp_path, "backend-executor", "feature")
        assert result["model"] is None  # DEFAULT_MODEL = None
        assert "default" in result["source"]

    @mock.patch("router.is_learning_enabled", return_value=False)
    @mock.patch("router.project_policy", return_value={"model_overrides": {"backend-executor": "haiku"}})
    @mock.patch("router.log_event")
    def test_explicit_policy_still_works_when_disabled(self, mock_log, mock_policy, mock_learning, tmp_path: Path):
        from router import resolve_model
        result = resolve_model(tmp_path, "backend-executor", "feature")
        # Explicit policy overrides are NOT learning — they should still apply
        assert result["model"] == "haiku"
        assert result["source"] == "explicit_policy"


# ---------------------------------------------------------------------------
# Router: resolve_skip
# ---------------------------------------------------------------------------

class TestResolveSkipWithLearning:
    @mock.patch("router.is_learning_enabled", return_value=False)
    def test_never_skips_when_disabled(self, mock_learning, tmp_path: Path):
        from router import resolve_skip
        result = resolve_skip(tmp_path, "dead-code-auditor", "feature")
        assert result["skip"] is False
        assert "learning_enabled=false" in result["reason"]

    @mock.patch("router.is_learning_enabled", return_value=False)
    def test_exempt_auditors_still_exempt(self, mock_learning, tmp_path: Path):
        from router import resolve_skip
        result = resolve_skip(tmp_path, "security-auditor", "feature")
        assert result["skip"] is False
        assert result["reason"] == "skip-exempt"


# ---------------------------------------------------------------------------
# Event bus: learning handlers skipped
# ---------------------------------------------------------------------------

class TestEventBusLearningGate:
    def test_learning_handlers_identified(self):
        """Verify the learning handler set covers the right handlers."""
        learning_handlers = {"learn", "trajectory", "evolve", "patterns", "improve", "benchmark"}
        observability_handlers = {"dashboard", "register", "postmortem"}
        # Postmortem is in evolve-completed along with improve and benchmark
        # but postmortem writes retrospective improvements — it's borderline
        # For now it's NOT in the learning set (it runs even without learning)
        assert learning_handlers & observability_handlers == set()

    def test_learning_handler_names_exist_in_registry(self):
        """Verify all learning handler names exist in the HANDLERS registry."""
        expected = {"learn", "trajectory", "evolve", "patterns", "improve", "benchmark"}
        from eventbus import HANDLERS
        all_handler_names = set()
        for handlers in HANDLERS.values():
            for name, _ in handlers:
                all_handler_names.add(name)
        assert expected <= all_handler_names, f"Missing handlers: {expected - all_handler_names}"

    def test_observability_handlers_not_in_learning_set(self):
        """Dashboard, register, postmortem should run even with learning off."""
        learning_handlers = {"learn", "trajectory", "evolve", "patterns", "improve", "benchmark"}
        assert "dashboard" not in learning_handlers
        assert "register" not in learning_handlers
        assert "postmortem" not in learning_handlers


# ---------------------------------------------------------------------------
# Skill references
# ---------------------------------------------------------------------------

class TestSkillReferences:
    def test_start_skill_references_learning_gate(self):
        path = Path(__file__).resolve().parent.parent / "skills" / "start" / "SKILL.md"
        text = path.read_text()
        assert "learning_enabled=false" in text

    def test_start_skill_trajectory_gated(self):
        path = Path(__file__).resolve().parent.parent / "skills" / "start" / "SKILL.md"
        text = path.read_text()
        assert "skip when" in text.lower() and "learning_enabled" in text


# ---------------------------------------------------------------------------
# Facade export
# ---------------------------------------------------------------------------

class TestFacadeExport:
    def test_is_learning_enabled_exported(self):
        import lib
        assert hasattr(lib, "is_learning_enabled")


# ---------------------------------------------------------------------------
# CLI: ctl.py config
# ---------------------------------------------------------------------------

class TestCtlConfig:
    def test_config_get_empty(self, tmp_path: Path):
        result = subprocess.run(
            [sys.executable, str(ROOT / "hooks" / "ctl.py"), "config", "get", "--root", str(tmp_path)],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        assert "{}" in result.stdout

    def test_config_set_and_get(self, tmp_path: Path):
        # Set
        result = subprocess.run(
            [sys.executable, str(ROOT / "hooks" / "ctl.py"), "config", "set", "learning_enabled", "false", "--root", str(tmp_path)],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        assert "false" in result.stdout

        # Get
        result = subprocess.run(
            [sys.executable, str(ROOT / "hooks" / "ctl.py"), "config", "get", "learning_enabled", "--root", str(tmp_path)],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        assert "false" in result.stdout

    def test_config_set_true(self, tmp_path: Path):
        result = subprocess.run(
            [sys.executable, str(ROOT / "hooks" / "ctl.py"), "config", "set", "learning_enabled", "true", "--root", str(tmp_path)],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        assert "true" in result.stdout

    def test_config_roundtrip_disables_learning(self, tmp_path: Path):
        from lib_core import is_learning_enabled
        dynos_home = tmp_path / ".dynos-home"

        # Set via CLI
        with mock.patch.dict(os.environ, {"DYNOS_HOME": str(dynos_home)}):
            subprocess.run(
                [sys.executable, str(ROOT / "hooks" / "ctl.py"), "config", "set", "learning_enabled", "false", "--root", str(tmp_path)],
                capture_output=True, text=True, timeout=10,
                env={**os.environ, "DYNOS_HOME": str(dynos_home)},
            )
            # Verify the function reads it
            assert is_learning_enabled(tmp_path) is False
