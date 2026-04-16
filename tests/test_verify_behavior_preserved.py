"""Tests for hooks/verify_behavior_preserved.py.

Strategy:
  - Pure-function tests for parse_pytest, diff_runs, detect_framework,
    render_human / render_json — these run fast and need no git state.
  - End-to-end test creates a tiny throwaway git repo with two commits and
    runs verify against it. Slower but real.

We mock subprocess for the worktree + test-runner integration paths because
running pytest inside pytest is tricky and slow.
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Make hooks/ importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

import verify_behavior_preserved as vbp  # noqa: E402


# ---------------------------------------------------------------------------
# parse_pytest
# ---------------------------------------------------------------------------

class TestParsePytest:
    def test_extracts_passing_and_failing(self) -> None:
        out = textwrap.dedent("""
            tests/test_a.py::test_one PASSED                                 [ 33%]
            tests/test_a.py::test_two FAILED                                 [ 66%]
            tests/test_b.py::TestX::test_three PASSED                        [100%]
        """)
        passing, failing, skipped = vbp.parse_pytest(out)
        assert passing == {"tests/test_a.py::test_one", "tests/test_b.py::TestX::test_three"}
        assert failing == {"tests/test_a.py::test_two"}
        assert skipped == set()

    def test_handles_skipped(self) -> None:
        out = "tests/test_a.py::test_x SKIPPED                              [100%]\n"
        passing, failing, skipped = vbp.parse_pytest(out)
        assert skipped == {"tests/test_a.py::test_x"}
        assert passing == set()
        assert failing == set()

    def test_xfail_treated_as_failing(self) -> None:
        out = "tests/test_a.py::test_x XFAIL                                 [100%]\n"
        passing, failing, _ = vbp.parse_pytest(out)
        assert failing == {"tests/test_a.py::test_x"}

    def test_empty_output(self) -> None:
        passing, failing, skipped = vbp.parse_pytest("")
        assert passing == set() and failing == set() and skipped == set()


# ---------------------------------------------------------------------------
# diff_runs
# ---------------------------------------------------------------------------

class TestDiffRuns:
    def _make(self, ref: str, passing: set[str], failing: set[str] = None) -> vbp.RunResult:
        return vbp.RunResult(
            ref=ref, framework="pytest",
            passing=passing, failing=failing or set(), skipped=set(),
            error=None,
        )

    def test_no_regressions_when_same_tests_pass(self) -> None:
        before = self._make("HEAD~1", {"a", "b", "c"})
        after = self._make("HEAD", {"a", "b", "c"})
        report = vbp.diff_runs(before, after)
        assert report.regressions == []
        assert report.new_passes == []

    def test_test_now_failing_is_regression(self) -> None:
        before = self._make("HEAD~1", {"a", "b", "c"})
        after = self._make("HEAD", {"a", "b"}, failing={"c"})
        report = vbp.diff_runs(before, after)
        assert "c" in report.regressions

    def test_test_no_longer_present_is_regression(self) -> None:
        """A test that passed before but doesn't appear in after results — could
        be deleted, renamed, or filtered out — is still a regression from the
        contract perspective: that test no longer protects us."""
        before = self._make("HEAD~1", {"a", "b", "c"})
        after = self._make("HEAD", {"a", "b"})
        report = vbp.diff_runs(before, after)
        assert "c" in report.regressions
        assert "c" in report.no_longer_run

    def test_new_test_passing_is_informational(self) -> None:
        before = self._make("HEAD~1", {"a", "b"})
        after = self._make("HEAD", {"a", "b", "new_test"})
        report = vbp.diff_runs(before, after)
        assert report.regressions == []
        assert "new_test" in report.new_passes

    def test_test_was_failing_now_passing_not_a_regression(self) -> None:
        before = self._make("HEAD~1", {"a"}, failing={"b"})
        after = self._make("HEAD", {"a", "b"})
        report = vbp.diff_runs(before, after)
        assert report.regressions == []
        assert "b" in report.new_passes


# ---------------------------------------------------------------------------
# detect_framework
# ---------------------------------------------------------------------------

class TestDetectFramework:
    def test_pytest_via_pytest_ini(self, tmp_path: Path) -> None:
        (tmp_path / "pytest.ini").write_text("[pytest]\n")
        fw = vbp.detect_framework(tmp_path)
        assert fw is not None and fw.name == "pytest"

    def test_pytest_via_pyproject_toml(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[tool.pytest.ini_options]\nminversion = \"7.0\"\n"
        )
        fw = vbp.detect_framework(tmp_path)
        assert fw is not None and fw.name == "pytest"

    def test_pytest_via_tests_dir_fallback(self, tmp_path: Path) -> None:
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_foo.py").write_text("def test_x(): pass\n")
        fw = vbp.detect_framework(tmp_path)
        assert fw is not None and fw.name == "pytest"

    def test_npm_via_package_json(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(
            json.dumps({"scripts": {"test": "jest"}})
        )
        fw = vbp.detect_framework(tmp_path)
        assert fw is not None and fw.name == "npm"

    def test_cargo(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text("[package]\nname = \"x\"\n")
        fw = vbp.detect_framework(tmp_path)
        assert fw is not None and fw.name == "cargo"

    def test_go(self, tmp_path: Path) -> None:
        (tmp_path / "go.mod").write_text("module example.com/foo\n")
        fw = vbp.detect_framework(tmp_path)
        assert fw is not None and fw.name == "go"

    def test_no_framework_returns_none(self, tmp_path: Path) -> None:
        fw = vbp.detect_framework(tmp_path)
        assert fw is None


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

class TestRendering:
    def _report(self, regressions: list[str] = None, new_passes: list[str] = None) -> vbp.DiffReport:
        return vbp.DiffReport(
            before_ref="HEAD~1", after_ref="HEAD", framework="pytest",
            before_passing=10, after_passing=10,
            regressions=regressions or [],
            new_passes=new_passes or [],
            no_longer_run=[],
        )

    def test_human_no_regressions(self) -> None:
        out = vbp.render_human(self._report(),
                               vbp.RunResult("HEAD~1", "pytest", set(), set(), set(), None),
                               vbp.RunResult("HEAD", "pytest", set(), set(), set(), None))
        assert "No regressions" in out

    def test_human_lists_regressions(self) -> None:
        out = vbp.render_human(
            self._report(regressions=["tests/test_a.py::test_x"]),
            vbp.RunResult("HEAD~1", "pytest", set(), set(), set(), None),
            vbp.RunResult("HEAD", "pytest", set(), set(), set(), None),
        )
        assert "REGRESSIONS" in out
        assert "tests/test_a.py::test_x" in out

    def test_json_parseable(self) -> None:
        out = vbp.render_json(self._report(regressions=["x", "y"]))
        parsed = json.loads(out)
        assert parsed["regressions"] == ["x", "y"]
        assert parsed["framework"] == "pytest"


# ---------------------------------------------------------------------------
# CLI integration (mocked git + test runner)
# ---------------------------------------------------------------------------

class TestCli:
    def test_missing_git_repo_returns_two(self, tmp_path: Path) -> None:
        # tmp_path has no .git
        with patch("sys.argv", [
            "verify_behavior_preserved.py",
            "--before", "HEAD~1", "--after", "HEAD",
            "--repo", str(tmp_path),
        ]):
            code = vbp.main()
        assert code == 2

    def test_invalid_ref_returns_two(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Initialize a real git repo (so .git exists), but reference a missing ref
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
        (tmp_path / "x").write_text("x")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
        with patch("sys.argv", [
            "verify_behavior_preserved.py",
            "--before", "definitely_not_a_ref", "--after", "HEAD",
            "--repo", str(tmp_path),
        ]):
            code = vbp.main()
        assert code == 2


# ---------------------------------------------------------------------------
# End-to-end: tiny real repo with a regression
# ---------------------------------------------------------------------------

class TestEndToEnd:
    def _init_repo(self, tmp_path: Path) -> Path:
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
        return repo

    def _commit_all(self, repo: Path, msg: str) -> None:
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", msg], cwd=repo, check=True)

    def test_no_regression_when_unchanged(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = self._init_repo(tmp_path)
        (repo / "tests").mkdir()
        (repo / "tests" / "test_x.py").write_text(
            "def test_one():\n    assert 1 + 1 == 2\n"
            "def test_two():\n    assert True\n"
        )
        self._commit_all(repo, "init")
        # Second commit: docs change only, behavior preserved
        (repo / "README.md").write_text("hello\n")
        self._commit_all(repo, "docs")
        with patch("sys.argv", [
            "verify_behavior_preserved.py",
            "--before", "HEAD~1", "--after", "HEAD",
            "--repo", str(repo), "--json",
        ]):
            code = vbp.main()
        out = capsys.readouterr().out
        report = json.loads(out)
        assert report["regressions"] == []
        assert code == 0

    def test_regression_caught(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = self._init_repo(tmp_path)
        (repo / "tests").mkdir()
        # First commit: passing test
        (repo / "tests" / "test_x.py").write_text(
            "def test_one():\n    assert 1 + 1 == 2\n"
        )
        self._commit_all(repo, "init")
        # Second commit: break the test
        (repo / "tests" / "test_x.py").write_text(
            "def test_one():\n    assert 1 + 1 == 3\n"
        )
        self._commit_all(repo, "regression!")
        with patch("sys.argv", [
            "verify_behavior_preserved.py",
            "--before", "HEAD~1", "--after", "HEAD",
            "--repo", str(repo), "--json",
        ]):
            code = vbp.main()
        out = capsys.readouterr().out
        report = json.loads(out)
        assert report["regressions"]
        assert any("test_one" in r for r in report["regressions"])
        assert code == 1
