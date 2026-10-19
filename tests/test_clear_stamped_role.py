"""Unit tests for lib_core.clear_stamped_role."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from lib_core import clear_stamped_role, stamp_role  # noqa: E402


def test_clears_matching_role(tmp_path):
    stamp_role(tmp_path, "planning")
    assert clear_stamped_role(tmp_path, "planning") is True
    assert not (tmp_path / "active-segment-role").exists()


def test_strips_whitespace_before_compare(tmp_path):
    (tmp_path / "active-segment-role").write_text("planning\n")
    assert clear_stamped_role(tmp_path, "planning") is True


def test_other_role_untouched(tmp_path):
    stamp_role(tmp_path, "backend-executor")
    assert clear_stamped_role(tmp_path, "planning") is False
    assert (tmp_path / "active-segment-role").read_text() == "backend-executor"


def test_missing_file_returns_false(tmp_path):
    assert clear_stamped_role(tmp_path, "planning") is False
