"""Tests for task-20260419-002 G4.a: ``append_deferred_findings``.

Covers acceptance criteria 9, 10, and 16 from the task spec:

  - cold-start creation of ``<root>/.dynos/deferred-findings.json``
  - happy-path append with all 7 augmented keys
    (id, category, files, task_id, first_seen_at,
     first_seen_at_task_count, acknowledged_until_task_count)
  - multi-entry single-call append
  - input validation failure modes:
      (a) top-level findings must be a list
      (b) each entry must be a dict
      (c) each entry must carry `id`, `category`, `files`
      (d) `id` / `category` must be non-empty strings
      (e) `files` must be a non-empty list
  - no-partial-write guarantee: first invalid entry raises, file on
    disk is unchanged from its pre-call state

These tests write directly against the function and inspect the
registry file on disk — no mocks of internal logic.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_core import append_deferred_findings  # noqa: E402


REQUIRED_ENTRY_KEYS = frozenset({
    "id",
    "category",
    "files",
    "task_id",
    "first_seen_at",
    "first_seen_at_task_count",
    "acknowledged_until_task_count",
})


def _make_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a clean project root under ``tmp_path/project`` with an
    empty ``.dynos/`` already created. Pins ``DYNOS_HOME`` so that
    ``_persistent_project_dir`` resolves inside the test sandbox."""
    monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "dynos-home"))
    root = tmp_path / "project"
    (root / ".dynos").mkdir(parents=True)
    return root


def _registry_path(root: Path) -> Path:
    return root / ".dynos" / "deferred-findings.json"


def _valid_finding(
    *,
    id: str = "SEC-003",
    category: str = "security",
    files: list[str] | None = None,
) -> dict:
    return {
        "id": id,
        "category": category,
        "files": list(files) if files is not None else ["hooks/lib_core.py"],
    }


# ---------------------------------------------------------------------------
# Cold start
# ---------------------------------------------------------------------------


def test_cold_start_creates_empty_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """When the registry file does not exist, the first append must
    create it. After calling with an empty findings list, the file
    exists and parses as ``{"findings": []}``."""
    root = _make_root(tmp_path, monkeypatch)
    reg = _registry_path(root)
    assert not reg.exists(), "precondition: registry must not exist yet"

    append_deferred_findings(root, "task-20260419-001", [])

    assert reg.exists(), "registry file was not created on cold-start append"
    data = json.loads(reg.read_text())
    assert data == {"findings": []}


# ---------------------------------------------------------------------------
# Happy-path appends (criterion 9 — all 7 augmented keys)
# ---------------------------------------------------------------------------


def test_appends_single_finding_with_augmented_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A single valid finding is appended with ALL seven required
    keys (id/category/files from the input + task_id, first_seen_at,
    first_seen_at_task_count, acknowledged_until_task_count from the
    writer).

    Also verifies that ``acknowledged_until_task_count`` defaults to 3
    (per criterion 9) and that ``first_seen_at_task_count`` is 0 on a
    fresh project (no retrospectives yet).
    """
    root = _make_root(tmp_path, monkeypatch)
    append_deferred_findings(
        root,
        "task-20260419-001",
        [_valid_finding(id="SEC-003", category="security",
                        files=["hooks/lib_core.py"])],
    )
    data = json.loads(_registry_path(root).read_text())
    assert isinstance(data["findings"], list)
    assert len(data["findings"]) == 1

    entry = data["findings"][0]
    # All 7 required keys present.
    missing = REQUIRED_ENTRY_KEYS - set(entry.keys())
    assert not missing, f"missing required keys in entry: {missing}"

    # Input fields preserved verbatim.
    assert entry["id"] == "SEC-003"
    assert entry["category"] == "security"
    assert entry["files"] == ["hooks/lib_core.py"]
    # Augmented fields.
    assert entry["task_id"] == "task-20260419-001"
    assert entry["acknowledged_until_task_count"] == 3
    # No retrospectives yet → baseline is 0.
    assert entry["first_seen_at_task_count"] == 0
    # ISO-8601 timestamp — must be a non-empty string with a Z suffix
    # (now_iso's contract).
    assert isinstance(entry["first_seen_at"], str)
    assert entry["first_seen_at"].endswith("Z")
    assert "T" in entry["first_seen_at"]


def test_appends_multiple_findings_in_one_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Two findings in one call → two registry entries. Both must
    carry the full augmented shape; both must share the same
    ``task_id`` AND the same ``first_seen_at`` snapshot (single
    append call = single timestamp baseline)."""
    root = _make_root(tmp_path, monkeypatch)
    findings = [
        _valid_finding(id="SEC-003", category="security",
                       files=["hooks/lib_core.py"]),
        _valid_finding(id="PERF-002", category="performance",
                       files=["hooks/lib_receipts.py",
                              "hooks/rules_engine.py"]),
    ]
    append_deferred_findings(root, "task-20260419-001", findings)

    data = json.loads(_registry_path(root).read_text())
    assert len(data["findings"]) == 2

    # Both entries share the same task_id + timestamp snapshot.
    ts = {e["first_seen_at"] for e in data["findings"]}
    assert len(ts) == 1, "both entries from one call must share timestamp"
    tids = {e["task_id"] for e in data["findings"]}
    assert tids == {"task-20260419-001"}

    # Input-level fields preserved per-entry.
    ids = [e["id"] for e in data["findings"]]
    assert ids == ["SEC-003", "PERF-002"]
    # Multi-file entry preserves full list.
    perf = next(e for e in data["findings"] if e["id"] == "PERF-002")
    assert perf["files"] == ["hooks/lib_receipts.py", "hooks/rules_engine.py"]


def test_second_append_preserves_existing_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Appending again on an existing non-empty registry must not
    drop or mutate prior entries — the helper is append-only."""
    root = _make_root(tmp_path, monkeypatch)
    append_deferred_findings(
        root, "task-20260419-001",
        [_valid_finding(id="SEC-003", files=["hooks/lib_core.py"])],
    )
    append_deferred_findings(
        root, "task-20260419-002",
        [_valid_finding(id="SEC-004", files=["hooks/lib_receipts.py"])],
    )
    data = json.loads(_registry_path(root).read_text())
    ids = [e["id"] for e in data["findings"]]
    assert ids == ["SEC-003", "SEC-004"], (
        f"expected both entries preserved in append order; got {ids}"
    )
    tids = [e["task_id"] for e in data["findings"]]
    assert tids == ["task-20260419-001", "task-20260419-002"]


# ---------------------------------------------------------------------------
# Input validation (criterion 10)
# ---------------------------------------------------------------------------


def test_rejects_non_list_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Rule (a): findings must be a list. A dict / str / None / tuple
    all fail with the same literal message."""
    root = _make_root(tmp_path, monkeypatch)
    for bad in ({}, "SEC-003", None, (_valid_finding(),), 42):
        with pytest.raises(ValueError) as excinfo:
            append_deferred_findings(
                root, "task-20260419-001", bad,  # type: ignore[arg-type]
            )
        assert "findings must be a list" in str(excinfo.value)


def test_rejects_non_dict_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Rule (b): each entry must be a dict. A string / None / list all
    fail; the message names the offending index."""
    root = _make_root(tmp_path, monkeypatch)
    with pytest.raises(ValueError) as excinfo:
        append_deferred_findings(
            root, "task-20260419-001",
            [_valid_finding(), "not a dict"],  # type: ignore[list-item]
        )
    assert "findings[1] must be a dict" in str(excinfo.value)


def test_rejects_entry_missing_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Rule (c): missing ``id`` key raises with the specific key named
    in the error message."""
    root = _make_root(tmp_path, monkeypatch)
    bad_entry = {"category": "security", "files": ["hooks/lib_core.py"]}
    with pytest.raises(ValueError) as excinfo:
        append_deferred_findings(root, "task-20260419-001", [bad_entry])
    msg = str(excinfo.value)
    assert "findings[0]" in msg
    assert "'id'" in msg or "missing required key: id" in msg, (
        f"expected the key 'id' to be named in the message; got: {msg}"
    )


def test_rejects_entry_missing_category(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Rule (c): missing ``category`` key raises with the specific
    key named in the error message."""
    root = _make_root(tmp_path, monkeypatch)
    bad_entry = {"id": "SEC-003", "files": ["hooks/lib_core.py"]}
    with pytest.raises(ValueError) as excinfo:
        append_deferred_findings(root, "task-20260419-001", [bad_entry])
    msg = str(excinfo.value)
    assert "findings[0]" in msg
    assert "'category'" in msg or "missing required key: category" in msg, (
        f"expected 'category' to be named in the message; got: {msg}"
    )


def test_rejects_entry_with_empty_files_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Rule (e): ``files`` must be a non-empty list. Empty list
    triggers a validator error naming the field."""
    root = _make_root(tmp_path, monkeypatch)
    with pytest.raises(ValueError) as excinfo:
        append_deferred_findings(
            root, "task-20260419-001",
            [{"id": "SEC-003", "category": "security", "files": []}],
        )
    msg = str(excinfo.value)
    assert "findings[0]" in msg
    assert "files" in msg


def test_rejects_entry_with_empty_id_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Non-empty str rule on ``id``: empty string is rejected."""
    root = _make_root(tmp_path, monkeypatch)
    with pytest.raises(ValueError) as excinfo:
        append_deferred_findings(
            root, "task-20260419-001",
            [{"id": "", "category": "security", "files": ["a.py"]}],
        )
    msg = str(excinfo.value)
    assert "findings[0]" in msg
    assert "id" in msg


# ---------------------------------------------------------------------------
# No-partial-write guarantee (criterion 10, final clause)
# ---------------------------------------------------------------------------


def test_invalid_entry_does_not_partial_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A mixed batch with one valid entry followed by one invalid
    entry must raise AND leave the on-disk registry byte-identical to
    its pre-call state. This proves the writer validates the whole
    batch BEFORE touching disk.

    We pre-seed the registry with one prior entry so we have a
    non-trivial byte stream to compare against — a naïve implementation
    that appends the valid entry before discovering the invalid second
    entry would change those bytes.
    """
    root = _make_root(tmp_path, monkeypatch)
    append_deferred_findings(
        root, "task-20260419-000",
        [_valid_finding(id="SEC-001", files=["hooks/lib_core.py"])],
    )
    reg = _registry_path(root)
    snapshot_bytes = reg.read_bytes()
    snapshot_mtime = reg.stat().st_mtime_ns

    findings = [
        _valid_finding(id="SEC-003", files=["hooks/lib_receipts.py"]),
        {"id": "BAD-002", "category": "security"},  # missing `files`
    ]
    with pytest.raises(ValueError):
        append_deferred_findings(root, "task-20260419-001", findings)

    # Registry file contents unchanged, byte-for-byte.
    assert reg.read_bytes() == snapshot_bytes, (
        "registry was modified on validator failure — no-partial-write "
        "guarantee broken"
    )
    # mtime likely unchanged too (atomic write would rename a temp file;
    # if nothing touched disk, mtime stays frozen). We assert byte
    # equality above as the hard contract; mtime is a secondary tell.
    assert reg.stat().st_mtime_ns == snapshot_mtime


def test_malformed_existing_registry_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A pre-existing registry file with corrupt JSON must raise (not
    silently overwrite). Criterion 9/10 explicitly requires that parse
    failure does not drop prior entries."""
    root = _make_root(tmp_path, monkeypatch)
    reg = _registry_path(root)
    reg.write_text("{this is not json")
    with pytest.raises(ValueError) as excinfo:
        append_deferred_findings(
            root, "task-20260419-001",
            [_valid_finding()],
        )
    assert "cannot parse" in str(excinfo.value).lower()
    # And the corrupt file must be untouched (operator manually repairs).
    assert reg.read_text() == "{this is not json"
