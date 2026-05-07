"""Shared pytest fixtures for the dynos-work test suite."""

from __future__ import annotations

import builtins
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]

# Ensure memory and hooks are importable for all tests.
_MEMORY_DIR = str(ROOT / "memory")
_HOOKS_DIR = str(ROOT / "hooks")
if _MEMORY_DIR not in sys.path:
    sys.path.insert(0, _MEMORY_DIR)
if _HOOKS_DIR not in sys.path:
    sys.path.insert(0, _HOOKS_DIR)


@pytest.fixture(autouse=True, scope="session")
def _inject_postmortem_symbols():
    """Inject postmortem_analysis symbols into builtins.

    Some TDD-committed tests in test_postmortem_analysis_apply.py call
    apply_analysis without a local import statement. This session fixture
    injects the symbol once into builtins so all test functions can resolve
    it via the built-in fallback lookup, without modifying the committed
    test files.
    """
    try:
        from postmortem_analysis import apply_analysis as _apply_analysis  # noqa: PLC0415
        _sentinel = object()
        old = getattr(builtins, "apply_analysis", _sentinel)
        builtins.apply_analysis = _apply_analysis  # type: ignore[attr-defined]
        yield
        if old is _sentinel:
            try:
                delattr(builtins, "apply_analysis")
            except AttributeError:
                pass
        else:
            builtins.apply_analysis = old  # type: ignore[attr-defined]
    except ImportError:
        yield


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
