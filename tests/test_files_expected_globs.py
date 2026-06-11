"""Tests for directory/glob entries in files_expected (D7-2).

Repo-wide segments can declare `src/pkg/` or `src/**/*.py` instead of
enumerating every file. The proof source is unchanged: entries are matched
against the git diff + untracked listing, and an entry that matches nothing
still fails the segment.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

from ctl import _entry_covered  # noqa: E402
from lib_validate import (  # noqa: E402
    files_expected_entries_overlap,
    files_expected_entry_matches,
)

COVERED = {
    "src/andromede/core.py",
    "src/andromede/util/helpers.py",
    "tests/test_core.py",
    "README.md",
}


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        # plain entries: exact membership, original behavior
        ("src/andromede/core.py", True),
        ("src/missing.py", False),
        # directory entries
        ("src/andromede/", True),
        ("src/other/", False),
        # glob entries
        ("src/andromede/*.py", True),
        ("src/*/util/*.py", True),
        ("docs/*.md", False),
        ("tests/test_*.py", True),
    ],
)
def test_entry_covered(entry: str, expected: bool) -> None:
    assert _entry_covered(entry, COVERED) is expected


@pytest.mark.parametrize(
    ("entry", "file_path", "expected"),
    [
        ("src/a.py", "src/a.py", True),
        ("src/a.py", "src/b.py", False),
        ("src/pkg/", "src/pkg/mod.py", True),
        ("src/pkg/", "src/pkgother/mod.py", False),
        ("src/*.py", "src/a.py", True),
        ("src/*.py", "src/sub/a.py", False),
    ],
)
def test_entry_matches_file(entry: str, file_path: str, expected: bool) -> None:
    assert files_expected_entry_matches(entry, file_path) is expected


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        # exact duplicates
        ("src/a.py", "src/a.py", True),
        # directory contains file -> overlap (stricter than before)
        ("src/pkg/", "src/pkg/mod.py", True),
        ("src/pkg/mod.py", "src/pkg/", True),
        # nested directories overlap
        ("src/pkg/", "src/pkg/sub/", True),
        # glob covers file -> overlap
        ("src/*.py", "src/a.py", True),
        # disjoint
        ("src/a.py", "src/b.py", False),
        ("src/pkg/", "docs/", False),
    ],
)
def test_entries_overlap(a: str, b: str, expected: bool) -> None:
    assert files_expected_entries_overlap(a, b) is expected


def test_segment_ownership_accepts_directory_scoped_files(tmp_path: Path) -> None:
    import json

    from lib_validate import check_segment_ownership

    root = tmp_path
    task_dir = root / ".dynos" / "task-20260611-001"
    task_dir.mkdir(parents=True)
    (task_dir / "execution-graph.json").write_text(json.dumps({
        "segments": [
            {
                "id": "seg-1",
                "executor": "backend-executor",
                "files_expected": ["src/andromede/"],
                "criteria_ids": [1],
                "depends_on": [],
            }
        ]
    }))
    violations = check_segment_ownership(
        task_dir, "seg-1", ["src/andromede/core.py", "src/andromede/util/x.py"]
    )
    assert violations == []
    # A file outside the directory is still a violation.
    violations = check_segment_ownership(task_dir, "seg-1", ["src/elsewhere.py"])
    assert violations == ["src/elsewhere.py"]
