"""TDD-first test for PRO-006 inject-prompt size guardrail.

AC covered: 36.

This test will FAIL with AssertionError until segment-E-router inserts the
size-check block in cmd_planner_inject_prompt (no planner_prompt_oversize event
is emitted by the current production code). That is the expected TDD-first state.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

ROOT = Path(__file__).resolve().parent.parent

# 100,001 bytes — just over the 100,000-byte cap defined in _MAX_INJECTED_PROMPT_BYTES
_OVERSIZE_BYTES = b"x" * 100_001


def test_planner_inject_prompt_over_60kb_is_refused(tmp_path: Path, capsys):
    """Planner prompts above the enforced cap fail before sidecar creation."""
    from router import cmd_planner_inject_prompt
    task=tmp_path/".dynos"/"task-test-001"; task.mkdir(parents=True)
    args=argparse.Namespace(task_id=task.name,phase="discovery",root=str(tmp_path))
    fake_buffer=mock.MagicMock(); fake_buffer.read.return_value=b"x"*60_001
    fake_stdin=mock.MagicMock(); fake_stdin.buffer=fake_buffer
    with mock.patch("sys.stdin",fake_stdin):
        assert cmd_planner_inject_prompt(args)==1
    assert "maximum is 60000" in capsys.readouterr().err
    assert not (task/"receipts"/"_injected-planner-prompts"/"discovery.sha256").exists()
