"""TDD-first tests for receipt_audit_done spawn-log cross-check (task-20260430-006).

Closes the receipt-side half of the audit-chain forgery defense. Even with
the orchestrator-can't-self-elevate (active-segment-role lockdown) and the
orchestrator-can't-materialize-audit-reports (audit-skill prose) layers
in place, an attacker who could call `receipt_audit_done` directly
(e.g. via the ctl audit-receipt wrapper) could still write a clean
receipt for an auditor that never spawned. The receipt schema currently
trusts model_used as a self-reported string with no harness-level
counter-evidence.

This test suite drives the new contract:

  - When `.dynos/task-{id}/spawn-log.jsonl` exists, receipt_audit_done
    REQUIRES a matching `agent_spawn_post` entry for the auditor before
    accepting the write. Match key normalizes both sides via
    strip("audit-") + strip("-auditor"). Absence raises ValueError with
    a message naming the forgery defense.
  - When the matching post entry has `truncated: true` (stop_reason=
    max_tokens), receipt_audit_done REFUSES with a truncation-specific
    error. This is the task #11 piece — truncation must be a hard fail,
    not the orchestrator's choice to backfill from primary evidence.
  - When spawn-log.jsonl does not exist, receipt_audit_done emits an
    `audit_receipt_spawn_log_missing` event and proceeds (graceful
    degradation for old deployments and test fixtures that have not yet
    been updated to write a spawn-log entry).

The match-normalization (`audit-X` and `X-auditor` both equivalent to
`X`) tolerates both naming conventions seen in the codebase: the role
string `audit-spec-completion` and the agent file name
`spec-completion-auditor`.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_receipts import receipt_audit_done  # noqa: E402


def _task_dir(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260430-XL"
    td.mkdir(parents=True)
    return td


def _write_sidecar(td: Path, auditor: str, model: str, digest: str) -> Path:
    sidecar_dir = td / "receipts" / "_injected-auditor-prompts"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    p = sidecar_dir / f"{auditor}-{model}.sha256"
    p.write_text(digest)
    return p


def _write_spawn_log(td: Path, entries: list[dict]) -> Path:
    p = td / "spawn-log.jsonl"
    with p.open("w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")
    return p


def _ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Existing sidecar contract still holds; spawn-log enforcement is additive.
# ---------------------------------------------------------------------------
def test_matching_spawn_log_entry_passes(tmp_path: Path):
    td = _task_dir(tmp_path)
    digest = "a" * 64
    _write_sidecar(td, "security-auditor", "haiku", digest)
    _write_spawn_log(td, [
        {"phase": "pre", "tool": "Agent", "subagent_type": "security-auditor",
         "prompt_sha256": "p" * 64, "timestamp": _ts()},
        {"phase": "post", "tool": "Agent", "subagent_type": "security-auditor",
         "prompt_sha256": "p" * 64, "result_sha256": "r" * 64,
         "result_excerpt": "FINDINGS: none", "stop_reason": "end_turn",
         "timestamp": _ts()},
    ])
    out = receipt_audit_done(
        td, "security-auditor", "haiku", 0, 0, None, 100,
        route_mode="replace", agent_path="learned/x.md",
        injected_agent_sha256=digest,
    )
    assert out.exists()


def test_no_matching_spawn_log_entry_raises(tmp_path: Path):
    td = _task_dir(tmp_path)
    digest = "a" * 64
    _write_sidecar(td, "security-auditor", "haiku", digest)
    _write_spawn_log(td, [
        {"phase": "post", "tool": "Agent", "subagent_type": "spec-completion-auditor",
         "result_sha256": "r" * 64, "stop_reason": "end_turn", "timestamp": _ts()},
    ])
    with pytest.raises(ValueError, match="spawn-log"):
        receipt_audit_done(
            td, "security-auditor", "haiku", 0, 0, None, 100,
            route_mode="replace", agent_path="learned/x.md",
            injected_agent_sha256=digest,
        )


def test_truncated_spawn_log_entry_raises(tmp_path: Path):
    td = _task_dir(tmp_path)
    digest = "a" * 64
    _write_sidecar(td, "security-auditor", "haiku", digest)
    _write_spawn_log(td, [
        {"phase": "post", "tool": "Agent", "subagent_type": "security-auditor",
         "result_sha256": "r" * 64, "stop_reason": "max_tokens",
         "truncated": True, "timestamp": _ts()},
    ])
    with pytest.raises(ValueError, match="truncat"):
        receipt_audit_done(
            td, "security-auditor", "haiku", 0, 0, None, 100,
            route_mode="replace", agent_path="learned/x.md",
            injected_agent_sha256=digest,
        )


def test_subagent_type_audit_prefix_matches_auditor_suffix(tmp_path: Path):
    """spawn-log subagent_type 'audit-spec-completion' must match auditor_name
    'spec-completion-auditor' after normalization (both → 'spec-completion')."""
    td = _task_dir(tmp_path)
    digest = "a" * 64
    _write_sidecar(td, "spec-completion-auditor", "haiku", digest)
    _write_spawn_log(td, [
        {"phase": "post", "tool": "Agent", "subagent_type": "audit-spec-completion",
         "result_sha256": "r" * 64, "stop_reason": "end_turn", "timestamp": _ts()},
    ])
    out = receipt_audit_done(
        td, "spec-completion-auditor", "haiku", 0, 0, None, 100,
        route_mode="replace", agent_path="learned/x.md",
        injected_agent_sha256=digest,
    )
    assert out.exists()


def test_missing_spawn_log_file_proceeds_gracefully(tmp_path: Path, monkeypatch):
    """Old deployments and test fixtures without spawn-log.jsonl must still
    write receipts — the strict check kicks in only when the file exists.
    Production has the file because hooks.json registers the hook."""
    td = _task_dir(tmp_path)
    digest = "a" * 64
    _write_sidecar(td, "security-auditor", "haiku", digest)
    # No spawn-log.jsonl at all
    out = receipt_audit_done(
        td, "security-auditor", "haiku", 0, 0, None, 100,
        route_mode="replace", agent_path="learned/x.md",
        injected_agent_sha256=digest,
    )
    assert out.exists()


def test_no_post_entry_only_pre_raises(tmp_path: Path):
    """An orchestrator that calls Agent (creating a pre-entry) but never
    receives a post (e.g., the spawn was cancelled) must NOT receive a
    receipt — the post entry is the proof of return, not the pre entry."""
    td = _task_dir(tmp_path)
    digest = "a" * 64
    _write_sidecar(td, "security-auditor", "haiku", digest)
    _write_spawn_log(td, [
        {"phase": "pre", "tool": "Agent", "subagent_type": "security-auditor",
         "prompt_sha256": "p" * 64, "timestamp": _ts()},
        # No matching post.
    ])
    with pytest.raises(ValueError, match="spawn-log"):
        receipt_audit_done(
            td, "security-auditor", "haiku", 0, 0, None, 100,
            route_mode="replace", agent_path="learned/x.md",
            injected_agent_sha256=digest,
        )
