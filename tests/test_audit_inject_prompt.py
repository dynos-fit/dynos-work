"""Tests for `python3 hooks/router.py audit-inject-prompt` (AC 8)."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "hooks" / "router.py"


def _setup(tmp_path: Path, *, route_mode: str, agent_path: str | None,
           agent_content: str = "Be careful.",
           plan_extra: dict | None = None) -> tuple[Path, Path]:
    project = tmp_path / "project"
    td = project / ".dynos" / "task-20260418-X"
    td.mkdir(parents=True)
    # Create the agent file if specified
    if agent_path:
        ap = project / agent_path
        ap.parent.mkdir(parents=True, exist_ok=True)
        ap.write_text(agent_content)
    plan = {
        "auditors": [
            {
                "name": "security-auditor",
                "route_mode": route_mode,
                "agent_path": agent_path,
            }
        ]
    }
    if plan_extra:
        plan.update(plan_extra)
    plan_path = td / "audit-plan.json"
    plan_path.write_text(json.dumps(plan))
    return project, plan_path


def _run(project: Path, plan_path: Path, *, model: str | None = None,
         auditor: str = "security-auditor",
         stdin: str = "base prompt body") -> subprocess.CompletedProcess:
    args = [
        sys.executable, str(ROUTER), "audit-inject-prompt",
        "--root", str(project),
        "--task-type", "feature",
        "--audit-plan", str(plan_path),
        "--auditor-name", auditor,
    ]
    if model is not None:
        args.extend(["--model", model])
    env = {**os.environ, "PYTHONPATH": str(ROOT / "hooks")}
    return subprocess.run(args, input=stdin, text=True,
                          capture_output=True, check=False, env=env,
                          cwd=str(ROOT))


def _sidecar_dir(plan_path: Path) -> Path:
    return plan_path.parent / "receipts" / "_injected-auditor-prompts"


def test_replace_mode_sidecar_matches_stdout(tmp_path: Path):
    project, plan_path = _setup(tmp_path, route_mode="replace",
                                agent_path="learned/auditor.md",
                                agent_content="Hard rule: deny all.")
    r = _run(project, plan_path, model="haiku")
    assert r.returncode == 0, r.stderr
    sha_path = _sidecar_dir(plan_path) / "security-auditor-haiku.sha256"
    assert sha_path.exists()
    on_disk = sha_path.read_text().strip()
    expected = hashlib.sha256(r.stdout.encode("utf-8")).hexdigest()
    assert on_disk == expected
    # The injected heading must appear in stdout
    assert "## Ruthless Audit Brief" in r.stdout
    assert "## Learned Auditor Instructions" in r.stdout
    assert "Hard rule: deny all." in r.stdout


def test_alongside_mode_sidecar_matches_stdout(tmp_path: Path):
    project, plan_path = _setup(tmp_path, route_mode="alongside",
                                agent_path="learned/aud2.md",
                                agent_content="Suggestion: deny most.")
    r = _run(project, plan_path, model="sonnet")
    assert r.returncode == 0, r.stderr
    sha_path = _sidecar_dir(plan_path) / "security-auditor-sonnet.sha256"
    on_disk = sha_path.read_text().strip()
    expected = hashlib.sha256(r.stdout.encode("utf-8")).hexdigest()
    assert on_disk == expected
    assert "## Ruthless Audit Brief" in r.stdout
    assert "## Learned Auditor Instructions" in r.stdout


def test_generic_mode_no_injection_but_sidecar_still_written(tmp_path: Path):
    project, plan_path = _setup(tmp_path, route_mode="generic",
                                agent_path=None)
    r = _run(project, plan_path, model="opus")
    assert r.returncode == 0, r.stderr
    sha_path = _sidecar_dir(plan_path) / "security-auditor-opus.sha256"
    assert sha_path.exists()
    on_disk = sha_path.read_text().strip()
    expected = hashlib.sha256(r.stdout.encode("utf-8")).hexdigest()
    assert on_disk == expected
    assert "## Ruthless Audit Brief" in r.stdout
    assert "## Learned Auditor Instructions" not in r.stdout


def test_ruthless_brief_includes_diff_scope_and_process_artifacts(tmp_path: Path):
    project, plan_path = _setup(
        tmp_path,
        route_mode="generic",
        agent_path=None,
        plan_extra={
            "diff_base": "abc123",
            "diff_files": ["src/api.py", "tests/test_api.py"],
            "diff_loc": 42,
        },
    )
    (project / "src").mkdir()
    (project / "src" / "api.py").write_text("def route():\n    return 1\n")
    (project / "tests").mkdir()
    (project / "tests" / "test_api.py").write_text("def test_route():\n    assert True\n")
    task_dir = plan_path.parent
    (task_dir / "manifest.json").write_text(json.dumps({
        "classification": {
            "type": "feature",
            "domains": ["backend", "testing"],
            "risk_level": "high",
        }
    }))
    (task_dir / "raw-input.md").write_text("Build a safer route.")
    (task_dir / "spec.md").write_text("Spec")
    (task_dir / "plan.md").write_text("Plan")
    (task_dir / "evidence").mkdir()
    (task_dir / "evidence" / "segment-1.md").write_text("Evidence")

    r = _run(project, plan_path, model="opus")

    assert r.returncode == 0, r.stderr
    assert "## Ruthless Audit Brief" in r.stdout
    assert "Default posture: distrust self-reported DONE/PASS" in r.stdout
    assert "- diff_base: abc123; changed_files: 2; diff_loc: 42" in r.stdout
    assert "- src/api.py (2 lines)" in r.stdout
    assert "- tests/test_api.py (2 lines)" in r.stdout
    assert "- spec.md" in r.stdout
    assert "- plan.md" in r.stdout
    assert "- evidence/* (1 files)" in r.stdout
    assert "Process-integrity checks:" in r.stdout
    sha_path = _sidecar_dir(plan_path) / "security-auditor-opus.sha256"
    assert sha_path.read_text().strip() == hashlib.sha256(r.stdout.encode("utf-8")).hexdigest()


def test_per_model_disambiguation(tmp_path: Path):
    project, plan_path = _setup(tmp_path, route_mode="generic",
                                agent_path=None)
    r1 = _run(project, plan_path, model="haiku")
    r2 = _run(project, plan_path, model=None)
    assert r1.returncode == 0
    assert r2.returncode == 0
    sd = _sidecar_dir(plan_path)
    assert (sd / "security-auditor-haiku.sha256").exists()
    assert (sd / "security-auditor-default.sha256").exists()


def test_unknown_auditor_exits_one(tmp_path: Path):
    project, plan_path = _setup(tmp_path, route_mode="generic", agent_path=None)
    r = _run(project, plan_path, auditor="ghost-auditor")
    assert r.returncode == 1


@pytest.mark.parametrize("model,budget,checkpoint", [
    ("haiku", 15, 5),
    ("sonnet", 20, 7),
    ("opus", 25, 9),
])
def test_turn_budget_block_injected_for_every_model(tmp_path: Path, model, budget, checkpoint):
    """Every auditor spawn — regardless of agent file — must receive the
    deterministic write-first turn-budget block. Regression for auditors that
    ran out of turns before writing any report."""
    project, plan_path = _setup(tmp_path, route_mode="generic", agent_path=None)
    r = _run(project, plan_path, model=model)
    assert r.returncode == 0, r.stderr
    assert "## Turn Budget Discipline (mandatory — injected)" in r.stdout
    assert f"about **{budget}** tool calls" in r.stdout
    assert f"By tool call {checkpoint} the file must already hold real content" in r.stdout
    # It must be the trailing (most salient) instruction the auditor reads.
    assert r.stdout.rstrip().endswith(
        "A truncated report that is written always beats running out of turns "
        "with nothing on disk."
    )


def test_turn_budget_block_present_when_model_unset(tmp_path: Path):
    """Null/default model falls back to the balanced floor, never lower."""
    project, plan_path = _setup(tmp_path, route_mode="generic", agent_path=None)
    r = _run(project, plan_path, model=None)
    assert r.returncode == 0, r.stderr
    assert "about **20** tool calls" in r.stdout
