"""Scope cut to fit the artifact ceilings is recorded, not lost in prose.

Field report: a brief needing ~26 acceptance criteria was trimmed to the
20-criterion ceiling; the cut work (every app screen of a feature) survived
only as prose in a follow-ups note, and no task was created for it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

from ctl import _record_deferred_scope  # noqa: E402
from lib_validate import parse_deferred_scope  # noqa: E402

SPEC = """# Normalized Spec

## Acceptance Criteria
1. The API returns today's plan.

## Out of Scope
- Offline sync.

## Deferred Scope
- Flutter screens — every app screen for plan-today (brief criteria 21-26)
* Analytics events for plan views
1. Push notification copy

## Assumptions
- None.
"""


def test_parse_deferred_scope_reads_only_its_section() -> None:
    assert parse_deferred_scope(SPEC) == [
        "Flutter screens — every app screen for plan-today (brief criteria 21-26)",
        "Analytics events for plan views",
        "Push notification copy",
    ]


def test_none_and_missing_section_mean_nothing_deferred() -> None:
    assert parse_deferred_scope("## Deferred Scope\n- None.\n") == []
    assert parse_deferred_scope("## Acceptance Criteria\n1. x\n") == []
    assert parse_deferred_scope("") == []


def test_done_records_deferred_scope_json_and_log(tmp_path: Path) -> None:
    td = tmp_path / "project" / ".dynos" / "task-20261112-002"
    td.mkdir(parents=True)
    (td / "spec.md").write_text(SPEC)

    items = _record_deferred_scope(td)

    assert len(items) == 3
    record = json.loads((td / "deferred-scope.json").read_text())
    assert record["items"] == items and record["task_id"] == td.name
    assert "[DEFERRED] 3 scope item(s)" in (td / "execution-log.md").read_text()


def test_no_deferred_scope_writes_nothing(tmp_path: Path) -> None:
    td = tmp_path / "project" / ".dynos" / "task-20261112-003"
    td.mkdir(parents=True)
    (td / "spec.md").write_text("## Deferred Scope\n- None.\n")
    assert _record_deferred_scope(td) == []
    assert not (td / "deferred-scope.json").exists()
