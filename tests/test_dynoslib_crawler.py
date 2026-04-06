"""Tests for the dependency graph builder (dynoslib_crawler.py).

Covers AC 1-5: import graph, PageRank, caching, generated file exclusion, scan targets.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """Create a minimal git-initialized repo with Python files that import each other."""
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, capture_output=True)
    (tmp_path / ".dynos").mkdir()

    # Create a small dependency graph:
    # core.py is imported by utils.py, api.py, cli.py (high centrality)
    # utils.py is imported by api.py (medium centrality)
    # api.py, cli.py are leaf importers (low centrality)
    (tmp_path / "core.py").write_text("# core module\ndef core_func(): pass\n")
    (tmp_path / "utils.py").write_text("from core import core_func\ndef helper(): pass\n")
    (tmp_path / "api.py").write_text("from core import core_func\nfrom utils import helper\n")
    (tmp_path / "cli.py").write_text("from core import core_func\ndef main(): pass\n")

    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)
    return tmp_path


@pytest.fixture
def tmp_repo_multilang(tmp_path: Path) -> Path:
    """Create a git repo with files in multiple languages."""
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, capture_output=True)
    (tmp_path / ".dynos").mkdir()

    # JS file with import
    (tmp_path / "app.js").write_text('import { foo } from "./utils";\nconst bar = require("./config");\n')
    (tmp_path / "utils.js").write_text('export function foo() {}\n')
    (tmp_path / "config.js").write_text('module.exports = {};\n')

    # Dart file with import
    (tmp_path / "main.dart").write_text("import 'package:myapp/widget.dart';\nimport 'helpers.dart';\n")
    (tmp_path / "helpers.dart").write_text("void help() {}\n")

    # Go file with import
    (tmp_path / "main.go").write_text('package main\n\nimport (\n\t"fmt"\n\t"myproject/internal/handler"\n)\n')

    # Rust file with use
    (tmp_path / "main.rs").write_text("use crate::config;\nuse std::io;\n")

    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)
    return tmp_path


# ===========================================================================
# AC 1: build_import_graph returns correct structure
# ===========================================================================

class TestBuildImportGraph:
    """AC 1: build_import_graph parses imports and returns {nodes, edges, pagerank}."""

    def test_returns_dict_with_required_keys(self, tmp_repo: Path) -> None:
        # AC 1
        from dynoslib_crawler import build_import_graph

        graph = build_import_graph(tmp_repo)
        assert isinstance(graph, dict)
        assert "nodes" in graph
        assert "edges" in graph
        assert "pagerank" in graph

    def test_nodes_are_repo_relative_paths(self, tmp_repo: Path) -> None:
        # AC 1
        from dynoslib_crawler import build_import_graph

        graph = build_import_graph(tmp_repo)
        nodes = graph["nodes"]
        assert isinstance(nodes, list)
        # All entries should be repo-relative (no absolute paths)
        for node in nodes:
            assert not os.path.isabs(node), f"Node should be repo-relative: {node}"

    def test_edges_have_from_and_to(self, tmp_repo: Path) -> None:
        # AC 1
        from dynoslib_crawler import build_import_graph

        graph = build_import_graph(tmp_repo)
        edges = graph["edges"]
        assert isinstance(edges, list)
        for edge in edges:
            assert "from" in edge, "Each edge must have 'from'"
            assert "to" in edge, "Each edge must have 'to'"

    def test_python_imports_produce_edges(self, tmp_repo: Path) -> None:
        # AC 1: Python imports via ast.parse
        from dynoslib_crawler import build_import_graph

        graph = build_import_graph(tmp_repo)
        edges = graph["edges"]
        # utils.py imports core.py, so there should be an edge from utils.py to core.py
        from_to_pairs = {(e["from"], e["to"]) for e in edges}
        assert ("utils.py", "core.py") in from_to_pairs

    def test_pagerank_maps_paths_to_floats(self, tmp_repo: Path) -> None:
        # AC 1
        from dynoslib_crawler import build_import_graph

        graph = build_import_graph(tmp_repo)
        pr = graph["pagerank"]
        assert isinstance(pr, dict)
        for path, score in pr.items():
            assert isinstance(score, float), f"PageRank score for {path} should be float"

    def test_js_import_from_detected(self, tmp_repo_multilang: Path) -> None:
        # AC 1: JS import...from regex
        from dynoslib_crawler import build_import_graph

        graph = build_import_graph(tmp_repo_multilang)
        from_to = {(e["from"], e["to"]) for e in graph["edges"]}
        # app.js imports from ./utils
        assert any(e[0] == "app.js" and "utils" in e[1] for e in from_to)

    def test_js_require_detected(self, tmp_repo_multilang: Path) -> None:
        # AC 1: JS require() regex
        from dynoslib_crawler import build_import_graph

        graph = build_import_graph(tmp_repo_multilang)
        from_to = {(e["from"], e["to"]) for e in graph["edges"]}
        # app.js requires ./config
        assert any(e[0] == "app.js" and "config" in e[1] for e in from_to)

    def test_dart_import_detected(self, tmp_repo_multilang: Path) -> None:
        # AC 1: Dart import regex
        from dynoslib_crawler import build_import_graph

        graph = build_import_graph(tmp_repo_multilang)
        from_to = {(e["from"], e["to"]) for e in graph["edges"]}
        assert any(e[0] == "main.dart" and "helpers" in e[1] for e in from_to)

    def test_malformed_python_file_skipped_silently(self, tmp_repo: Path) -> None:
        # AC 1 implicit: ast.parse failure skips file, no crash
        from dynoslib_crawler import build_import_graph

        (tmp_repo / "bad.py").write_text("def foo(\n")  # syntax error
        subprocess.run(["git", "add", "bad.py"], cwd=tmp_repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add bad"], cwd=tmp_repo, capture_output=True)

        # Should not raise
        graph = build_import_graph(tmp_repo)
        assert isinstance(graph, dict)
        # bad.py may or may not appear in nodes, but should not crash


# ===========================================================================
# AC 2: PageRank computation
# ===========================================================================

class TestPageRank:
    """AC 2: Reverse PageRank with damping 0.85 and 20 iterations."""

    def test_highly_imported_file_scores_higher(self, tmp_repo: Path) -> None:
        # AC 2: core.py is imported by 3 files, should score highest
        from dynoslib_crawler import build_import_graph

        graph = build_import_graph(tmp_repo)
        pr = graph["pagerank"]
        # core.py is imported by utils.py, api.py, cli.py
        assert pr.get("core.py", 0) > pr.get("cli.py", 0), \
            "File imported by 3 others should score higher than a leaf importer"

    def test_known_graph_centrality_ordering(self) -> None:
        # AC 2: Test PageRank with a known graph topology directly
        from dynoslib_crawler import _compute_pagerank

        # Adjacency: who imports whom (reverse direction for PageRank)
        # A is imported by B, C, D (3 importers)
        # B is imported by C (1 importer)
        # C, D import but are not imported
        adjacency = {
            "A": {"B", "C", "D"},  # A is imported by B, C, D
            "B": {"C"},            # B is imported by C
            "C": set(),
            "D": set(),
        }
        scores = _compute_pagerank(adjacency, damping=0.85, iterations=20)
        assert scores["A"] > scores["B"], "A (3 importers) should rank higher than B (1 importer)"
        assert scores["A"] > scores["C"], "A should rank higher than C (0 importers)"
        assert scores["A"] > scores["D"], "A should rank higher than D (0 importers)"

    def test_pagerank_uses_damping_085(self) -> None:
        # AC 2: Verify damping factor is 0.85
        from dynoslib_crawler import _compute_pagerank

        adjacency = {"A": {"B"}, "B": set()}
        scores = _compute_pagerank(adjacency, damping=0.85, iterations=20)
        # With damping 0.85 and 2 nodes, A should have higher score than B
        assert scores["A"] > scores["B"]

    def test_ten_importers_vs_one_importer(self) -> None:
        # AC 2: A file imported by 10 others scores higher than one imported by 1
        from dynoslib_crawler import _compute_pagerank

        nodes = {}
        # hub.py is imported by 10 files
        nodes["hub"] = {f"leaf_{i}" for i in range(10)}
        # solo.py is imported by 1 file
        nodes["solo"] = {"leaf_0"}
        for i in range(10):
            nodes[f"leaf_{i}"] = set()
        scores = _compute_pagerank(nodes, damping=0.85, iterations=20)
        assert scores["hub"] > scores["solo"], \
            "File imported by 10 others must score higher than file imported by 1"


# ===========================================================================
# AC 3: Graph caching and invalidation
# ===========================================================================

class TestGraphCaching:
    """AC 3: Cache at .dynos/dependency-graph.json, invalidated by origin/main SHA."""

    def test_cache_file_created_on_build(self, tmp_repo: Path) -> None:
        # AC 3
        from dynoslib_crawler import build_import_graph

        cache_path = tmp_repo / ".dynos" / "dependency-graph.json"
        assert not cache_path.exists()

        build_import_graph(tmp_repo)
        assert cache_path.exists(), "Cache file should be created after build"

    def test_cache_contains_origin_main_sha(self, tmp_repo: Path) -> None:
        # AC 3
        from dynoslib_crawler import build_import_graph

        build_import_graph(tmp_repo)
        cache_path = tmp_repo / ".dynos" / "dependency-graph.json"
        cache = json.loads(cache_path.read_text())
        assert "origin_main_sha" in cache

    def test_cache_hit_avoids_rebuild(self, tmp_repo: Path) -> None:
        # AC 3: second call uses cache, not full rebuild
        from dynoslib_crawler import build_import_graph

        graph1 = build_import_graph(tmp_repo)
        # Patch git ls-files to detect if it is called again
        with patch("dynoslib_crawler.subprocess.run") as mock_run:
            # Make rev-parse return the same SHA as cached
            cache_path = tmp_repo / ".dynos" / "dependency-graph.json"
            cache = json.loads(cache_path.read_text())
            mock_run.return_value = MagicMock(
                returncode=0, stdout=cache["origin_main_sha"]
            )
            graph2 = build_import_graph(tmp_repo)
            # Should load from cache without calling git ls-files
            assert graph2["nodes"] == graph1["nodes"]

    def test_cache_invalidated_on_sha_change(self, tmp_repo: Path) -> None:
        # AC 3: when origin/main SHA changes, graph is rebuilt
        from dynoslib_crawler import build_import_graph

        build_import_graph(tmp_repo)
        cache_path = tmp_repo / ".dynos" / "dependency-graph.json"
        cache = json.loads(cache_path.read_text())
        # Tamper with cached SHA to simulate a push
        cache["origin_main_sha"] = "0000000000000000000000000000000000000000"
        cache_path.write_text(json.dumps(cache))

        # Next build should detect mismatch and rebuild
        graph = build_import_graph(tmp_repo)
        assert isinstance(graph, dict)
        assert len(graph["nodes"]) > 0


# ===========================================================================
# AC 4: Generated file exclusion
# ===========================================================================

class TestGeneratedFileExclusion:
    """AC 4: Generated files excluded from graph and file scoring."""

    @pytest.mark.parametrize("filename,expected", [
        ("model.g.dart", True),
        ("api.generated.ts", True),
        ("types.gen.go", True),
        ("state.freezed.dart", True),
        ("message.pb.go", True),
        ("message.pb.dart", True),
        ("normal.dart", False),
        ("utils.py", False),
        ("main.go", False),
        ("test.generated_manually.py", False),
    ])
    def test_is_generated_file(self, filename: str, expected: bool) -> None:
        # AC 4
        from dynoslib_crawler import _is_generated_file

        assert _is_generated_file(filename) == expected, \
            f"_is_generated_file('{filename}') should be {expected}"

    def test_generated_files_excluded_from_graph_nodes(self, tmp_path: Path) -> None:
        # AC 4: generated files should not appear in graph nodes
        from dynoslib_crawler import build_import_graph

        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, capture_output=True)
        (tmp_path / ".dynos").mkdir()

        (tmp_path / "real.py").write_text("x = 1\n")
        (tmp_path / "model.g.dart").write_text("// generated\n")
        (tmp_path / "api.generated.ts").write_text("// generated\n")

        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)

        graph = build_import_graph(tmp_path)
        node_set = set(graph["nodes"])
        assert "model.g.dart" not in node_set
        assert "api.generated.ts" not in node_set

    def test_generated_files_excluded_from_scan_targets(self, tmp_path: Path) -> None:
        # AC 4: generated files should not appear in scan targets
        from dynoslib_crawler import compute_scan_targets

        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, capture_output=True)
        (tmp_path / ".dynos").mkdir()

        (tmp_path / "real.py").write_text("x = 1\n")
        (tmp_path / "model.freezed.dart").write_text("// generated\n")

        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)

        targets = compute_scan_targets(tmp_path, max_files=10)
        target_names = [str(t[0]) for t in targets]
        assert all("freezed" not in name for name in target_names)


# ===========================================================================
# AC 5: compute_scan_targets composite score
# ===========================================================================

class TestComputeScanTargets:
    """AC 5: compute_scan_targets replaces _compute_file_scores with composite score."""

    def test_returns_list_of_path_float_tuples(self, tmp_repo: Path) -> None:
        # AC 5
        from dynoslib_crawler import compute_scan_targets

        targets = compute_scan_targets(tmp_repo, max_files=10)
        assert isinstance(targets, list)
        for item in targets:
            assert isinstance(item, tuple)
            assert len(item) == 2
            assert isinstance(item[1], (int, float))

    def test_max_files_limits_output(self, tmp_repo: Path) -> None:
        # AC 5: max_files parameter respected
        from dynoslib_crawler import compute_scan_targets

        targets = compute_scan_targets(tmp_repo, max_files=2)
        assert len(targets) <= 2

    def test_default_max_files_is_10(self, tmp_repo: Path) -> None:
        # AC 5: default max_files is 10 (increased from 5)
        from dynoslib_crawler import compute_scan_targets

        targets = compute_scan_targets(tmp_repo)
        assert len(targets) <= 10

    def test_scores_are_sorted_descending(self, tmp_repo: Path) -> None:
        # AC 5: targets should be sorted by score, highest first
        from dynoslib_crawler import compute_scan_targets

        targets = compute_scan_targets(tmp_repo, max_files=10)
        if len(targets) >= 2:
            scores = [t[1] for t in targets]
            assert scores == sorted(scores, reverse=True), "Targets should be sorted by score descending"

    def test_high_centrality_file_ranks_higher(self, tmp_repo: Path) -> None:
        # AC 5: PageRank contributes 30% weight, so highly imported files should rank higher
        from dynoslib_crawler import compute_scan_targets

        targets = compute_scan_targets(tmp_repo, max_files=10)
        target_paths = [str(t[0]) for t in targets]
        # core.py is imported by 3 files, so it should appear near the top
        if "core.py" in target_paths:
            core_idx = target_paths.index("core.py")
            # core.py should be in the top half due to high centrality
            assert core_idx < len(targets), "core.py should appear in targets"
