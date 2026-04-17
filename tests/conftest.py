"""Shared fixtures for the test suite."""

from __future__ import annotations

import pytest
from pathlib import Path


@pytest.fixture
def dynos_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set DYNOS_HOME to a temporary directory and return it."""
    home = tmp_path / ".dynos-home"
    home.mkdir()
    monkeypatch.setenv("DYNOS_HOME", str(home))
    return home
