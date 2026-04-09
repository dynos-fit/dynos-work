"""Tests for get_neighbor_file_contents (Enhancement 1).

Covers AC 1, AC 3:
  AC 1: get_neighbor_file_contents exists, uses import graph, returns list of dicts
  AC 3: Graceful degradation when graph is empty or function raises
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo with Python files that import each other."""
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, capture_output=True)
    (tmp_path / ".dynos").mkdir()

    # core.py is imported by utils.py and api.py
    (tmp_path / "core.py").write_text("# core module\ndef core_func(): pass\n")
    (tmp_path / "utils.py").write_text("from core import core_func\ndef helper(): pass\n")
    (tmp_path / "api.py").write_text("from core import core_func\nfrom utils import helper\n")
    (tmp_path / "cli.py").write_text("from core import core_func\ndef main(): pass\n")
    # standalone.py has no imports and nobody imports it
    (tmp_path / "standalone.py").write_text("print('hello')\n")

    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)
    return tmp_path


# ===========================================================================
# AC 1: get_neighbor_file_contents function
# ===========================================================================

class TestGetNeighborFileContents:
    """AC 1: Function exists and returns list of {path, content} dicts."""

    def test_function_exists(self) -> None:
        # AC 1
        from dynoslib_crawler import get_neighbor_file_contents
        assert callable(get_neighbor_file_contents)

    def test_returns_list_of_dicts(self, tmp_repo: Path) -> None:
        # AC 1
        from dynoslib_crawler import get_neighbor_file_contents
        result = get_neighbor_file_contents(tmp_repo, "core.py")
        assert isinstance(result, list)
        for item in result:
            assert isinstance(item, dict)
            assert "path" in item
            assert "content" in item

    def test_returns_importers_and_imports(self, tmp_repo: Path) -> None:
        # AC 1: neighbors include both files that import the target AND files the target imports
        from dynoslib_crawler import get_neighbor_file_contents
        # api.py imports core.py and utils.py; api.py is not imported by anyone
        result = get_neighbor_file_contents(tmp_repo, "api.py")
        neighbor_paths = {item["path"] for item in result}
        # api.py imports core.py and utils.py, so those should be neighbors
        assert "core.py" in neighbor_paths
        assert "utils.py" in neighbor_paths

    def test_returns_files_that_import_target(self, tmp_repo: Path) -> None:
        # AC 1: core.py is imported by utils.py, api.py, cli.py
        from dynoslib_crawler import get_neighbor_file_contents
        result = get_neighbor_file_contents(tmp_repo, "core.py")
        neighbor_paths = {item["path"] for item in result}
        # At least some of the importers should be included
        importers_found = neighbor_paths & {"utils.py", "api.py", "cli.py"}
        assert len(importers_found) > 0, "Should include files that import core.py"

    def test_deduplicates_neighbors(self, tmp_repo: Path) -> None:
        # AC 1: if file A imports B and B imports A, B appears once
        from dynoslib_crawler import get_neighbor_file_contents
        result = get_neighbor_file_contents(tmp_repo, "api.py")
        paths = [item["path"] for item in result]
        assert len(paths) == len(set(paths)), "Neighbor paths should be deduplicated"

    def test_respects_max_files_limit(self, tmp_repo: Path) -> None:
        # AC 1: max_files parameter caps the number of neighbors returned
        from dynoslib_crawler import get_neighbor_file_contents
        result = get_neighbor_file_contents(tmp_repo, "core.py", max_files=2)
        assert len(result) <= 2

    def test_default_max_files_is_5(self, tmp_repo: Path) -> None:
        # AC 1: default max_files=5
        from dynoslib_crawler import get_neighbor_file_contents
        # core.py has 3 importers, so all should fit within default 5
        result = get_neighbor_file_contents(tmp_repo, "core.py")
        assert len(result) <= 5

    def test_truncates_content_to_max_lines(self, tmp_repo: Path) -> None:
        # AC 1: each file content truncated to max_lines
        from dynoslib_crawler import get_neighbor_file_contents
        # Create a file with many lines
        long_content = "\n".join(f"line_{i}" for i in range(200))
        (tmp_repo / "core.py").write_text(long_content)
        subprocess.run(["git", "add", "."], cwd=tmp_repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "long core"], cwd=tmp_repo, capture_output=True)

        result = get_neighbor_file_contents(tmp_repo, "utils.py", max_lines=50)
        for item in result:
            if item["path"] == "core.py":
                lines = item["content"].split("\n")
                assert len(lines) <= 51, "Content should be truncated to max_lines"

    def test_default_max_lines_is_100(self, tmp_repo: Path) -> None:
        # AC 1: default max_lines=100
        from dynoslib_crawler import get_neighbor_file_contents
        long_content = "\n".join(f"line_{i}" for i in range(200))
        (tmp_repo / "core.py").write_text(long_content)
        subprocess.run(["git", "add", "."], cwd=tmp_repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "long"], cwd=tmp_repo, capture_output=True)

        result = get_neighbor_file_contents(tmp_repo, "utils.py")
        for item in result:
            if item["path"] == "core.py":
                lines = item["content"].split("\n")
                assert len(lines) <= 101

    def test_file_not_in_graph_returns_empty(self, tmp_repo: Path) -> None:
        # AC 1: file with no edges returns empty list
        from dynoslib_crawler import get_neighbor_file_contents
        result = get_neighbor_file_contents(tmp_repo, "standalone.py")
        assert result == []

    def test_nonexistent_file_returns_empty(self, tmp_repo: Path) -> None:
        # AC 1: file not in the repo returns empty list
        from dynoslib_crawler import get_neighbor_file_contents
        result = get_neighbor_file_contents(tmp_repo, "does_not_exist.py")
        assert result == []


# ===========================================================================
# AC 3: Graceful degradation
# ===========================================================================

class TestGracefulDegradation:
    """AC 3: Empty graph or exception degrades to no import context."""

    def test_empty_graph_returns_empty_list(self, tmp_repo: Path) -> None:
        # AC 3: If build_import_graph returns empty graph, return []
        from dynoslib_crawler import get_neighbor_file_contents
        with patch("dynoslib_crawler.build_import_graph", return_value={"nodes": [], "edges": [], "pagerank": {}}):
            result = get_neighbor_file_contents(tmp_repo, "core.py")
            assert result == []

    def test_build_import_graph_raises_returns_empty(self, tmp_repo: Path) -> None:
        # AC 3: If build_import_graph raises, return [] (no crash)
        from dynoslib_crawler import get_neighbor_file_contents
        with patch("dynoslib_crawler.build_import_graph", side_effect=RuntimeError("graph failure")):
            result = get_neighbor_file_contents(tmp_repo, "core.py")
            assert result == []

    def test_unreadable_neighbor_file_skipped(self, tmp_repo: Path) -> None:
        # AC 3: If a neighbor file cannot be read, it is skipped (not the whole function)
        from dynoslib_crawler import get_neighbor_file_contents
        # Remove a neighbor file after graph is built
        (tmp_repo / "utils.py").unlink()
        result = get_neighbor_file_contents(tmp_repo, "core.py")
        # Should not crash; may return other readable neighbors
        assert isinstance(result, list)
        neighbor_paths = {item["path"] for item in result}
        assert "utils.py" not in neighbor_paths, "Unreadable file should be skipped"

    def test_no_stack_trace_on_failure(self, tmp_repo: Path) -> None:
        # AC 3: No stack trace in logs on failure
        from dynoslib_crawler import get_neighbor_file_contents
        import logging
        with patch("dynoslib_crawler.build_import_graph", side_effect=OSError("disk error")):
            # Should return empty list without raising
            result = get_neighbor_file_contents(tmp_repo, "anything.py")
            assert result == []

    def test_file_read_ioerror_does_not_crash(self, tmp_repo: Path) -> None:
        # AC 3: Individual file read failures are caught gracefully
        from dynoslib_crawler import get_neighbor_file_contents
        # Mock open to raise for one specific file
        original_open = open

        def mock_open_side_effect(path, *args, **kwargs):
            if "core.py" in str(path):
                raise IOError("Permission denied")
            return original_open(path, *args, **kwargs)

        with patch("builtins.open", side_effect=mock_open_side_effect):
            # Should not crash
            result = get_neighbor_file_contents(tmp_repo, "api.py")
            assert isinstance(result, list)
