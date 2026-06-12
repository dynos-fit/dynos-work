"""Tests for hooks/lib_host.py (AC-5, AC-28 partial).

Covers:
  AC-5  — lib_host.py detection and persistence (detect_host, persist_host,
           get_persisted_host)
  AC-28 — lib_host.py is a leaf module (no forbidden imports)
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = ROOT / "hooks"
MEMORY_DIR = ROOT / "memory"

if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))
if str(MEMORY_DIR) not in sys.path:
    sys.path.insert(0, str(MEMORY_DIR))

import lib_host  # noqa: E402  (production module to be created — RED phase)


# ---------------------------------------------------------------------------
# AC-5: detect_host — env-var detection
# ---------------------------------------------------------------------------

class TestHostDetectFallbackClaude:
    def test_host_detect_fallback_claude(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AC-5: detect_host() returns 'claude' when neither env var is set."""
        monkeypatch.delenv("CODEX_PLUGIN_ROOT", raising=False)
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        result = lib_host.detect_host()
        assert result == "claude", (
            f"detect_host() must return 'claude' as safe default, got {result!r}"
        )

    def test_detect_host_codex_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AC-5: detect_host() returns 'codex' when CODEX_PLUGIN_ROOT is non-empty."""
        monkeypatch.setenv("CODEX_PLUGIN_ROOT", "/some/path")
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        result = lib_host.detect_host()
        assert result == "codex", (
            f"detect_host() must return 'codex' when CODEX_PLUGIN_ROOT is set, got {result!r}"
        )

    def test_detect_host_claude_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AC-5: detect_host() returns 'claude' when CLAUDE_PLUGIN_ROOT is non-empty."""
        monkeypatch.delenv("CODEX_PLUGIN_ROOT", raising=False)
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/some/path")
        result = lib_host.detect_host()
        assert result == "claude", (
            f"detect_host() must return 'claude' when CLAUDE_PLUGIN_ROOT is set, "
            f"got {result!r}"
        )

    def test_detect_host_codex_takes_priority_over_claude(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-5: CODEX_PLUGIN_ROOT takes priority over CLAUDE_PLUGIN_ROOT."""
        monkeypatch.setenv("CODEX_PLUGIN_ROOT", "/codex/path")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/claude/path")
        result = lib_host.detect_host()
        assert result == "codex", (
            "CODEX_PLUGIN_ROOT must take priority over CLAUDE_PLUGIN_ROOT"
        )

    def test_detect_host_empty_codex_env_falls_to_claude(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-5: Empty CODEX_PLUGIN_ROOT is treated as unset; falls back to claude."""
        monkeypatch.setenv("CODEX_PLUGIN_ROOT", "")
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        result = lib_host.detect_host()
        # Empty string means not set — falls back to "claude" default
        assert result == "claude", (
            f"Empty CODEX_PLUGIN_ROOT should not trigger codex detection, got {result!r}"
        )


# ---------------------------------------------------------------------------
# AC-5: persist_host / get_persisted_host round-trip
# ---------------------------------------------------------------------------

class TestHostPersistedOnceAtSessionStart:
    def test_host_persisted_once_at_session_start(self, tmp_path: Path) -> None:
        """AC-5: persist_host followed by get_persisted_host returns the same value."""
        cp = tmp_path / "control-plane.json"

        lib_host.persist_host(cp, "codex")
        result = lib_host.get_persisted_host(cp)
        assert result == "codex", (
            f"get_persisted_host must return 'codex' after persist_host, got {result!r}"
        )

    def test_persist_host_writes_json(self, tmp_path: Path) -> None:
        """AC-5: persist_host writes valid JSON with a 'host' key."""
        cp = tmp_path / "control-plane.json"
        lib_host.persist_host(cp, "claude")

        assert cp.exists(), "control-plane.json must be created by persist_host"
        data = json.loads(cp.read_text())
        assert data["host"] == "claude"

    def test_persist_host_overwrites_previous_value(self, tmp_path: Path) -> None:
        """AC-5: A second persist_host call overwrites the previous host value."""
        cp = tmp_path / "control-plane.json"
        lib_host.persist_host(cp, "claude")
        lib_host.persist_host(cp, "codex")
        result = lib_host.get_persisted_host(cp)
        assert result == "codex"


class TestHostReadFromControlPlane:
    def test_host_read_from_control_plane(self, tmp_path: Path) -> None:
        """AC-5: get_persisted_host reads 'host' field from control-plane.json."""
        cp = tmp_path / "control-plane.json"
        cp.write_text(json.dumps({"host": "codex", "other_field": "value"}))
        result = lib_host.get_persisted_host(cp)
        assert result == "codex"

    def test_get_persisted_host_absent_file_returns_none(self, tmp_path: Path) -> None:
        """AC-5: get_persisted_host returns None when control-plane.json is absent."""
        cp = tmp_path / "no-such-file.json"
        result = lib_host.get_persisted_host(cp)
        assert result is None, (
            f"get_persisted_host must return None when file is absent, got {result!r}"
        )

    def test_get_persisted_host_missing_host_key_returns_none(
        self, tmp_path: Path
    ) -> None:
        """AC-5: get_persisted_host returns None when 'host' key is absent from JSON."""
        cp = tmp_path / "control-plane.json"
        cp.write_text(json.dumps({"other_key": "value"}))
        result = lib_host.get_persisted_host(cp)
        assert result is None

    def test_get_persisted_host_corrupt_json_returns_none(
        self, tmp_path: Path
    ) -> None:
        """AC-5: get_persisted_host returns None on JSON parse error."""
        cp = tmp_path / "control-plane.json"
        cp.write_text("{ invalid json }")
        result = lib_host.get_persisted_host(cp)
        assert result is None


# ---------------------------------------------------------------------------
# AC-28: lib_host.py is a leaf module
# ---------------------------------------------------------------------------

class TestLibHostLeafModule:
    def test_lib_host_no_forbidden_imports(self) -> None:
        """AC-28: lib_host.py must not import from any dynos-work hooks/ or memory/ module."""
        lib_host_path = HOOKS_DIR / "lib_host.py"
        src = lib_host_path.read_text(encoding="utf-8")
        tree = ast.parse(src)

        _FORBIDDEN = {
            "router", "lib_defaults", "lib_tokens", "lib_validate",
            "lib_tool_budget", "circuit_breaker", "receipts", "ctl",
            "lib_qlearn", "policy_engine", "postmortem", "lib_migrate",
            "lib_log", "lib_receipts", "lib_core", "lib_tokens_hook",
            "lib_models",
        }

        violations: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mod = getattr(node, "module", None) or ""
                names = [a.name for a in getattr(node, "names", [])]
                combined = mod + " " + " ".join(names)
                for bad in _FORBIDDEN:
                    if bad in combined:
                        violations.append(
                            f"lib_host.py: forbidden import: {combined.strip()!r}"
                        )

        assert not violations, "\n".join(violations)
