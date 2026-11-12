"""Repair scope covers files a finding's fix asks the repair to CREATE, and
the project policy's ``always_in_scope`` paths.

Field report (7.7.4): a documentation finding (doc-001) whose fix created
docs/follow-ups/<task>.md got a repair log whose files_to_modify left that
file out, so the write-scope gate demanded a human acknowledgement before
the task could finish -- a stall in unattended runs.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))
sys.path.insert(0, str(ROOT / "tests"))

from ctl import _repair_files_for_finding  # noqa: E402
from test_field_report_7_5_31 import _project  # noqa: E402
from test_repair_findings_ensemble import _setup  # noqa: E402


def _with_followups_dir(td: Path) -> Path:
    root = _project(td)
    (root / "docs" / "follow-ups").mkdir(parents=True)
    (root / "docs" / "follow-ups" / "t12a.md").write_text("older task\n")
    return root


def test_cited_finding_also_covers_the_file_its_fix_creates(tmp_path: Path) -> None:
    td = _setup(tmp_path, [])
    _with_followups_dir(td)
    finding = {
        "id": "doc-001",
        "location": "src/auth.py:10",
        "recommendation": "Record the deferred work in docs/follow-ups/t39c.md as CONVENTIONS.md requires.",
    }
    files, _ = _repair_files_for_finding(td, finding, None, "docs-executor")
    assert files == ["src/auth.py", "docs/follow-ups/t39c.md"]


def test_citation_less_finding_is_scoped_to_the_file_to_create(tmp_path: Path) -> None:
    td = _setup(tmp_path, [])
    _with_followups_dir(td)
    finding = {"id": "doc-002", "fix": "Create docs/follow-ups/t39c.md listing the follow-ups."}
    files, _ = _repair_files_for_finding(td, finding, None, "docs-executor")
    assert files == ["docs/follow-ups/t39c.md"]


def test_new_file_in_a_missing_directory_or_only_in_evidence_is_not_added(tmp_path: Path) -> None:
    td = _setup(tmp_path, [])
    _project(td)
    finding = {
        "id": "doc-003",
        "location": "src/auth.py:10",
        "recommendation": "See nowhere/made/up.md for background.",
        "evidence": "Observed in docs/notes/new.md",
    }
    files, _ = _repair_files_for_finding(td, finding, None, "docs-executor")
    assert files == ["src/auth.py"]


def test_policy_always_in_scope_extends_declared_scope(tmp_path: Path) -> None:
    from lib_core import _persistent_project_dir
    from lib_write_scope import declared_scope

    td = _setup(tmp_path, [])
    before = declared_scope(td)
    policy_dir = _persistent_project_dir(td.parent.parent)
    policy_dir.mkdir(parents=True, exist_ok=True)
    (policy_dir / "policy.json").write_text(json.dumps({"always_in_scope": ["docs/follow-ups/"]}))
    after = declared_scope(td)
    assert "docs/follow-ups/" not in before
    assert after == before + ["docs/follow-ups/"]
