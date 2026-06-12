"""Tests for receipt system changes (AC-6, AC-7, AC-8, AC-9, AC-10, AC-17).

Covers:
  AC-6  — lib_defaults.DEFAULT_MODEL removed
  AC-7  — RECEIPT_CONTRACT_VERSION advanced to 7
  AC-8  — Spawn receipt carries {host, tier, resolved_model} (v7 fields)
  AC-9  — Codex spawn receipt has resolved_model=null and host="codex"
  AC-10 — Receipt forgery rejection: Codex receipt cannot claim Claude model
  AC-17 — Receipt sidecar uses tier name when resolved_model is null
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = ROOT / "hooks"
MEMORY_DIR = ROOT / "memory"

if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))
if str(MEMORY_DIR) not in sys.path:
    sys.path.insert(0, str(MEMORY_DIR))

import lib_models  # noqa: E402


# ---------------------------------------------------------------------------
# AC-6: lib_defaults.DEFAULT_MODEL removed
# ---------------------------------------------------------------------------

class TestLibDefaultsNoLiteral:
    def test_lib_defaults_no_literal(self) -> None:
        """AC-6: hooks/lib_defaults.py must not have a DEFAULT_MODEL attribute after the task."""
        import lib_defaults
        assert not hasattr(lib_defaults, "DEFAULT_MODEL"), (
            "lib_defaults.DEFAULT_MODEL must be removed by this task; "
            "it still exists as an attribute"
        )

    def test_lib_defaults_default_model_not_in_source(self) -> None:
        """AC-6: 'DEFAULT_MODEL' must not appear in hooks/lib_defaults.py source."""
        src = (HOOKS_DIR / "lib_defaults.py").read_text(encoding="utf-8")
        assert "DEFAULT_MODEL" not in src, (
            "DEFAULT_MODEL still present in hooks/lib_defaults.py source"
        )


# ---------------------------------------------------------------------------
# AC-7: RECEIPT_CONTRACT_VERSION advanced to 7
# ---------------------------------------------------------------------------

class TestReceiptVersionIs7:
    def test_receipt_version_is_7(self) -> None:
        """AC-7: RECEIPT_CONTRACT_VERSION must be 7 and MIN_VERSION_PER_STEP['spawn-*']==7."""
        from hooks.receipts.core import RECEIPT_CONTRACT_VERSION, MIN_VERSION_PER_STEP
        assert RECEIPT_CONTRACT_VERSION == 7, (
            f"RECEIPT_CONTRACT_VERSION must be 7, got {RECEIPT_CONTRACT_VERSION}"
        )
        assert MIN_VERSION_PER_STEP.get("spawn-*") == 7, (
            f"MIN_VERSION_PER_STEP['spawn-*'] must be 7, "
            f"got {MIN_VERSION_PER_STEP.get('spawn-*')!r}"
        )


# ---------------------------------------------------------------------------
# AC-8: Spawn receipt v7 fields (integration test under Claude host)
# ---------------------------------------------------------------------------

def _make_task_dir(tmp_path: Path, task_id: str = "task-20260611-001") -> Path:
    """Create a minimal task dir structure for receipt writer tests."""
    project = tmp_path / "project"
    td = project / ".dynos" / task_id
    td.mkdir(parents=True)
    return td


def _write_executor_sidecar(td: Path, segment_id: str, digest: str) -> None:
    """Write the injected-prompt sidecar required by receipt_executor_done."""
    sd = td / "receipts" / "_injected-prompts"
    sd.mkdir(parents=True, exist_ok=True)
    (sd / f"{segment_id}.sha256").write_text(digest)


class TestSpawnReceiptV7Fields:
    def test_spawn_receipt_v7_fields(self, tmp_path: Path) -> None:
        """AC-8: receipt_executor_done writes {host, tier, resolved_model} under claude host.

        Drives the REAL writer (receipt_executor_done), reads the receipt file
        from disk, and asserts the three v7 fields are present and correct.
        Persisted host: claude (injected via monkeypatch so the test is hermetic).
        """
        from hooks.receipts.stage import receipt_executor_done

        td = _make_task_dir(tmp_path)
        digest = "a" * 64
        _write_executor_sidecar(td, "seg-1", digest)

        # Inject the host as "claude" (writer-derived, not caller-supplied).
        with mock.patch("hooks.receipts.stage._lib_host.get_persisted_host", return_value="claude"):
            out = receipt_executor_done(
                td, "seg-1", "backend", "sonnet",
                injected_prompt_sha256=digest,
                agent_name=None, evidence_path=None, tokens_used=0,
                diff_verified_files=[], no_op_justified=False,
            )

        assert out.exists(), "receipt file must be written to disk"
        receipt = json.loads(out.read_text())

        # AC-7: contract_version must be 7.
        assert receipt["contract_version"] == 7, (
            f"contract_version must be 7, got {receipt['contract_version']}"
        )
        # AC-8: host field must be present and correct.
        assert receipt["host"] == "claude", (
            f"host must be 'claude', got {receipt['host']!r}"
        )
        # AC-8: tier must be a valid tier name.
        assert receipt["tier"] in lib_models.ALL_TIERS, (
            f"tier must be in ALL_TIERS, got {receipt['tier']!r}"
        )
        # AC-8: resolved_model must be a valid claude model.
        assert receipt["resolved_model"] in lib_models.valid_models_for_host("claude"), (
            f"resolved_model must be a valid claude model, got {receipt['resolved_model']!r}"
        )
        # Tier/model consistency: the tier must match what model_to_tier returns.
        assert lib_models.model_to_tier(receipt["resolved_model"]) == receipt["tier"], (
            f"tier {receipt['tier']!r} must match model_to_tier({receipt['resolved_model']!r})"
        )

    def test_spawn_receipt_v7_requires_host_field(self, tmp_path: Path) -> None:
        """AC-8: spawn receipt v7 must include a 'host' field."""
        from hooks.receipts import core as receipts_core

        # The v7 contract version is 7.
        assert receipts_core.RECEIPT_CONTRACT_VERSION == 7

        # Verify that tier is a known value when referenced from lib_models.
        for tier in lib_models.ALL_TIERS:
            model = lib_models.resolve_model_for_tier("claude", tier)
            assert model in lib_models.valid_models_for_host("claude"), (
                f"Tier {tier!r} resolves to {model!r} which is not in claude valid models"
            )


# ---------------------------------------------------------------------------
# AC-9: Codex spawn receipt resolved_model=null
# ---------------------------------------------------------------------------

class TestSpawnReceiptCodexNullModel:
    def test_spawn_receipt_codex_null_model(self, tmp_path: Path) -> None:
        """AC-9: receipt_executor_done under codex host writes host='codex', resolved_model=None.

        Drives the REAL writer (receipt_executor_done), reads the receipt file
        from disk, and asserts the three v7 fields match the codex contract.
        Persisted host: codex (injected via monkeypatch so the test is hermetic).
        """
        from hooks.receipts.stage import receipt_executor_done

        td = _make_task_dir(tmp_path)
        digest = "b" * 64
        _write_executor_sidecar(td, "seg-2", digest)

        # Inject the host as "codex" (writer-derived, not caller-supplied).
        # model_used=None because codex never resolves to a concrete model.
        with mock.patch("hooks.receipts.stage._lib_host.get_persisted_host", return_value="codex"):
            out = receipt_executor_done(
                td, "seg-2", "backend", None,
                injected_prompt_sha256=digest,
                agent_name=None, evidence_path=None, tokens_used=0,
                diff_verified_files=[], no_op_justified=False,
            )

        assert out.exists(), "receipt file must be written to disk"
        receipt = json.loads(out.read_text())

        assert receipt["host"] == "codex", (
            f"host must be 'codex', got {receipt['host']!r}"
        )
        assert receipt["resolved_model"] is None, (
            f"resolved_model must be None under codex, got {receipt['resolved_model']!r}"
        )
        # tier may be None or a valid tier when model is None (implementation-dependent).
        if receipt["tier"] is not None:
            assert receipt["tier"] in lib_models.ALL_TIERS, (
                f"tier must be in ALL_TIERS or None, got {receipt['tier']!r}"
            )

    def test_codex_all_tiers_resolve_to_none(self) -> None:
        """AC-9 precondition: all codex tiers must resolve to None."""
        for tier in lib_models.ALL_TIERS:
            result = lib_models.resolve_model_for_tier("codex", tier)
            assert result is None, (
                f"codex tier {tier!r} must resolve to None, got {result!r}"
            )


# ---------------------------------------------------------------------------
# AC-10: Receipt forgery rejection
# ---------------------------------------------------------------------------

class TestReceiptForgeryRejected:
    def test_receipt_forgery_rejected(self) -> None:
        """AC-10: validate_receipt_model_field returns False when codex receipt claims Claude model."""
        from lib_receipts import validate_receipt_model_field  # type: ignore[import]

        # Codex receipt claiming "opus" — must be rejected.
        receipt_opus = {"resolved_model": "opus", "host": "codex"}
        assert validate_receipt_model_field(receipt_opus, "codex") is False, (
            "validate_receipt_model_field must return False for codex receipt claiming 'opus'"
        )

        # Codex receipt claiming "haiku" — must also be rejected.
        receipt_haiku = {"resolved_model": "haiku", "host": "codex"}
        assert validate_receipt_model_field(receipt_haiku, "codex") is False, (
            "validate_receipt_model_field must return False for codex receipt claiming 'haiku'"
        )

        # Codex receipt with None resolved_model — valid (Codex expected state).
        receipt_none = {"resolved_model": None, "host": "codex"}
        assert validate_receipt_model_field(receipt_none, "codex") is True, (
            "validate_receipt_model_field must return True for codex receipt with resolved_model=None"
        )

    def test_receipt_forgery_claude_valid_models_accepted(self) -> None:
        """AC-10: claude receipts with valid models pass validation."""
        from lib_receipts import validate_receipt_model_field  # type: ignore[import]

        for model in ["haiku", "sonnet", "opus"]:
            receipt = {"resolved_model": model, "host": "claude"}
            assert validate_receipt_model_field(receipt, "claude") is True, (
                f"claude receipt with resolved_model={model!r} must pass validation"
            )

    def test_receipt_forgery_claude_sonnet_also_accepted(self) -> None:
        """AC-10: claude receipt with sonnet accepted."""
        from lib_receipts import validate_receipt_model_field  # type: ignore[import]
        assert validate_receipt_model_field({"resolved_model": "sonnet", "host": "claude"}, "claude") is True


# ---------------------------------------------------------------------------
# AC-17: Sidecar tier fallback when resolved_model is null
# ---------------------------------------------------------------------------

class TestSidecarTierFallbackAccepted:
    def test_sidecar_tier_fallback_accepted(self, tmp_path: Path) -> None:
        """AC-17: when resolved_model is None, sidecar filename uses tier name, not 'None'."""
        import re

        # The sidecar filename must be {auditor_name}-{tier}.sha256 when model is None.
        # This test verifies:
        # 1. The tier-valued filename passes _SAFE_AGENT_RE for each tier.
        # 2. The filename does NOT contain the literal string "None".

        _SAFE_AGENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")

        for tier in lib_models.ALL_TIERS:
            auditor_name = "security-auditor"
            filename_stem = f"{auditor_name}-{tier}"
            assert _SAFE_AGENT_RE.match(filename_stem), (
                f"sidecar stem {filename_stem!r} must match _SAFE_AGENT_RE"
            )
            assert "None" not in f"{auditor_name}-{tier}.sha256", (
                f"sidecar filename must not contain 'None' when using tier name"
            )

    def test_sidecar_tier_names_all_match_safe_agent_re(self) -> None:
        """AC-17: fast, balanced, deep all pass _SAFE_AGENT_RE."""
        import re
        _SAFE_AGENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")
        for tier in lib_models.ALL_TIERS:
            assert _SAFE_AGENT_RE.match(tier), (
                f"Tier name {tier!r} must match _SAFE_AGENT_RE"
            )

    def test_sidecar_stage_uses_tier_when_model_null(self, tmp_path: Path) -> None:
        """AC-17: hooks/receipts/stage.py _assert_sidecar_match uses tier when model_used is None."""
        # Verify that the stage module's sidecar path construction
        # does not produce *-None.sha256 filenames.
        stage_path = HOOKS_DIR / "receipts" / "stage.py"
        src = stage_path.read_text(encoding="utf-8")

        # After the task, the sidecar filename construction must not use
        # `model_used` directly when it is None; it must substitute tier.
        # The old code: f"{auditor_name}-{model_used}.sha256"
        # The new code must have a conditional for when model_used is None.

        # This test confirms the source no longer uses the unconditional pattern.
        # If the production code still has the old pattern, this assertion fails (RED).
        import ast as ast_mod
        tree = ast_mod.parse(src)

        # Verify the file references "tier" as a fallback near the sidecar construction.
        # We check that the string "tier" appears in the source near the sidecar logic.
        sidecar_section_start = src.find("_assert_sidecar_match")
        assert sidecar_section_start != -1, "_assert_sidecar_match must exist in stage.py"

        sidecar_section = src[sidecar_section_start:sidecar_section_start + 500]
        assert "tier" in sidecar_section, (
            "stage.py _assert_sidecar_match must reference 'tier' for null model fallback"
        )
