"""Dependency graph builder and file scoring for proactive scans.

Parses import statements across Python, JS/TS, Dart, Rust, and Go.
Computes reverse PageRank so heavily-imported files score highest.
Caches the graph at .dynos/dependency-graph.json, invalidated by origin/main SHA.
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Generated file detection
# ---------------------------------------------------------------------------

_GENERATED_PATTERNS = [
    re.compile(r"\.g\.dart$"),
    re.compile(r"\.generated\.[^.]+$"),
    re.compile(r"\.gen\.[^.]+$"),
    re.compile(r"\.freezed\.dart$"),
    re.compile(r"\.pb\.go$"),
    re.compile(r"\.pb\.dart$"),
]


def _is_generated_file(path: str) -> bool:
    """Return True if *path* matches a generated-file naming pattern."""
    basename = path.rsplit("/", 1)[-1] if "/" in path else path
    for pat in _GENERATED_PATTERNS:
        if pat.search(basename):
            return True
    return False


# ---------------------------------------------------------------------------
# Source extension whitelist
# ---------------------------------------------------------------------------

_SOURCE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx",
    ".go", ".rs", ".rb", ".java", ".kt",
    ".c", ".cpp", ".h", ".hpp", ".cs",
    ".swift", ".dart", ".lua", ".php",
    ".sh", ".bash", ".zsh",
}

# ---------------------------------------------------------------------------
# Import parsers
# ---------------------------------------------------------------------------


def _parse_python_imports(file_path: Path, root: Path) -> list[str]:
    """Parse Python imports using ast.parse, returning repo-relative paths."""
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(file_path))
    except (SyntaxError, ValueError, OSError):
        return []

    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                resolved = _resolve_import_to_path(alias.name, root, file_path)
                if resolved:
                    imports.append(resolved)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level and node.level > 0:
                # Relative import
                source_dir = file_path.parent
                for _ in range(node.level - 1):
                    source_dir = source_dir.parent
                if module:
                    rel_module = str(source_dir.relative_to(root) / module.replace(".", "/"))
                else:
                    rel_module = str(source_dir.relative_to(root))
                resolved = _resolve_import_to_path(rel_module, root, file_path, is_path=True)
                if resolved:
                    imports.append(resolved)
            else:
                resolved = _resolve_import_to_path(module, root, file_path)
                if resolved:
                    imports.append(resolved)
    return imports


def _parse_js_imports(content: str) -> list[str]:
    """Parse JS/TS import...from and require() statements."""
    results: list[str] = []
    # import ... from "..."  or  import ... from '...'
    for m in re.finditer(r"""import\s+.*?\s+from\s+['"]([^'"]+)['"]""", content, re.DOTALL):
        results.append(m.group(1))
    # require("...") or require('...')
    for m in re.finditer(r"""require\(\s*['"]([^'"]+)['"]\s*\)""", content):
        results.append(m.group(1))
    return results


def _parse_dart_imports(content: str) -> list[str]:
    """Parse Dart import statements."""
    results: list[str] = []
    for m in re.finditer(r"""import\s+['"]([^'"]+)['"]""", content):
        results.append(m.group(1))
    return results


def _parse_rust_imports(content: str) -> list[str]:
    """Parse Rust use statements (crate-local only)."""
    results: list[str] = []
    for m in re.finditer(r"use\s+(crate::\S+)", content):
        results.append(m.group(1))
    return results


def _parse_go_imports(content: str) -> list[str]:
    """Parse Go import statements (single and block form)."""
    results: list[str] = []
    # Single: import "..."
    for m in re.finditer(r'import\s+"([^"]+)"', content):
        results.append(m.group(1))
    # Block: import ( ... )
    for m in re.finditer(r"import\s*\((.*?)\)", content, re.DOTALL):
        block = m.group(1)
        for line in block.splitlines():
            line = line.strip().strip('"')
            if line:
                results.append(line)
    return results


# ---------------------------------------------------------------------------
# Import resolution
# ---------------------------------------------------------------------------


def _resolve_import_to_path(
    import_str: str, root: Path, source_file: Path, *, is_path: bool = False
) -> str | None:
    """Resolve an import string to a repo-relative file path, or None if external."""
    if not import_str:
        return None

    # For relative path imports (./foo, ../bar) in JS/TS/Dart
    if import_str.startswith("."):
        base_dir = source_file.parent
        candidate = (base_dir / import_str).resolve()
        try:
            rel = str(candidate.relative_to(root.resolve()))
        except ValueError:
            return None
        return _find_existing_file(root, rel)

    # For Python dotted module imports
    if not is_path:
        module_path = import_str.replace(".", "/")
    else:
        module_path = import_str

    return _find_existing_file(root, module_path)


def _find_existing_file(root: Path, module_path: str) -> str | None:
    """Try various extensions and __init__ patterns to find an actual file."""
    # Direct match (already has extension)
    candidate = root / module_path
    if candidate.is_file():
        return module_path

    # Try common extensions
    for ext in (".py", ".js", ".ts", ".tsx", ".jsx", ".dart", ".go", ".rs"):
        full = root / (module_path + ext)
        if full.is_file():
            return module_path + ext

    # Try as package (__init__.py)
    init = root / module_path / "__init__.py"
    if init.is_file():
        return str(Path(module_path) / "__init__.py")

    # Try index files for JS
    for idx in ("index.js", "index.ts", "index.tsx"):
        full = root / module_path / idx
        if full.is_file():
            return str(Path(module_path) / idx)

    return None


# ---------------------------------------------------------------------------
# PageRank
# ---------------------------------------------------------------------------


def _compute_pagerank(
    adjacency: dict[str, set[str]],
    damping: float = 0.85,
    iterations: int = 20,
) -> dict[str, float]:
    """Compute reverse PageRank on an import adjacency graph.

    *adjacency* maps ``file -> {files_that_import_it}``.
    Files with many importers score highest.
    """
    nodes = list(adjacency.keys())
    n = len(nodes)
    if n == 0:
        return {}

    scores: dict[str, float] = {node: 1.0 for node in nodes}

    for _ in range(iterations):
        new_scores: dict[str, float] = {}
        for node in nodes:
            rank = (1 - damping)
            importers = adjacency.get(node, set())
            for importer in importers:
                # How many files does the importer import? (out-degree of importer)
                # We need the forward adjacency for this.
                out_degree = sum(
                    1 for target_importers in adjacency.values()
                    if importer in target_importers
                )
                if out_degree > 0:
                    rank += damping * scores[importer] / out_degree
            new_scores[node] = rank
        scores = new_scores

    return scores


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------


def _get_origin_main_sha(root: Path) -> str | None:
    """Get the SHA of origin/main, or None on failure."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "origin/main"],
            capture_output=True, text=True, timeout=10, cwd=root,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    # Fallback: try HEAD
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, cwd=root,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def _cache_path(root: Path) -> Path:
    return root / ".dynos" / "dependency-graph.json"


def _load_graph_cache(root: Path) -> dict | None:
    """Load cached graph, or None if missing/corrupt."""
    cp = _cache_path(root)
    if not cp.exists():
        return None
    try:
        data = json.loads(cp.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        if "origin_main_sha" not in data or "nodes" not in data:
            return None
        return data
    except (json.JSONDecodeError, OSError):
        return None


def _save_graph_cache(root: Path, graph: dict, sha: str) -> None:
    """Persist graph to cache file."""
    cache = {
        "origin_main_sha": sha,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "nodes": graph["nodes"],
        "edges": graph["edges"],
        "pagerank": graph["pagerank"],
    }
    cp = _cache_path(root)
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text(json.dumps(cache, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Main graph builder
# ---------------------------------------------------------------------------


def build_import_graph(root: Path) -> dict:
    """Build (or load from cache) the repo import graph.

    Returns dict with keys ``nodes``, ``edges``, ``pagerank``.
    """
    sha = _get_origin_main_sha(root)

    # Try cache
    cached = _load_graph_cache(root)
    if cached is not None and sha is not None and cached.get("origin_main_sha") == sha:
        return {
            "nodes": cached["nodes"],
            "edges": cached["edges"],
            "pagerank": {k: float(v) for k, v in cached["pagerank"].items()},
        }

    # List tracked files via git
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            capture_output=True, text=True, timeout=15, cwd=root,
        )
        if result.returncode != 0:
            return {"nodes": [], "edges": [], "pagerank": {}}
        all_files = [
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip()
        ]
    except (subprocess.TimeoutExpired, OSError):
        return {"nodes": [], "edges": [], "pagerank": {}}

    # Filter to source files, exclude generated, exclude hidden dirs
    source_files: list[str] = []
    for f in all_files:
        p = Path(f)
        if p.suffix not in _SOURCE_EXTENSIONS:
            continue
        if any(part.startswith(".") for part in p.parts):
            continue
        if _is_generated_file(f):
            continue
        if (root / f).is_file():
            source_files.append(f)

    # Build adjacency: forward map (importer -> set of imported files)
    forward: dict[str, set[str]] = {f: set() for f in source_files}
    source_set = set(source_files)

    for f in source_files:
        file_path = root / f
        suffix = Path(f).suffix

        try:
            if suffix == ".py":
                imported = _parse_python_imports(file_path, root)
            elif suffix in (".js", ".ts", ".tsx", ".jsx"):
                content = file_path.read_text(encoding="utf-8", errors="replace")
                raw = _parse_js_imports(content)
                imported = []
                for imp in raw:
                    resolved = _resolve_import_to_path(imp, root, file_path)
                    if resolved:
                        imported.append(resolved)
            elif suffix == ".dart":
                content = file_path.read_text(encoding="utf-8", errors="replace")
                raw = _parse_dart_imports(content)
                imported = []
                for imp in raw:
                    # Dart: bare filenames like 'helpers.dart' are relative
                    if not imp.startswith(".") and not imp.startswith("package:"):
                        imp = "./" + imp
                    resolved = _resolve_import_to_path(imp, root, file_path)
                    if resolved:
                        imported.append(resolved)
            elif suffix == ".rs":
                content = file_path.read_text(encoding="utf-8", errors="replace")
                raw = _parse_rust_imports(content)
                imported = []
                for imp in raw:
                    # crate::config -> config
                    mod_path = imp.replace("crate::", "").replace("::", "/")
                    resolved = _find_existing_file(root, mod_path)
                    if resolved:
                        imported.append(resolved)
            elif suffix == ".go":
                content = file_path.read_text(encoding="utf-8", errors="replace")
                raw = _parse_go_imports(content)
                imported = []
                for imp in raw:
                    # Only intra-repo: try resolving relative to root
                    resolved = _find_existing_file(root, imp)
                    if resolved:
                        imported.append(resolved)
            else:
                imported = []
        except OSError:
            continue

        for target in imported:
            if target in source_set and target != f:
                forward[f].add(target)

    # Build edges list
    edges: list[dict[str, str]] = []
    for src, targets in forward.items():
        for tgt in sorted(targets):
            edges.append({"from": src, "to": tgt})

    # Build reverse adjacency for PageRank: file -> set of its importers
    reverse: dict[str, set[str]] = {f: set() for f in source_files}
    for src, targets in forward.items():
        for tgt in targets:
            reverse[tgt].add(src)

    # Compute PageRank
    pagerank = _compute_pagerank(reverse, damping=0.85, iterations=20)

    graph = {
        "nodes": sorted(source_files),
        "edges": edges,
        "pagerank": pagerank,
    }

    # Save cache
    if sha is not None:
        _save_graph_cache(root, graph, sha)
    else:
        # Save with empty sha so the file exists
        _save_graph_cache(root, graph, "")

    return graph


# ---------------------------------------------------------------------------
# Neighbor file contents
# ---------------------------------------------------------------------------


def get_neighbor_file_contents(
    root: Path, file_path: str, max_files: int = 5, max_lines: int = 100
) -> list[dict]:
    """Return content of files neighboring *file_path* in the import graph.

    Neighbors are files that import *file_path* (importers) plus files that
    *file_path* imports.  Results are deduplicated, capped to *max_files*,
    and each file's content is truncated to *max_lines* lines.

    Returns ``[{"path": str, "content": str}, ...]``.
    On any error returns ``[]`` without raising.
    """
    try:
        graph = build_import_graph(root)
    except Exception:
        return []

    edges = graph.get("edges", [])
    if not edges:
        return []

    neighbors: set[str] = set()
    for edge in edges:
        src = edge.get("from", "")
        tgt = edge.get("to", "")
        if tgt == file_path and src != file_path:
            neighbors.add(src)
        if src == file_path and tgt != file_path:
            neighbors.add(tgt)

    if not neighbors:
        return []

    result: list[dict] = []
    for neighbor in sorted(neighbors):
        if len(result) >= max_files:
            break
        try:
            full_path = root / neighbor
            content = full_path.read_text(encoding="utf-8", errors="replace")
            lines = content.splitlines()
            truncated = "\n".join(lines[:max_lines])
            result.append({"path": neighbor, "content": truncated})
        except Exception:
            continue

    return result


# ---------------------------------------------------------------------------
# Scan target scoring
# ---------------------------------------------------------------------------


def compute_scan_targets(
    root: Path,
    max_files: int = 10,
    coverage: dict | None = None,
    findings: list | None = None,
) -> list[tuple[Path, float]]:
    """Score files using a composite formula and return the top targets.

    Composite: pagerank*30 + freshness*25 + finding_history*20
               + change_velocity*15 + complexity*10 - cooldown
    """
    graph = build_import_graph(root)
    pr = graph.get("pagerank", {})
    nodes = graph.get("nodes", [])

    if not nodes:
        return []

    if coverage is None:
        coverage = {}
    if findings is None:
        findings = []

    # Normalize PageRank to 0-1
    pr_values = list(pr.values())
    pr_max = max(pr_values) if pr_values else 1.0
    pr_min = min(pr_values) if pr_values else 0.0
    pr_range = pr_max - pr_min if pr_max != pr_min else 1.0

    # Finding history: count per file
    finding_counts: dict[str, int] = {}
    for f in findings:
        efile = ""
        if isinstance(f, dict):
            efile = f.get("evidence", {}).get("file", "") or f.get("file", "")
        if efile:
            finding_counts[efile] = finding_counts.get(efile, 0) + 1
    max_findings = max(finding_counts.values()) if finding_counts else 1

    # Change velocity: commits in last 30 days
    churn: dict[str, int] = {}
    try:
        result = subprocess.run(
            ["git", "log", "--since=30.days.ago", "--name-only", "--pretty=format:"],
            capture_output=True, text=True, timeout=15, cwd=root,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                line = line.strip()
                if line:
                    churn[line] = churn.get(line, 0) + 1
    except (subprocess.TimeoutExpired, OSError):
        pass
    max_churn = max(churn.values()) if churn else 1

    # Freshness: git log -1 per file is expensive; batch via ls-files timestamps
    # Use git log for all files at once
    now_ts = datetime.now(timezone.utc).timestamp()

    file_mtime: dict[str, float] = {}
    for f in nodes:
        try:
            result = subprocess.run(
                ["git", "log", "-1", "--format=%ct", "--", f],
                capture_output=True, text=True, timeout=5, cwd=root,
            )
            if result.returncode == 0 and result.stdout.strip():
                file_mtime[f] = float(result.stdout.strip())
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass

    max_age_days = 365.0  # normalize against 1 year
    now = datetime.now(timezone.utc)
    file_coverage = coverage.get("files", {})

    scored: list[tuple[Path, float]] = []
    for f in nodes:
        if _is_generated_file(f):
            continue

        file_path = root / f
        if not file_path.is_file():
            continue

        # PageRank score (0-1)
        pr_score = (pr.get(f, 0.0) - pr_min) / pr_range if pr_range else 0.0

        # Freshness (0-1): recent files score higher
        mtime = file_mtime.get(f)
        if mtime is not None:
            days_old = (now_ts - mtime) / 86400.0
            freshness = max(0.0, 1.0 - days_old / max_age_days)
        else:
            freshness = 0.5  # unknown, default middle

        # Finding history (0-1)
        fh = finding_counts.get(f, 0)
        finding_history = min(fh / max_findings, 1.0) if max_findings > 0 else 0.0

        # Change velocity (0-1)
        cv = churn.get(f, 0)
        change_velocity = min(cv / max_churn, 1.0) if max_churn > 0 else 0.0

        # Complexity: line count as proxy (0-1, cap at 1000 lines)
        try:
            line_count = len(file_path.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            line_count = 0
        complexity = min(line_count / 1000.0, 1.0)

        # Cooldown
        cooldown = 0.0
        info = file_coverage.get(f, {})
        last_scanned = info.get("last_scanned_at", "")
        if last_scanned:
            try:
                scanned_dt = datetime.fromisoformat(last_scanned.replace("Z", "+00:00"))
                days_since = (now - scanned_dt).total_seconds() / 86400.0
                if days_since < 1:
                    cooldown = 100.0
                elif days_since < 3:
                    cooldown = 30.0
                elif days_since < 7:
                    cooldown = 10.0
            except (ValueError, TypeError):
                pass
        if info.get("last_result") == "clean":
            cooldown += 5.0

        composite = (
            pr_score * 30
            + freshness * 25
            + finding_history * 20
            + change_velocity * 15
            + complexity * 10
            - cooldown
        )

        scored.append((Path(f), composite))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:max_files]
