"""Tests for receipt_tdd_tests writer (AC 16)."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

import lib_receipts  # noqa: E402
from lib_receipts import receipt_tdd_tests  # noqa: E402


def _task_dir(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260418-TT"
    td.mkdir(parents=True)
    return td


def test_happy_path_writes_receipt(tmp_path: Path):
    td = _task_dir(tmp_path)
    out = receipt_tdd_tests(
        td, ["tests/test_a.py", "tests/test_b.py"],
        "f" * 64, tokens_used=2500, model_used="haiku",
    )
    assert out.exists()
    payload = json.loads(out.read_text())
    assert payload["test_file_paths"] == ["tests/test_a.py", "tests/test_b.py"]
    assert payload["tests_evidence_sha256"] == "f" * 64
    assert payload["tokens_used"] == 2500
    assert payload["model_used"] == "haiku"
    assert payload["valid"] is True


def test_rejects_non_list_paths(tmp_path: Path):
    td = _task_dir(tmp_path)
    with pytest.raises(ValueError, match="test_file_paths"):
        receipt_tdd_tests(td, "tests/x.py", "f" * 64, 100, "haiku")  # type: ignore[arg-type]


def test_rejects_non_str_path_entries(tmp_path: Path):
    td = _task_dir(tmp_path)
    with pytest.raises(ValueError, match="test_file_paths"):
        receipt_tdd_tests(td, ["a", 2], "f" * 64, 100, "haiku")  # type: ignore[list-item]


def test_rejects_empty_evidence_sha256(tmp_path: Path):
    td = _task_dir(tmp_path)
    with pytest.raises(ValueError, match="tests_evidence_sha256"):
        receipt_tdd_tests(td, ["a.py"], "", 100, "haiku")


def test_rejects_empty_model_used(tmp_path: Path):
    td = _task_dir(tmp_path)
    with pytest.raises(ValueError, match="model_used"):
        receipt_tdd_tests(td, ["a.py"], "f" * 64, 100, "")


def test_record_tokens_invoked_when_tokens_positive(tmp_path: Path):
    td = _task_dir(tmp_path)
    with mock.patch.object(lib_receipts, "_record_tokens") as rt:
        receipt_tdd_tests(td, ["a.py"], "f" * 64, 1000, "haiku")
    assert rt.called
    args, _kwargs = rt.call_args
    assert args[1] == "tdd-tests"
    assert args[2] == "haiku"
    assert args[3] == 1000


def test_record_tokens_skipped_when_zero(tmp_path: Path):
    td = _task_dir(tmp_path)
    with mock.patch.object(lib_receipts, "_record_tokens") as rt:
        receipt_tdd_tests(td, ["a.py"], "f" * 64, 0, "haiku")
    assert not rt.called


def test_receipt_tokens_do_not_mutate_token_usage_json(tmp_path: Path):
    td = _task_dir(tmp_path)
    token_usage = td / "token-usage.json"
    baseline = {
        "agents": {"executor": 300},
        "by_agent": {
            "executor": {
                "input_tokens": 200,
                "output_tokens": 100,
                "tokens": 300,
                "model": "sonnet",
            }
        },
        "by_model": {
            "sonnet": {
                "input_tokens": 200,
                "output_tokens": 100,
                "tokens": 300,
            }
        },
        "total": 300,
        "total_input_tokens": 200,
        "total_output_tokens": 100,
        "events": [],
    }
    token_usage.write_text(json.dumps(baseline, indent=2))

    receipt_tdd_tests(td, ["a.py"], "f" * 64, 1000, "haiku")

    assert json.loads(token_usage.read_text()) == baseline
