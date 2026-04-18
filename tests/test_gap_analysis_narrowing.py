"""Regression tests for narrow-first plan_gap_analysis scanning.

Verifies the optimization is a STRICT SUPERSET of the prior full-scan
behavior — same correctness on every input, less work in the common case.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))


def _setup_repo(tmp_path: Path):
    """Create a small repo with one route in the plan-implied path and
    one route in an unrelated path."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "users.py").write_text(
        "from flask import Blueprint\nbp = Blueprint('u', __name__)\n"
        "@bp.get('/api/users')\ndef list_users(): pass\n"
    )
    (tmp_path / "legacy").mkdir()
    (tmp_path / "legacy" / "old.py").write_text(
        "@app.get('/api/legacy/health')\ndef health(): pass\n"
    )


class TestNarrowFirst:
    def test_all_claims_match_in_narrow_set_skips_full_scan(self, tmp_path: Path):
        """When the plan only references files inside src/ and the claim
        matches inside src/, the full repo walk must NOT run."""
        _setup_repo(tmp_path)
        plan = (
            "## Reference Code\n- `src/users.py`\n\n"
            "## API Contracts\n| Endpoint | Method |\n|---|---|\n| `/api/users` | GET |\n"
        )

        from plan_gap_analysis import analyze_api_contracts
        with mock.patch("plan_gap_analysis._iter_code_files") as mock_full:
            mock_full.return_value = []
            result = analyze_api_contracts(plan, tmp_path)
        assert mock_full.call_count == 0, (
            "narrow scan matched all claims; full repo walk must not be invoked"
        )
        assert result["unverified"] == [], "claim should be matched in narrow set"

    def test_unmatched_claim_falls_back_to_full_scan(self, tmp_path: Path):
        """When the plan-implied subset doesn't satisfy a claim, full repo
        scan MUST run as fallback so we never miss a real definition."""
        _setup_repo(tmp_path)
        # Plan only references src/, but claim is for /api/legacy/health which lives in legacy/
        plan = (
            "## Reference Code\n- `src/users.py`\n\n"
            "## API Contracts\n| Endpoint | Method |\n|---|---|\n| `/api/legacy/health` | GET |\n"
        )

        from plan_gap_analysis import analyze_api_contracts
        result = analyze_api_contracts(plan, tmp_path)
        # Result must reflect the full-scan finding (no real fallback would mean unverified)
        assert result["unverified"] == [], (
            "fallback to full scan must find /api/legacy/health and verify it"
        )

    def test_no_plan_paths_uses_full_scan(self, tmp_path: Path):
        """When the plan doesn't reference any paths, the narrow path is
        empty and full scan happens unconditionally — same as before."""
        _setup_repo(tmp_path)
        plan = (
            "## API Contracts\n| Endpoint | Method |\n|---|---|\n| `/api/users` | GET |\n"
        )

        from plan_gap_analysis import analyze_api_contracts
        result = analyze_api_contracts(plan, tmp_path)
        assert result["unverified"] == [], "full scan must verify the claim"

    def test_data_model_narrows_too(self, tmp_path: Path):
        """The same narrow-first optimization applies to data model scans."""
        (tmp_path / "models").mkdir()
        (tmp_path / "models" / "user.py").write_text(
            "from sqlalchemy.orm import DeclarativeBase\nclass Base(DeclarativeBase): pass\n"
            "class User(Base): pass\n"
        )
        plan = (
            "## Reference Code\n- `models/user.py`\n\n"
            "## Data Model\n| Table | Column |\n|---|---|\n| `User` | `id` |\n"
        )

        from plan_gap_analysis import analyze_data_model
        with mock.patch("plan_gap_analysis._iter_code_files") as mock_full:
            mock_full.return_value = []
            result = analyze_data_model(plan, tmp_path)
        assert mock_full.call_count == 0, (
            "narrow scan should match data model claims; no full walk"
        )
        assert result["unverified"] == [], "User model should be verified"

    def test_path_traversal_outside_repo_ignored(self, tmp_path: Path):
        """A plan that names ../ paths must not let the scanner escape the repo."""
        _setup_repo(tmp_path)
        plan = (
            "## Reference Code\n- `../etc/passwd`\n- `src/users.py`\n\n"
            "## API Contracts\n| Endpoint | Method |\n|---|---|\n| `/api/users` | GET |\n"
        )

        from plan_gap_analysis import _extract_plan_paths
        paths = _extract_plan_paths(plan)
        assert "../etc/passwd" not in paths and "etc/passwd" not in paths, (
            f"path traversal must be filtered, got {paths}"
        )
