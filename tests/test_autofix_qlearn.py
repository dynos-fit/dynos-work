"""Tests for autofix Q-learning state encoding and Q-table setup.

Covers AC 12-13: state encoding, Q-table named q-autofix.json.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))


# ===========================================================================
# AC 12: encode_autofix_state
# ===========================================================================

class TestEncodeAutofixState:
    """AC 12: State key is colon-separated: category:extension:centrality_tier:severity."""

    def test_basic_encoding(self) -> None:
        # AC 12
        from dynoslib_qlearn import encode_autofix_state

        result = encode_autofix_state(
            finding_category="llm-review",
            file_extension=".dart",
            centrality_tier="high",
            severity="medium",
        )
        assert result == "llm-review:.dart:high:medium"

    def test_python_file_low_centrality(self) -> None:
        # AC 12
        from dynoslib_qlearn import encode_autofix_state

        result = encode_autofix_state(
            finding_category="dead-code",
            file_extension=".py",
            centrality_tier="low",
            severity="low",
        )
        assert result == "dead-code:.py:low:low"

    def test_all_centrality_tiers(self) -> None:
        # AC 12: centrality_tier must be one of high, medium, low
        from dynoslib_qlearn import encode_autofix_state

        for tier in ("high", "medium", "low"):
            result = encode_autofix_state("llm-review", ".py", tier, "medium")
            parts = result.split(":")
            assert parts[2] == tier

    def test_output_has_four_colon_separated_parts(self) -> None:
        # AC 12
        from dynoslib_qlearn import encode_autofix_state

        result = encode_autofix_state("security", ".ts", "high", "critical")
        parts = result.split(":")
        assert len(parts) == 4, f"Expected 4 parts, got {len(parts)}: {result}"

    def test_extension_includes_dot(self) -> None:
        # AC 12: file_extension preserves the dot prefix
        from dynoslib_qlearn import encode_autofix_state

        result = encode_autofix_state("llm-review", ".go", "medium", "high")
        assert ":.go:" in result

    def test_various_categories(self) -> None:
        # AC 12: works with any finding category
        from dynoslib_qlearn import encode_autofix_state

        for cat in ("llm-review", "dead-code", "syntax-error", "dependency-vuln", "recurring-audit"):
            result = encode_autofix_state(cat, ".py", "high", "medium")
            assert result.startswith(f"{cat}:")

    def test_various_severities(self) -> None:
        # AC 12
        from dynoslib_qlearn import encode_autofix_state

        for sev in ("low", "medium", "high", "critical"):
            result = encode_autofix_state("llm-review", ".py", "high", sev)
            assert result.endswith(f":{sev}")


# ===========================================================================
# AC 13: Q-table named q-autofix.json
# ===========================================================================

class TestAutofixQTable:
    """AC 13: Separate Q-table q-autofix.json with actions [attempt_fix, open_issue, skip]."""

    def test_autofix_table_path_is_q_autofix_json(self, tmp_path: Path) -> None:
        # AC 13: file must be named q-autofix.json, NOT q-repair-autofix.json
        from dynoslib_qlearn import _q_autofix_table_path

        path = _q_autofix_table_path(tmp_path)
        assert path.name == "q-autofix.json"
        assert "q-repair" not in str(path)

    def test_load_autofix_q_table_returns_empty_on_missing(self, tmp_path: Path) -> None:
        # AC 13
        from dynoslib_qlearn import load_autofix_q_table

        with patch("dynoslib_qlearn._persistent_project_dir", return_value=tmp_path):
            table = load_autofix_q_table(tmp_path)
        assert isinstance(table, dict)
        assert "entries" in table
        assert table["entries"] == {}

    def test_save_and_load_autofix_q_table_roundtrip(self, tmp_path: Path) -> None:
        # AC 13
        from dynoslib_qlearn import load_autofix_q_table, save_autofix_q_table

        with patch("dynoslib_qlearn._persistent_project_dir", return_value=tmp_path):
            table = {"version": 1, "updated_at": "", "entries": {
                "llm-review:.py:high:medium": {
                    "attempt_fix": 0.5,
                    "open_issue": 0.2,
                    "skip": -0.1,
                }
            }}
            save_autofix_q_table(tmp_path, table)
            loaded = load_autofix_q_table(tmp_path)
        assert loaded["entries"]["llm-review:.py:high:medium"]["attempt_fix"] == 0.5

    def test_autofix_table_separate_from_repair_tables(self, tmp_path: Path) -> None:
        # AC 13: autofix table is separate from q-repair-executor.json and q-repair-model.json
        from dynoslib_qlearn import _q_autofix_table_path, _q_table_path

        autofix_path = _q_autofix_table_path(tmp_path)
        repair_exec_path = _q_table_path(tmp_path, "executor")
        repair_model_path = _q_table_path(tmp_path, "model")

        assert autofix_path != repair_exec_path
        assert autofix_path != repair_model_path

    def test_autofix_actions_are_attempt_fix_open_issue_skip(self) -> None:
        # AC 13: the three actions for the autofix Q-table
        # This tests that select_action works with these action names
        from dynoslib_qlearn import select_action

        table = {"version": 1, "entries": {
            "test_state": {"attempt_fix": 1.0, "open_issue": 0.0, "skip": -1.0}
        }}
        actions = ["attempt_fix", "open_issue", "skip"]
        # With epsilon=0, should always pick attempt_fix (highest Q-value)
        action, source = select_action(table, "test_state", actions, epsilon=0.0)
        assert action == "attempt_fix"
        assert source == "q-learning"

    def test_select_action_picks_open_issue_when_highest(self) -> None:
        # AC 13
        from dynoslib_qlearn import select_action

        table = {"version": 1, "entries": {
            "test_state": {"attempt_fix": -0.5, "open_issue": 0.8, "skip": 0.0}
        }}
        action, _ = select_action(table, "test_state", ["attempt_fix", "open_issue", "skip"], epsilon=0.0)
        assert action == "open_issue"

    def test_select_action_picks_skip_when_highest(self) -> None:
        # AC 13
        from dynoslib_qlearn import select_action

        table = {"version": 1, "entries": {
            "test_state": {"attempt_fix": -1.0, "open_issue": -0.5, "skip": 0.5}
        }}
        action, _ = select_action(table, "test_state", ["attempt_fix", "open_issue", "skip"], epsilon=0.0)
        assert action == "skip"

    def test_unseen_state_explores_randomly(self) -> None:
        # AC 13: unseen state should explore (random selection)
        from dynoslib_qlearn import select_action

        table = {"version": 1, "entries": {}}
        actions = ["attempt_fix", "open_issue", "skip"]
        action, source = select_action(table, "never_seen_state", actions, epsilon=0.0)
        assert action in actions
        assert source == "q-explore"
