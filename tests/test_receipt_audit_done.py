"""Tests for receipt_audit_done sidecar assertion (AC 9)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_receipts import receipt_audit_done  # noqa: E402


def _task_dir(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260418-AD"
    td.mkdir(parents=True)
    return td


def _write_sidecar(td: Path, auditor: str, model: str, digest: str) -> Path:
    sidecar_dir = td / "receipts" / "_injected-auditor-prompts"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    p = sidecar_dir / f"{auditor}-{model}.sha256"
    p.write_text(digest)
    return p


def test_matching_sidecar_passes(tmp_path: Path):
    td = _task_dir(tmp_path)
    digest = "a" * 64
    _write_sidecar(td, "security-auditor", "haiku", digest)
    out = receipt_audit_done(
        td, "security-auditor", "haiku", 0, 0, None, 100,
        route_mode="replace", agent_path="learned/x.md",
        injected_agent_sha256=digest,
    )
    assert out.exists()
    payload = json.loads(out.read_text())
    assert payload["injected_agent_sha256"] == digest
    assert payload["route_mode"] == "replace"


def test_mismatched_sidecar_raises(tmp_path: Path):
    td = _task_dir(tmp_path)
    _write_sidecar(td, "sec", "haiku", "a" * 64)
    with pytest.raises(ValueError, match="mismatch"):
        receipt_audit_done(
            td, "sec", "haiku", 0, 0, None, 100,
            route_mode="replace", agent_path="learned/x.md",
            injected_agent_sha256="b" * 64,
        )


def test_missing_sidecar_raises(tmp_path: Path):
    td = _task_dir(tmp_path)
    with pytest.raises(ValueError, match="sidecar"):
        receipt_audit_done(
            td, "sec", "haiku", 0, 0, None, 100,
            route_mode="replace", agent_path="learned/x.md",
            injected_agent_sha256="a" * 64,
        )


def test_env_var_no_longer_bypasses_assertion(tmp_path: Path, monkeypatch):
    """Regression: DYNOS_SKIP_RECEIPT_SIDECAR_ASSERT=1 was removed (SEC-003).
    Even with the env var set, sidecar enforcement must still fire."""
    td = _task_dir(tmp_path)
    # No sidecar at all on disk
    monkeypatch.setenv("DYNOS_SKIP_RECEIPT_SIDECAR_ASSERT", "1")
    with pytest.raises(ValueError, match="sidecar"):
        receipt_audit_done(
            td, "sec", "haiku", 0, 0, None, 100,
            route_mode="replace", agent_path="learned/x.md",
            injected_agent_sha256="a" * 64,
        )


def test_generic_mode_allows_none_injected(tmp_path: Path):
    td = _task_dir(tmp_path)
    out = receipt_audit_done(
        td, "sec", "haiku", 0, 0, None, 100,
        route_mode="generic", agent_path=None,
        injected_agent_sha256=None,
    )
    assert out.exists()
    payload = json.loads(out.read_text())
    assert payload["injected_agent_sha256"] is None


def test_non_generic_with_none_injected_rejected(tmp_path: Path):
    td = _task_dir(tmp_path)
    with pytest.raises(ValueError, match="generic"):
        receipt_audit_done(
            td, "sec", "haiku", 0, 0, None, 100,
            route_mode="replace", agent_path="learned/x.md",
            injected_agent_sha256=None,
        )
