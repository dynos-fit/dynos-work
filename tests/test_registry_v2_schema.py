"""Tests for hooks/registry.py v2 schema — RED state.

These tests verify v1→v2 migration, write_version vs schema_version,
checksum coverage, round-trip v2 shape, and path-validation gate.

Imports from lib_project_id will fail until production code is written —
that is the expected RED state for the registry tests that depend on it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# registry.py already exists — import it.
import registry  # hooks/registry.py

# lib_project_id is new and does not exist yet.
from lib_project_id import ProjectIdSecurityError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_v1_registry(path: Path, version: int = 3) -> dict:
    """Write a minimal v1 registry to *path* and return the dict."""
    data = {
        "version": version,
        "projects": [
            {
                "path": "/Users/hassam/work/foo",
                "registered_at": "2026-01-01T00:00:00Z",
                "last_active_at": "2026-01-02T00:00:00Z",
                "status": "active",
            }
        ],
    }
    # No checksum so we write raw; tests that need a valid checksum will use
    # save_registry.
    path.write_text(json.dumps(data), encoding="utf-8")
    return data


def _set_registry_path(monkeypatch, tmp_path: Path) -> Path:
    """Redirect registry._registry_path() to a file under tmp_path."""
    reg_file = tmp_path / "registry.json"
    monkeypatch.setattr(registry, "_registry_path", lambda: reg_file)
    return reg_file


# ---------------------------------------------------------------------------
# AC 21 — v1 file (no schema_version) loaded without error, treated as v1
# ---------------------------------------------------------------------------


def test_registry_v1_read_path_treats_missing_schema_version_as_1(
    tmp_path: Path, monkeypatch
):
    """AC 21 — load_registry on a v1 JSON blob treats absence of schema_version as 1."""
    reg_file = _set_registry_path(monkeypatch, tmp_path)
    _write_v1_registry(reg_file)
    data = registry.load_registry()
    # Either the key is absent (still treated as 1 by all consumers) or it is
    # explicitly set to 1 in-memory by load_registry.
    schema_v = data.get("schema_version", 1)
    assert schema_v == 1, (
        f"Expected schema_version=1 for v1 file, got {schema_v}"
    )


# ---------------------------------------------------------------------------
# AC 22 — v1→v2 migration preserves write_version counter
# ---------------------------------------------------------------------------


def test_registry_v1_to_v2_preserves_write_version_counter(
    tmp_path: Path, monkeypatch
):
    """AC 22 — migrating a v1 registry to v2 retains the original 'version' counter
    value in the new 'write_version' field.
    """
    reg_file = _set_registry_path(monkeypatch, tmp_path)
    _write_v1_registry(reg_file, version=7)
    data = registry.load_registry()
    # After in-memory migration, write_version must be the old counter (7).
    assert data.get("write_version") == 7, (
        f"Expected write_version=7, got {data.get('write_version')}"
    )
    # schema_version must be set to 2.
    assert data.get("schema_version") == 2, (
        f"Expected schema_version=2 after migration, got {data.get('schema_version')}"
    )


def test_registry_v2_has_no_legacy_version_key(tmp_path: Path, monkeypatch):
    """AC 22 — in a freshly created (v2) registry, there is no 'version' key, only 'write_version'."""
    reg_file = _set_registry_path(monkeypatch, tmp_path)
    # Trigger creation via save_registry on an empty registry.
    empty = registry._empty_registry()  # type: ignore[attr-defined]
    assert "version" not in empty, (
        "'version' key must not be present in v2 _empty_registry()"
    )
    assert "write_version" in empty, "'write_version' key missing from _empty_registry()"
    assert empty.get("schema_version") == 2, "schema_version must be 2 in _empty_registry()"


# ---------------------------------------------------------------------------
# AC 23 — checksum covers both write_version and schema_version
# ---------------------------------------------------------------------------


def test_registry_v2_checksum_covers_both_keys(tmp_path: Path, monkeypatch):
    """AC 23 — _compute_checksum covers write_version and schema_version; mutating
    either key produces a different checksum.
    """
    base = {
        "write_version": 1,
        "schema_version": 2,
        "projects": [],
        "checksum": "",
    }
    original_checksum = registry._compute_checksum(base)  # type: ignore[attr-defined]

    # Mutate write_version — checksum must change.
    mutated_wv = dict(base, write_version=99)
    assert registry._compute_checksum(mutated_wv) != original_checksum, (  # type: ignore[attr-defined]
        "checksum did not change when write_version was mutated"
    )

    # Mutate schema_version — checksum must change.
    mutated_sv = dict(base, schema_version=9)
    assert registry._compute_checksum(mutated_sv) != original_checksum, (  # type: ignore[attr-defined]
        "checksum did not change when schema_version was mutated"
    )


# ---------------------------------------------------------------------------
# AC 24 — v2 entries use id+paths[] shape; register_project merges paths
# ---------------------------------------------------------------------------


def test_registry_round_trip_v2(tmp_path: Path, monkeypatch):
    """AC 24 — registering the same git root twice produces one entry with one paths[] item.

    This test requires a git repo because register_project calls resolve_project_id.
    """
    import subprocess

    # Create a minimal git repo.
    repo = tmp_path / "myrepo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "t@t.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "T"],
        check=True,
        capture_output=True,
    )

    reg_file = _set_registry_path(monkeypatch, tmp_path)

    registry.register_project(repo)
    registry.register_project(repo)  # second call must merge, not append

    data = registry.load_registry()
    projects = data["projects"]
    assert len(projects) == 1, (
        f"Expected 1 project entry, got {len(projects)}"
    )
    entry = projects[0]
    # v2 shape: must have 'id' and 'paths' keys.
    assert "id" in entry, "v2 entry missing 'id' key"
    assert "paths" in entry, "v2 entry missing 'paths' key"
    # Only one paths item for the same root.
    assert len(entry["paths"]) == 1, (
        f"Expected 1 path entry after duplicate registration, got {len(entry['paths'])}"
    )


# ---------------------------------------------------------------------------
# AC 25 — migration rejects registry paths outside HOME and unowned paths
# ---------------------------------------------------------------------------


def test_migration_rejects_registry_paths_outside_home(tmp_path: Path):
    """AC 25 / T-7 — _assert_safe_registry_path raises ValueError for a path
    that resolves outside Path.home().
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    # tmp_path is typically /tmp/... which is outside HOME.
    with pytest.raises((ValueError, ProjectIdSecurityError)):
        registry._assert_safe_registry_path(outside)  # type: ignore[attr-defined]


def test_migration_rejects_unowned_paths(tmp_path: Path, monkeypatch):
    """AC 25 / T-7 — _assert_safe_registry_path raises ValueError for a path
    whose st_uid does not match os.geteuid() (simulated via monkeypatch on os.stat).
    """
    # Build a path that resolves inside HOME so the HOME check passes.
    inside_home = Path.home() / ".dynos" / "projects" / "some-slug"
    inside_home.mkdir(parents=True, exist_ok=True)

    real_stat = os.stat(str(inside_home))

    class _FakeStat:
        st_uid = real_stat.st_uid + 9999  # definitely not the current user
        st_gid = real_stat.st_gid
        st_mode = real_stat.st_mode
        st_size = real_stat.st_size

    monkeypatch.setattr(os, "stat", lambda path: _FakeStat())

    with pytest.raises((ValueError, ProjectIdSecurityError)):
        registry._assert_safe_registry_path(inside_home)  # type: ignore[attr-defined]

    # Cleanup.
    try:
        inside_home.rmdir()
    except Exception:
        pass
