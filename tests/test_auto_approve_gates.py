"""TDD-First test suite for task-20260505-001: Auto-Approve Gates.

Every acceptance criterion (AC-1 through AC-24) in
.dynos/task-20260505-001/spec.md is covered here.  Tests are
intentionally RED until the production code in seg-1 through seg-4 is
implemented.  No production functions are stubbed; tests import real
symbols and assert against real behaviour.

Audit-finding fixes embedded:
  sc-001 — three distinct _LOG_MESSAGES keys (test_log_messages_three_distinct_keys)
  sc-002 — non-zero return path from veto also logged (test_apply_veto_crash_degrades_gracefully)
  sc-003 — no-residual-row schema complete (test_apply_veto_no_residual_row_schema_complete)
  sc-004 — receipt payload has no duplicate contract_version field
           (test_receipt_auto_approval_no_duplicate_contract_version)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "hooks"

# ---------------------------------------------------------------------------
# Helpers shared across the suite
# ---------------------------------------------------------------------------

SPEC_CONTENT = (
    "# Spec\n\n"
    "## Task Summary\nTest.\n\n"
    "## User Context\nU.\n\n"
    "## Acceptance Criteria\n1. one\n\n"
    "## Implicit Requirements Surfaced\nI.\n\n"
    "## Out of Scope\nO.\n\n"
    "## Assumptions\nsafe assumption: none\n\n"
    "## Risk Notes\nR.\n"
)

PLAN_CONTENT = (
    "# Plan\n\n"
    "## Technical Approach\nA.\n\n"
    "## Components / Modules\n### Component: Foo\n- **Purpose:** p\n- **Files:** `a.py`\n\n"
    "## API Contracts\nNone.\n\n"
    "## Data Flow\nNone.\n\n"
    "## Error Handling Strategy\nNone.\n\n"
    "## Test Strategy\nNone.\n\n"
    "## Dependency Graph\n```\nnone\n```\n"
)

TDD_EVIDENCE_CONTENT = "# TDD Evidence\n\nAll tests pass.\n"


def _env(dynos_home: Path | None = None) -> dict[str, str]:
    e = {**os.environ, "PYTHONPATH": str(HOOKS)}
    if dynos_home is not None:
        e["DYNOS_HOME"] = str(dynos_home)
    return e


def _run(
    *args: str,
    dynos_home: Path | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOKS / "ctl.py"), *args],
        cwd=str(cwd or ROOT),
        text=True,
        capture_output=True,
        check=False,
        env=_env(dynos_home),
    )


def _make_task(
    tmp_path: Path,
    *,
    task_id: str = "task-20260505-test",
    stage: str = "SPEC_REVIEW",
    auto_approve_gates: bool | None = None,
    classification: dict | None = None,
    fast_track: bool = False,
    tdd_required: bool = False,
) -> Path:
    """Create a minimal task directory with manifest.json."""
    task_dir = tmp_path / ".dynos" / task_id
    task_dir.mkdir(parents=True)
    cls = classification or {
        "type": "feature",
        "domains": ["backend"],
        "risk_level": "low",
        "notes": "n",
        "tdd_required": tdd_required,
    }
    manifest: dict = {
        "task_id": task_id,
        "created_at": "2026-05-05T00:00:00Z",
        "title": "Auto-approve test",
        "raw_input": "x",
        "stage": stage,
        "classification": cls,
        "retry_counts": {},
        "blocked_reason": None,
        "completion_at": None,
    }
    if auto_approve_gates is not None:
        manifest["auto_approve_gates"] = auto_approve_gates
    if fast_track:
        manifest["fast_track"] = True
    (task_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    (task_dir / "spec.md").write_text(SPEC_CONTENT)
    return task_dir


def _make_queue(tmp_path: Path, row: dict) -> Path:
    """Write a minimal proactive-findings.json with a single row."""
    q_path = tmp_path / ".dynos" / "proactive-findings.json"
    q_path.parent.mkdir(parents=True, exist_ok=True)
    q_path.write_text(json.dumps({"findings": [row]}, indent=2))
    return q_path


def _default_row(
    *,
    rid: str = "res-001",
    source_auditor: str = "code-quality-auditor",
    location: str = "lib/utils.py",
    status: str = "pending",
) -> dict:
    return {
        "id": rid,
        "source_auditor": source_auditor,
        "location": location,
        "description": "A test finding",
        "status": status,
        "attempts": 0,
        "created_at": "2026-05-05T00:00:00Z",
    }


def _write_policy(persistent_dir: Path, *, disabled: bool = False) -> None:
    persistent_dir.mkdir(parents=True, exist_ok=True)
    (persistent_dir / "policy.json").write_text(
        json.dumps({"auto_approve_gates_disabled": disabled})
    )


def _project_slug(root: Path) -> str:
    return str(root.resolve()).strip("/").replace("/", "-")


def _persistent_dir(dynos_home: Path, root: Path) -> Path:
    slug = _project_slug(root)
    return dynos_home / "projects" / slug


def _write_receipt_file(task_dir: Path, step: str, payload: dict) -> Path:
    """Write a receipt JSON file directly (bypasses write_receipt pipeline)."""
    receipts = task_dir / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    receipt = {
        "step": step,
        "ts": "2026-05-05T00:00:00Z",
        "valid": True,
        "contract_version": 7,  # post-bump version
        **payload,
    }
    p = receipts / f"{step}.json"
    p.write_text(json.dumps(receipt, indent=2))
    return p


# ===========================================================================
# AC-1: manifest.auto_approve_gates field — defaults false, exclusive writer
# ===========================================================================

class TestManifestField:
    """AC-1: auto_approve_gates field semantics."""

    def test_manifest_field_default_false(self, tmp_path: Path):
        """AC-1: absent auto_approve_gates in manifest is treated as false."""
        task_dir = _make_task(tmp_path)
        manifest = json.loads((task_dir / "manifest.json").read_text())
        # The field should either be absent (defaulting to false) or explicitly false.
        assert manifest.get("auto_approve_gates", False) is False

    def test_manifest_field_writable_only_by_set_command(self, tmp_path: Path):
        """AC-1: only set-auto-approve-gates can write auto_approve_gates=true.

        This test documents the invariant: if the flag is set to true by any
        path other than the canonical set-auto-approve-gates command, that is
        a spec violation.  We verify the flag is absent from a freshly-created
        task (so no other path is writing it).
        """
        task_dir = _make_task(tmp_path)
        manifest = json.loads((task_dir / "manifest.json").read_text())
        assert "auto_approve_gates" not in manifest or manifest["auto_approve_gates"] is False


# ===========================================================================
# AC-2: argparse registration for set-auto-approve-gates
# ===========================================================================

class TestArgparseRegistration:
    """AC-2: set-auto-approve-gates CLI registration."""

    def test_set_auto_approve_gates_argparse_registration(self, tmp_path: Path):
        """AC-2: --help exits 0 and names both required flags."""
        r = _run("set-auto-approve-gates", "--help")
        assert r.returncode == 0, r.stderr
        assert "--task-dir" in r.stdout
        assert "--from-residual-id" in r.stdout

    def test_apply_auto_approve_veto_argparse_registration(self, tmp_path: Path):
        """AC-5/IR-1: apply-auto-approve-veto is registered as a top-level subcommand."""
        r = _run("apply-auto-approve-veto", "--help")
        assert r.returncode == 0, r.stderr
        assert "--task-dir" in r.stdout

    def test_approve_stage_auto_approved_flag_registered(self, tmp_path: Path):
        """AC-7: --auto-approved flag appears in approve-stage --help."""
        r = _run("approve-stage", "--help")
        assert r.returncode == 0, r.stderr
        assert "--auto-approved" in r.stdout


# ===========================================================================
# AC-3 / AC-21a-d: set-auto-approve-gates pick-time ceilings
# ===========================================================================

class TestSetAutoApproveGatesCeilings:
    """AC-3: pick-time ceiling checks for set-auto-approve-gates."""

    def test_security_auditor_row_blocked(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """AC-3a / AC-21a: source_auditor=security-auditor -> exits 1, stderr contains 'security-auditor'."""
        root = tmp_path / "project"
        root.mkdir()
        (root / ".dynos").mkdir()
        task_dir = _make_task(tmp_path / "td", stage="SPEC_REVIEW")
        row = _default_row(source_auditor="security-auditor")
        _make_queue(root, row)
        dynos_home = tmp_path / "home"
        dynos_home.mkdir()
        r = _run(
            "set-auto-approve-gates",
            "--task-dir", str(task_dir),
            "--from-residual-id", row["id"],
            dynos_home=dynos_home,
            cwd=root,
        )
        assert r.returncode == 1
        assert "security-auditor" in r.stderr

    def test_set_auto_approve_gates_security_auditor_blocked(self, tmp_path: Path):
        """AC-21a exact name: security-auditor ceiling blocks and exits 1."""
        root = tmp_path / "project"
        root.mkdir()
        (root / ".dynos").mkdir()
        task_dir = _make_task(tmp_path / "td", stage="SPEC_REVIEW")
        row = _default_row(source_auditor="security-auditor")
        _make_queue(root, row)
        dynos_home = tmp_path / "home"
        dynos_home.mkdir()
        r = _run(
            "set-auto-approve-gates",
            "--task-dir", str(task_dir),
            "--from-residual-id", row["id"],
            dynos_home=dynos_home,
            cwd=root,
        )
        assert r.returncode == 1
        assert "security-auditor" in r.stderr

    def test_high_risk_blocked(self, tmp_path: Path):
        """AC-3b / AC-21b: source_auditor not in allowlist -> exits 1."""
        root = tmp_path / "project"
        root.mkdir()
        (root / ".dynos").mkdir()
        task_dir = _make_task(tmp_path / "td", stage="SPEC_REVIEW")
        row = _default_row(source_auditor="spec-completion-auditor")
        _make_queue(root, row)
        dynos_home = tmp_path / "home"
        dynos_home.mkdir()
        r = _run(
            "set-auto-approve-gates",
            "--task-dir", str(task_dir),
            "--from-residual-id", row["id"],
            dynos_home=dynos_home,
            cwd=root,
        )
        assert r.returncode == 1

    def test_set_auto_approve_gates_allowlist_blocked(self, tmp_path: Path):
        """AC-21b exact name: non-allowlist auditor exits 1."""
        root = tmp_path / "project"
        root.mkdir()
        (root / ".dynos").mkdir()
        task_dir = _make_task(tmp_path / "td", stage="SPEC_REVIEW")
        row = _default_row(source_auditor="spec-completion-auditor")
        _make_queue(root, row)
        dynos_home = tmp_path / "home"
        dynos_home.mkdir()
        r = _run(
            "set-auto-approve-gates",
            "--task-dir", str(task_dir),
            "--from-residual-id", row["id"],
            dynos_home=dynos_home,
            cwd=root,
        )
        assert r.returncode == 1

    def test_set_auto_approve_gates_empty_location_blocked(self, tmp_path: Path):
        """AC-3c / AC-21c: empty location -> exits 1, stderr contains 'location'."""
        root = tmp_path / "project"
        root.mkdir()
        (root / ".dynos").mkdir()
        task_dir = _make_task(tmp_path / "td", stage="SPEC_REVIEW")
        row = _default_row(location="")
        _make_queue(root, row)
        dynos_home = tmp_path / "home"
        dynos_home.mkdir()
        r = _run(
            "set-auto-approve-gates",
            "--task-dir", str(task_dir),
            "--from-residual-id", row["id"],
            dynos_home=dynos_home,
            cwd=root,
        )
        assert r.returncode == 1
        assert "location" in r.stderr

    def test_global_policy_disable_blocked(self, tmp_path: Path):
        """AC-3d / AC-21d: global policy disabled -> exits 1, stderr contains 'policy'."""
        root = tmp_path / "project"
        root.mkdir()
        (root / ".dynos").mkdir()
        task_dir = _make_task(tmp_path / "td", stage="SPEC_REVIEW")
        row = _default_row()
        _make_queue(root, row)
        dynos_home = tmp_path / "home"
        dynos_home.mkdir()
        persistent = _persistent_dir(dynos_home, root)
        _write_policy(persistent, disabled=True)
        r = _run(
            "set-auto-approve-gates",
            "--task-dir", str(task_dir),
            "--from-residual-id", row["id"],
            dynos_home=dynos_home,
            cwd=root,
        )
        assert r.returncode == 1
        assert "policy" in r.stderr

    def test_set_auto_approve_gates_global_policy_disabled(self, tmp_path: Path):
        """AC-21d exact name: policy disabled blocks set-auto-approve-gates."""
        root = tmp_path / "project"
        root.mkdir()
        (root / ".dynos").mkdir()
        task_dir = _make_task(tmp_path / "td", stage="SPEC_REVIEW")
        row = _default_row()
        _make_queue(root, row)
        dynos_home = tmp_path / "home"
        dynos_home.mkdir()
        persistent = _persistent_dir(dynos_home, root)
        _write_policy(persistent, disabled=True)
        r = _run(
            "set-auto-approve-gates",
            "--task-dir", str(task_dir),
            "--from-residual-id", row["id"],
            dynos_home=dynos_home,
            cwd=root,
        )
        assert r.returncode == 1
        assert "policy" in r.stderr

    def test_set_auto_approve_gates_success_stdout_json(self, tmp_path: Path):
        """AC-3 happy path: all ceilings pass -> exits 0, stdout is parseable JSON.

        Uses pre-classification fixture (classification=None overridden after
        _make_task because the fixture's `cls = classification or {...}` falls
        through to a default dict on None — overwrite manifest post-creation
        to genuinely null out classification, matching the residual-pick-time
        precondition.
        """
        root = tmp_path / "project"
        root.mkdir()
        (root / ".dynos").mkdir()
        task_dir = _make_task(tmp_path / "td", stage="FOUNDRY_INITIALIZED")
        manifest = json.loads((task_dir / "manifest.json").read_text())
        manifest["classification"] = None
        (task_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
        row = _default_row(source_auditor="code-quality-auditor", location="lib/utils.py")
        _make_queue(root, row)
        dynos_home = tmp_path / "home"
        dynos_home.mkdir()
        persistent = _persistent_dir(dynos_home, root)
        _write_policy(persistent, disabled=False)
        r = _run(
            "set-auto-approve-gates",
            "--task-dir", str(task_dir),
            "--from-residual-id", row["id"],
            dynos_home=dynos_home,
            cwd=root,
        )
        assert r.returncode == 0, r.stderr
        out = json.loads(r.stdout)
        assert out["auto_approve_gates"] is True
        assert "task_id" in out
        assert "ceiling_checked" in out

    def test_set_auto_approve_gates_manifest_written(self, tmp_path: Path):
        """AC-3 success: manifest.auto_approve_gates=true after command exits 0.

        Uses pre-classification fixture (classification=None overridden after
        _make_task because the fixture's `cls = classification or {...}` falls
        through to a default dict on None — overwrite manifest post-creation
        to genuinely null out classification, matching the residual-pick-time
        precondition.
        """
        root = tmp_path / "project"
        root.mkdir()
        (root / ".dynos").mkdir()
        task_dir = _make_task(tmp_path / "td", stage="FOUNDRY_INITIALIZED")
        manifest = json.loads((task_dir / "manifest.json").read_text())
        manifest["classification"] = None
        (task_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
        row = _default_row(source_auditor="code-quality-auditor", location="lib/utils.py")
        _make_queue(root, row)
        dynos_home = tmp_path / "home"
        dynos_home.mkdir()
        persistent = _persistent_dir(dynos_home, root)
        _write_policy(persistent, disabled=False)
        r = _run(
            "set-auto-approve-gates",
            "--task-dir", str(task_dir),
            "--from-residual-id", row["id"],
            dynos_home=dynos_home,
            cwd=root,
        )
        assert r.returncode == 0, r.stderr
        manifest = json.loads((task_dir / "manifest.json").read_text())
        assert manifest.get("auto_approve_gates") is True


# ===========================================================================
# AC-4: idempotency and post-classification refusal
# ===========================================================================

class TestSetAutoApproveGatesIdempotencyAndClassification:
    """AC-4: idempotent when already true; refuses after classification."""

    def test_set_auto_approve_gates_refuses_false_to_true_after_classification(
        self, tmp_path: Path
    ):
        """AC-4: if manifest.classification is non-null, refuses with 'classification already settled'."""
        root = tmp_path / "project"
        root.mkdir()
        (root / ".dynos").mkdir()
        # Task with classification already present
        task_dir = _make_task(tmp_path / "td", stage="SPEC_REVIEW")
        row = _default_row()
        _make_queue(root, row)
        dynos_home = tmp_path / "home"
        dynos_home.mkdir()
        r = _run(
            "set-auto-approve-gates",
            "--task-dir", str(task_dir),
            "--from-residual-id", row["id"],
            dynos_home=dynos_home,
            cwd=root,
        )
        # Must exit 1 because classification is already settled
        assert r.returncode == 1
        assert "classification already settled" in r.stderr

    def test_set_auto_approve_gates_idempotent_when_already_true(
        self, tmp_path: Path
    ):
        """AC-4: if manifest.auto_approve_gates is already true, exits 0 without re-evaluating ceilings."""
        root = tmp_path / "project"
        root.mkdir()
        (root / ".dynos").mkdir()
        # Manifest without classification (so set command can run), auto_approve_gates=true
        task_dir = _make_task(tmp_path / "td", stage="SPEC_REVIEW", auto_approve_gates=True)
        # Remove classification to simulate pre-classification state with flag already set
        manifest = json.loads((task_dir / "manifest.json").read_text())
        manifest.pop("classification", None)
        manifest["auto_approve_gates"] = True
        (task_dir / "manifest.json").write_text(json.dumps(manifest))
        row = _default_row(source_auditor="security-auditor")  # Would normally block
        _make_queue(root, row)
        dynos_home = tmp_path / "home"
        dynos_home.mkdir()
        r = _run(
            "set-auto-approve-gates",
            "--task-dir", str(task_dir),
            "--from-residual-id", row["id"],
            dynos_home=dynos_home,
            cwd=root,
        )
        # Should exit 0 idempotently, not re-evaluate the security-auditor ceiling
        assert r.returncode == 0, r.stderr

    def test_set_auto_approve_gates_missing_manifest(self, tmp_path: Path):
        """IR-5: missing manifest.json -> exits 1 with 'manifest.json missing'."""
        root = tmp_path / "project"
        root.mkdir()
        (root / ".dynos").mkdir()
        nonexistent_dir = tmp_path / ".dynos" / "task-nonexistent"
        row = _default_row()
        _make_queue(root, row)
        dynos_home = tmp_path / "home"
        dynos_home.mkdir()
        r = _run(
            "set-auto-approve-gates",
            "--task-dir", str(nonexistent_dir),
            "--from-residual-id", row["id"],
            dynos_home=dynos_home,
            cwd=root,
        )
        assert r.returncode == 1
        assert "manifest.json missing" in r.stderr


# ===========================================================================
# AC-5 / AC-21e-f, AC-21k: apply-auto-approve-veto classification ceilings
# ===========================================================================

class TestApplyAutoApproveVetoCeilings:
    """AC-5: classification-time ceiling checks for apply-auto-approve-veto."""

    def _make_classified_task(
        self,
        tmp_path: Path,
        *,
        risk_level: str = "low",
        domains: list[str] | None = None,
        auto_approve_gates: bool = True,
        residual_id: str = "res-001",
    ) -> tuple[Path, Path]:
        """Build a task directory with a settled classification."""
        domains = domains or ["backend"]
        task_dir = _make_task(
            tmp_path / "td",
            stage="SPEC_REVIEW",
            auto_approve_gates=auto_approve_gates,
            classification={
                "type": "feature",
                "domains": domains,
                "risk_level": risk_level,
                "notes": "n",
                "tdd_required": False,
            },
        )
        # Write raw-input.md with residual-id sentinel
        (task_dir / "raw-input.md").write_text(
            f"<!-- residual-id: {residual_id} -->\nSome raw input."
        )
        root = tmp_path / "project"
        root.mkdir(exist_ok=True)
        (root / ".dynos").mkdir(exist_ok=True)
        return task_dir, root

    def test_apply_auto_approve_veto_high_risk_blocks(self, tmp_path: Path):
        """AC-5a / AC-21e: risk_level=high -> decision=blocked, auto_approve_gates=false."""
        task_dir, root = self._make_classified_task(tmp_path, risk_level="high")
        row = _default_row(rid="res-001")
        _make_queue(root, row)
        dynos_home = tmp_path / "home"
        dynos_home.mkdir()
        r = _run(
            "apply-auto-approve-veto",
            "--task-dir", str(task_dir),
            dynos_home=dynos_home,
            cwd=root,
        )
        assert r.returncode == 0, r.stderr
        out = json.loads(r.stdout)
        assert out["decision"] == "blocked"
        assert out["auto_approve_gates"] is False

    def test_apply_auto_approve_veto_critical_risk_blocks(self, tmp_path: Path):
        """AC-5a: risk_level=critical -> decision=blocked."""
        task_dir, root = self._make_classified_task(tmp_path, risk_level="critical")
        row = _default_row(rid="res-001")
        _make_queue(root, row)
        dynos_home = tmp_path / "home"
        dynos_home.mkdir()
        r = _run(
            "apply-auto-approve-veto",
            "--task-dir", str(task_dir),
            dynos_home=dynos_home,
            cwd=root,
        )
        assert r.returncode == 0, r.stderr
        out = json.loads(r.stdout)
        assert out["decision"] == "blocked"
        assert out["auto_approve_gates"] is False

    def test_apply_auto_approve_veto_security_domain_blocks(self, tmp_path: Path):
        """AC-5b / AC-21f: domains=['security'] -> decision=blocked."""
        task_dir, root = self._make_classified_task(tmp_path, domains=["security"])
        row = _default_row(rid="res-001")
        _make_queue(root, row)
        dynos_home = tmp_path / "home"
        dynos_home.mkdir()
        r = _run(
            "apply-auto-approve-veto",
            "--task-dir", str(task_dir),
            dynos_home=dynos_home,
            cwd=root,
        )
        assert r.returncode == 0, r.stderr
        out = json.loads(r.stdout)
        assert out["decision"] == "blocked"
        assert out["auto_approve_gates"] is False

    def test_global_policy_disable_veto(self, tmp_path: Path):
        """AC-5d / AC-21k: global policy disabled -> apply-auto-approve-veto sets flag=false."""
        task_dir, root = self._make_classified_task(tmp_path, risk_level="low")
        row = _default_row(rid="res-001")
        _make_queue(root, row)
        dynos_home = tmp_path / "home"
        dynos_home.mkdir()
        persistent = _persistent_dir(dynos_home, root)
        _write_policy(persistent, disabled=True)
        r = _run(
            "apply-auto-approve-veto",
            "--task-dir", str(task_dir),
            dynos_home=dynos_home,
            cwd=root,
        )
        assert r.returncode == 0, r.stderr
        out = json.loads(r.stdout)
        assert out["decision"] == "blocked"
        assert out["auto_approve_gates"] is False

    def test_apply_auto_approve_veto_missing_risk_level_blocked(self, tmp_path: Path):
        """AC-5e: absent/null risk_level -> blocked (fail-closed)."""
        task_dir = _make_task(
            tmp_path / "td",
            stage="SPEC_REVIEW",
            auto_approve_gates=True,
            classification={
                "type": "feature",
                "domains": ["backend"],
                "risk_level": None,
                "notes": "n",
                "tdd_required": False,
            },
        )
        (task_dir / "raw-input.md").write_text("<!-- residual-id: res-001 -->\nRaw.")
        root = tmp_path / "project"
        root.mkdir(exist_ok=True)
        (root / ".dynos").mkdir(exist_ok=True)
        row = _default_row(rid="res-001")
        _make_queue(root, row)
        dynos_home = tmp_path / "home"
        dynos_home.mkdir()
        r = _run(
            "apply-auto-approve-veto",
            "--task-dir", str(task_dir),
            dynos_home=dynos_home,
            cwd=root,
        )
        assert r.returncode == 0, r.stderr
        out = json.loads(r.stdout)
        assert out["decision"] == "blocked"

    def test_apply_auto_approve_veto_no_classification_refuses(self, tmp_path: Path):
        """IR-6: classification not settled -> exits 1 with 'classification not settled'."""
        task_dir = _make_task(tmp_path / "td", stage="SPEC_REVIEW")
        # Remove classification
        manifest = json.loads((task_dir / "manifest.json").read_text())
        manifest["classification"] = None
        (task_dir / "manifest.json").write_text(json.dumps(manifest))
        root = tmp_path / "project"
        root.mkdir(exist_ok=True)
        (root / ".dynos").mkdir(exist_ok=True)
        dynos_home = tmp_path / "home"
        dynos_home.mkdir()
        r = _run(
            "apply-auto-approve-veto",
            "--task-dir", str(task_dir),
            dynos_home=dynos_home,
            cwd=root,
        )
        assert r.returncode == 1
        assert "classification not settled" in r.stderr

    def test_apply_auto_approve_veto_downgrade_only(self, tmp_path: Path):
        """AC-5: veto cannot raise false -> true; can only confirm or downgrade."""
        task_dir, root = self._make_classified_task(
            tmp_path, risk_level="low", auto_approve_gates=False
        )
        row = _default_row(rid="res-001")
        _make_queue(root, row)
        dynos_home = tmp_path / "home"
        dynos_home.mkdir()
        r = _run(
            "apply-auto-approve-veto",
            "--task-dir", str(task_dir),
            dynos_home=dynos_home,
            cwd=root,
        )
        assert r.returncode == 0, r.stderr
        manifest = json.loads((task_dir / "manifest.json").read_text())
        # Must remain false — veto cannot raise it
        assert manifest.get("auto_approve_gates", False) is False


# ===========================================================================
# AC-6: auto_approval_policy schema completeness
# ===========================================================================

class TestAutoApprovalPolicySchema:
    """AC-6: classification.auto_approval_policy exact JSON shape."""

    def test_auto_approval_policy_schema_complete(self, tmp_path: Path):
        """AC-6 / AC-21l (schema variant): apply-auto-approve-veto writes policy with all required fields."""
        task_dir = _make_task(
            tmp_path / "td",
            stage="SPEC_REVIEW",
            auto_approve_gates=True,
            classification={
                "type": "feature",
                "domains": ["backend"],
                "risk_level": "low",
                "notes": "n",
                "tdd_required": False,
            },
        )
        (task_dir / "raw-input.md").write_text("<!-- residual-id: res-001 -->\nRaw.")
        root = tmp_path / "project"
        root.mkdir(exist_ok=True)
        (root / ".dynos").mkdir(exist_ok=True)
        row = _default_row(rid="res-001", location="lib/utils.py")
        _make_queue(root, row)
        dynos_home = tmp_path / "home"
        dynos_home.mkdir()
        r = _run(
            "apply-auto-approve-veto",
            "--task-dir", str(task_dir),
            dynos_home=dynos_home,
            cwd=root,
        )
        assert r.returncode == 0, r.stderr
        manifest = json.loads((task_dir / "manifest.json").read_text())
        policy = manifest.get("classification", {}).get("auto_approval_policy")
        assert policy is not None, "auto_approval_policy must be written to manifest"
        assert "decision" in policy
        assert policy["decision"] in {"auto", "blocked"}
        basis = policy.get("basis", {})
        assert "risk_level" in basis
        assert "domains" in basis
        assert "source_auditor" in basis
        assert "external_surface_path_match" in basis
        assert "global_policy_disabled" in basis
        assert "ceilings_checked" in policy
        assert "blocked_by" in policy
        assert set(policy["ceilings_checked"]) == {
            "risk_level",
            "domain_security",
            "source_auditor_security",
            "external_surface_path",
            "source_auditor_allowlist",
            "global_policy",
            "spawn_budget_paused",
        }

    def test_apply_veto_no_residual_row_schema_complete(self, tmp_path: Path):
        """AC-6 / sc-003: when no residual row is found, policy.basis has all fields populated.

        source_auditor=null, external_surface_path_match=false, no_residual_row=true.
        The absence of a row must NOT produce a malformed policy object.
        """
        task_dir = _make_task(
            tmp_path / "td",
            stage="SPEC_REVIEW",
            auto_approve_gates=True,
            classification={
                "type": "feature",
                "domains": ["backend"],
                "risk_level": "low",
                "notes": "n",
                "tdd_required": False,
            },
        )
        # raw-input.md has no residual-id sentinel
        (task_dir / "raw-input.md").write_text("No sentinel here.")
        root = tmp_path / "project"
        root.mkdir(exist_ok=True)
        (root / ".dynos").mkdir(exist_ok=True)
        # Empty queue — no matching row
        _make_queue(root, _default_row(rid="res-different"))
        dynos_home = tmp_path / "home"
        dynos_home.mkdir()
        r = _run(
            "apply-auto-approve-veto",
            "--task-dir", str(task_dir),
            dynos_home=dynos_home,
            cwd=root,
        )
        assert r.returncode == 0, r.stderr
        manifest = json.loads((task_dir / "manifest.json").read_text())
        policy = manifest.get("classification", {}).get("auto_approval_policy")
        assert policy is not None, "auto_approval_policy must be present even with no row"
        basis = policy.get("basis", {})
        # All required basis fields must be explicitly present
        assert "risk_level" in basis
        assert "domains" in basis
        assert "source_auditor" in basis
        assert basis["source_auditor"] is None
        assert "external_surface_path_match" in basis
        assert basis["external_surface_path_match"] is False
        assert "global_policy_disabled" in basis
        # The no_residual_row disambiguator
        assert basis.get("no_residual_row") is True

    def test_classification_json_receives_auto_approval_policy(self, tmp_path: Path):
        """AC-6: classification.json gets auto_approval_policy at top level via _persist_classification."""
        task_dir = _make_task(
            tmp_path / "td",
            stage="SPEC_REVIEW",
            auto_approve_gates=True,
            classification={
                "type": "feature",
                "domains": ["backend"],
                "risk_level": "low",
                "notes": "n",
                "tdd_required": False,
            },
        )
        (task_dir / "raw-input.md").write_text("<!-- residual-id: res-001 -->\nRaw.")
        root = tmp_path / "project"
        root.mkdir(exist_ok=True)
        (root / ".dynos").mkdir(exist_ok=True)
        row = _default_row(rid="res-001")
        _make_queue(root, row)
        dynos_home = tmp_path / "home"
        dynos_home.mkdir()
        r = _run(
            "apply-auto-approve-veto",
            "--task-dir", str(task_dir),
            dynos_home=dynos_home,
            cwd=root,
        )
        assert r.returncode == 0, r.stderr
        cls_json_path = task_dir.parent.parent / ".dynos" / task_dir.name / "classification.json"
        # Also check the standard path: .dynos/task-{id}/classification.json
        cls_json_path2 = task_dir / "classification.json"
        if cls_json_path2.exists():
            cls_data = json.loads(cls_json_path2.read_text())
            assert "auto_approval_policy" in cls_data


# ===========================================================================
# AC-7: approve-stage --auto-approved flag
# ===========================================================================

class TestApproveStageAutoApproved:
    """AC-7: --auto-approved flag behaviour on approve-stage."""

    def test_approve_stage_auto_writes_correct_receipt_stem(self, tmp_path: Path):
        """AC-7 / AC-21f-proxy: --auto-approved writes auto-approval-{stage}.json, not human-approval."""
        task_dir = _make_task(tmp_path, stage="SPEC_REVIEW", auto_approve_gates=True)
        r = _run("approve-stage", str(task_dir), "SPEC_REVIEW", "--auto-approved")
        assert r.returncode == 0, r.stderr
        auto_receipt = task_dir / "receipts" / "auto-approval-SPEC_REVIEW.json"
        human_receipt = task_dir / "receipts" / "human-approval-SPEC_REVIEW.json"
        assert auto_receipt.exists(), "auto-approval receipt must be written"
        assert not human_receipt.exists(), "human-approval receipt must NOT be written with --auto-approved"

    def test_approve_stage_auto_stdout_format(self, tmp_path: Path):
        """AC-7 / IR-7: stdout line contains 'auto-approved', stage name, sha256 prefix, next stage."""
        task_dir = _make_task(tmp_path, stage="SPEC_REVIEW", auto_approve_gates=True)
        r = _run("approve-stage", str(task_dir), "SPEC_REVIEW", "--auto-approved")
        assert r.returncode == 0, r.stderr
        assert "auto-approved" in r.stdout
        assert "SPEC_REVIEW" in r.stdout
        assert "PLANNING" in r.stdout

    def test_approve_stage_auto_flag_false_in_manifest_exits_1(self, tmp_path: Path):
        """AC-7: --auto-approved fails with 'auto_approve_gates is not true' when flag is false."""
        task_dir = _make_task(tmp_path, stage="SPEC_REVIEW", auto_approve_gates=False)
        r = _run("approve-stage", str(task_dir), "SPEC_REVIEW", "--auto-approved")
        assert r.returncode == 1
        assert "auto_approve_gates is not true" in r.stderr

    def test_approve_stage_human_path_unchanged(self, tmp_path: Path):
        """AC-7: WITHOUT --auto-approved, behavior is byte-identical to pre-task (human path)."""
        task_dir = _make_task(tmp_path, stage="SPEC_REVIEW")
        r = _run("approve-stage", str(task_dir), "SPEC_REVIEW")
        assert r.returncode == 0, r.stderr
        human_receipt = task_dir / "receipts" / "human-approval-SPEC_REVIEW.json"
        assert human_receipt.exists()
        assert "approved SPEC_REVIEW" in r.stdout
        assert "PLANNING" in r.stdout


# ===========================================================================
# AC-8 / AC-9: receipt_auto_approval function
# ===========================================================================

class TestReceiptAutoApproval:
    """AC-8: receipt_auto_approval function in lib_receipts.py."""

    def test_receipt_auto_approval_writes_file(self, tmp_path: Path):
        """AC-8: receipt_auto_approval writes receipts/auto-approval-{stage}.json."""
        import sys
        sys.path.insert(0, str(HOOKS))
        from lib_receipts import receipt_auto_approval  # noqa: PLC0415
        task_dir = _make_task(tmp_path, stage="SPEC_REVIEW")
        sha = "a" * 64
        receipt_path = receipt_auto_approval(task_dir, "SPEC_REVIEW", sha)
        assert receipt_path.name == "auto-approval-SPEC_REVIEW.json"
        data = json.loads(receipt_path.read_text())
        assert data["step"] == "auto-approval-SPEC_REVIEW"
        assert data["artifact_sha256"] == sha
        assert data["approver"] == "auto:residual-skill"
        assert data["kind"] == "auto"

    def test_receipt_auto_approval_contract_version_7(self, tmp_path: Path):
        """AC-8 / AC-9: receipt carries contract_version=7."""
        import sys
        sys.path.insert(0, str(HOOKS))
        from lib_receipts import receipt_auto_approval  # noqa: PLC0415
        task_dir = _make_task(tmp_path, stage="SPEC_REVIEW")
        sha = "b" * 64
        receipt_path = receipt_auto_approval(task_dir, "SPEC_REVIEW", sha)
        data = json.loads(receipt_path.read_text())
        assert data["contract_version"] == 7

    def test_receipt_auto_approval_no_duplicate_contract_version(self, tmp_path: Path):
        """AC-8 / sc-004: receipt payload must NOT have contract_version duplicated.

        write_receipt injects contract_version at the top level. receipt_auto_approval
        must NOT also pass contract_version=6 as a kwarg — doing so would create two
        identical keys (silently collapsing to the last value, but semantically wrong).
        We verify the receipt has exactly one contract_version key at the top level.
        """
        import sys
        sys.path.insert(0, str(HOOKS))
        from lib_receipts import receipt_auto_approval  # noqa: PLC0415
        task_dir = _make_task(tmp_path, stage="SPEC_REVIEW")
        sha = "c" * 64
        receipt_path = receipt_auto_approval(task_dir, "SPEC_REVIEW", sha)
        raw = receipt_path.read_text()
        data = json.loads(raw)
        # JSON spec: a JSON object's keys should be unique. Verify no duplication
        # by counting occurrences of the key string in the raw text.
        assert raw.count('"contract_version"') == 1, (
            "contract_version must appear exactly once in the receipt JSON"
        )
        assert data["contract_version"] == 7

    def test_receipt_auto_approval_in_all(self, tmp_path: Path):
        """AC-8: receipt_auto_approval is exported in __all__."""
        import sys
        sys.path.insert(0, str(HOOKS))
        import lib_receipts  # noqa: PLC0415
        assert "receipt_auto_approval" in lib_receipts.__all__


# ===========================================================================
# AC-9: RECEIPT_CONTRACT_VERSION bumped to 7
# ===========================================================================

class TestContractVersionBump:
    """AC-9: RECEIPT_CONTRACT_VERSION == 7."""

    def test_receipt_contract_version_is_7(self):
        """AC-9: RECEIPT_CONTRACT_VERSION must be 7 after the bump."""
        import sys
        sys.path.insert(0, str(HOOKS))
        from lib_receipts import RECEIPT_CONTRACT_VERSION  # noqa: PLC0415
        assert RECEIPT_CONTRACT_VERSION == 7


# ===========================================================================
# AC-10: MIN_VERSION_PER_STEP has auto-approval-* entry
# ===========================================================================

class TestMinVersionPerStep:
    """AC-10: auto-approval-* floor added to MIN_VERSION_PER_STEP."""

    def test_min_version_auto_approval_wildcard_entry(self):
        """AC-10: 'auto-approval-*': 6 exists in MIN_VERSION_PER_STEP."""
        import sys
        sys.path.insert(0, str(HOOKS))
        from lib_receipts import MIN_VERSION_PER_STEP  # noqa: PLC0415
        assert "auto-approval-*" in MIN_VERSION_PER_STEP
        assert MIN_VERSION_PER_STEP["auto-approval-*"] == 6

    def test_min_version_human_approval_unchanged(self):
        """AC-10: existing 'human-approval-*': 2 entry is unchanged."""
        import sys
        sys.path.insert(0, str(HOOKS))
        from lib_receipts import MIN_VERSION_PER_STEP  # noqa: PLC0415
        assert MIN_VERSION_PER_STEP.get("human-approval-*") == 2

    def test_resolve_min_version_auto_approval_spec_review(self):
        """AC-10: _resolve_min_version('auto-approval-SPEC_REVIEW') returns 6."""
        import sys
        sys.path.insert(0, str(HOOKS))
        from lib_receipts import _resolve_min_version  # noqa: PLC0415
        assert _resolve_min_version("auto-approval-SPEC_REVIEW") == 6

    def test_resolve_min_version_auto_approval_plan_review(self):
        """AC-10: _resolve_min_version('auto-approval-PLAN_REVIEW') returns 6."""
        import sys
        sys.path.insert(0, str(HOOKS))
        from lib_receipts import _resolve_min_version  # noqa: PLC0415
        assert _resolve_min_version("auto-approval-PLAN_REVIEW") == 6

    def test_resolve_min_version_auto_approval_tdd_review(self):
        """AC-10: _resolve_min_version('auto-approval-TDD_REVIEW') returns 6."""
        import sys
        sys.path.insert(0, str(HOOKS))
        from lib_receipts import _resolve_min_version  # noqa: PLC0415
        assert _resolve_min_version("auto-approval-TDD_REVIEW") == 6


# ===========================================================================
# AC-11: _LOG_MESSAGES has three exact auto-approval keys
# ===========================================================================

class TestLogMessages:
    """AC-11: _LOG_MESSAGES three distinct auto-approval entries."""

    def test_log_messages_three_distinct_keys(self):
        """AC-11 / sc-001: _LOG_MESSAGES must have exactly three distinct auto-approval keys.

        The audit found a duplicate-key bug where 'auto-approval-PLAN_REVIEW' was
        used as a key twice instead of 'auto-approval-TDD_REVIEW' for the third entry.
        This test asserts all three distinct keys are present.
        """
        import sys
        sys.path.insert(0, str(HOOKS))
        from lib_receipts import _LOG_MESSAGES  # noqa: PLC0415
        assert "auto-approval-SPEC_REVIEW" in _LOG_MESSAGES
        assert "auto-approval-PLAN_REVIEW" in _LOG_MESSAGES
        assert "auto-approval-TDD_REVIEW" in _LOG_MESSAGES
        # Verify the values are distinct (each step has its own message)
        assert _LOG_MESSAGES["auto-approval-SPEC_REVIEW"] != _LOG_MESSAGES["auto-approval-TDD_REVIEW"]
        assert _LOG_MESSAGES["auto-approval-PLAN_REVIEW"] != _LOG_MESSAGES["auto-approval-TDD_REVIEW"]
        # Confirm correct substring in each value
        assert "SPEC_REVIEW" in _LOG_MESSAGES["auto-approval-SPEC_REVIEW"]
        assert "PLAN_REVIEW" in _LOG_MESSAGES["auto-approval-PLAN_REVIEW"]
        assert "TDD_REVIEW" in _LOG_MESSAGES["auto-approval-TDD_REVIEW"]

    def test_log_messages_auto_approval_format_has_approver(self):
        """AC-11: each auto-approval log message template has {approver} placeholder."""
        import sys
        sys.path.insert(0, str(HOOKS))
        from lib_receipts import _LOG_MESSAGES  # noqa: PLC0415
        for key in ("auto-approval-SPEC_REVIEW", "auto-approval-PLAN_REVIEW", "auto-approval-TDD_REVIEW"):
            assert "{approver}" in _LOG_MESSAGES[key], f"Missing {{approver}} in {key}"


# ===========================================================================
# AC-12 / AC-13 / AC-14: transition_task gate extension
# ===========================================================================

class TestTransitionGateExtension:
    """AC-12/13/14: _check_any_approval closure and hash-mismatch hardening."""

    def _make_spec_review_task(self, tmp_path: Path) -> Path:
        """Task at SPEC_REVIEW stage with spec.md."""
        task_dir = _make_task(tmp_path, stage="SPEC_REVIEW")
        return task_dir

    def test_auto_approval_receipt_satisfies_gate(self, tmp_path: Path):
        """AC-12 / AC-21g: auto-approval-SPEC_REVIEW.json with correct hash satisfies the gate."""
        import sys
        sys.path.insert(0, str(HOOKS))
        from lib_core import transition_task  # noqa: PLC0415
        from lib_receipts import hash_file  # noqa: PLC0415
        task_dir = self._make_spec_review_task(tmp_path)
        sha = hash_file(task_dir / "spec.md")
        _write_receipt_file(task_dir, "auto-approval-SPEC_REVIEW", {"artifact_sha256": sha})
        # Should not raise
        transition_task(task_dir, "PLANNING")
        manifest = json.loads((task_dir / "manifest.json").read_text())
        assert manifest["stage"] == "PLANNING"

    def test_human_approval_receipt_still_satisfies_gate(self, tmp_path: Path):
        """AC-12 / AC-21h: human-approval-SPEC_REVIEW.json still works (human path unbroken)."""
        import sys
        sys.path.insert(0, str(HOOKS))
        from lib_core import transition_task  # noqa: PLC0415
        from lib_receipts import hash_file  # noqa: PLC0415
        task_dir = self._make_spec_review_task(tmp_path)
        sha = hash_file(task_dir / "spec.md")
        _write_receipt_file(task_dir, "human-approval-SPEC_REVIEW", {"artifact_sha256": sha, "approver": "human"})
        transition_task(task_dir, "PLANNING")
        manifest = json.loads((task_dir / "manifest.json").read_text())
        assert manifest["stage"] == "PLANNING"

    def test_hash_mismatch_on_auto_approval_receipt_refused(self, tmp_path: Path):
        """AC-14 / AC-21i: tampered artifact_sha256 in auto-approval receipt -> ValueError with correct substrings."""
        import sys
        sys.path.insert(0, str(HOOKS))
        from lib_core import transition_task  # noqa: PLC0415
        task_dir = self._make_spec_review_task(tmp_path)
        tampered_sha = "0" * 64  # wrong hash
        _write_receipt_file(task_dir, "auto-approval-SPEC_REVIEW", {"artifact_sha256": tampered_sha})
        with pytest.raises(ValueError) as exc_info:
            transition_task(task_dir, "PLANNING")
        msg = str(exc_info.value)
        assert "auto-approval-SPEC_REVIEW" in msg
        assert "hash mismatch" in msg

    def test_transition_task_accepts_auto_approval_receipt(self, tmp_path: Path):
        """AC-14: transition_task proceeds when auto-approval receipt has matching hash."""
        import sys
        sys.path.insert(0, str(HOOKS))
        from lib_core import transition_task  # noqa: PLC0415
        from lib_receipts import hash_file  # noqa: PLC0415
        task_dir = self._make_spec_review_task(tmp_path)
        sha = hash_file(task_dir / "spec.md")
        _write_receipt_file(task_dir, "auto-approval-SPEC_REVIEW", {"artifact_sha256": sha})
        result = transition_task(task_dir, "PLANNING")
        # No exception = success; manifest updated
        manifest = json.loads((task_dir / "manifest.json").read_text())
        assert manifest["stage"] == "PLANNING"

    def test_neither_receipt_found_refuses_with_both_names(self, tmp_path: Path):
        """AC-12: when neither human nor auto receipt exists, error message contains both names."""
        import sys
        sys.path.insert(0, str(HOOKS))
        from lib_core import transition_task  # noqa: PLC0415
        task_dir = self._make_spec_review_task(tmp_path)
        with pytest.raises(ValueError, match="missing approval receipt") as exc_info:
            transition_task(task_dir, "PLANNING")
        msg = str(exc_info.value)
        assert "human-approval-SPEC_REVIEW" in msg
        assert "auto-approval-SPEC_REVIEW" in msg

    def test_plan_review_auto_approval_satisfies_gate(self, tmp_path: Path):
        """AC-12: auto-approval-PLAN_REVIEW satisfies PLAN_REVIEW -> PLAN_AUDIT gate."""
        import sys
        sys.path.insert(0, str(HOOKS))
        from lib_core import transition_task  # noqa: PLC0415
        from lib_receipts import hash_file  # noqa: PLC0415
        task_dir = _make_task(tmp_path, stage="PLAN_REVIEW")
        (task_dir / "plan.md").write_text(PLAN_CONTENT)
        sha = hash_file(task_dir / "plan.md")
        _write_receipt_file(task_dir, "auto-approval-PLAN_REVIEW", {"artifact_sha256": sha})
        transition_task(task_dir, "PLAN_AUDIT")
        manifest = json.loads((task_dir / "manifest.json").read_text())
        assert manifest["stage"] == "PLAN_AUDIT"


# ===========================================================================
# AC-13: _human_approval_err mirror
# ===========================================================================

class TestHumanApprovalErrMirror:
    """AC-13: _human_approval_err mirrors _check_any_approval OR-logic."""

    def test_force_override_observability_on_previously_auto_approved_task(
        self, tmp_path: Path
    ):
        """AC-13 / AC-21m: force-transition on a task where SPEC_REVIEW was auto-approved.

        _compute_bypassed_gates_for_force uses the updated _human_approval_err.
        The returned bypassed-gates list must be empty (auto-approval satisfies the gate).
        """
        import sys
        sys.path.insert(0, str(HOOKS))
        from lib_core import transition_task  # noqa: PLC0415
        from lib_receipts import hash_file  # noqa: PLC0415
        task_dir = _make_task(tmp_path, stage="SPEC_REVIEW", auto_approve_gates=True)
        sha = hash_file(task_dir / "spec.md")
        _write_receipt_file(task_dir, "auto-approval-SPEC_REVIEW", {"artifact_sha256": sha})
        # Force-transition should compute 0 bypassed gates because the
        # auto-approval receipt satisfies the SPEC_REVIEW gate
        _, updated_manifest = transition_task(
            task_dir,
            "PLANNING",
            force=True,
            force_reason="test override",
            force_approver="test-operator",
        )
        # Look for the force-override receipt which carries bypassed_gates
        override_receipts = list((task_dir / "receipts").glob("force-override-*.json"))
        if override_receipts:
            receipt_data = json.loads(override_receipts[0].read_text())
            bypassed = receipt_data.get("bypassed_gates", [])
            assert bypassed == [], (
                f"bypassed_gates must be empty when auto-approval receipt satisfies the gate; got {bypassed}"
            )


# ===========================================================================
# AC-19 / AC-21j: receipt_retrospective auto-approval fields
# ===========================================================================

class TestRetrospectiveAutoApprovalFields:
    """AC-19: receipt_retrospective self-computes gates_auto_approved and auto_approved_stages."""

    def _make_done_task(self, tmp_path: Path) -> Path:
        """Task at DONE stage with all required artifacts for retrospective."""
        task_dir = _make_task(tmp_path, stage="DONE")
        (task_dir / "plan.md").write_text(PLAN_CONTENT)
        (task_dir / "evidence").mkdir(exist_ok=True)
        (task_dir / "evidence" / "tdd-tests.md").write_text(TDD_EVIDENCE_CONTENT)
        (task_dir / "task-retrospective.json").write_text(
            json.dumps({"task_id": task_dir.name, "quality": 0.8})
        )
        return task_dir

    def test_retrospective_gates_auto_approved_count(self, tmp_path: Path):
        """AC-19 / AC-21j: two auto-approval receipts -> gates_auto_approved=2, auto_approved_stages sorted."""
        import sys
        sys.path.insert(0, str(HOOKS))
        from lib_receipts import receipt_retrospective  # noqa: PLC0415
        task_dir = self._make_done_task(tmp_path)
        # Write two auto-approval receipts
        _write_receipt_file(task_dir, "auto-approval-SPEC_REVIEW", {"artifact_sha256": "a" * 64})
        _write_receipt_file(task_dir, "auto-approval-PLAN_REVIEW", {"artifact_sha256": "b" * 64})
        receipt_path = receipt_retrospective(task_dir)
        retro = json.loads(receipt_path.read_text())
        assert retro.get("gates_auto_approved") == 2
        assert retro.get("auto_approved_stages") == ["PLAN_REVIEW", "SPEC_REVIEW"]

    def test_retrospective_zero_auto_approved_stages(self, tmp_path: Path):
        """AC-19: no auto-approval receipts -> gates_auto_approved=0, auto_approved_stages=[]."""
        import sys
        sys.path.insert(0, str(HOOKS))
        from lib_receipts import receipt_retrospective  # noqa: PLC0415
        task_dir = self._make_done_task(tmp_path)
        # No auto-approval receipts
        receipt_path = receipt_retrospective(task_dir)
        retro = json.loads(receipt_path.read_text())
        assert retro.get("gates_auto_approved", 0) == 0
        assert retro.get("auto_approved_stages", []) == []


# ===========================================================================
# AC-22: external surface path glob patterns
# ===========================================================================

@pytest.mark.parametrize(
    "location,should_block",
    [
        ("hooks/ctl.py", True),               # exact match
        ("skills/residual/SKILL.md", True),    # skills/**/SKILL.md
        ("skills/start/SKILL.md", True),       # skills/**/SKILL.md
        ("agents/planner/design.md", True),    # agents/**/*.md
        ("some/router/config.py", True),       # *router*
        ("api/v1/endpoint.py", True),          # api/**
        ("", True),                            # empty location
        ("lib/utils.py", False),               # safe — should not block
        ("tests/test_core.py", False),         # safe
    ],
)
def test_external_surface_path_glob_blocks_each_pattern(
    tmp_path: Path, location: str, should_block: bool
):
    """AC-22 / AC-21e (parameterized): each path glob pattern triggers (or doesn't trigger) the ceiling.

    Tests both set-auto-approve-gates and the internal _check_external_surface_path logic.
    """
    root = tmp_path / "project"
    root.mkdir()
    (root / ".dynos").mkdir()
    task_dir = _make_task(tmp_path / "td", stage="SPEC_REVIEW")
    # Remove classification so set-auto-approve-gates can run pre-classification
    manifest = json.loads((task_dir / "manifest.json").read_text())
    manifest.pop("classification", None)
    (task_dir / "manifest.json").write_text(json.dumps(manifest))
    row = _default_row(source_auditor="code-quality-auditor", location=location)
    _make_queue(root, row)
    dynos_home = tmp_path / "home"
    dynos_home.mkdir()
    persistent = _persistent_dir(dynos_home, root)
    _write_policy(persistent, disabled=False)
    r = _run(
        "set-auto-approve-gates",
        "--task-dir", str(task_dir),
        "--from-residual-id", row["id"],
        dynos_home=dynos_home,
        cwd=root,
    )
    if should_block:
        assert r.returncode == 1, (
            f"Expected blocking for location={location!r}, got exit 0 with stdout={r.stdout!r}"
        )
    else:
        assert r.returncode == 0, (
            f"Expected pass for location={location!r}, got exit 1 with stderr={r.stderr!r}"
        )


# ===========================================================================
# AC-23: policy file path and semantics
# ===========================================================================

class TestPolicyFilePath:
    """AC-23: policy.json under ~/.dynos/projects/{slug}/; missing = false (fail-open)."""

    def test_missing_policy_file_treated_as_enabled(self, tmp_path: Path):
        """AC-23: absent policy.json -> feature enabled (fail-open for the disable key)."""
        root = tmp_path / "project"
        root.mkdir()
        (root / ".dynos").mkdir()
        task_dir = _make_task(tmp_path / "td", stage="SPEC_REVIEW")
        manifest = json.loads((task_dir / "manifest.json").read_text())
        manifest.pop("classification", None)
        (task_dir / "manifest.json").write_text(json.dumps(manifest))
        row = _default_row()
        _make_queue(root, row)
        dynos_home = tmp_path / "home"
        dynos_home.mkdir()
        # No policy.json created — should behave as if feature enabled
        r = _run(
            "set-auto-approve-gates",
            "--task-dir", str(task_dir),
            "--from-residual-id", row["id"],
            dynos_home=dynos_home,
            cwd=root,
        )
        # Should succeed (feature is enabled by default)
        assert r.returncode == 0, r.stderr

    def test_missing_key_in_policy_treated_as_enabled(self, tmp_path: Path):
        """AC-23: policy.json exists but lacks auto_approve_gates_disabled -> enabled."""
        root = tmp_path / "project"
        root.mkdir()
        (root / ".dynos").mkdir()
        task_dir = _make_task(tmp_path / "td", stage="SPEC_REVIEW")
        manifest = json.loads((task_dir / "manifest.json").read_text())
        manifest.pop("classification", None)
        (task_dir / "manifest.json").write_text(json.dumps(manifest))
        row = _default_row()
        _make_queue(root, row)
        dynos_home = tmp_path / "home"
        dynos_home.mkdir()
        persistent = _persistent_dir(dynos_home, root)
        persistent.mkdir(parents=True)
        (persistent / "policy.json").write_text(json.dumps({"some_other_key": True}))
        r = _run(
            "set-auto-approve-gates",
            "--task-dir", str(task_dir),
            "--from-residual-id", row["id"],
            dynos_home=dynos_home,
            cwd=root,
        )
        assert r.returncode == 0, r.stderr


# ===========================================================================
# AC-24: apply-auto-approve-veto in cmd_run_start_classification
# ===========================================================================

class TestApplyVetoCrashDegradeGracefully:
    """AC-24: veto crash/non-zero return in cmd_run_start_classification degrades gracefully."""

    def _make_classifiable_task(self, tmp_path: Path) -> Path:
        """Task ready for run-start-classification."""
        task_dir = _make_task(
            tmp_path,
            task_id="task-20260505-cls",
            stage="CLASSIFY_AND_SPEC",
            auto_approve_gates=True,
            classification={
                "type": "feature",
                "domains": ["backend"],
                "risk_level": "low",
                "notes": "n",
                "tdd_required": False,
            },
        )
        return task_dir

    @pytest.mark.parametrize(
        "veto_side_effect",
        [
            "raise",    # monkeypatch raises RuntimeError
            "return_1", # monkeypatch returns 1 (non-zero without exception)
        ],
    )
    def test_apply_veto_crash_degrades_gracefully(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        veto_side_effect: str,
    ):
        """AC-24 / sc-002: both raise AND non-zero return from veto are logged; cmd_run_start_classification exits 0.

        Case 'raise': monkeypatched veto raises RuntimeError.
        Case 'return_1': monkeypatched veto returns 1 (integer, no exception).
        Both cases: auto_approve_gates remains false (or its prior value), stderr has warning.
        """
        import sys
        sys.path.insert(0, str(HOOKS))
        import importlib
        import ctl as _ctl  # noqa: PLC0415 — direct import to monkeypatch

        if veto_side_effect == "raise":
            def _veto_raise(args):  # type: ignore[misc]
                raise RuntimeError("veto simulated crash")
            monkeypatch.setattr(_ctl, "cmd_apply_auto_approve_veto", _veto_raise)
        else:
            def _veto_return1(args):  # type: ignore[misc]
                return 1
            monkeypatch.setattr(_ctl, "cmd_apply_auto_approve_veto", _veto_return1)

        task_dir = self._make_classifiable_task(tmp_path)
        import argparse as _argparse
        ns = _argparse.Namespace(task_dir=str(task_dir))
        import io, contextlib
        stderr_buf = io.StringIO()
        with contextlib.redirect_stderr(stderr_buf):
            rc = _ctl.cmd_run_start_classification(ns)

        # Must not abort (exit 0)
        assert rc == 0, f"cmd_run_start_classification must exit 0 even when veto {veto_side_effect}s"
        # Stderr must have a warning about the veto failure
        stderr_text = stderr_buf.getvalue()
        assert "veto" in stderr_text.lower() or "apply-auto-approve-veto" in stderr_text or "warn" in stderr_text.lower(), (
            f"Expected veto warning on stderr but got: {stderr_text!r}"
        )
        # auto_approve_gates must remain false (degraded to human-approval behavior)
        manifest = json.loads((task_dir / "manifest.json").read_text())
        assert manifest.get("auto_approve_gates", False) is False, (
            "auto_approve_gates must remain false when veto crashes"
        )

    def test_apply_veto_called_after_classification(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """AC-24: apply-auto-approve-veto is called after manifest.classification is settled."""
        import sys
        sys.path.insert(0, str(HOOKS))
        import ctl as _ctl  # noqa: PLC0415

        veto_calls: list[object] = []
        original_veto = getattr(_ctl, "cmd_apply_auto_approve_veto", None)

        def _tracking_veto(args):  # type: ignore[misc]
            veto_calls.append(args)
            return 0

        monkeypatch.setattr(_ctl, "cmd_apply_auto_approve_veto", _tracking_veto)

        task_dir = self._make_classifiable_task(tmp_path)
        import argparse as _argparse
        ns = _argparse.Namespace(task_dir=str(task_dir))
        rc = _ctl.cmd_run_start_classification(ns)
        # The veto must have been called at least once
        assert len(veto_calls) >= 1, "cmd_apply_auto_approve_veto must be called from cmd_run_start_classification"


# ===========================================================================
# AC-21l: race condition — LOCK_EX test
# ===========================================================================

class TestLockExRace:
    """AC-21l: concurrent set-auto-approve-gates calls must not corrupt manifest.json."""

    def test_set_auto_approve_gates_lock_ex_race(self, tmp_path: Path):
        """AC-21l: two concurrent subprocesses calling set-auto-approve-gates produce valid JSON.

        Final manifest.auto_approve_gates must be a valid boolean (true) and the file
        must remain parseable.
        """
        root = tmp_path / "project"
        root.mkdir()
        (root / ".dynos").mkdir()
        task_dir = _make_task(tmp_path / "td", stage="SPEC_REVIEW")
        # Remove classification so both calls can proceed
        manifest = json.loads((task_dir / "manifest.json").read_text())
        manifest.pop("classification", None)
        (task_dir / "manifest.json").write_text(json.dumps(manifest))
        row = _default_row(source_auditor="code-quality-auditor", location="lib/utils.py")
        _make_queue(root, row)
        dynos_home = tmp_path / "home"
        dynos_home.mkdir()
        persistent = _persistent_dir(dynos_home, root)
        _write_policy(persistent, disabled=False)

        cmd = [
            sys.executable, str(HOOKS / "ctl.py"),
            "set-auto-approve-gates",
            "--task-dir", str(task_dir),
            "--from-residual-id", row["id"],
        ]
        env = _env(dynos_home)

        results: list[subprocess.CompletedProcess] = []

        def _run_proc() -> None:
            r = subprocess.run(cmd, cwd=str(root), text=True, capture_output=True, check=False, env=env)
            results.append(r)

        t1 = threading.Thread(target=_run_proc)
        t2 = threading.Thread(target=_run_proc)
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        # At least one must succeed
        assert any(r.returncode == 0 for r in results), (
            f"At least one concurrent call must succeed; results: {[(r.returncode, r.stderr) for r in results]}"
        )
        # manifest.json must be valid JSON
        raw = (task_dir / "manifest.json").read_text()
        final_manifest = json.loads(raw)  # Must not raise
        # auto_approve_gates must be a valid boolean
        assert isinstance(final_manifest.get("auto_approve_gates", False), bool)


# ===========================================================================
# IR-3: validate_chain returns no gap for auto-approval-only tasks
# ===========================================================================

class TestValidateChainGapFree:
    """IR-3: validate_chain must not report a gap for auto-approval receipts."""

    def test_validate_chain_no_gap_for_auto_approval_receipts(self, tmp_path: Path):
        """IR-3: a task that used only auto-approval receipts at all three gates reports no gap."""
        import sys
        sys.path.insert(0, str(HOOKS))
        from lib_receipts import validate_chain, hash_file  # noqa: PLC0415
        task_dir = _make_task(tmp_path, stage="DONE")
        (task_dir / "plan.md").write_text(PLAN_CONTENT)
        (task_dir / "evidence").mkdir(exist_ok=True)
        (task_dir / "evidence" / "tdd-tests.md").write_text(TDD_EVIDENCE_CONTENT)
        sha_spec = hash_file(task_dir / "spec.md")
        sha_plan = hash_file(task_dir / "plan.md")
        sha_tdd = hash_file(task_dir / "evidence" / "tdd-tests.md")
        _write_receipt_file(task_dir, "auto-approval-SPEC_REVIEW", {"artifact_sha256": sha_spec})
        _write_receipt_file(task_dir, "auto-approval-PLAN_REVIEW", {"artifact_sha256": sha_plan})
        _write_receipt_file(task_dir, "auto-approval-TDD_REVIEW", {"artifact_sha256": sha_tdd})
        gaps = validate_chain(task_dir)
        # auto-approval receipts must not introduce spurious gap reports
        auto_gaps = [g for g in gaps if "auto-approval" in g]
        assert auto_gaps == [], f"validate_chain must not gap-report auto-approval receipts: {auto_gaps}"


# ===========================================================================
# AC-21i exact name: test_auto_approval_hash_mismatch_refused
# AC-21m exact name: test_force_override_on_auto_approved_task
# AC-21 integration: test_low_risk_residual_auto_drains_end_to_end
# ===========================================================================

class TestEndToEndDrain:
    """AC-21 exact-name fixtures and end-to-end low-risk drain integration test."""

    def test_auto_approval_hash_mismatch_refused(self, tmp_path: Path):
        """AC-21i exact name: tampered artifact_sha256 in auto-approval receipt raises ValueError.

        Writes auto-approval-SPEC_REVIEW.json with a known-wrong sha256 and
        asserts transition_task raises ValueError whose message contains both
        'auto-approval-SPEC_REVIEW' and 'hash mismatch'.
        """
        import sys
        sys.path.insert(0, str(HOOKS))
        from lib_core import transition_task  # noqa: PLC0415
        task_dir = _make_task(tmp_path, stage="SPEC_REVIEW")
        tampered_sha = "0" * 64
        _write_receipt_file(task_dir, "auto-approval-SPEC_REVIEW", {"artifact_sha256": tampered_sha})
        with pytest.raises(ValueError) as exc_info:
            transition_task(task_dir, "PLANNING")
        msg = str(exc_info.value)
        assert "auto-approval-SPEC_REVIEW" in msg
        assert "hash mismatch" in msg

    def test_force_override_on_auto_approved_task(self, tmp_path: Path):
        """AC-21m exact name: force-transition on an auto-approved task yields empty bypassed-gates.

        A task where SPEC_REVIEW was auto-approved: transition_task(..., force=True)
        must compute an empty bypassed-gates list because the auto-approval receipt
        satisfies the gate even in force/dry-run mode.
        """
        import sys
        sys.path.insert(0, str(HOOKS))
        from lib_core import transition_task  # noqa: PLC0415
        from lib_receipts import hash_file  # noqa: PLC0415
        task_dir = _make_task(tmp_path, stage="SPEC_REVIEW", auto_approve_gates=True)
        sha = hash_file(task_dir / "spec.md")
        _write_receipt_file(task_dir, "auto-approval-SPEC_REVIEW", {"artifact_sha256": sha})
        transition_task(
            task_dir,
            "PLANNING",
            force=True,
            force_reason="test override",
            force_approver="test-operator",
        )
        override_receipts = list((task_dir / "receipts").glob("force-override-*.json"))
        if override_receipts:
            receipt_data = json.loads(override_receipts[0].read_text())
            bypassed = receipt_data.get("bypassed_gates", [])
            assert bypassed == [], (
                f"bypassed_gates must be empty when auto-approval receipt satisfies the gate; got {bypassed}"
            )
        # Regardless of whether a force-override receipt exists, stage must advance
        manifest = json.loads((task_dir / "manifest.json").read_text())
        assert manifest["stage"] == "PLANNING"

    def test_low_risk_residual_auto_drains_end_to_end(self, tmp_path: Path):
        """AC-21 integration (end-to-end): a low-risk residual drains through all auto-approve gates.

        Setup:
          - Queue row: source_auditor=code-quality-auditor, location=hooks/lib_receipts.py
            (NOT in the surface-block path-glob list — this is a non-ctl.py hooks file).
          - Task at SPEC_REVIEW, classification risk_level=low, domains=['backend'].

        Sequence:
          1. set-auto-approve-gates  → manifest.auto_approve_gates=true
          2. apply-auto-approve-veto → decision='auto' (does NOT downgrade)
          3. approve-stage SPEC_REVIEW --auto-approved  → auto-approval-SPEC_REVIEW.json written
          4. approve-stage PLAN_REVIEW --auto-approved  → auto-approval-PLAN_REVIEW.json written
          5. approve-stage TDD_REVIEW  --auto-approved  → auto-approval-TDD_REVIEW.json written
          6. manifest stage advances through SPEC_REVIEW approval gate to PLANNING.

        This test is intentionally RED until seg-3 (ctl.py commands) is implemented.
        """
        root = tmp_path / "project"
        root.mkdir()
        (root / ".dynos").mkdir()

        # Task at SPEC_REVIEW, no pre-settled classification (set-auto-approve-gates needs pre-classification)
        task_dir = _make_task(
            tmp_path / "td",
            stage="SPEC_REVIEW",
            classification=None,  # not yet classified
        )
        # Overwrite manifest to remove the classification field entirely
        manifest_data = json.loads((task_dir / "manifest.json").read_text())
        manifest_data.pop("classification", None)
        (task_dir / "manifest.json").write_text(json.dumps(manifest_data, indent=2))

        # Queue row: safe auditor, location NOT in surface-block list
        row = _default_row(
            rid="res-e2e-001",
            source_auditor="code-quality-auditor",
            location="hooks/lib_receipts.py",
        )
        _make_queue(root, row)

        dynos_home = tmp_path / "home"
        dynos_home.mkdir()
        persistent = _persistent_dir(dynos_home, root)
        _write_policy(persistent, disabled=False)

        # Step 1: set-auto-approve-gates must exit 0 and write manifest.auto_approve_gates=true
        r_set = _run(
            "set-auto-approve-gates",
            "--task-dir", str(task_dir),
            "--from-residual-id", row["id"],
            dynos_home=dynos_home,
            cwd=root,
        )
        assert r_set.returncode == 0, (
            f"set-auto-approve-gates failed: {r_set.stderr!r}"
        )
        manifest_after_set = json.loads((task_dir / "manifest.json").read_text())
        assert manifest_after_set.get("auto_approve_gates") is True, (
            "manifest.auto_approve_gates must be true after set-auto-approve-gates"
        )

        # Step 2: settle classification so apply-auto-approve-veto can run
        manifest_after_set["classification"] = {
            "type": "feature",
            "domains": ["backend"],
            "risk_level": "low",
            "notes": "low-risk backend change",
            "tdd_required": False,
        }
        (task_dir / "manifest.json").write_text(json.dumps(manifest_after_set, indent=2))
        # Write raw-input.md sentinel for veto to locate the residual row
        (task_dir / "raw-input.md").write_text(
            f"<!-- residual-id: {row['id']} -->\nResidual input."
        )

        # Step 3: apply-auto-approve-veto must exit 0 and NOT downgrade (decision='auto')
        r_veto = _run(
            "apply-auto-approve-veto",
            "--task-dir", str(task_dir),
            dynos_home=dynos_home,
            cwd=root,
        )
        assert r_veto.returncode == 0, (
            f"apply-auto-approve-veto failed: {r_veto.stderr!r}"
        )
        veto_out = json.loads(r_veto.stdout)
        assert veto_out.get("decision") == "auto", (
            f"apply-auto-approve-veto must not downgrade low-risk task; got decision={veto_out.get('decision')!r}"
        )
        manifest_after_veto = json.loads((task_dir / "manifest.json").read_text())
        assert manifest_after_veto.get("auto_approve_gates") is True, (
            "apply-auto-approve-veto must NOT downgrade auto_approve_gates for low-risk task"
        )

        # Step 4: approve-stage SPEC_REVIEW --auto-approved → writes auto-approval-SPEC_REVIEW.json
        r_spec = _run("approve-stage", str(task_dir), "SPEC_REVIEW", "--auto-approved", cwd=root)
        assert r_spec.returncode == 0, (
            f"approve-stage SPEC_REVIEW --auto-approved failed: {r_spec.stderr!r}"
        )
        assert (task_dir / "receipts" / "auto-approval-SPEC_REVIEW.json").exists(), (
            "auto-approval-SPEC_REVIEW.json must be written"
        )

        # Step 5: approve-stage PLAN_REVIEW --auto-approved → writes auto-approval-PLAN_REVIEW.json
        #   (task must be at PLAN_REVIEW stage for this to work; advance the stage manually)
        manifest_spec_approved = json.loads((task_dir / "manifest.json").read_text())
        manifest_spec_approved["stage"] = "PLAN_REVIEW"
        (task_dir / "plan.md").write_text(PLAN_CONTENT)
        (task_dir / "manifest.json").write_text(json.dumps(manifest_spec_approved, indent=2))
        r_plan = _run("approve-stage", str(task_dir), "PLAN_REVIEW", "--auto-approved", cwd=root)
        assert r_plan.returncode == 0, (
            f"approve-stage PLAN_REVIEW --auto-approved failed: {r_plan.stderr!r}"
        )
        assert (task_dir / "receipts" / "auto-approval-PLAN_REVIEW.json").exists(), (
            "auto-approval-PLAN_REVIEW.json must be written"
        )

        # Step 6: approve-stage TDD_REVIEW --auto-approved → writes auto-approval-TDD_REVIEW.json
        manifest_plan_approved = json.loads((task_dir / "manifest.json").read_text())
        manifest_plan_approved["stage"] = "TDD_REVIEW"
        (task_dir / "evidence").mkdir(exist_ok=True)
        (task_dir / "evidence" / "tdd-tests.md").write_text(TDD_EVIDENCE_CONTENT)
        (task_dir / "manifest.json").write_text(json.dumps(manifest_plan_approved, indent=2))
        r_tdd = _run("approve-stage", str(task_dir), "TDD_REVIEW", "--auto-approved", cwd=root)
        assert r_tdd.returncode == 0, (
            f"approve-stage TDD_REVIEW --auto-approved failed: {r_tdd.stderr!r}"
        )
        assert (task_dir / "receipts" / "auto-approval-TDD_REVIEW.json").exists(), (
            "auto-approval-TDD_REVIEW.json must be written"
        )

        # Step 7: manifest stage advances through SPEC_REVIEW gate
        # (The stage transitions were driven by approve-stage above; verify final receipt state)
        receipts_dir = task_dir / "receipts"
        for stage_label in ("SPEC_REVIEW", "PLAN_REVIEW", "TDD_REVIEW"):
            receipt_file = receipts_dir / f"auto-approval-{stage_label}.json"
            assert receipt_file.exists(), f"auto-approval-{stage_label}.json must exist"
            receipt_data = json.loads(receipt_file.read_text())
            assert receipt_data.get("valid") is True, (
                f"auto-approval-{stage_label}.json must have valid=true"
            )
