"""Tests for the crawl CLI subcommand in dynosctl.py.

Covers AC 16: crawl graph and crawl targets sub-subcommands.
"""
from __future__ import annotations

import json
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
    """Create a minimal git repo."""
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, capture_output=True)
    (tmp_path / ".dynos").mkdir()
    (tmp_path / "main.py").write_text("import os\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)
    return tmp_path


# ===========================================================================
# AC 16: crawl CLI subcommand
# ===========================================================================

class TestCrawlSubcommand:
    """AC 16: dynosctl.py gains a crawl subcommand with graph and targets sub-subcommands."""

    def test_parser_has_crawl_subcommand(self) -> None:
        # AC 16
        from dynosctl import build_parser

        parser = build_parser()
        # The parser should accept 'crawl' as a valid command
        # Parse a minimal crawl command to verify it exists
        args = parser.parse_args(["crawl", "graph", "--root", "."])
        assert args.command == "crawl"

    def test_crawl_graph_subcommand_accepted(self) -> None:
        # AC 16: crawl graph --root <path>
        from dynosctl import build_parser

        parser = build_parser()
        args = parser.parse_args(["crawl", "graph", "--root", "/tmp/myrepo"])
        assert hasattr(args, "root")
        assert args.root == "/tmp/myrepo"

    def test_crawl_targets_subcommand_accepted(self) -> None:
        # AC 16: crawl targets --root <path> --max <N>
        from dynosctl import build_parser

        parser = build_parser()
        args = parser.parse_args(["crawl", "targets", "--root", "/tmp/myrepo", "--max", "5"])
        assert hasattr(args, "root")
        assert args.root == "/tmp/myrepo"

    def test_crawl_targets_max_argument(self) -> None:
        # AC 16: --max parameter for targets
        from dynosctl import build_parser

        parser = build_parser()
        args = parser.parse_args(["crawl", "targets", "--root", ".", "--max", "15"])
        assert int(args.max) == 15

    def test_crawl_graph_has_func_handler(self) -> None:
        # AC 16: graph subcommand should have a function handler
        from dynosctl import build_parser

        parser = build_parser()
        args = parser.parse_args(["crawl", "graph", "--root", "."])
        assert hasattr(args, "func"), "crawl graph should have a func handler"

    def test_crawl_targets_has_func_handler(self) -> None:
        # AC 16: targets subcommand should have a function handler
        from dynosctl import build_parser

        parser = build_parser()
        args = parser.parse_args(["crawl", "targets", "--root", ".", "--max", "10"])
        assert hasattr(args, "func"), "crawl targets should have a func handler"

    def test_crawl_graph_outputs_json(self, tmp_repo: Path, capsys: pytest.CaptureFixture) -> None:
        # AC 16: crawl graph prints the import graph as JSON
        from dynosctl import build_parser

        parser = build_parser()
        args = parser.parse_args(["crawl", "graph", "--root", str(tmp_repo)])
        # Call the handler function
        try:
            args.func(args)
        except SystemExit:
            pass
        captured = capsys.readouterr()
        # Output should be valid JSON with graph structure
        output = json.loads(captured.out)
        assert "nodes" in output or "edges" in output or "pagerank" in output

    def test_crawl_targets_outputs_scored_list(self, tmp_repo: Path, capsys: pytest.CaptureFixture) -> None:
        # AC 16: crawl targets prints top N scan targets with scores
        from dynosctl import build_parser

        parser = build_parser()
        args = parser.parse_args(["crawl", "targets", "--root", str(tmp_repo), "--max", "5"])
        try:
            args.func(args)
        except SystemExit:
            pass
        captured = capsys.readouterr()
        # Output should contain file paths and scores
        assert len(captured.out.strip()) > 0, "Should produce output for targets"
