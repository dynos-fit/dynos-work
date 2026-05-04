"""Tests for derive_user_summary + verify-audit-summary-text (task-20260503-002).

Pure unit tests for derive_user_summary; subprocess tests for the CLI.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

from lib_validate import derive_user_summary  # noqa: E402


# ---- Pure derivation tests (AC 1-4, AC 9 cases) ----

def test_derive_clean_pass():
    """All auditors PASS → header is ALL PASSED, all per-auditor lines PASS."""
    summary = {
        "task_id": "task-20260503-001",
        "reports": [
            {"auditor_name": "spec-completion-auditor", "blocking_count": 0, "finding_count": 0},
            {"auditor_name": "security-auditor", "blocking_count": 0, "finding_count": 2},
            {"auditor_name": "code-quality-auditor", "blocking_count": 0, "finding_count": 5},
        ],
    }
    result = derive_user_summary(summary)
    assert result.startswith("Audit complete — ALL PASSED\n")
    assert "  spec-completion-auditor: PASS" in result
    assert "  security-auditor: PASS" in result
    assert "  code-quality-auditor: PASS" in result
    assert "FAIL" not in result
    assert "Task complete. Snapshot branch dynos/task-20260503-001-snapshot" in result
    # No trailing newline
    assert not result.endswith("\n")


def test_derive_with_findings():
    """One auditor with blocking → FAILED header + that auditor's FAIL line."""
    summary = {
        "task_id": "task-X",
        "reports": [
            {"auditor_name": "spec-completion-auditor", "blocking_count": 0},
            {"auditor_name": "security-auditor", "blocking_count": 2},
            {"auditor_name": "code-quality-auditor", "blocking_count": 0},
        ],
    }
    result = derive_user_summary(summary)
    assert result.startswith("Audit complete — FAILED — 2 blocking findings\n")
    assert "  security-auditor: FAIL (2 blocking)" in result
    assert "  spec-completion-auditor: PASS" in result


def test_derive_skipped_auditor_not_in_summary():
    """Audit-plan may have skipped auditors; they don't appear in reports →
    they're absent from user_summary."""
    summary = {
        "task_id": "task-X",
        "reports": [
            {"auditor_name": "spec-completion-auditor", "blocking_count": 0},
            {"auditor_name": "security-auditor", "blocking_count": 0},
        ],
    }
    result = derive_user_summary(summary)
    # Only the 2 auditors that actually ran
    assert "spec-completion-auditor" in result
    assert "security-auditor" in result
    assert "code-quality-auditor" not in result
    assert "performance-auditor" not in result
    # Total count of "auditor:" lines
    assert result.count("auditor:") == 2


def test_derive_multi_cycle_aggregates():
    """Same auditor with two reports (cycle 1 + reaudit) → blocking_counts sum."""
    summary = {
        "task_id": "task-X",
        "reports": [
            {"auditor_name": "security-auditor", "blocking_count": 1},  # cycle 1
            {"auditor_name": "security-auditor", "blocking_count": 0},  # reaudit cycle 2
        ],
    }
    result = derive_user_summary(summary)
    # Sum of blocking is 1
    assert "FAILED — 1 blocking findings" in result
    assert "  security-auditor: FAIL (1 blocking)" in result
    # Only ONE per-auditor line (aggregated by name)
    assert result.count("security-auditor") == 1


def test_derive_empty_reports():
    """Empty reports → header + blank line + footer only."""
    summary = {"task_id": "task-X", "reports": []}
    result = derive_user_summary(summary)
    expected = (
        "Audit complete — ALL PASSED\n"
        "\n"
        "Task complete. Snapshot branch dynos/task-X-snapshot can be deleted if desired."
    )
    assert result == expected


def test_derive_no_trailing_newline():
    """Result must not end with \\n. Caller can add one if printing."""
    summary = {"task_id": "task-X", "reports": []}
    assert not derive_user_summary(summary).endswith("\n")
    # Also for non-empty case
    summary2 = {
        "task_id": "task-X",
        "reports": [{"auditor_name": "spec-completion-auditor", "blocking_count": 0}],
    }
    assert not derive_user_summary(summary2).endswith("\n")


def test_derive_single_blocking_finding():
    """Scenario: one auditor with blocking_count=1 (singular)."""
    summary = {
        "task_id": "task-X",
        "reports": [
            {"auditor_name": "spec-completion-auditor", "blocking_count": 0},
            {"auditor_name": "security-auditor", "blocking_count": 1},
        ],
    }
    result = derive_user_summary(summary)
    assert "Audit complete — FAILED — 1 blocking findings" in result
    assert "  security-auditor: FAIL (1 blocking)" in result
    assert "  spec-completion-auditor: PASS" in result


def test_derive_multiple_blocking_across_auditors():
    """Scenario: multiple auditors each contributing blocking findings."""
    summary = {
        "task_id": "task-X",
        "reports": [
            {"auditor_name": "security-auditor", "blocking_count": 3},
            {"auditor_name": "code-quality-auditor", "blocking_count": 2},
            {"auditor_name": "spec-completion-auditor", "blocking_count": 0},
        ],
    }
    result = derive_user_summary(summary)
    # Total = 3 + 2 + 0 = 5
    assert "Audit complete — FAILED — 5 blocking findings" in result
    assert "  security-auditor: FAIL (3 blocking)" in result
    assert "  code-quality-auditor: FAIL (2 blocking)" in result
    assert "  spec-completion-auditor: PASS" in result


def test_derive_preserves_insertion_order():
    """Per-auditor lines appear in order of first appearance, not alphabetical."""
    summary = {
        "task_id": "task-X",
        "reports": [
            {"auditor_name": "zzz-last-auditor", "blocking_count": 0},
            {"auditor_name": "aaa-first-auditor", "blocking_count": 0},
        ],
    }
    result = derive_user_summary(summary)
    zzz_pos = result.index("zzz-last-auditor")
    aaa_pos = result.index("aaa-first-auditor")
    # Insertion order: zzz comes first because it's first in reports
    assert zzz_pos < aaa_pos, \
        f"insertion-order violated: zzz_pos={zzz_pos} aaa_pos={aaa_pos}"


def test_derive_em_dash_codepoint():
    """Header uses U+2014 em-dash, not '--' or U+2013 en-dash."""
    summary = {"task_id": "task-X", "reports": []}
    result = derive_user_summary(summary)
    # The em-dash codepoint is —
    assert "—" in result, "em-dash U+2014 must be present in header"
    assert any(ord(c) == 0x2014 for c in result), "codepoint must be U+2014 specifically"
    # Not the en-dash
    assert "–" not in result


# ---- CLI tests for verify-audit-summary-text (AC 7, AC 9 verify cases) ----

def _make_task_with_summary(tmp_path: Path, *, task_id: str = "task-X",
                             reports: list | None = None,
                             include_user_summary: bool = True,
                             tampered_user_summary: str | None = None) -> Path:
    """Build a task dir with audit-summary.json + completion.json on disk."""
    task_dir = tmp_path / ".dynos" / task_id
    task_dir.mkdir(parents=True)
    summary = {
        "task_id": task_id,
        "reports": reports or [],
        "total_blocking": sum(r.get("blocking_count", 0) for r in (reports or [])),
        "audit_result": "pass" if not any(r.get("blocking_count", 0) for r in (reports or [])) else "fail",
    }
    summary["user_summary"] = derive_user_summary(summary)
    (task_dir / "audit-summary.json").write_text(json.dumps(summary))

    completion = dict(summary)
    if not include_user_summary:
        completion.pop("user_summary", None)
    elif tampered_user_summary is not None:
        completion["user_summary"] = tampered_user_summary
    (task_dir / "completion.json").write_text(json.dumps(completion))
    return task_dir


def _ctl(*args, env=None) -> subprocess.CompletedProcess:
    repo_root = Path(__file__).resolve().parent.parent
    ctl = repo_root / "hooks" / "ctl.py"
    e = os.environ.copy()
    if env:
        e.update(env)
    return subprocess.run(
        [sys.executable, str(ctl), *args],
        capture_output=True, text=True, timeout=30, env=e,
    )


def test_verify_command_pass(tmp_path):
    """Legitimate completion.json + audit-summary.json → exit 0."""
    task_dir = _make_task_with_summary(tmp_path)
    r = _ctl("verify-audit-summary-text", str(task_dir))
    assert r.returncode == 0, f"expected 0, got {r.returncode}; stderr={r.stderr}"


def test_verify_command_tampered_text(tmp_path):
    """Tampered user_summary in completion.json → exit 1 with stored_sha+derived_sha."""
    task_dir = _make_task_with_summary(
        tmp_path,
        reports=[{"auditor_name": "security-auditor", "blocking_count": 0}],
        tampered_user_summary="Audit complete — ALL PASSED\n\n  security-auditor: PASS\n\nFAKE FOOTER",
    )
    r = _ctl("verify-audit-summary-text", str(task_dir))
    assert r.returncode == 1
    err = json.loads(r.stderr)
    assert err["error"] == "mismatch"
    assert "stored_sha" in err and "derived_sha" in err
    assert err["stored_sha"] != err["derived_sha"]


def test_verify_command_legacy_completion(tmp_path):
    """completion.json without user_summary → exit 3."""
    task_dir = _make_task_with_summary(tmp_path, include_user_summary=False)
    r = _ctl("verify-audit-summary-text", str(task_dir))
    assert r.returncode == 3
    err = json.loads(r.stderr)
    assert err["error"] == "legacy_completion_missing_user_summary"


def test_verify_command_missing_file(tmp_path):
    """completion.json missing → exit 2."""
    task_dir = tmp_path / ".dynos" / "task-X"
    task_dir.mkdir(parents=True)
    # Only audit-summary.json exists
    (task_dir / "audit-summary.json").write_text(json.dumps({"task_id": "task-X", "reports": []}))
    r = _ctl("verify-audit-summary-text", str(task_dir))
    assert r.returncode == 2
    err = json.loads(r.stderr)
    assert err["error"] == "missing_file"
    assert err["completion_exists"] is False
