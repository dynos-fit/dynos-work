"""Tests for receipt_rules_check_passed (AC 1) — new signature + self-compute.

The writer's signature is now `(task_dir, mode)` — caller cannot supply the
counts, hashes, or engine version. Everything is computed internally from
`rules_engine.run_checks`. Legacy keyword callers must break (TypeError).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

import lib_receipts  # noqa: E402
from lib_receipts import receipt_rules_check_passed  # noqa: E402


def _make_task(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260419-RC"
    td.mkdir(parents=True)
    return td


def test_new_signature_writes_valid_receipt(tmp_path: Path, monkeypatch):
    """AC 1: caller passes (task_dir, mode) only — writer self-computes counts."""
    td = _make_task(tmp_path)
    # Stub run_checks to return a clean result — no violations.
    import rules_engine
    monkeypatch.setattr(rules_engine, "run_checks", lambda root, mode: [])
    monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "dynos-home"))

    out = receipt_rules_check_passed(td, "all")
    assert out.exists()
    payload = json.loads(out.read_text())
    assert payload["step"] == "rules-check-passed"
    assert payload["valid"] is True
    assert payload["violations_count"] == 0
    assert payload["error_violations"] == 0
    assert payload["advisory_violations"] == 0
    assert payload["mode"] == "all"
    # engine_version and rules_file_sha256 must also be present
    assert "engine_version" in payload
    assert "rules_file_sha256" in payload


def test_legacy_kwargs_raise_type_error(tmp_path: Path):
    """AC 1: the old 4-kwarg signature must break with TypeError."""
    td = _make_task(tmp_path)
    with pytest.raises(TypeError):
        receipt_rules_check_passed(  # type: ignore[call-arg]
            td,
            rules_evaluated=2,
            violations_count=0,
            error_violations=0,
            mode="all",
        )


def test_bad_mode_raises_value_error(tmp_path: Path):
    """mode must be 'staged' or 'all'."""
    td = _make_task(tmp_path)
    with pytest.raises(ValueError):
        receipt_rules_check_passed(td, "garbage")


def test_error_violations_positive_refuses(tmp_path: Path, monkeypatch):
    """AC 1 refuse-by-construction: error_violations > 0 → writer refuses."""
    td = _make_task(tmp_path)
    import rules_engine
    # Two violations — one error, one warn.
    monkeypatch.setattr(
        rules_engine,
        "run_checks",
        lambda root, mode: [
            SimpleNamespace(severity="error", rule_id="r1"),
            SimpleNamespace(severity="warn", rule_id="r2"),
        ],
    )
    monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "dynos-home"))
    with pytest.raises(ValueError) as exc_info:
        receipt_rules_check_passed(td, "all")
    # Refusal message must include the literal error_violations=N token.
    assert "error_violations=1" in str(exc_info.value)
    assert "REFUSES" in str(exc_info.value)
    # No receipt should have been written.
    assert not (td / "receipts" / "rules-check-passed.json").exists()


def test_zero_errors_records_correct_counts(tmp_path: Path, monkeypatch):
    """AC 1: with 0 errors but 3 warnings, receipt records correct counts."""
    td = _make_task(tmp_path)
    import rules_engine
    monkeypatch.setattr(
        rules_engine,
        "run_checks",
        lambda root, mode: [
            SimpleNamespace(severity="warn", rule_id=f"r{i}")
            for i in range(3)
        ],
    )
    monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "dynos-home"))

    out = receipt_rules_check_passed(td, "staged")
    payload = json.loads(out.read_text())
    assert payload["error_violations"] == 0
    assert payload["advisory_violations"] == 3
    assert payload["violations_count"] == 3
    assert payload["mode"] == "staged"


def test_rules_file_hash_computed_when_present(tmp_path: Path, monkeypatch):
    """AC 1: when prevention-rules.json exists, rules_file_sha256 is its hash,
    and rules_evaluated counts the 'rules' list length."""
    td = _make_task(tmp_path)
    import rules_engine
    monkeypatch.setattr(rules_engine, "run_checks", lambda root, mode: [])

    # Create a persistent dir with a prevention-rules.json
    home = tmp_path / "dynos-home"
    monkeypatch.setenv("DYNOS_HOME", str(home))

    from lib_core import ensure_persistent_project_dir
    root = td.parent.parent
    pd = ensure_persistent_project_dir(root)
    rules_payload = {"rules": [{"id": "r1"}, {"id": "r2"}, {"id": "r3"}]}
    (pd / "prevention-rules.json").write_text(json.dumps(rules_payload))

    out = receipt_rules_check_passed(td, "all")
    payload = json.loads(out.read_text())
    assert payload["rules_evaluated"] == 3
    # rules_file_sha256 is a 64-char hex digest when file exists
    assert payload["rules_file_sha256"] != "none"
    assert len(payload["rules_file_sha256"]) == 64


def test_rules_file_absent_uses_none_sentinel(tmp_path: Path, monkeypatch):
    """AC 1: when no rules file, rules_file_sha256 literal 'none' and count=0."""
    td = _make_task(tmp_path)
    import rules_engine
    monkeypatch.setattr(rules_engine, "run_checks", lambda root, mode: [])
    monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "dynos-home"))

    out = receipt_rules_check_passed(td, "all")
    payload = json.loads(out.read_text())
    assert payload["rules_file_sha256"] == "none"
    assert payload["rules_evaluated"] == 0
