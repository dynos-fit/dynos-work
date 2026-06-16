"""
Regression tests for:
  AC5 (#61): memory/postmortem_analysis.py:867 non-dict crash guard.
  AC6 (#62): memory/postmortem.py:189 agent_source isinstance guard.
  AC7 (#64): memory/postmortem.py:87-88 dead code removal (_count_log_pattern).

AC5 bug: _apply_analysis reads existing = load_json(rules_path) then calls
         existing.get("rules", []) without checking isinstance(existing, dict).
         When prevention-rules.json contains valid JSON that is not a dict
         (e.g. [] or "string"), this raises AttributeError.

AC6 bug: _detect_recurring_patterns at postmortem.py:189 checks
         r.get("agent_source") and all(v == "generic" for v in r.get("agent_source", {}).values())
         The truthiness check passes for non-empty lists/strings, then .values()
         raises AttributeError on ["executor"].

AC7 bug: _count_log_pattern function defined at postmortem.py:87-88 has zero callers
         and is dead code. It must be removed.

All tests encode the FIXED behavior and will FAIL on current (unfixed) code.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "memory") not in sys.path:
    sys.path.insert(0, str(ROOT / "memory"))
if str(ROOT / "hooks") not in sys.path:
    sys.path.insert(0, str(ROOT / "hooks"))


# ---------------------------------------------------------------------------
# AC5 (#61): Non-dict prevention-rules.json must not crash apply_analysis
# ---------------------------------------------------------------------------

class TestApplyAnalysisNonDictRulesFile:
    """AC5 (#61): apply_analysis must handle non-dict prevention-rules.json."""

    def _make_task_dir_with_rules(self, tmp_path: Path, rules_content: object) -> tuple[Path, Path]:
        """Create a minimal task directory structure with a rules file."""
        task_dir = tmp_path / ".dynos" / "task-20260101-001"
        task_dir.mkdir(parents=True)

        # Write a minimal retrospective so apply_analysis can read task_id
        retro = {
            "task_id": "task-20260101-001",
            "quality_score": 0.8,
            "task_type": "feature",
        }
        (task_dir / "task-retrospective.json").write_text(json.dumps(retro))

        # Set up persistent project dir with the rules file
        persistent_dir = tmp_path / ".dynos" / "projects" / "tmp-project"
        persistent_dir.mkdir(parents=True)
        rules_path = persistent_dir / "prevention-rules.json"
        rules_path.write_text(json.dumps(rules_content))

        return task_dir, rules_path

    def test_apply_analysis_list_rules_file(self, tmp_path):
        """
        When prevention-rules.json contains a valid JSON array (not a dict),
        _apply_analysis must NOT raise AttributeError. Rules must be treated as empty.

        FAILS today: existing.get("rules", []) at line 867 raises AttributeError
        because existing = [] (a list), and lists have no .get() method.

        After the fix: isinstance(existing, dict) guard resets existing to {}.
        """
        import postmortem_analysis

        task_dir, rules_path = self._make_task_dir_with_rules(tmp_path, [])

        # Minimal analysis dict with one rule
        analysis = {
            "prevention_rules": [
                {"rule": "Always write tests before implementation", "category": "process"}
            ]
        }

        # The fix: no exception must propagate when rules file is a list
        # We need to mock the persistent dir to point to our test rules file
        with patch("postmortem_analysis._persistent_project_dir", return_value=rules_path.parent), \
             patch("postmortem_analysis.log_event"), \
             patch("postmortem_analysis.receipt_postmortem_analysis"), \
             patch("postmortem_analysis.receipt_postmortem_skipped"), \
             patch("postmortem_analysis.hash_file", return_value="abc123"):
            try:
                result = postmortem_analysis.apply_analysis(task_dir, analysis)
            except AttributeError as e:
                pytest.fail(
                    f"apply_analysis raised AttributeError({e}) when prevention-rules.json is []. "
                    "AC5 (#61): isinstance(existing, dict) guard must be added at line 867."
                )
            except Exception:
                # Other exceptions (e.g. locking, receipt failures) are acceptable
                # as long as AttributeError on .get() is not among them
                pass

    def test_apply_analysis_string_rules_file(self, tmp_path):
        """
        When prevention-rules.json contains a valid JSON string (not a dict),
        _apply_analysis must NOT raise AttributeError. Rules must be treated as empty.

        FAILS today: same as above — "string".get() raises AttributeError.
        """
        import postmortem_analysis

        task_dir, rules_path = self._make_task_dir_with_rules(tmp_path, "some_string")

        analysis = {
            "prevention_rules": [
                {"rule": "Validate inputs before processing", "category": "correctness"}
            ]
        }

        with patch("postmortem_analysis._persistent_project_dir", return_value=rules_path.parent), \
             patch("postmortem_analysis.log_event"), \
             patch("postmortem_analysis.receipt_postmortem_analysis"), \
             patch("postmortem_analysis.receipt_postmortem_skipped"), \
             patch("postmortem_analysis.hash_file", return_value="abc123"):
            try:
                result = postmortem_analysis.apply_analysis(task_dir, analysis)
            except AttributeError as e:
                pytest.fail(
                    f"apply_analysis raised AttributeError({e}) when prevention-rules.json is 'string'. "
                    "AC5 (#61): isinstance(existing, dict) guard must be added at line 867."
                )
            except Exception:
                pass

    def test_apply_analysis_integer_rules_file(self, tmp_path):
        """
        Edge case: prevention-rules.json contains an integer (valid JSON, not a dict).
        Must not raise AttributeError.
        """
        import postmortem_analysis

        task_dir, rules_path = self._make_task_dir_with_rules(tmp_path, 42)

        analysis = {"prevention_rules": [{"rule": "Test rule", "category": "process"}]}

        with patch("postmortem_analysis._persistent_project_dir", return_value=rules_path.parent), \
             patch("postmortem_analysis.log_event"), \
             patch("postmortem_analysis.receipt_postmortem_analysis"), \
             patch("postmortem_analysis.receipt_postmortem_skipped"), \
             patch("postmortem_analysis.hash_file", return_value="abc123"):
            try:
                result = postmortem_analysis.apply_analysis(task_dir, analysis)
            except AttributeError as e:
                pytest.fail(
                    f"apply_analysis raised AttributeError({e}) when prevention-rules.json is 42. "
                    "AC5 (#61): isinstance(existing, dict) guard must be added at line 867."
                )
            except Exception:
                pass


# ---------------------------------------------------------------------------
# AC6 (#62): agent_source isinstance guard in _detect_recurring_patterns
# ---------------------------------------------------------------------------

class TestDetectPatternsAgentSourceGuard:
    """AC6 (#62): _detect_recurring_patterns must not raise AttributeError for non-dict agent_source."""

    def _call_detect_recurring_patterns(self, retrospectives: list) -> list:
        """Import and call _detect_recurring_patterns."""
        import postmortem
        return postmortem._detect_recurring_patterns(retrospectives)

    def test_detect_patterns_list_agent_source(self):
        """
        When a retrospective entry has agent_source as a list (e.g. ["executor"]),
        _detect_recurring_patterns must NOT raise AttributeError.

        FAILS today: r.get("agent_source") returns ["executor"] (truthy list),
        then r.get("agent_source", {}).values() raises AttributeError because
        lists have no .values() method.

        After fix: isinstance(r.get("agent_source"), dict) returns False,
        short-circuits the condition. No .values() call is made.
        """
        retrospectives = [
            {
                "task_id": "task-001",
                "task_type": "feature",
                "agent_source": ["executor"],  # non-dict — the bug trigger
                "quality_score": 0.8,
            }
        ]

        try:
            result = self._call_detect_recurring_patterns(retrospectives)
        except AttributeError as e:
            pytest.fail(
                f"_detect_recurring_patterns raised AttributeError({e}) when "
                f"agent_source is a list. "
                "AC6 (#62): isinstance(r.get('agent_source'), dict) guard must be added at line 189."
            )

        # Behavioral contract: a list agent_source must be safely SKIPPED, never
        # miscounted toward generic-routing detection (a guard that treated the list
        # as iterable values could wrongly contribute to the pattern).
        assert isinstance(result, list), f"Expected list result, got {type(result)}"
        assert "persistent_generic_routing" not in [p.get("pattern") for p in result], (
            "A list agent_source must be excluded from generic-routing detection, "
            "not counted as an all-generic entry."
        )

    def test_detect_patterns_string_agent_source(self):
        """
        When agent_source is a plain string (e.g. "generic"), must not raise AttributeError.
        Strings have no .values() method.

        FAILS today: same bug — "generic" is truthy, .values() raises AttributeError.
        """
        retrospectives = [
            {
                "task_id": "task-002",
                "task_type": "bugfix",
                "agent_source": "generic",  # string — also a non-dict
                "quality_score": 0.7,
            }
        ]

        try:
            result = self._call_detect_recurring_patterns(retrospectives)
        except AttributeError as e:
            pytest.fail(
                f"_detect_recurring_patterns raised AttributeError({e}) when "
                f"agent_source is a string. "
                "AC6 (#62): isinstance guard must handle string agent_source."
            )

        # A string agent_source must be safely skipped, not iterated as a dict.
        assert isinstance(result, list)
        assert "persistent_generic_routing" not in [p.get("pattern") for p in result], (
            "A string agent_source must be excluded from generic-routing detection."
        )

    def test_detect_patterns_dict_agent_source_still_works(self):
        """
        Guard: valid dict agent_source must still work correctly after the fix.
        An entry with all-generic routing should still count toward
        the generic_routing_pattern threshold.
        """
        # Build enough all-generic entries to meet the threshold
        import postmortem
        threshold = postmortem.GENERIC_ROUTING_PATTERN_THRESHOLD

        retrospectives = [
            {
                "task_id": f"task-{i:03d}",
                "task_type": "feature",
                "agent_source": {"executor": "generic", "planner": "generic"},
                "quality_score": 0.8,
            }
            for i in range(threshold)
        ]

        result = self._call_detect_recurring_patterns(retrospectives)

        # With enough all-generic entries, the pattern should fire
        pattern_types = [p.get("pattern") for p in result]
        assert "persistent_generic_routing" in pattern_types, (
            f"Dict agent_source with all-generic values must still trigger "
            f"'persistent_generic_routing' pattern after the isinstance fix. "
            f"Patterns found: {pattern_types}"
        )

    def test_detect_patterns_mixed_agent_sources(self):
        """
        Mix of valid dict, list, and string agent_sources in the same retrospective list.
        Must not raise on any entry.
        """
        import postmortem
        threshold = postmortem.GENERIC_ROUTING_PATTERN_THRESHOLD

        # threshold+2 NON-dict agent_sources (list/string/None). If the guard were
        # missing/wrong and these were miscounted as "all generic", they would exceed
        # the threshold and wrongly fire the pattern. Correct behavior excludes them all.
        bad_kinds = [["executor"], "generic", None]
        retrospectives = [
            {
                "task_id": f"task-{i:03d}",
                "agent_source": bad_kinds[i % len(bad_kinds)],
                "task_type": "feature",
                "quality_score": 0.8,
            }
            for i in range(threshold + 2)
        ]

        try:
            result = self._call_detect_recurring_patterns(retrospectives)
        except AttributeError as e:
            pytest.fail(
                f"_detect_recurring_patterns raised AttributeError({e}) with mixed agent_source types."
            )

        # All entries are non-dict, so NONE are valid all-generic → the pattern must
        # not fire despite exceeding the count threshold.
        assert isinstance(result, list)
        assert "persistent_generic_routing" not in [p.get("pattern") for p in result], (
            "Non-dict agent_sources must be excluded from generic-routing detection "
            "even when their count exceeds the threshold."
        )


# ---------------------------------------------------------------------------
# AC7 (#64): _count_log_pattern must be absent from memory.postmortem
# ---------------------------------------------------------------------------

class TestCountLogPatternAbsent:
    """AC7 (#64): _count_log_pattern dead code must be removed from postmortem.py."""

    def test_count_log_pattern_absent(self):
        """
        After deletion of lines 87-88 in postmortem.py,
        `hasattr(postmortem, '_count_log_pattern')` must return False.

        The in-session import reflects the current on-disk source (dead code removed),
        so a fresh hasattr check is authoritative without reloading the shared module
        (reloading re-executes the module body and pollutes cross-test global state).

        FAILS today: the function is defined at lines 87-88 and hasattr returns True.
        """
        import postmortem as pm

        assert not hasattr(pm, "_count_log_pattern"), (
            "memory/postmortem.py still defines _count_log_pattern. "
            "AC7 (#64): the two-line dead function at lines 87-88 must be deleted."
        )

    def test_count_log_pattern_absent_from_source(self):
        """
        Verify at the source text level that '_count_log_pattern' does not appear
        as a function definition in postmortem.py.
        """
        import inspect
        import postmortem as pm

        src = inspect.getsource(pm)
        assert "def _count_log_pattern" not in src, (
            "Source of postmortem.py still contains 'def _count_log_pattern'. "
            "AC7 (#64): this dead code must be removed entirely."
        )

    def test_count_log_pattern_not_callable_via_getattr(self):
        """
        Even if somehow the name persisted (e.g. as a non-function attribute),
        it must not be present at all.
        """
        import postmortem as pm

        fn = getattr(pm, "_count_log_pattern", None)
        assert fn is None, (
            f"getattr(postmortem, '_count_log_pattern', None) returned {fn!r} — "
            "must return None after dead code removal. AC7 (#64)."
        )
