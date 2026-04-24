"""Tests for fix template storage and retrieval (Enhancement 2).

Covers AC 4-6:
  AC 4: lib_templates module with save_fix_template and find_matching_template
  AC 5: Storage at ~/.dynos/projects/{slug}/fix-templates.json, FIFO eviction at 50
  AC 6: find_matching_template matches on (category, file_extension), returns most recent
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a minimal project with DYNOS_HOME pointing to tmp."""
    dynos_home = tmp_path / ".dynos-home"
    dynos_home.mkdir()
    monkeypatch.setenv("DYNOS_HOME", str(dynos_home))
    project_root = tmp_path / "myproject"
    project_root.mkdir()
    (project_root / ".dynos").mkdir()
    return project_root


def _make_finding(**overrides) -> dict:
    """Build a minimal finding dict for template tests."""
    base = {
        "finding_id": "test-001",
        "category": "llm-review",
        "description": "Potential null pointer dereference",
        "evidence": {"file": "src/main.py", "line": 10},
        "severity": "medium",
    }
    base.update(overrides)
    return base


# ===========================================================================
# AC 4: Module and function existence
# ===========================================================================

class TestTemplateModuleExists:
    """AC 4: lib_templates module with save_fix_template and find_matching_template."""

    def test_save_fix_template_signature(self) -> None:
        # AC 4: save_fix_template(root, finding, diff)
        import inspect
        from lib_templates import save_fix_template
        sig = inspect.signature(save_fix_template)
        params = list(sig.parameters.keys())
        assert "root" in params
        assert "finding" in params
        assert "diff" in params

    def test_find_matching_template_signature(self) -> None:
        # AC 4: find_matching_template(root, finding) -> dict | None
        import inspect
        from lib_templates import find_matching_template
        sig = inspect.signature(find_matching_template)
        params = list(sig.parameters.keys())
        assert "root" in params
        assert "finding" in params


# ===========================================================================
# AC 5: Storage path, structure, and FIFO eviction
# ===========================================================================

class TestTemplateSaveAndStorage:
    """AC 5: Templates stored at correct path with correct structure; FIFO at 50."""

    def test_save_creates_template_file(self, tmp_project: Path) -> None:
        # AC 5
        from lib_templates import save_fix_template
        finding = _make_finding()
        save_fix_template(tmp_project, finding, "--- a/main.py\n+++ b/main.py\n@@ -1 +1 @@\n-bad\n+good\n")

        # Template file should exist under DYNOS_HOME/projects/{slug}/
        from lib_core import _persistent_project_dir
        template_path = _persistent_project_dir(tmp_project) / "fix-templates.json"
        assert template_path.exists(), f"Template file should be created at {template_path}"

    def test_template_entry_has_required_fields(self, tmp_project: Path) -> None:
        # AC 5: Each entry has category, file_ext, diff, saved_at
        from lib_templates import save_fix_template
        from lib_core import _persistent_project_dir

        finding = _make_finding()
        save_fix_template(tmp_project, finding, "some diff content")

        template_path = _persistent_project_dir(tmp_project) / "fix-templates.json"
        templates = json.loads(template_path.read_text())
        assert isinstance(templates, list)
        assert len(templates) == 1
        entry = templates[0]
        assert "category" in entry
        assert "file_ext" in entry
        assert "diff" in entry
        assert "saved_at" in entry

    def test_template_category_matches_finding(self, tmp_project: Path) -> None:
        # AC 5
        from lib_templates import save_fix_template
        from lib_core import _persistent_project_dir

        finding = _make_finding(category="security")
        save_fix_template(tmp_project, finding, "diff")

        template_path = _persistent_project_dir(tmp_project) / "fix-templates.json"
        templates = json.loads(template_path.read_text())
        assert templates[0]["category"] == "security"

    def test_template_file_ext_from_evidence_file(self, tmp_project: Path) -> None:
        # AC 5: file_ext derived from evidence.file
        from lib_templates import save_fix_template
        from lib_core import _persistent_project_dir

        finding = _make_finding(evidence={"file": "src/widget.dart", "line": 5})
        save_fix_template(tmp_project, finding, "diff")

        template_path = _persistent_project_dir(tmp_project) / "fix-templates.json"
        templates = json.loads(template_path.read_text())
        assert templates[0]["file_ext"] == ".dart"

    def test_multiple_saves_append(self, tmp_project: Path) -> None:
        # AC 5: multiple saves append to the array
        from lib_templates import save_fix_template

        for i in range(5):
            finding = _make_finding(finding_id=f"test-{i:03d}")
            save_fix_template(tmp_project, finding, f"diff-{i}")

        from lib_core import _persistent_project_dir
        template_path = _persistent_project_dir(tmp_project) / "fix-templates.json"
        templates = json.loads(template_path.read_text())
        assert len(templates) == 5

    def test_fifo_eviction_at_50_entries(self, tmp_project: Path) -> None:
        # AC 5: FIFO eviction when at 50 capacity
        from lib_templates import save_fix_template
        from lib_core import _persistent_project_dir

        # Save 50 templates
        for i in range(50):
            finding = _make_finding(finding_id=f"test-{i:03d}", category=f"cat-{i}")
            save_fix_template(tmp_project, finding, f"diff-{i}")

        template_path = _persistent_project_dir(tmp_project) / "fix-templates.json"
        templates = json.loads(template_path.read_text())
        assert len(templates) == 50

        # Save one more -- oldest should be evicted
        finding = _make_finding(finding_id="test-overflow", category="cat-overflow")
        save_fix_template(tmp_project, finding, "diff-overflow")

        templates = json.loads(template_path.read_text())
        assert len(templates) == 50, "Should not exceed 50 entries"
        # The oldest entry (cat-0) should be evicted
        categories = [t["category"] for t in templates]
        assert "cat-0" not in categories, "Oldest entry should be evicted (FIFO)"
        assert "cat-overflow" in categories, "Newest entry should be present"

    def test_diff_truncated_to_100_lines(self, tmp_project: Path) -> None:
        # AC 5 implicit: diff truncated to 100 lines before storage
        from lib_templates import save_fix_template
        from lib_core import _persistent_project_dir

        long_diff = "\n".join(f"line-{i}" for i in range(200))
        finding = _make_finding()
        save_fix_template(tmp_project, finding, long_diff)

        template_path = _persistent_project_dir(tmp_project) / "fix-templates.json"
        templates = json.loads(template_path.read_text())
        stored_lines = templates[0]["diff"].split("\n")
        assert len(stored_lines) <= 100, "Stored diff should be truncated to 100 lines"

    def test_handles_missing_template_file_gracefully(self, tmp_project: Path) -> None:
        # AC 5 implicit: cold-start path — first save creates the file with one entry
        from lib_templates import save_fix_template
        from lib_core import _persistent_project_dir
        template_path = _persistent_project_dir(tmp_project) / "fix-templates.json"
        assert not template_path.exists(), "fix-templates.json should not exist before first save"
        finding = _make_finding()
        save_fix_template(tmp_project, finding, "diff")
        assert template_path.exists(), "fix-templates.json should be created after first save"
        templates = json.loads(template_path.read_text())
        assert len(templates) == 1, "exactly one entry should be written on first save"

    def test_handles_corrupt_template_file(self, tmp_project: Path) -> None:
        # AC 5 implicit: corrupt file treated as empty
        from lib_templates import save_fix_template
        from lib_core import _persistent_project_dir

        template_dir = _persistent_project_dir(tmp_project)
        template_dir.mkdir(parents=True, exist_ok=True)
        template_path = template_dir / "fix-templates.json"
        template_path.write_text("{{{{invalid json")

        # Should not raise; treat as empty and create fresh
        finding = _make_finding()
        save_fix_template(tmp_project, finding, "diff")

        templates = json.loads(template_path.read_text())
        assert len(templates) >= 1

    def test_save_never_raises(self, tmp_project: Path) -> None:
        # AC 5 implicit: save_fix_template swallows write errors and leaves on-disk state unchanged
        from lib_templates import save_fix_template
        from lib_core import _persistent_project_dir
        # Pre-condition: write one valid entry so the file exists with known state
        finding = _make_finding()
        save_fix_template(tmp_project, finding, "first diff")
        template_path = _persistent_project_dir(tmp_project) / "fix-templates.json"
        assert len(json.loads(template_path.read_text())) == 1
        # Simulate a disk-full error on the next write
        with patch("lib_templates.write_json", side_effect=OSError("disk full")):
            save_fix_template(tmp_project, finding, "second diff")  # must not raise
        # File must still contain exactly one entry — no partial write, no corruption
        assert len(json.loads(template_path.read_text())) == 1, (
            "on-disk state should be unchanged after a failed write"
        )


# ===========================================================================
# AC 6: find_matching_template matching logic
# ===========================================================================

class TestFindMatchingTemplate:
    """AC 6: Matches on (category, file_extension), returns most recent or None."""

    def test_returns_none_when_no_templates_exist(self, tmp_project: Path) -> None:
        # AC 6
        from lib_templates import find_matching_template
        finding = _make_finding()
        result = find_matching_template(tmp_project, finding)
        assert result is None

    def test_returns_none_when_no_match(self, tmp_project: Path) -> None:
        # AC 6: no match on (category, file_ext)
        from lib_templates import save_fix_template, find_matching_template

        save_fix_template(tmp_project,
                          _make_finding(category="security", evidence={"file": "a.js", "line": 1}),
                          "security js diff")

        # Search for different category + extension
        finding = _make_finding(category="llm-review", evidence={"file": "b.py", "line": 1})
        result = find_matching_template(tmp_project, finding)
        assert result is None

    def test_matches_on_category_and_extension(self, tmp_project: Path) -> None:
        # AC 6: exact match on (category, file_extension)
        from lib_templates import save_fix_template, find_matching_template

        save_fix_template(tmp_project,
                          _make_finding(category="llm-review", evidence={"file": "main.py", "line": 1}),
                          "python fix diff")

        finding = _make_finding(category="llm-review", evidence={"file": "other.py", "line": 5})
        result = find_matching_template(tmp_project, finding)
        assert result is not None
        assert result["category"] == "llm-review"
        assert result["file_ext"] == ".py"

    def test_returns_most_recent_match(self, tmp_project: Path) -> None:
        # AC 6: most recently saved matching template returned
        from lib_templates import save_fix_template, find_matching_template

        save_fix_template(tmp_project,
                          _make_finding(category="llm-review", evidence={"file": "a.py", "line": 1}),
                          "old diff")
        save_fix_template(tmp_project,
                          _make_finding(category="llm-review", evidence={"file": "b.py", "line": 2}),
                          "new diff")

        finding = _make_finding(category="llm-review", evidence={"file": "c.py", "line": 3})
        result = find_matching_template(tmp_project, finding)
        assert result is not None
        assert result["diff"] == "new diff"

    def test_category_mismatch_no_match(self, tmp_project: Path) -> None:
        # AC 6: different category does not match
        from lib_templates import save_fix_template, find_matching_template

        save_fix_template(tmp_project,
                          _make_finding(category="security", evidence={"file": "a.py", "line": 1}),
                          "security diff")

        finding = _make_finding(category="llm-review", evidence={"file": "b.py", "line": 1})
        result = find_matching_template(tmp_project, finding)
        assert result is None

    def test_extension_mismatch_no_match(self, tmp_project: Path) -> None:
        # AC 6: same category but different extension does not match
        from lib_templates import save_fix_template, find_matching_template

        save_fix_template(tmp_project,
                          _make_finding(category="llm-review", evidence={"file": "a.py", "line": 1}),
                          "python diff")

        finding = _make_finding(category="llm-review", evidence={"file": "b.dart", "line": 1})
        result = find_matching_template(tmp_project, finding)
        assert result is None

    def test_result_contains_diff_field(self, tmp_project: Path) -> None:
        # AC 6: returned dict has at least diff, category, file_ext, saved_at
        from lib_templates import save_fix_template, find_matching_template

        save_fix_template(tmp_project,
                          _make_finding(category="llm-review", evidence={"file": "a.py", "line": 1}),
                          "the diff content")

        finding = _make_finding(category="llm-review", evidence={"file": "b.py", "line": 1})
        result = find_matching_template(tmp_project, finding)
        assert result is not None
        assert "diff" in result
        assert "category" in result
        assert "file_ext" in result
        assert "saved_at" in result
        assert result["diff"] == "the diff content"

    def test_handles_corrupt_template_file_returns_none(self, tmp_project: Path) -> None:
        # AC 6 implicit: corrupt file returns None
        from lib_templates import find_matching_template
        from lib_core import _persistent_project_dir

        template_dir = _persistent_project_dir(tmp_project)
        template_dir.mkdir(parents=True, exist_ok=True)
        (template_dir / "fix-templates.json").write_text("not valid json")

        finding = _make_finding()
        result = find_matching_template(tmp_project, finding)
        assert result is None
