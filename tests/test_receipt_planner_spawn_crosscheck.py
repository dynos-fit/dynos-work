"""Planner receipts bind to hook-observed spawn-log evidence."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_receipts import receipt_planner_spawn  # noqa: E402


def _task_dir(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260418-PL"
    td.mkdir(parents=True)
    return td


def test_missing_spawn_log_degrades_and_emits_event(tmp_path: Path):
    td = _task_dir(tmp_path)
    receipt = receipt_planner_spawn(td, "discovery", tokens_used=0)
    payload = json.loads(receipt.read_text())
    assert payload["spawn_evidence"] == "degraded"
    assert "injected_prompt_sha256" not in payload
    events = td / "events.jsonl"
    assert "planner_spawn_evidence_degraded" in events.read_text()


def test_spawn_log_requires_a_new_planner_spawn(tmp_path: Path):
    td = _task_dir(tmp_path)
    (td / "spawn-log.jsonl").write_text(json.dumps({"phase":"pre", "subagent_type":"dynos-work:planning", "timestamp":"2026-01-01T00:00:00Z"})+"\n")
    with pytest.raises(ValueError, match="no planner spawn recorded"):
        receipt_planner_spawn(td, "spec", tokens_used=0)


def test_receipt_binds_latest_post_spawn_hashes(tmp_path: Path):
    td = _task_dir(tmp_path)
    rows = [
        {"phase":"pre", "subagent_type":"dynos-work:planning", "timestamp":"2026-01-01T00:00:00Z", "prompt_sha256":"p1"},
        {"phase":"post", "subagent_type":"dynos-work:planning", "timestamp":"2026-01-01T00:01:00Z", "prompt_sha256":"p1", "result_sha256":"r1"},
    ]
    (td / "spawn-log.jsonl").write_text("".join(json.dumps(row)+"\n" for row in rows))
    receipt = receipt_planner_spawn(td, "discovery", tokens_used=0)
    payload = json.loads(receipt.read_text())
    assert payload["spawn_evidence"] == "hook"
    assert payload["spawn_prompt_sha256"] == "p1"
    assert payload["spawn_result_sha256"] == "r1"


def test_receipt_refuses_spawn_older_than_previous_receipt(tmp_path: Path):
    td = _task_dir(tmp_path)
    receipts = td / "receipts"
    receipts.mkdir()
    (receipts / "planner-discovery.json").write_text(json.dumps({"ts":"2026-01-02T00:00:00Z","valid":True,"step":"planner-discovery"}))
    (td / "spawn-log.jsonl").write_text(json.dumps({"phase":"post", "subagent_type":"planning", "timestamp":"2026-01-01T00:00:00Z", "prompt_sha256":"p", "result_sha256":"r"})+"\n")
    with pytest.raises(ValueError, match="no planner spawn recorded"):
        receipt_planner_spawn(td, "spec", tokens_used=0)
