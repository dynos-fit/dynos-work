"""Regression tests for registry corruption handling.

Covers the data-loss bug where load_registry() returned an empty in-memory
registry on checksum mismatch and the next register_project() call wrote
that empty state back to disk, wiping every previously-registered project.

The fix quarantines the corrupt file (renames to .corrupt-{ts}) before
returning the empty registry, so the original data is preserved on disk
and recoverable.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))


def _seed_corrupt_registry(home: Path, projects: list[str]) -> Path:
    """Write a registry with a deliberately wrong checksum."""
    home.mkdir(parents=True, exist_ok=True)
    reg = {
        "version": 7,
        "projects": [
            {"path": p, "registered_at": "2026-04-01T00:00:00Z",
             "last_active_at": "2026-04-01T00:00:00Z", "status": "active"}
            for p in projects
        ],
        "checksum": "0" * 64,
    }
    path = home / "registry.json"
    path.write_text(json.dumps(reg))
    return path


class TestQuarantineOnChecksumMismatch:
    def test_load_registry_quarantines_corrupt_file(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("DYNOS_HOME", str(tmp_path))
        registry_path = _seed_corrupt_registry(
            tmp_path, ["/old/project-a", "/old/project-b"]
        )

        from registry import load_registry
        with mock.patch("registry.log_global"):
            reg = load_registry()

        assert reg["projects"] == [], "should return empty registry"
        assert not registry_path.exists(), "original corrupt file should be moved"
        quarantines = list(tmp_path.glob("registry.json.corrupt-*"))
        assert len(quarantines) == 1, f"expected 1 quarantine file, got {quarantines}"
        preserved = json.loads(quarantines[0].read_text())
        preserved_paths = {p["path"] for p in preserved.get("projects", [])}
        assert preserved_paths == {"/old/project-a", "/old/project-b"}, \
            "quarantine must preserve the original project list"

    def test_register_after_corruption_does_not_wipe_original_data(
        self, tmp_path: Path, monkeypatch
    ):
        """The data-loss regression: register after a checksum mismatch must
        NOT silently discard the original project list. The original data
        must be recoverable from the quarantine file even though the live
        registry is now a single-entry blob.
        """
        monkeypatch.setenv("DYNOS_HOME", str(tmp_path))
        _seed_corrupt_registry(
            tmp_path, ["/old/project-a", "/old/project-b", "/old/project-c"]
        )
        new_project = tmp_path / "new-project"
        new_project.mkdir()

        from registry import register_project
        with mock.patch("registry.log_global"):
            register_project(new_project)

        live = json.loads((tmp_path / "registry.json").read_text())
        live_paths = {p["path"] for p in live["projects"]}
        assert live_paths == {str(new_project)}, \
            "live registry should contain only the freshly registered project"

        quarantines = sorted(tmp_path.glob("registry.json.corrupt-*"))
        assert len(quarantines) == 1, "exactly one quarantine file expected"
        preserved = json.loads(quarantines[0].read_text())
        preserved_paths = {p["path"] for p in preserved["projects"]}
        assert preserved_paths == {
            "/old/project-a", "/old/project-b", "/old/project-c"
        }, "quarantine must hold all 3 original projects (data loss otherwise)"

    def test_clean_registry_is_unmodified(self, tmp_path: Path, monkeypatch):
        """A registry with a valid checksum must not be quarantined."""
        monkeypatch.setenv("DYNOS_HOME", str(tmp_path))
        from registry import _compute_checksum
        reg = {
            "version": 1,
            "projects": [{"path": "/x", "status": "active",
                         "registered_at": "2026-04-01T00:00:00Z",
                         "last_active_at": "2026-04-01T00:00:00Z"}],
        }
        reg["checksum"] = _compute_checksum(reg)
        (tmp_path / "registry.json").write_text(json.dumps(reg))

        from registry import load_registry
        loaded = load_registry()
        assert loaded["projects"] == reg["projects"]
        assert list(tmp_path.glob("registry.json.corrupt-*")) == [], \
            "clean registry must not produce a quarantine file"

    def test_quarantine_failure_raises_rather_than_wipes(
        self, tmp_path: Path, monkeypatch
    ):
        """If the corrupt file cannot be moved (e.g. permission error), refuse
        to operate rather than silently allowing the next mutation to overwrite
        it. This is the safer failure mode."""
        monkeypatch.setenv("DYNOS_HOME", str(tmp_path))
        _seed_corrupt_registry(tmp_path, ["/preserved"])

        from registry import RegistryCorruptError, load_registry
        with mock.patch("pathlib.Path.rename", side_effect=OSError("EACCES")), \
             mock.patch("registry.log_global"):
            with pytest.raises(RegistryCorruptError):
                load_registry()
        assert (tmp_path / "registry.json").exists(), \
            "corrupt file must remain on disk if quarantine fails"
