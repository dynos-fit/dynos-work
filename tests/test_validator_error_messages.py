"""Tests for self-correcting validator errors (D7-4).

Enum violations must name the valid set and offer a did-you-mean hint;
Reference-Code path checking must not reject non-path backtick tokens or
files declared under a 'Files to be created' heading. Per the 2026-06
decision, domain "ci" stays OUT of VALID_DOMAINS and hints to "infra".
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

from lib_core import VALID_DOMAINS  # noqa: E402
from lib_validate import (  # noqa: E402
    invalid_enum_error,
    validate_manifest,
    validate_task_artifacts,
)


def test_ci_is_not_a_valid_domain() -> None:
    assert "ci" not in VALID_DOMAINS


def test_ci_domain_error_hints_infra() -> None:
    msg = invalid_enum_error("classification domain", "ci", VALID_DOMAINS)
    assert "valid values:" in msg
    assert "infra" in msg
    assert "did you mean 'infra'?" in msg


def test_executor_error_hints_suffix() -> None:
    msg = invalid_enum_error("seg-1: executor", "backend", {"backend-executor", "ui-executor"})
    assert "did you mean 'backend-executor'?" in msg


def test_typo_hint_via_difflib() -> None:
    msg = invalid_enum_error("classification domain", "bakend", VALID_DOMAINS)
    assert "did you mean 'backend'?" in msg


def test_no_hint_for_unrelated_value() -> None:
    msg = invalid_enum_error("classification domain", "zzzz", VALID_DOMAINS)
    assert "valid values:" in msg
    assert "did you mean" not in msg


def test_manifest_domain_error_includes_valid_set() -> None:
    errors = validate_manifest({
        "task_id": "task-20260611-001",
        "stage": "PLANNING",
        "classification": {
            "type": "feature",
            "risk_level": "low",
            "domains": ["ci"],
        },
    })
    domain_errors = [e for e in errors if "classification domain" in e]
    assert domain_errors, errors
    assert "infra" in domain_errors[0]


# ---------------------------------------------------------------------------
# Reference Code path checking
# ---------------------------------------------------------------------------

def _write_task(tmp_path: Path, plan_reference_section: str) -> Path:
    task_dir = tmp_path / ".dynos" / "task-20260611-001"
    task_dir.mkdir(parents=True)
    (task_dir / "manifest.json").write_text(json.dumps({
        "task_id": "task-20260611-001",
        "created_at": "2026-06-11T00:00:00Z",
        "raw_input": "x",
        "stage": "PLANNING",
        "classification": {
            "type": "feature",
            "domains": ["docs"],
            "risk_level": "low",
        },
    }))
    (task_dir / "spec.md").write_text(
        "## Task Summary\nA.\n\n## User Context\nB.\n\n"
        "## Acceptance Criteria\n1. One criterion\n\n"
        "## Implicit Requirements Surfaced\nC.\n\n## Out of Scope\nD.\n\n"
        "## Assumptions\nsafe assumption: none\n\n## Risk Notes\nE.\n"
    )
    (task_dir / "plan.md").write_text(
        "## Technical Approach\nA.\n\n"
        f"## Reference Code\n{plan_reference_section}\n\n"
        "## Components / Modules\n`docs/notes.md`\n\n"
        "## Data Flow\nD.\n\n## Error Handling Strategy\nE.\n\n"
        "## Test Strategy\nF.\n\n## Dependency Graph\nG.\n\n## Open Questions\nH.\n"
    )
    (tmp_path / "docs").mkdir(exist_ok=True)
    (tmp_path / "docs" / "notes.md").write_text("x")
    return task_dir


def _ref_errors(task_dir: Path) -> list[str]:
    errors = validate_task_artifacts(task_dir, run_gap=False)
    return [e for e in errors if "Reference Code path" in e]


def test_bare_filename_tokens_are_not_path_checked(tmp_path: Path) -> None:
    task_dir = _write_task(tmp_path, "See `policy.json` and `settings.yaml` concepts.")
    assert _ref_errors(task_dir) == []


def test_missing_slash_path_still_rejected(tmp_path: Path) -> None:
    task_dir = _write_task(tmp_path, "See `src/missing/file.py`.")
    errors = _ref_errors(task_dir)
    assert errors
    assert "Files to be created" in errors[0]  # remediation guidance present


def test_files_to_be_created_section_exempts_paths(tmp_path: Path) -> None:
    task_dir = _write_task(
        tmp_path,
        "See `src/new/module.py`.\n\n"
        "### Files to be created\n\n- `src/new/module.py`\n",
    )
    assert _ref_errors(task_dir) == []


def test_existing_path_passes(tmp_path: Path) -> None:
    task_dir = _write_task(tmp_path, "See `docs/notes.md`.")
    assert _ref_errors(task_dir) == []
