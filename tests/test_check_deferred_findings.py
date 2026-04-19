"""Tests for task-20260419-002 G4.b: ``check_deferred_findings``
(importable helper + CLI).

Covers acceptance criteria 11 and 16 from the task spec:

  - missing registry → empty result / exit 0 (cold start, fail-open)
  - no intersection between entry.files and changed_files → empty /
    exit 0
  - intersection but still within TTL → empty / exit 0
  - intersection AND TTL-expired → entry returned (with ``elapsed``
    field) / exit 1, with stdout printing one line per expired entry
  - multiple expired findings → all reported
  - CLI wrapper: exercises the argparse entry point via subprocess to
    verify stdout format and exit code independently of in-process
    test state

These tests write the registry file directly (not via
``append_deferred_findings``) so they control the TTL baseline
exactly. For the retrospectives-count side they write files directly
under ``DYNOS_HOME/projects/<slug>/retrospectives/*.json``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "hooks"))

from check_deferred_findings import check_deferred_findings  # noqa: E402
from lib_core import _persistent_project_dir  # noqa: E402


def _make_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    """Create a project root at ``tmp_path/project`` AND pin a test
    DYNOS_HOME. Returns (root, persistent_dir) — the persistent dir is
    NOT yet materialized so the test can control retrospective count."""
    dynos_home = tmp_path / "dynos-home"
    monkeypatch.setenv("DYNOS_HOME", str(dynos_home))
    root = tmp_path / "project"
    (root / ".dynos").mkdir(parents=True)
    persistent = _persistent_project_dir(root)
    return root, persistent


def _write_registry(root: Path, entries: list[dict]) -> Path:
    """Write ``.dynos/deferred-findings.json`` verbatim."""
    reg = root / ".dynos" / "deferred-findings.json"
    reg.write_text(json.dumps({"findings": entries}))
    return reg


def _set_retro_count(persistent: Path, n: int) -> None:
    """Materialize ``n`` retrospective JSON files so the current-count
    computation returns ``n``. Empty files are fine; the check just
    counts glob matches."""
    retro_dir = persistent / "retrospectives"
    retro_dir.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (retro_dir / f"task-retro-{i:04d}.json").write_text("{}")


def _entry(
    *,
    id: str = "SEC-003",
    category: str = "security",
    task_id: str = "task-20260319-001",
    files: list[str] | None = None,
    first_seen: int = 0,
    ttl: int = 3,
) -> dict:
    return {
        "id": id,
        "category": category,
        "task_id": task_id,
        "files": list(files) if files is not None else ["hooks/lib_core.py"],
        "first_seen_at": "2026-03-19T00:00:00Z",
        "first_seen_at_task_count": first_seen,
        "acknowledged_until_task_count": ttl,
    }


# ---------------------------------------------------------------------------
# (a) missing registry → empty result / exit 0
# ---------------------------------------------------------------------------


def test_missing_registry_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Cold start: no registry file anywhere → helper returns ``[]``.
    The caller (CLI or DONE gate) sees an empty list = clean pass."""
    root, _ = _make_root(tmp_path, monkeypatch)
    expired = check_deferred_findings(root, ["hooks/lib_core.py"])
    assert expired == []


# ---------------------------------------------------------------------------
# (b) no intersection → empty / exit 0
# ---------------------------------------------------------------------------


def test_no_intersection_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A deferred entry exists but its ``files`` do not overlap the
    ``changed_files`` argument. The TTL is already expired — but
    without intersection, the entry is not reported."""
    root, persistent = _make_root(tmp_path, monkeypatch)
    _set_retro_count(persistent, 10)  # TTL would have fired if intersected
    _write_registry(root, [
        _entry(files=["hooks/lib_core.py"], first_seen=0, ttl=3),
    ])
    expired = check_deferred_findings(root, ["some/other/file.py"])
    assert expired == []


# ---------------------------------------------------------------------------
# (c) intersection within TTL → empty / exit 0
# ---------------------------------------------------------------------------


def test_intersection_within_ttl_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Entry.files intersects changed_files, but elapsed < ttl so the
    TTL has not fired yet. Nothing to report."""
    root, persistent = _make_root(tmp_path, monkeypatch)
    _set_retro_count(persistent, 2)  # elapsed = 2 - 0 = 2 < ttl=3
    _write_registry(root, [
        _entry(files=["hooks/lib_core.py"], first_seen=0, ttl=3),
    ])
    expired = check_deferred_findings(root, ["hooks/lib_core.py"])
    assert expired == []


# ---------------------------------------------------------------------------
# (d) intersection AND TTL-expired → entry returned / exit 1 with citation
# ---------------------------------------------------------------------------


def test_intersection_ttl_expired_exits_one_with_citation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The canonical failure: a deferred entry whose files overlap
    the touched changed_files, AND elapsed >= ttl. The helper must
    return the entry; the CLI would print one line + exit 1."""
    root, persistent = _make_root(tmp_path, monkeypatch)
    _set_retro_count(persistent, 3)  # elapsed = 3 - 0 = 3 >= ttl=3
    _write_registry(root, [
        _entry(id="SEC-003", category="security",
               task_id="task-20260319-001",
               files=["hooks/lib_core.py"],
               first_seen=0, ttl=3),
    ])
    expired = check_deferred_findings(root, ["hooks/lib_core.py"])
    assert len(expired) == 1
    entry = expired[0]
    # All source fields preserved.
    assert entry["id"] == "SEC-003"
    assert entry["category"] == "security"
    assert entry["task_id"] == "task-20260319-001"
    assert entry["files"] == ["hooks/lib_core.py"]
    # Augmented `elapsed` field is set to the overshoot.
    assert entry["elapsed"] == 3


def test_boundary_elapsed_equals_ttl_reports_expired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Boundary: ``elapsed >= ttl`` (spec uses ``>=``, not ``>``).
    When elapsed==ttl exactly, the entry is TTL-expired."""
    root, persistent = _make_root(tmp_path, monkeypatch)
    _set_retro_count(persistent, 5)  # elapsed = 5 - 2 = 3 == ttl=3
    _write_registry(root, [
        _entry(files=["hooks/lib_core.py"], first_seen=2, ttl=3),
    ])
    expired = check_deferred_findings(root, ["hooks/lib_core.py"])
    assert len(expired) == 1
    assert expired[0]["elapsed"] == 3


def test_multiple_expired_findings_all_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Three entries; two intersect and are expired; one intersects
    but is within TTL; one does not intersect at all. The result
    must contain exactly the two expired entries, in registry
    order, with correct ids."""
    root, persistent = _make_root(tmp_path, monkeypatch)
    _set_retro_count(persistent, 5)
    _write_registry(root, [
        _entry(id="SEC-003", files=["hooks/lib_core.py"],
               first_seen=0, ttl=3),    # elapsed=5 → expired
        _entry(id="SEC-004", files=["hooks/lib_receipts.py"],
               first_seen=4, ttl=3),    # elapsed=1 < 3 → within TTL
        _entry(id="PERF-002", files=["hooks/rules_engine.py"],
               first_seen=1, ttl=2),    # elapsed=4 → expired
        _entry(id="DOC-001", files=["docs/readme.md"],
               first_seen=0, ttl=3),    # does NOT intersect
    ])
    changed = [
        "hooks/lib_core.py",
        "hooks/lib_receipts.py",
        "hooks/rules_engine.py",
    ]
    expired = check_deferred_findings(root, changed)
    ids = [e["id"] for e in expired]
    assert ids == ["SEC-003", "PERF-002"], (
        f"expected the two expired + intersecting entries; got {ids}"
    )


# ---------------------------------------------------------------------------
# CLI wrapper — subprocess test
# ---------------------------------------------------------------------------


def test_cli_prints_one_line_per_expired_and_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Run the CLI as a real subprocess: it must exit 1, print one
    line per expired entry in the exact format
    ``DEFERRED FINDING EXPIRED: id=... category=... task_id=... files=...``,
    and print nothing else.

    We pass DYNOS_HOME through the env dict so the subprocess sees
    the same persistent-dir slug derivation as the in-process
    helper test above. The CLI expects ``--changed-files`` as a
    nargs="*" space-separated list after ``--root``.
    """
    root, persistent = _make_root(tmp_path, monkeypatch)
    _set_retro_count(persistent, 5)
    _write_registry(root, [
        _entry(id="SEC-003", category="security",
               task_id="task-20260319-001",
               files=["hooks/lib_core.py"],
               first_seen=0, ttl=3),
        _entry(id="PERF-002", category="performance",
               task_id="task-20260319-007",
               files=["hooks/rules_engine.py"],
               first_seen=1, ttl=2),
    ])

    cli = REPO_ROOT / "hooks" / "check_deferred_findings.py"
    env = {
        **os.environ,
        "DYNOS_HOME": str(tmp_path / "dynos-home"),
    }
    proc = subprocess.run(
        [
            sys.executable,
            str(cli),
            "--root", str(root),
            "--changed-files",
            "hooks/lib_core.py",
            "hooks/rules_engine.py",
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 1, (
        f"expected exit 1 on TTL-expired; got {proc.returncode}\n"
        f"stdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
    )
    # Exactly two output lines — one per expired entry.
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert len(lines) == 2, (
        f"expected 2 expired lines; got {len(lines)}:\n{proc.stdout}"
    )
    # Every line must carry the exact header + id/category/task_id/files
    # in the documented order.
    assert all(
        ln.startswith("DEFERRED FINDING EXPIRED:") for ln in lines
    ), f"bad header line format: {lines}"
    sec = next(ln for ln in lines if "id=SEC-003" in ln)
    assert "category=security" in sec
    assert "task_id=task-20260319-001" in sec
    # ``files`` is rendered as a JSON array so downstream parsers get
    # an unambiguous structure.
    assert 'files=["hooks/lib_core.py"]' in sec

    perf = next(ln for ln in lines if "id=PERF-002" in ln)
    assert "category=performance" in perf
    assert "task_id=task-20260319-007" in perf
    assert 'files=["hooks/rules_engine.py"]' in perf


def test_cli_missing_registry_exits_zero_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """CLI cold-start: no registry → exit 0, no stdout. The fail-open
    behavior means missing signal never wedges DONE in CI."""
    root, _ = _make_root(tmp_path, monkeypatch)
    cli = REPO_ROOT / "hooks" / "check_deferred_findings.py"
    env = {
        **os.environ,
        "DYNOS_HOME": str(tmp_path / "dynos-home"),
    }
    proc = subprocess.run(
        [
            sys.executable,
            str(cli),
            "--root", str(root),
            "--changed-files", "hooks/lib_core.py",
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"expected exit 0 on cold start; got {proc.returncode}\n"
        f"stdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
    )
    assert proc.stdout == "", (
        f"expected empty stdout on cold start; got: {proc.stdout!r}"
    )
