"""Regressions from the 7.5.31 field report (high-risk Django/DRF task).

1. A deep-tier ensemble report from a finished cascade stayed binding after a
   re-audit whose voting tiers passed, so its already-fixed findings blocked
   every later cycle until the retry cap.
2. Plan audit had no ``to-be-created`` exemption for API Contracts, treated a
   two-path cell as one endpoint, and never saw Django routes mounted through
   ``include()``.
3. The repair-log builder queued one fix per auditor for a defect a dozen
   auditors reported at the same line, split annotated citations into junk
   paths, and scoped citation-less findings to a whole segment.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))
sys.path.insert(0, str(ROOT / "tests"))

from ctl import _repair_files_for_finding, _split_citation  # noqa: E402
from lib_validate import collect_repair_findings  # noqa: E402
from plan_gap_analysis import analyze_api_contracts  # noqa: E402
from test_repair_findings_ensemble import (  # noqa: E402
    DEEP,
    FAST,
    _ensemble_entry,
    _report,
    _setup,
    _shard,
)


def _blocking(fid: str, location: str, category: str = "bug") -> dict:
    return {"id": fid, "severity": "high", "blocking": True, "location": location, "category": category}


# ---------------------------------------------------------------------------
# 1. Stale deep-tier report after a passing re-audit
# ---------------------------------------------------------------------------

def test_passing_reaudit_supersedes_the_previous_deep_tier_report(tmp_path: Path) -> None:
    td = _setup(tmp_path, [_ensemble_entry("security-auditor")])
    # Attempt 1: fast tier blocks, deep tier confirms F.
    _report(td, "security-auditor", FAST, [_blocking("sec-01", "src/auth.py:10")], attempt=1)
    _report(td, "security-auditor", DEEP, [_blocking("sec-R8-01", "src/auth.py:10")], attempt=1)
    _shard(td, "security-auditor", DEEP, 1, "2026-10-27T00:00:05Z")
    # Attempt 2 after repair: the cascade restarts at the fast tier, passes,
    # and never reaches the deep tier again.
    _report(td, "security-auditor", FAST, [], attempt=2)
    _shard(td, "security-auditor", FAST, 0, "2026-10-27T01:00:00Z")

    view = collect_repair_findings(td)

    assert view["findings"] == []
    deep = next(r for r in view["reports"] if r["model"] == DEEP)
    assert deep["superseded_by"] == f"cascade pass at attempt-2: {DEEP} did not run"


def test_passing_cascade_keeps_a_deep_report_from_the_current_attempt(tmp_path: Path) -> None:
    """Fail closed: only deep reports older than the current fast-tier attempt
    are from a finished cascade."""
    td = _setup(tmp_path, [_ensemble_entry("security-auditor")])
    _report(td, "security-auditor", FAST, [], attempt=1)
    _report(td, "security-auditor", DEEP, [_blocking("sec-01", "src/auth.py:10")], attempt=1)
    _shard(td, "security-auditor", FAST, 0, "2026-10-27T01:00:00Z")

    view = collect_repair_findings(td)

    assert [f["id"] for f in view["findings"]] == ["sec-01"]


def test_escalated_reaudit_still_binds_the_new_deep_report(tmp_path: Path) -> None:
    td = _setup(tmp_path, [_ensemble_entry("security-auditor")])
    _report(td, "security-auditor", DEEP, [_blocking("sec-old", "src/auth.py:10")], attempt=1)
    _report(td, "security-auditor", FAST, [_blocking("sec-01", "src/auth.py:20")], attempt=2)
    _report(td, "security-auditor", DEEP, [_blocking("sec-new", "src/auth.py:20")], attempt=2)
    _shard(td, "security-auditor", FAST, 1, "2026-10-27T01:00:00Z")
    _shard(td, "security-auditor", DEEP, 1, "2026-10-27T01:00:05Z")

    view = collect_repair_findings(td)

    assert [f["id"] for f in view["findings"]] == ["sec-new"]


# ---------------------------------------------------------------------------
# 3a. One defect, many auditors
# ---------------------------------------------------------------------------

def test_same_file_and_line_merges_across_auditors_and_categories(tmp_path: Path) -> None:
    td = _setup(tmp_path, [])
    for auditor, fid, category in (
        ("performance-auditor", "PERF-2-1", "performance"),
        ("security-auditor", "sec-01", "dos"),
        ("release-auditor", "REL-3", "rollout"),
    ):
        _report(td, auditor, DEEP, [_blocking(fid, "src/pagination.py:81", category)])

    findings = collect_repair_findings(td)["findings"]

    assert len(findings) == 1
    assert len(findings[0]["_merged"]) == 2


def test_file_only_findings_with_different_categories_stay_apart(tmp_path: Path) -> None:
    td = _setup(tmp_path, [])
    _report(td, "security-auditor", DEEP, [_blocking("sec-01", "src/auth.py", "authz")])
    _report(td, "performance-auditor", DEEP, [_blocking("PERF-1", "src/auth.py", "n+1")])

    assert len(collect_repair_findings(td)["findings"]) == 2


# ---------------------------------------------------------------------------
# 3b. Citation parsing and file scope
# ---------------------------------------------------------------------------

def test_annotated_citation_does_not_produce_junk_paths() -> None:
    raw = "apps/core/pagination.py:81, :103-104 (DB-eval guard, see revision.py)"
    assert _split_citation(raw) == ["apps/core/pagination.py"]


def test_citation_line_forms_are_stripped() -> None:
    assert _split_citation("a/b.py:L81; c/d.ts:7-9") == ["a/b.py", "c/d.ts"]


def _project(td: Path) -> Path:
    root = td.parent.parent
    for rel in ("src/auth.py", "src/a.py", "apps/audit/revision.py", "web/app.tsx"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("x\n")
    # Untracked files are listed too (``ls-files --others``); no commit needed.
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


def test_bare_basename_resolves_to_the_one_repo_file(tmp_path: Path) -> None:
    td = _setup(tmp_path, [])
    _project(td)
    files, _ = _repair_files_for_finding(td, {"id": "DI-2", "location": "revision.py:40"}, None, "db-executor")
    assert files == ["apps/audit/revision.py"]


def test_citation_less_finding_is_scoped_to_files_its_prose_names(tmp_path: Path) -> None:
    td = _setup(tmp_path, [])
    _project(td)
    finding = {
        "id": "DI-2",
        "location": "",
        "description": "Race between two writers in apps/audit/revision.py when saving.",
    }
    # db-executor owns no segment that lists revision.py; the old fallback
    # handed the executor files it was not meant to fix.
    files, _ = _repair_files_for_finding(td, finding, None, "db-executor")
    assert files == ["apps/audit/revision.py"]


def test_citation_less_finding_with_no_named_file_still_falls_back(tmp_path: Path) -> None:
    td = _setup(tmp_path, [])
    _project(td)
    files, _ = _repair_files_for_finding(
        td, {"id": "AC-001", "description": "Acceptance criterion 3 is not met."}, None, "ui-executor",
    )
    assert files == ["web/app.tsx"]


# ---------------------------------------------------------------------------
# 2. API Contracts gap analysis
# ---------------------------------------------------------------------------

def _plan(rows: str) -> str:
    return (
        "# Plan\n\n## API Contracts\n\n"
        "| Endpoint | Method | Notes |\n|---|---|---|\n"
        f"{rows}\n"
    )


def _django_project(root: Path) -> None:
    (root / "config").mkdir(parents=True)
    (root / "config" / "urls.py").write_text(
        'from django.urls import include, path\n'
        'urlpatterns = [path("api/v1/", include("apps.core.urls"))]\n'
    )
    (root / "apps" / "core").mkdir(parents=True)
    (root / "apps" / "core" / "urls.py").write_text(
        'from django.urls import path\n'
        'urlpatterns = [\n'
        '    path("health/live", views.live),\n'
        '    path("widgets/<int:pk>/", views.widget),\n'
        ']\n'
    )


def test_django_include_prefix_is_resolved(tmp_path: Path) -> None:
    _django_project(tmp_path)
    result = analyze_api_contracts(
        _plan("| `/api/v1/health/live` | GET | |\n| `/api/v1/widgets/{id}` | GET | |"), tmp_path,
    )
    assert result["unverified"] == []
    assert result["verified"] == 2


def test_django_route_under_the_wrong_prefix_is_unverified(tmp_path: Path) -> None:
    _django_project(tmp_path)
    result = analyze_api_contracts(_plan("| `/api/v2/health/live` | GET | |"), tmp_path)
    assert [c["endpoint"] for c in result["unverified"]] == ["/api/v2/health/live"]


def test_to_be_created_endpoint_is_exempt(tmp_path: Path) -> None:
    _django_project(tmp_path)
    result = analyze_api_contracts(
        _plan("| `/api/v1/health/ready` | GET | to-be-created |\n| `/api/v1/health/live` | GET | |"), tmp_path,
    )
    assert result["unverified"] == []
    assert [c["endpoint"] for c in result["to_be_created"]] == ["/api/v1/health/ready"]


def test_multi_path_cell_is_several_endpoints(tmp_path: Path) -> None:
    _django_project(tmp_path)
    result = analyze_api_contracts(
        _plan("| `/api/v1/health/live`, `/api/v1/nope` | GET | |"), tmp_path,
    )
    assert result["claimed_endpoints"] == 2
    assert [c["endpoint"] for c in result["unverified"]] == ["/api/v1/nope"]
