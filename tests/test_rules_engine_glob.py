"""Tests for _glob_to_regex and _glob_match in hooks/rules_engine.py.

Covers AC-6 through AC-19 from task-20260508-004.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "hooks"))

from rules_engine import _glob_match, _glob_to_regex  # noqa: E402


class TestGlobToRegexPositive:
    """AC-6, 7, 8, 11, 12: patterns with ** should match recursively."""

    def test_ac6_zero_intermediate_dirs(self):
        # AC-6: tests/**/*.py matches tests/foo.py (zero intermediate dirs)
        assert _glob_to_regex("tests/**/*.py").fullmatch("tests/foo.py")

    def test_ac7_one_intermediate_dir(self):
        # AC-7: tests/**/*.py matches tests/sub/foo.py
        assert _glob_to_regex("tests/**/*.py").fullmatch("tests/sub/foo.py")

    def test_ac8_three_intermediate_dirs(self):
        # AC-8: tests/**/*.py matches tests/a/b/c/foo.py
        assert _glob_to_regex("tests/**/*.py").fullmatch("tests/a/b/c/foo.py")

    def test_ac11_hooks_zero_intermediate_dirs(self):
        # AC-11: hooks/**/*.py matches hooks/lib_X.py
        assert _glob_to_regex("hooks/**/*.py").fullmatch("hooks/lib_X.py")

    def test_ac12_hooks_one_intermediate_dir(self):
        # AC-12: hooks/**/*.py matches hooks/sub/lib_X.py
        assert _glob_to_regex("hooks/**/*.py").fullmatch("hooks/sub/lib_X.py")


class TestGlobToRegexNegative:
    """AC-9, 10, 15: patterns must not match wrong extensions or wrong roots."""

    def test_ac9_wrong_extension(self):
        # AC-9: tests/**/*.py must not match tests/foo.txt
        assert _glob_to_regex("tests/**/*.py").fullmatch("tests/foo.txt") is None

    def test_ac10_wrong_root(self):
        # AC-10: tests/**/*.py must not match src/foo.py
        assert _glob_to_regex("tests/**/*.py").fullmatch("src/foo.py") is None

    def test_ac15_literal_dot_escaped(self):
        # AC-15: tests/foo.py must not match tests/fooXpy (dot is literal)
        assert _glob_to_regex("tests/foo.py").fullmatch("tests/fooXpy") is None


class TestGlobToRegexEdgeCases:
    """AC-13, 14: ? wildcard and empty pattern."""

    def test_ac13_question_mark_matches_non_slash(self):
        # AC-13: ?oo.py matches foo.py
        assert _glob_to_regex("?oo.py").fullmatch("foo.py")

    def test_ac13_question_mark_does_not_match_slash(self):
        # AC-13: ?oo.py must not match /oo.py (? should not match /)
        assert _glob_to_regex("?oo.py").fullmatch("/oo.py") is None

    def test_ac14_empty_pattern_matches_empty_string(self):
        # AC-14: empty pattern matches empty string
        assert _glob_to_regex("").fullmatch("")

    def test_ac14_empty_pattern_does_not_match_nonempty(self):
        # AC-14: empty pattern must not match "a"
        assert _glob_to_regex("").fullmatch("a") is None


class TestGlobToRegexMemoization:
    """AC-16: same pattern returns identical compiled re.Pattern via identity."""

    def test_ac16_same_object_returned(self):
        p1 = _glob_to_regex("tests/**/*.py")
        p2 = _glob_to_regex("tests/**/*.py")
        assert p1 is p2


class TestGlobMatchEndToEnd:
    """AC-19: exercises _glob_match end-to-end with pathlib.Path objects."""

    def _make_file(self, rel_path: str, root: Path) -> Path:
        """Return a synthetic Path without requiring the file to exist."""
        return root / rel_path

    def test_relative_path_match(self, tmp_path):
        # ** glob matches nested file via relative path branch
        root = tmp_path
        file = self._make_file("tests/sub/foo.py", root)
        assert _glob_match(file, root, "tests/**/*.py")

    def test_relative_path_no_match(self, tmp_path):
        # Wrong extension does not match
        root = tmp_path
        file = self._make_file("tests/sub/foo.txt", root)
        assert not _glob_match(file, root, "tests/**/*.py")

    def test_basename_fallback(self, tmp_path):
        # Simple pattern like "*.py" should match via the basename fallback
        # (fnmatch on file.name). The relative path "tests/sub/foo.py" would
        # not fullmatch "*.py" but the basename "foo.py" should.
        root = tmp_path
        file = self._make_file("tests/sub/foo.py", root)
        assert _glob_match(file, root, "*.py")

    def test_basename_fallback_negative(self, tmp_path):
        # "*.txt" should not match a .py file via either branch
        root = tmp_path
        file = self._make_file("tests/sub/foo.py", root)
        assert not _glob_match(file, root, "*.txt")
