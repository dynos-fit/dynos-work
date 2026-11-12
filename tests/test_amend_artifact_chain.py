"""Regression: `ctl amend-artifact` must keep the task receipt chain valid.

Field report (7.7.1): amending the plan after PLAN_REVIEW rewrote
receipts/plan-validated.json in place with os.replace and never appended
a chain entry. validate_chain compares each path's live bytes against its
NEWEST entry, so DONE later refused with ``content_mismatch`` on
plan-validated.json. Every receipt the amend path rewrites must be chained.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "hooks"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_amend_plan_validated_drift import _make_task_with_full_planning_state  # noqa: E402


def _chain_everything(task_dir: Path) -> None:
    from lib_chain import extend_chain_for_artifact, extend_chain_for_receipt

    for name in ("spec.md", "plan.md", "execution-graph.json"):
        extend_chain_for_artifact(task_dir, task_dir / name)
    for receipt in sorted((task_dir / "receipts").glob("*.json")):
        extend_chain_for_receipt(task_dir, receipt.stem, receipt)


@pytest.mark.parametrize("artifact", ["plan", "spec"])
def test_amend_artifact_leaves_chain_valid(tmp_path: Path, artifact: str):
    from ctl import cmd_amend_artifact
    from lib_chain import validate_chain

    task_dir, *_ = _make_task_with_full_planning_state(tmp_path)
    _chain_everything(task_dir)
    assert validate_chain(task_dir).status == "valid"

    target = task_dir / f"{artifact}.md"
    target.write_text(target.read_text() + "\nAMENDED after approval\n")

    rc = cmd_amend_artifact(argparse.Namespace(
        task_dir=str(task_dir), artifact_name=artifact, reason="field-report regression",
    ))
    assert rc == 0

    result = validate_chain(task_dir)
    assert result.status == "valid", (
        f"chain broken after amend-artifact {artifact}: {result.status} "
        f"at {result.first_failed_index} ({result.error_reason})"
    )
