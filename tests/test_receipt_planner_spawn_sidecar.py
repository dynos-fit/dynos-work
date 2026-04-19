"""Tests for `receipt_planner_spawn` sidecar assertion (CRITERION 6, Fix F
receipt).

`receipt_planner_spawn(task_dir, phase, ..., injected_prompt_sha256=...)`
now asserts that `receipts/_injected-planner-prompts/{phase}.sha256`
exists and matches the supplied digest when non-None. When None, the
receipt is written without assertion (legacy path).
"""
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


def _write_sidecar(td: Path, phase: str, digest: str) -> Path:
    sd = td / "receipts" / "_injected-planner-prompts"
    sd.mkdir(parents=True, exist_ok=True)
    p = sd / f"{phase}.sha256"
    p.write_text(digest)
    return p


def test_sidecar_present_matches_digest(tmp_path: Path):
    """Sidecar present with matching digest → receipt written successfully."""
    td = _task_dir(tmp_path)
    digest = "a" * 64
    _write_sidecar(td, "discovery", digest)

    out = receipt_planner_spawn(
        td,
        "discovery",
        tokens_used=100,
        model_used="opus",
        injected_prompt_sha256=digest,
    )
    assert out.exists()
    payload = json.loads(out.read_text())
    assert payload["phase"] == "discovery"
    assert payload["injected_prompt_sha256"] == digest


def test_sidecar_missing_raises(tmp_path: Path):
    """Missing sidecar with non-None digest → ValueError mentioning phase."""
    td = _task_dir(tmp_path)
    with pytest.raises(ValueError) as excinfo:
        receipt_planner_spawn(
            td,
            "spec",
            tokens_used=0,
            injected_prompt_sha256="abc",
        )
    # The error must name the phase so the operator can locate the gap.
    assert "spec" in str(excinfo.value)


def test_sidecar_hash_mismatch_raises(tmp_path: Path):
    """Sidecar contains a different digest → ValueError with 'hash mismatch'."""
    td = _task_dir(tmp_path)
    _write_sidecar(td, "plan", "x" * 64)
    with pytest.raises(ValueError, match="hash mismatch"):
        receipt_planner_spawn(
            td,
            "plan",
            tokens_used=0,
            injected_prompt_sha256="y" * 64,
        )


def test_injected_prompt_sha256_none_writes_receipt(tmp_path: Path):
    """Legacy path: injected_prompt_sha256=None → no sidecar required."""
    td = _task_dir(tmp_path)
    out = receipt_planner_spawn(
        td,
        "plan",
        tokens_used=50,
        model_used="sonnet",
        injected_prompt_sha256=None,
    )
    assert out.exists()
    payload = json.loads(out.read_text())
    assert payload["phase"] == "plan"
    # Legacy receipts carry an explicit null for the injected digest.
    assert payload.get("injected_prompt_sha256") is None


def test_omitted_kwarg_raises_typeerror(tmp_path: Path):
    """SEC-004 hardening: callers MUST pass `injected_prompt_sha256`
    explicitly. Omitting the kwarg is a caller bug — a forgotten sidecar
    assertion cannot silently ship."""
    td = _task_dir(tmp_path)
    with pytest.raises(TypeError, match="injected_prompt_sha256 is required"):
        receipt_planner_spawn(td, "plan", tokens_used=10, model_used="sonnet")
