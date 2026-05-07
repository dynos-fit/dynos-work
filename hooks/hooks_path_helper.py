"""Thin helper exposing persistent-dir resolution for tests and external callers.

This module wraps lib_core._persistent_project_dir under a stable public name
so test code that needs the path can import it without depending on lib_core
internals.
"""
from __future__ import annotations

from pathlib import Path

from lib_core import _persistent_project_dir


def get_persistent_dir(root: Path) -> Path:
    """Return the persistent project directory for *root*.

    Equivalent to lib_core._persistent_project_dir(root). This public wrapper
    exists so test helpers can import a stable symbol without depending on
    private lib_core internals.
    """
    return _persistent_project_dir(Path(root))
