"""The suite must never write into the developer's real ~/.dynos."""

from __future__ import annotations

import os
from pathlib import Path


def test_dynos_home_is_isolated_by_default():
    home = os.environ.get("DYNOS_HOME")
    assert home, "conftest must set DYNOS_HOME for every test"
    assert Path(home).resolve() != (Path.home() / ".dynos").resolve()


def test_persistent_project_dir_lands_in_isolated_home(tmp_path: Path):
    from lib_core import _persistent_project_dir

    project = tmp_path / "project"
    (project / ".dynos").mkdir(parents=True)
    persistent = _persistent_project_dir(project)
    assert Path(os.environ["DYNOS_HOME"]).resolve() in persistent.resolve().parents
