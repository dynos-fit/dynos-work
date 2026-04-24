"""Tests for receipt_executor_done sidecar enforcement (AC 12)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_receipts import receipt_executor_done  # noqa: E402

_REQUIRED = dict(diff_verified_files=[], no_op_justified=False)


def _task_dir(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260418-EX"
    td.mkdir(parents=True)
    return td


def _write_sidecar(td: Path, segment_id: str, digest: str) -> Path:
    sd = td / "receipts" / "_injected-prompts"
    sd.mkdir(parents=True, exist_ok=True)
    p = sd / f"{segment_id}.sha256"
    p.write_text(digest)
    return p


def test_sidecar_missing_raises_with_literal(tmp_path: Path):
    td = _task_dir(tmp_path)
    with pytest.raises(ValueError, match="injected_prompt_sha256 sidecar missing"):
        receipt_executor_done(
            td, "seg-1", "backend", "haiku",
            injected_prompt_sha256="a" * 64,
            agent_name=None, evidence_path=None, tokens_used=10,
            **_REQUIRED,
        )


def test_sidecar_mismatch_raises_with_literal(tmp_path: Path):
    td = _task_dir(tmp_path)
    _write_sidecar(td, "seg-1", "a" * 64)
    with pytest.raises(ValueError, match="injected_prompt_sha256 mismatch"):
        receipt_executor_done(
            td, "seg-1", "backend", "haiku",
            injected_prompt_sha256="b" * 64,
            agent_name=None, evidence_path=None, tokens_used=10,
            **_REQUIRED,
        )


def test_sidecar_match_passes(tmp_path: Path):
    td = _task_dir(tmp_path)
    digest = "c" * 64
    _write_sidecar(td, "seg-1", digest)
    out = receipt_executor_done(
        td, "seg-1", "backend", "haiku",
        injected_prompt_sha256=digest,
        agent_name=None, evidence_path=None, tokens_used=0,
        **_REQUIRED,
    )
    assert out.exists()
    payload = json.loads(out.read_text())
    assert payload["injected_prompt_sha256"] == digest


def test_env_var_no_longer_bypasses_assertion(tmp_path: Path, monkeypatch):
    """Regression: DYNOS_SKIP_RECEIPT_SIDECAR_ASSERT=1 was removed (SEC-003).
    Setting the env var must NOT disable the sidecar check."""
    td = _task_dir(tmp_path)
    monkeypatch.setenv("DYNOS_SKIP_RECEIPT_SIDECAR_ASSERT", "1")
    # No sidecar file on disk.
    with pytest.raises(ValueError, match="sidecar"):
        receipt_executor_done(
            td, "seg-99", "backend", "haiku",
            injected_prompt_sha256="d" * 64,
            agent_name=None, evidence_path=None, tokens_used=0,
            **_REQUIRED,
        )


def test_writer_does_not_carry_learned_agent_injected(tmp_path: Path):
    """Regression: receipt payload must not contain the removed
    `learned_agent_injected` boolean."""
    td = _task_dir(tmp_path)
    digest = "e" * 64
    _write_sidecar(td, "seg-2", digest)
    out = receipt_executor_done(
        td, "seg-2", "backend", "haiku",
        injected_prompt_sha256=digest,
        agent_name="learned-backend", evidence_path=None, tokens_used=0,
        **_REQUIRED,
    )
    payload = json.loads(out.read_text())
    assert "learned_agent_injected" not in payload
