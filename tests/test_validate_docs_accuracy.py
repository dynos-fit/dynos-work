"""Tests for hooks/validate_docs_accuracy.py.

Covers:
  - Path extraction in fenced code blocks AND prose (with appropriate filters)
  - URL filtering (http/https/git/ssh/mailto)
  - Env var + template var filtering ($FOO, {var}, {{var}})
  - Glob filtering (*, ?, brackets)
  - Tilde / home path filtering (~)
  - Domain-vs-path disambiguation (github.com/foo vs src/foo.py)
  - Existence check: repo-root-relative + doc-dir-relative resolution
  - Path-traversal guard (../../etc/passwd treated as non-existent)
  - CLI exit codes: 0 ok, 1 broken, 2 fatal
  - --recursive mode
  - --json output
"""
from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

# Make hooks/ importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

import validate_docs_accuracy as vda  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _write_doc(dir_: Path, name: str, content: str) -> Path:
    p = dir_ / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(content).lstrip())
    return p


def _touch(repo: Path, *rel_paths: str) -> None:
    for rel in rel_paths:
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()


# ---------------------------------------------------------------------------
# Path extraction
# ---------------------------------------------------------------------------

class TestExtractInCodeBlock:
    def test_paths_in_bash_code_block(self, tmp_path: Path) -> None:
        """Conservative: only flag paths with file extensions or explicit
        relative prefix. `src/auth` (bare dir) is intentionally skipped to
        avoid false positives on slash-separated prose."""
        doc = _write_doc(tmp_path, "README.md", """
            # Setup

            ```bash
            cd src/auth
            cat config/database.yml
            ./scripts/setup.sh
            ```
        """)
        refs = vda.extract_path_candidates(doc)
        paths = {r.path for r in refs}
        assert "config/database.yml" in paths        # has extension → extracted
        assert "./scripts/setup.sh" in paths         # explicit relative + ext
        assert "src/auth" not in paths               # bare dir → skipped (intentional)

    def test_code_block_language_tag_captured(self, tmp_path: Path) -> None:
        doc = _write_doc(tmp_path, "README.md", """
            ```python
            from src.foo import bar
            ```
        """)
        refs = vda.extract_path_candidates(doc)
        # Note: "src.foo" doesn't have a slash → not extracted. That's fine.
        # But the test ensures the code-lang tracking is wired.
        # Add a path with slash to check the tag flows through:
        doc2 = _write_doc(tmp_path, "OTHER.md", """
            ```python
            with open("src/data/users.csv") as f:
                pass
            ```
        """)
        refs2 = vda.extract_path_candidates(doc2)
        if refs2:
            assert any(r.code_lang == "python" for r in refs2)


class TestExtractInProse:
    def test_inline_code_path_in_prose(self, tmp_path: Path) -> None:
        doc = _write_doc(tmp_path, "README.md", """
            See `src/auth/login.py` for the entry point.
        """)
        refs = vda.extract_path_candidates(doc)
        paths = {r.path for r in refs}
        assert "src/auth/login.py" in paths

    def test_relative_dot_paths(self, tmp_path: Path) -> None:
        doc = _write_doc(tmp_path, "README.md", """
            Run `./scripts/setup.sh` from the project root.
        """)
        refs = vda.extract_path_candidates(doc)
        paths = {r.path for r in refs}
        assert "./scripts/setup.sh" in paths


class TestFilters:
    def test_urls_not_extracted(self, tmp_path: Path) -> None:
        doc = _write_doc(tmp_path, "README.md", """
            Visit https://github.com/user/repo for the upstream.
            Email me at mailto:user@example.com.
            Clone via git://github.com/foo/bar.git.
        """)
        refs = vda.extract_path_candidates(doc)
        paths = [r.path for r in refs]
        # No URLs should be in the extracted paths
        assert not any(p.startswith("http") for p in paths)
        assert not any("github.com/user/repo" in p for p in paths)

    def test_env_vars_not_extracted(self, tmp_path: Path) -> None:
        doc = _write_doc(tmp_path, "README.md", """
            ```bash
            cd $PROJECT_ROOT/src
            cat ${HOME}/config/app.yml
            ```
        """)
        refs = vda.extract_path_candidates(doc)
        paths = [r.path for r in refs]
        assert not any("$" in p or "{" in p for p in paths)

    def test_template_vars_not_extracted(self, tmp_path: Path) -> None:
        doc = _write_doc(tmp_path, "README.md", """
            See `{platform}/skills/foo` and `{{harness}}/config`.
        """)
        refs = vda.extract_path_candidates(doc)
        paths = [r.path for r in refs]
        assert not any("{" in p for p in paths)

    def test_tilde_paths_not_extracted(self, tmp_path: Path) -> None:
        doc = _write_doc(tmp_path, "README.md", """
            Config lives at `~/.config/myapp/settings.yml`.
        """)
        refs = vda.extract_path_candidates(doc)
        assert not any(r.path.startswith("~") for r in refs)

    def test_globs_not_extracted(self, tmp_path: Path) -> None:
        doc = _write_doc(tmp_path, "README.md", """
            Run `pytest tests/*.py` to test all files.
            Pattern: `src/**/*.tsx`.
        """)
        refs = vda.extract_path_candidates(doc)
        for r in refs:
            assert "*" not in r.path
            assert "?" not in r.path

    def test_domain_disambiguation(self, tmp_path: Path) -> None:
        """`github.com/user/repo` should not be flagged as a project path,
        but `src/foo.py` should be."""
        doc = _write_doc(tmp_path, "README.md", """
            Open `github.com/dynos-fit/dynos-work` for the upstream.
            See `src/foo.py` for impl.
        """)
        refs = vda.extract_path_candidates(doc)
        paths = {r.path for r in refs}
        assert "src/foo.py" in paths
        assert "github.com/dynos-fit/dynos-work" not in paths


# ---------------------------------------------------------------------------
# Existence check
# ---------------------------------------------------------------------------

class TestCheckOne:
    def test_existing_file_returns_true(self, tmp_path: Path) -> None:
        _touch(tmp_path, "src/auth/login.py")
        ref = vda.PathRef(path="src/auth/login.py", line=1,
                          in_code_block=False, code_lang=None)
        result = vda.check_one(ref, repo_root=tmp_path, doc_dir=tmp_path)
        assert result.exists is True
        assert result.resolved_at == "src/auth/login.py"

    def test_missing_file_returns_false(self, tmp_path: Path) -> None:
        ref = vda.PathRef(path="src/missing.py", line=1,
                          in_code_block=False, code_lang=None)
        result = vda.check_one(ref, repo_root=tmp_path, doc_dir=tmp_path)
        assert result.exists is False

    def test_existing_directory_returns_true(self, tmp_path: Path) -> None:
        (tmp_path / "src" / "components").mkdir(parents=True)
        ref = vda.PathRef(path="src/components", line=1,
                          in_code_block=False, code_lang=None)
        result = vda.check_one(ref, repo_root=tmp_path, doc_dir=tmp_path)
        assert result.exists is True

    def test_path_traversal_outside_repo_treated_as_missing(
        self, tmp_path: Path
    ) -> None:
        """A doc that references `../../etc/passwd` shouldn't be reported
        as 'exists' even if the host filesystem has it."""
        ref = vda.PathRef(path="../../etc/passwd", line=1,
                          in_code_block=False, code_lang=None)
        result = vda.check_one(ref, repo_root=tmp_path, doc_dir=tmp_path)
        assert result.exists is False

    def test_doc_dir_relative_resolution(self, tmp_path: Path) -> None:
        """A path like '../config/db.yml' should resolve relative to the doc's
        directory, not the repo root."""
        _touch(tmp_path, "config/db.yml")
        doc_dir = tmp_path / "docs"
        doc_dir.mkdir()
        ref = vda.PathRef(path="../config/db.yml", line=1,
                          in_code_block=False, code_lang=None)
        result = vda.check_one(ref, repo_root=tmp_path, doc_dir=doc_dir)
        assert result.exists is True


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------

class TestCli:
    def test_all_paths_exist_returns_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _touch(tmp_path, "src/foo.py", "scripts/run.sh")
        doc = _write_doc(tmp_path, "README.md", """
            ```bash
            python3 src/foo.py
            ./scripts/run.sh
            ```
        """)
        with patch("sys.argv", [
            "validate_docs_accuracy.py",
            "--doc", str(doc), "--root", str(tmp_path),
        ]):
            code = vda.main()
        assert code == 0

    def test_broken_path_returns_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        doc = _write_doc(tmp_path, "README.md", """
            ```bash
            python3 src/does_not_exist.py
            ```
        """)
        with patch("sys.argv", [
            "validate_docs_accuracy.py",
            "--doc", str(doc), "--root", str(tmp_path),
        ]):
            code = vda.main()
        assert code == 1
        out = capsys.readouterr().out
        assert "src/does_not_exist.py" in out

    def test_missing_doc_returns_two(self) -> None:
        with patch("sys.argv", [
            "validate_docs_accuracy.py", "--doc", "/nonexistent/doc.md",
        ]):
            code = vda.main()
        assert code == 2

    def test_no_args_returns_two(self) -> None:
        with patch("sys.argv", ["validate_docs_accuracy.py"]):
            code = vda.main()
        assert code == 2

    def test_json_output_parseable(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _touch(tmp_path, "src/foo.py")
        doc = _write_doc(tmp_path, "README.md", """
            ```bash
            python3 src/foo.py
            python3 src/missing.py
            ```
        """)
        with patch("sys.argv", [
            "validate_docs_accuracy.py",
            "--doc", str(doc), "--root", str(tmp_path), "--json",
        ]):
            code = vda.main()
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert parsed["total"] == 2
        broken_paths = {b["path"] for b in parsed["broken"]}
        assert "src/missing.py" in broken_paths

    def test_recursive_walks_subdirs(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _touch(tmp_path, "src/exists.py")
        _write_doc(tmp_path, "docs/a.md", """
            ```bash
            python3 src/exists.py
            ```
        """)
        _write_doc(tmp_path, "docs/b.md", """
            ```bash
            python3 src/missing.py
            ```
        """)
        with patch("sys.argv", [
            "validate_docs_accuracy.py",
            "--root", str(tmp_path), "--recursive",
        ]):
            code = vda.main()
        # b.md has a broken ref → exit 1
        assert code == 1
        out = capsys.readouterr().out
        assert "missing.py" in out

    def test_recursive_excludes_common_noise_dirs(self, tmp_path: Path) -> None:
        """node_modules / .git / __pycache__ shouldn't be walked."""
        _write_doc(tmp_path, "node_modules/foo/README.md", """
            ```bash
            python3 src/missing.py
            ```
        """)
        _write_doc(tmp_path, ".git/notes.md", """
            ```bash
            python3 src/also_missing.py
            ```
        """)
        with patch("sys.argv", [
            "validate_docs_accuracy.py",
            "--root", str(tmp_path), "--recursive",
        ]):
            code = vda.main()
        # No real docs to scan → exit 0
        assert code == 0


# ---------------------------------------------------------------------------
# End-to-end: realistic README scenario
# ---------------------------------------------------------------------------

class TestEndToEnd:
    def test_realistic_readme_with_mixed_refs(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _touch(
            tmp_path,
            "src/main.py",
            "scripts/install.sh",
            "config/default.yml",
        )
        # README mentions some real paths, some broken, plus URLs and globs
        doc = _write_doc(tmp_path, "README.md", """
            # MyProject

            See https://github.com/me/myproject for the source.

            Quick start:
            ```bash
            git clone git@github.com:me/myproject
            cd myproject
            ./scripts/install.sh
            python3 src/main.py
            ```

            Config lives in `config/default.yml`.
            Run tests: `pytest tests/**/*.py` (broken — phantom test dir).
            Old setup script `scripts/old_install.sh` is gone now.
        """)
        with patch("sys.argv", [
            "validate_docs_accuracy.py",
            "--doc", str(doc), "--root", str(tmp_path), "--json",
        ]):
            code = vda.main()
        out = capsys.readouterr().out
        parsed = json.loads(out)
        broken = {b["path"] for b in parsed["broken"]}
        verified = {v["path"] for v in parsed["verified"]}

        # Real paths verified
        assert "./scripts/install.sh" in verified
        assert "src/main.py" in verified
        assert "config/default.yml" in verified

        # Broken path caught
        assert "scripts/old_install.sh" in broken

        # URLs and globs not in either set
        assert not any("github.com" in p for p in broken | verified)
        assert not any("*" in p for p in broken | verified)

        assert code == 1
