"""Tests for the DONE-edge retrospective flush (F10, CRITERION 10).

On any transition into `next_stage == "DONE"`, `transition_task` copies
`task_dir/task-retrospective.json` to
`_persistent_project_dir(root)/retrospectives/{task_id}.json` so the
retro survives worktree removal and reaches the calibration
pipeline's `collect_retrospectives(root)` sweep.

Invariants:

  - Success path emits a `retrospective_flushed` event and the
    destination file exists with the same content as the source.
  - Failure path (e.g. destination read-only) emits a
    `retrospective_flush_failed` event but DOES NOT block the DONE
    transition. Force-style break-glass — observability must never
    lock the state machine.
  - Subsequent DONE transitions on the same task_id (re-play) overwrite
    the persistent copy with the latest source.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_core import (  # noqa: E402
    _persistent_project_dir,
    transition_task,
)
from lib_receipts import (  # noqa: E402
    receipt_audit_routing,
    receipt_postmortem_skipped,
    receipt_retrospective,
    receipt_rules_check_passed,
)


@pytest.fixture(autouse=True)
def _empty_auditor_registry(monkeypatch):
    """PR #127 (task-006) AC 6 added a registry-eligible auditor cross-check
    inside require_receipts_for_done. These flush tests pre-date that gate,
    so we mock the registry to be empty — the gate becomes vacuous and the
    test focuses on the flush behavior under test."""
    import router
    monkeypatch.setattr(router, "_load_auditor_registry", lambda root: {
        "always": [], "fast_track": [], "domain_conditional": {},
    })


def _setup_done_ready(tmp_path: Path, slug: str = "RF",
                      quality: float = 0.95) -> Path:
    """Build a task at CHECKPOINT_AUDIT with every receipt/artifact the
    DONE gate requires so `transition_task(..., "DONE")` succeeds
    without needing `force=True`.

    Returns the task directory."""
    project = tmp_path / "project"
    td = project / ".dynos" / f"task-20260419-{slug}"
    td.mkdir(parents=True)
    (td / "manifest.json").write_text(json.dumps({
        "task_id": td.name,
        "stage": "CHECKPOINT_AUDIT",
        "classification": {"risk_level": "medium"},
    }))
    # Retrospective artifact (source of the flush).
    (td / "task-retrospective.json").write_text(json.dumps({
        "task_id": td.name,
        "quality_score": quality,
        "cost_score": 0.8,
        "efficiency_score": 0.8,
    }, indent=2))
    audit_dir = td / "audit-reports"
    audit_dir.mkdir()
    (audit_dir / "report.json").write_text(json.dumps({"findings": []}))

    receipt_retrospective(td, quality, 0.9, 0.9, 1000)
    # PR #127 (task-006) AC 1: receipt_rules_check_passed self-computes;
    # callers pass only (task_dir, mode). Stub run_checks to a clean pass.
    import rules_engine
    _orig_run_checks = rules_engine.run_checks
    rules_engine.run_checks = lambda root, mode: []
    try:
        receipt_rules_check_passed(td, "all")
    finally:
        rules_engine.run_checks = _orig_run_checks
    receipt_audit_routing(td, [])
    # task-20260419-002 G2: subsumed_by is required; empty list is
    # valid because reason is `no-findings`.
    receipt_postmortem_skipped(td, "no-findings", "a" * 64, subsumed_by=[])
    return td


def _install_dynos_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point DYNOS_HOME at a tmp dir so _persistent_project_dir resolves
    to a writable location under the test's tmp tree, not the user's
    real ~/.dynos."""
    home = tmp_path / "dynos-home"
    home.mkdir()
    monkeypatch.setenv("DYNOS_HOME", str(home))
    return home


def _read_events(td: Path) -> list[dict]:
    events_path = td / "events.jsonl"
    if not events_path.exists():
        return []
    return [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


def test_flush_writes_to_persistent_dir(tmp_path: Path,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """DONE transition copies task-retrospective.json into the
    persistent project dir; destination content matches the source
    byte-for-byte (file is a straight text copy via _atomic_write_text)."""
    _install_dynos_home(monkeypatch, tmp_path)
    td = _setup_done_ready(tmp_path, slug="FLUSH-OK")
    root = td.parent.parent

    transition_task(td, "DONE")

    persistent = _persistent_project_dir(root) / "retrospectives" / f"{td.name}.json"
    assert persistent.exists(), f"persistent retro must exist at {persistent}"
    src = (td / "task-retrospective.json").read_text()
    dst = persistent.read_text()
    assert src == dst, "persistent copy content must match source byte-for-byte"


def test_flush_overwrites_existing_persistent_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later DONE transition for the same task_id replaces an older
    persistent copy with the current source. Pre-populate the
    persistent dir with stale content; run the DONE transition; assert
    the persistent file now matches the source (new content), not the
    stale one."""
    _install_dynos_home(monkeypatch, tmp_path)
    td = _setup_done_ready(tmp_path, slug="FLUSH-OVR", quality=0.77)
    root = td.parent.parent

    # Pre-populate the persistent copy with stale content.
    pdir = _persistent_project_dir(root) / "retrospectives"
    pdir.mkdir(parents=True, exist_ok=True)
    stale = pdir / f"{td.name}.json"
    stale.write_text(json.dumps({"task_id": td.name, "quality_score": 0.01}))

    transition_task(td, "DONE")

    reloaded = json.loads(stale.read_text())
    assert reloaded["quality_score"] == 0.77, (
        f"stale persistent copy was NOT overwritten — got {reloaded!r}"
    )


def test_flush_rejects_path_traversal_task_id(tmp_path: Path) -> None:
    """SEC-001 regression: a crafted manifest task_id like '../../evil'
    must NOT escape the persistent retrospectives dir. The flush helper
    validates task_id against ^task-[A-Za-z0-9][A-Za-z0-9_.-]*$ and emits
    retrospective_flush_failed when the slug is invalid."""
    project = tmp_path / "proj"
    (project / ".dynos").mkdir(parents=True)
    td = project / ".dynos" / "task-20260419-XX"
    td.mkdir()
    (td / "manifest.json").write_text(json.dumps({
        "task_id": "../../evil",
        "stage": "CHECKPOINT_AUDIT",
        "classification": {"risk_level": "low"},
    }))
    (td / "task-retrospective.json").write_text('{"task_id": "../../evil", "quality_score": 0.9}')

    from lib_core import _flush_retrospective_on_done, _persistent_project_dir
    manifest = json.loads((td / "manifest.json").read_text())
    # Must not raise; slug validation blocks the write.
    _flush_retrospective_on_done(task_dir=td, manifest=manifest)

    pd = _persistent_project_dir(project)
    retros_dir = pd / "retrospectives"
    # The poisoned slug produces no file anywhere under retrospectives.
    # (Directory may or may not exist — either way, no '../../evil.json'.)
    if retros_dir.exists():
        assert not any(p.name == "../../evil.json" or "evil" in p.name
                       for p in retros_dir.iterdir())
    # And absolutely nothing outside the project root.
    assert not (project.parent / "evil.json").exists()


def test_flush_emits_success_event(tmp_path: Path,
                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """Successful DONE flush emits a `retrospective_flushed` event with
    task_id, source, destination, and sha256 fields populated."""
    _install_dynos_home(monkeypatch, tmp_path)
    td = _setup_done_ready(tmp_path, slug="FLUSH-EVT")

    transition_task(td, "DONE")

    events = _read_events(td)
    flushed = [e for e in events if e.get("event") == "retrospective_flushed"]
    assert len(flushed) >= 1, (
        f"expected a retrospective_flushed event — got events: "
        f"{[e.get('event') for e in events]}"
    )
    ev = flushed[0]
    assert ev.get("task_id") == td.name
    assert str(ev.get("source", "")).endswith("task-retrospective.json")
    assert str(ev.get("destination", "")).endswith(f"{td.name}.json")


# ---------------------------------------------------------------------------
# Failure path
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="read-only directory guard is ineffective for root — skip",
)
def test_flush_failure_does_not_block_done(tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the persistent retrospectives dir read-only so
    `_atomic_write_text` raises OSError. The DONE transition MUST
    still succeed (stage advances), and a `retrospective_flush_failed`
    event MUST be emitted. This pins the "observability cannot block
    the state machine" invariant."""
    _install_dynos_home(monkeypatch, tmp_path)
    td = _setup_done_ready(tmp_path, slug="FLUSH-FAIL")
    root = td.parent.parent

    # Pre-create a read-only persistent retrospectives dir so
    # `_atomic_write_text`'s tempfile write inside it raises.
    pdir = _persistent_project_dir(root) / "retrospectives"
    pdir.mkdir(parents=True, exist_ok=True)
    # Restrict to read+execute only (no write). os.chmod applies to
    # directory entries; combined with exist_ok=True in the impl this
    # surfaces an OSError at tempfile.mkstemp time.
    original_mode = pdir.stat().st_mode
    os.chmod(pdir, stat.S_IRUSR | stat.S_IXUSR)
    try:
        transition_task(td, "DONE")
        manifest = json.loads((td / "manifest.json").read_text())
        assert manifest["stage"] == "DONE", (
            "DONE transition MUST NOT be blocked by a flush-write failure"
        )
        events = _read_events(td)
        failed = [e for e in events if e.get("event") == "retrospective_flush_failed"]
        assert len(failed) >= 1, (
            f"expected retrospective_flush_failed event — got events: "
            f"{[e.get('event') for e in events]}"
        )
    finally:
        # Restore so pytest can clean up the tmp tree.
        os.chmod(pdir, original_mode)
