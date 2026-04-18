"""Shared pytest fixtures for the dynos-work test suite."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def dynos_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    root = tmp_path / "project"
    root.mkdir()
    (root / ".dynos").mkdir()

    home = tmp_path / "dynos-home"
    home.mkdir()
    monkeypatch.setenv("DYNOS_HOME", str(home))

    slug = str(root.resolve()).strip("/").replace("/", "-")
    persistent = home / "projects" / slug

    return SimpleNamespace(root=root, dynos_home=home, persistent_dir=persistent)


def _run_py(env: SimpleNamespace, script: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(ROOT / "hooks" / script), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "DYNOS_HOME": str(env.dynos_home)},
    )
