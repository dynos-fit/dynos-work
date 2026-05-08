"""TDD-first tests for cmd_write_search_receipt validation (task-20260507-005).

AC coverage:
  AC 6 — urls_consulted and findings_summary required when search_recommended=true;
          validated at write time; stored in receipt JSON
  AC 7 — When search_recommended=false, new fields not required (zero-friction path)

These tests are RED until ctl.py is updated with the new --urls-consulted and
--findings-summary arguments and validation logic.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CTL = str(ROOT / "hooks" / "ctl.py")


def _make_task_dir(tmp_path: Path, *, search_recommended: bool = True) -> Path:
    """Create a task dir with a pre-written external-solution-gate.json."""
    task_dir = tmp_path / ".dynos" / "task-20260507-receipt-test"
    task_dir.mkdir(parents=True)
    manifest = {
        "task_id": task_dir.name,
        "stage": "SPEC_NORMALIZATION",
        "classification": {"type": "feature", "risk_level": "medium", "domains": []},
    }
    (task_dir / "manifest.json").write_text(json.dumps(manifest))
    gate = {
        "search_recommended": search_recommended,
        "search_used": False,
        "query_reason": "external search recommended" if search_recommended else "local sufficient",
        "candidates": [],
        "recommended_choice": None,
        "decision_basis": {},
    }
    (task_dir / "external-solution-gate.json").write_text(json.dumps(gate))
    (task_dir / "receipts").mkdir(parents=True, exist_ok=True)
    return task_dir


def _run_write_receipt(task_dir: Path, extra_args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, CTL, "write-search-receipt", str(task_dir)] + extra_args,
        capture_output=True, text=True,
        cwd=str(ROOT),
    )


VALID_SUMMARY = "x" * 210  # 210 chars, well above the 200-char minimum
VALID_URL = "https://example.com/page1"
VALID_QUERY = "circuit breaker hystrix"


# ---------------------------------------------------------------------------
# AC 6: Rejection cases when search_recommended=true
# ---------------------------------------------------------------------------

def test_write_search_receipt_rejects_missing_urls_when_search_recommended(tmp_path: Path) -> None:
    """AC 6: When search_recommended=true and --urls-consulted absent, exits 1."""
    task_dir = _make_task_dir(tmp_path, search_recommended=True)
    result = _run_write_receipt(task_dir, [
        "--query", VALID_QUERY,
        "--findings-summary", VALID_SUMMARY,
        # no --urls-consulted
    ])
    assert result.returncode == 1, (
        f"Expected exit 1 for missing --urls-consulted.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "urls_consulted" in result.stderr.lower() or "urls-consulted" in result.stderr.lower(), (
        f"stderr must name the missing field.\nstderr: {result.stderr}"
    )
    # No partial receipt should be written
    receipt_path = task_dir / "receipts" / "search-conducted.json"
    assert not receipt_path.exists(), "Receipt must NOT be written on rejection"


def test_write_search_receipt_rejects_non_https_url(tmp_path: Path) -> None:
    """AC 6: A URL not matching ^https?:// causes exit 1 with the offending value named."""
    task_dir = _make_task_dir(tmp_path, search_recommended=True)
    result = _run_write_receipt(task_dir, [
        "--query", VALID_QUERY,
        "--urls-consulted", "ftp://example.com/invalid",
        "--findings-summary", VALID_SUMMARY,
    ])
    assert result.returncode == 1, (
        f"Expected exit 1 for non-https URL.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "ftp://example.com/invalid" in result.stderr, (
        f"stderr must name the offending URL.\nstderr: {result.stderr}"
    )
    assert not (task_dir / "receipts" / "search-conducted.json").exists()


def test_write_search_receipt_rejects_short_findings_summary(tmp_path: Path) -> None:
    """AC 6: findings_summary shorter than 200 chars causes exit 1."""
    task_dir = _make_task_dir(tmp_path, search_recommended=True)
    short_summary = "x" * 50  # 50 chars — below the 200-char minimum
    result = _run_write_receipt(task_dir, [
        "--query", VALID_QUERY,
        "--urls-consulted", VALID_URL,
        "--findings-summary", short_summary,
    ])
    assert result.returncode == 1, (
        f"Expected exit 1 for short findings summary.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    # stderr must state actual length and minimum
    stderr = result.stderr
    assert "50" in stderr or "200" in stderr, (
        f"stderr must state actual length (50) and minimum (200).\nstderr: {stderr}"
    )
    assert not (task_dir / "receipts" / "search-conducted.json").exists()


def test_write_search_receipt_rejects_missing_findings_summary(tmp_path: Path) -> None:
    """AC 6: When search_recommended=true and --findings-summary absent, exits 1."""
    task_dir = _make_task_dir(tmp_path, search_recommended=True)
    result = _run_write_receipt(task_dir, [
        "--query", VALID_QUERY,
        "--urls-consulted", VALID_URL,
        # no --findings-summary
    ])
    assert result.returncode == 1, (
        f"Expected exit 1 for missing --findings-summary.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "findings" in result.stderr.lower(), (
        f"stderr must name the missing field.\nstderr: {result.stderr}"
    )
    assert not (task_dir / "receipts" / "search-conducted.json").exists()


# ---------------------------------------------------------------------------
# AC 6: Happy path — valid inputs are admitted and stored
# ---------------------------------------------------------------------------

def test_write_search_receipt_admits_valid(tmp_path: Path) -> None:
    """AC 6: Valid inputs with search_recommended=true produce exit 0 and a receipt."""
    task_dir = _make_task_dir(tmp_path, search_recommended=True)
    result = _run_write_receipt(task_dir, [
        "--query", VALID_QUERY,
        "--urls-consulted", VALID_URL,
        "--findings-summary", VALID_SUMMARY,
    ])
    assert result.returncode == 0, (
        f"Expected exit 0 for valid inputs.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    receipt_path = task_dir / "receipts" / "search-conducted.json"
    assert receipt_path.exists(), "Receipt must be written on success"
    receipt = json.loads(receipt_path.read_text())
    assert receipt.get("query") == VALID_QUERY
    # urls_consulted and findings_summary must be persisted in receipt JSON
    assert "urls_consulted" in receipt, "urls_consulted must be stored in receipt"
    assert "findings_summary" in receipt, "findings_summary must be stored in receipt"
    assert isinstance(receipt["urls_consulted"], list)
    assert VALID_URL in receipt["urls_consulted"]
    assert receipt["findings_summary"] == VALID_SUMMARY


def test_write_search_receipt_persists_urls_and_summary_in_receipt_json(tmp_path: Path) -> None:
    """AC 6: urls_consulted list and findings_summary string are persisted verbatim."""
    task_dir = _make_task_dir(tmp_path, search_recommended=True)
    urls = "https://a.example.com,https://b.example.com"
    summary = "A" * 250
    result = _run_write_receipt(task_dir, [
        "--query", VALID_QUERY,
        "--urls-consulted", urls,
        "--findings-summary", summary,
    ])
    assert result.returncode == 0, f"stderr: {result.stderr}"
    receipt = json.loads((task_dir / "receipts" / "search-conducted.json").read_text())
    assert isinstance(receipt["urls_consulted"], list)
    assert len(receipt["urls_consulted"]) == 2
    assert "https://a.example.com" in receipt["urls_consulted"]
    assert "https://b.example.com" in receipt["urls_consulted"]
    assert receipt["findings_summary"] == summary


# ---------------------------------------------------------------------------
# AC 7: Zero-friction path when search_recommended=false
# ---------------------------------------------------------------------------

def test_write_search_receipt_optional_fields_when_search_not_recommended(tmp_path: Path) -> None:
    """AC 7: When search_recommended=false, new fields are not required; exit 0."""
    task_dir = _make_task_dir(tmp_path, search_recommended=False)
    result = _run_write_receipt(task_dir, [
        "--query", VALID_QUERY,
        # no --urls-consulted, no --findings-summary
    ])
    assert result.returncode == 0, (
        f"Expected exit 0 when search_recommended=false without new fields.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert (task_dir / "receipts" / "search-conducted.json").exists()


def test_write_search_receipt_accepts_comma_separated_urls(tmp_path: Path) -> None:
    """AC 6: Comma-separated URLs in a single --urls-consulted value are split into a list."""
    task_dir = _make_task_dir(tmp_path, search_recommended=True)
    result = _run_write_receipt(task_dir, [
        "--query", VALID_QUERY,
        "--urls-consulted", "https://a.com,https://b.com,https://c.com",
        "--findings-summary", VALID_SUMMARY,
    ])
    assert result.returncode == 0, f"stderr: {result.stderr}"
    receipt = json.loads((task_dir / "receipts" / "search-conducted.json").read_text())
    urls = receipt.get("urls_consulted", [])
    assert len(urls) == 3
    assert "https://a.com" in urls
    assert "https://b.com" in urls
    assert "https://c.com" in urls
