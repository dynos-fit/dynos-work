"""Regression tests for the 7.6.0 field-report fixes.

- files_optional: a conditionally regenerated file that came out
  byte-identical no longer fails run-execution-segment-done.
- TDD-covered testing segments are finalized by run-execution-batch-plan
  without an executor spawn.
- next-continuation does not count a stall when the only blocker is missing
  verification evidence, and segment-done surfaces it.
- run-verification-evidence takes its default timeout from project policy.
- build_prompt_context holds its output to a byte budget.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

from test_ctl import _run_ctl, _setup_task_dir  # noqa: E402
from test_next_continuation import _complete_segment, _seed_execution  # noqa: E402

BPC = ROOT / "hooks" / "build_prompt_context.py"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _graph(task_dir: Path) -> dict:
    return json.loads((task_dir / "execution-graph.json").read_text())


def _write_graph(task_dir: Path, graph: dict) -> None:
    (task_dir / "execution-graph.json").write_text(json.dumps(graph, indent=2))


# ---------------------------------------------------------------------------
# Bug 6: files_optional
# ---------------------------------------------------------------------------

def test_normalizer_keeps_files_optional_subset():
    from ctl import _normalize_execution_graph_payload

    payload = {"segments": [{
        "id": "seg-1", "executor": "backend-executor", "description": "d",
        "files_expected": ["src/a.py", "frontend/schema.d.ts"],
        "files_optional": ["frontend/schema.d.ts", "frontend/schema.d.ts"],
        "criteria_ids": [1],
    }]}
    seg = _normalize_execution_graph_payload(Path("/tmp/.dynos/task-x"), payload)["segments"][0]
    assert seg["files_optional"] == ["frontend/schema.d.ts"]


def test_normalizer_rejects_optional_file_outside_files_expected():
    from ctl import _normalize_execution_graph_payload

    payload = {"segments": [{
        "id": "seg-1", "executor": "backend-executor", "description": "d",
        "files_expected": ["src/a.py"], "files_optional": ["other.py"], "criteria_ids": [1],
    }]}
    with pytest.raises(ValueError, match="files_optional"):
        _normalize_execution_graph_payload(Path("/tmp/.dynos/task-x"), payload)


def _git_task(tmp_path: Path, *, optional: list[str]) -> Path:
    """Task in a real git repo: src/a.py changed since snapshot, schema unchanged."""
    task_dir = _setup_task_dir(tmp_path)
    root = tmp_path
    (root / "src").mkdir()
    (root / "frontend").mkdir()
    (root / "src" / "a.py").write_text("x = 1\n")
    (root / "frontend" / "schema.d.ts").write_text("export type A = 1;\n")
    (root / ".gitignore").write_text(".dynos/\n")
    _git(root, "init", "-q")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    head = _git(root, "rev-parse", "HEAD")
    (root / "src" / "a.py").write_text("x = 2\n")

    _seed_execution(task_dir)
    manifest = json.loads((task_dir / "manifest.json").read_text())
    manifest["snapshot"] = {"head_sha": head}
    (task_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    graph = _graph(task_dir)
    seg = graph["segments"][0]
    seg.pop("no_op_justified", None)
    seg.pop("no_op_reason", None)
    seg["files_expected"] = ["src/a.py", "frontend/schema.d.ts"]
    if optional:
        seg["files_optional"] = optional
    _write_graph(task_dir, graph)

    (task_dir / "evidence").mkdir(exist_ok=True)
    (task_dir / "evidence" / "seg-1.md").write_text("seg-1 evidence\n")
    sidecar = task_dir / "receipts" / "_injected-prompts"
    sidecar.mkdir(parents=True, exist_ok=True)
    (sidecar / "seg-1.sha256").write_text("f" * 64)
    return task_dir


def _segment_done(task_dir: Path, home: Path) -> subprocess.CompletedProcess[str]:
    return _run_ctl(
        "run-execution-segment-done", str(task_dir), "seg-1",
        "--injected-prompt-sha256", "f" * 64, "--model", "sonnet",
        "--executor-type", "backend-executor",
        env={"DYNOS_HOME": str(home)},
    )


def test_segment_done_accepts_unchanged_optional_file(tmp_path: Path):
    task_dir = _git_task(tmp_path / "p", optional=["frontend/schema.d.ts"])
    result = _segment_done(task_dir, tmp_path / "home")
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "segment_finalized"
    assert payload["optional_unchanged"] == ["frontend/schema.d.ts"]
    receipt = json.loads((task_dir / "receipts" / "executor-seg-1.json").read_text())
    assert receipt["diff_verified_files"] == ["src/a.py"]


def test_segment_done_still_requires_non_optional_files(tmp_path: Path):
    task_dir = _git_task(tmp_path / "p", optional=[])
    result = _segment_done(task_dir, tmp_path / "home")
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "segment_invalid"
    assert payload["missing_files"] == ["frontend/schema.d.ts"]


def test_segment_done_reports_pending_verification(tmp_path: Path):
    task_dir = _git_task(tmp_path / "p", optional=["frontend/schema.d.ts"])
    graph = _graph(task_dir)
    graph["segments"][0]["verify_commands"] = [{"id": "vc", "command": "true"}]
    _write_graph(task_dir, graph)
    result = _segment_done(task_dir, tmp_path / "home")
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["verification_pending"]
    assert "run-verification-evidence" in payload["next_action"]


def test_evidence_check_skips_absent_optional_file(tmp_path: Path):
    from ctl import _verify_execution_evidence

    task_dir = _setup_task_dir(tmp_path)
    graph = _graph(task_dir)
    graph["segments"] = graph["segments"][:1]
    graph["segments"][0]["files_expected"] = ["src/a.py", "gen/maybe.ts"]
    graph["segments"][0]["files_optional"] = ["gen/maybe.ts"]
    _write_graph(task_dir, graph)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x\n")
    (task_dir / "evidence").mkdir()
    (task_dir / "evidence" / "seg-1.md").write_text("ok\n")
    errors, _ = _verify_execution_evidence(task_dir, set())
    assert errors == []


# ---------------------------------------------------------------------------
# Bug 10: TDD-covered testing segments
# ---------------------------------------------------------------------------

def _tdd_task(tmp_path: Path) -> Path:
    from lib_receipts import hash_file, receipt_tdd_tests

    task_dir = _setup_task_dir(tmp_path)
    _seed_execution(task_dir)
    manifest = json.loads((task_dir / "manifest.json").read_text())
    manifest["classification"]["tdd_required"] = True
    (task_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    _complete_segment(task_dir, "seg-1", "backend-executor", "src/a.py")

    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
    evidence = task_dir / "evidence" / "tdd-tests.md"
    evidence.write_text("# TDD tests\n- tests/test_a.py covers AC 2\n")
    receipt_tdd_tests(
        task_dir=task_dir,
        test_file_paths=["tests/test_a.py"],
        tests_evidence_sha256=hash_file(evidence),
        tokens_used=0,
        model_used="sonnet",
    )
    return task_dir


def test_batch_plan_finalizes_tdd_covered_testing_segment(tmp_path: Path):
    task_dir = _tdd_task(tmp_path)
    result = _run_ctl("run-execution-batch-plan", str(task_dir))
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["tdd_covered_segments"] == ["seg-2"]
    assert "seg-2" in payload["completed_segments"]
    assert payload["next_batch"] == []
    receipt = json.loads((task_dir / "receipts" / "executor-seg-2.json").read_text())
    assert receipt["tdd_covered"] is True
    assert receipt["evidence_path"].endswith("tdd-tests.md")
    assert receipt["diff_verified_files"] == ["tests/test_a.py"]

    cont = json.loads(_run_ctl("next-continuation", str(task_dir)).stdout)
    assert cont["status"] == "complete", cont


def test_batch_plan_does_not_finalize_when_tdd_evidence_drifted(tmp_path: Path):
    task_dir = _tdd_task(tmp_path)
    (task_dir / "evidence" / "tdd-tests.md").write_text("edited after the receipt\n")
    payload = json.loads(_run_ctl("run-execution-batch-plan", str(task_dir)).stdout)
    assert "tdd_covered_segments" not in payload
    assert "seg-2" in payload["pending_segments"]


def test_batch_plan_does_not_finalize_without_tdd_required(tmp_path: Path):
    task_dir = _tdd_task(tmp_path)
    manifest = json.loads((task_dir / "manifest.json").read_text())
    manifest["classification"]["tdd_required"] = False
    (task_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    payload = json.loads(_run_ctl("run-execution-batch-plan", str(task_dir)).stdout)
    assert "seg-2" in payload["pending_segments"]


# ---------------------------------------------------------------------------
# Verification: stall counting and policy timeout
# ---------------------------------------------------------------------------

def test_missing_verification_evidence_never_stalls(tmp_path: Path):
    task_dir = _setup_task_dir(tmp_path)
    _seed_execution(task_dir)
    _complete_segment(task_dir, "seg-1", "backend-executor", "src/a.py")
    _complete_segment(task_dir, "seg-2", "testing-executor", "tests/test_a.py")
    graph = _graph(task_dir)
    graph["segments"][0]["verify_commands"] = [{"id": "e2e", "command": "true"}]
    _write_graph(task_dir, graph)

    for _ in range(4):
        result = _run_ctl("next-continuation", str(task_dir))
        assert result.returncode == 0, result.stdout + result.stderr
        payload = json.loads(result.stdout)
        assert payload["status"] == "verification_needed"
        assert payload["verification_segments"] == ["seg-1"]
        seg = payload["incomplete_segments"][0]
        assert seg["verification_only"] is True
        assert seg["stall_count"] == 0
        assert seg["stalled"] is False

    assert _run_ctl("run-verification-evidence", str(task_dir)).returncode == 0
    done = json.loads(_run_ctl("next-continuation", str(task_dir)).stdout)
    assert done["status"] == "complete"


def test_failing_verification_still_counts_as_stall(tmp_path: Path):
    task_dir = _setup_task_dir(tmp_path)
    _seed_execution(task_dir)
    _complete_segment(task_dir, "seg-1", "backend-executor", "src/a.py")
    _complete_segment(task_dir, "seg-2", "testing-executor", "tests/test_a.py")
    graph = _graph(task_dir)
    graph["segments"][0]["verify_commands"] = [{"id": "e2e", "command": "false"}]
    _write_graph(task_dir, graph)
    _run_ctl("run-verification-evidence", str(task_dir))

    first = json.loads(_run_ctl("next-continuation", str(task_dir)).stdout)
    assert first["status"] == "continuation_needed"
    assert first["incomplete_segments"][0]["verification_only"] is False
    second = json.loads(_run_ctl("next-continuation", str(task_dir)).stdout)
    assert second["incomplete_segments"][0]["stall_count"] == 1


def test_verification_timeout_default_comes_from_policy(tmp_path: Path, monkeypatch):
    import ctl
    import lib_validate

    task_dir = _setup_task_dir(tmp_path)
    monkeypatch.setattr(lib_validate, "load_project_policy", lambda _t: {"verification_timeout_s": 1200})
    assert ctl._policy_verification_timeout(task_dir) == 1200
    monkeypatch.setattr(lib_validate, "load_project_policy", lambda _t: {"verification_timeout_s": 0})
    assert ctl._policy_verification_timeout(task_dir) == 3600
    monkeypatch.setattr(lib_validate, "load_project_policy", lambda _t: {})
    assert ctl._policy_verification_timeout(task_dir) == 3600


# ---------------------------------------------------------------------------
# Bug 9: build_prompt_context byte budget
# ---------------------------------------------------------------------------

def _bpc(cwd: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    merged.pop("DYNOS_PROMPT_CONTEXT_MAX_BYTES", None)
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, str(BPC), "--root", str(cwd), *args],
        cwd=str(cwd), capture_output=True, text=True, timeout=30, env=merged,
    )


def _big_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for name in ("a.py", "b.py", "c.py", "d.py"):
        (repo / name).write_text("".join(f"line_{i} = {i}  # {name}\n" for i in range(1500)))
    (repo / "small.py").write_text("x = 1\n")
    return repo


def test_small_files_are_preloaded_in_full(tmp_path: Path):
    repo = _big_repo(tmp_path)
    out = _bpc(repo, "small.py").stdout
    assert "full=1 excerpt=0 path_only=0" in out
    assert "x = 1" in out


def test_output_stays_within_default_budget(tmp_path: Path):
    repo = _big_repo(tmp_path)
    out = _bpc(repo, "a.py", "b.py", "c.py", "d.py", "small.py").stdout
    assert len(out.encode()) < 52_000  # 48 KB budget plus header
    assert "max_bytes=48000" in out
    assert "(excerpt: first" in out


def test_tiny_budget_degrades_to_path_listing(tmp_path: Path):
    repo = _big_repo(tmp_path)
    out = _bpc(repo, "--max-bytes", "3000", "a.py", "b.py", "c.py").stdout
    assert "Not pre-loaded (byte budget reached" in out
    assert "- `c.py`" in out
    assert "path_only=" in out and "path_only=0" not in out


def test_budget_env_override_and_sidecar_records_budget(tmp_path: Path):
    repo = _big_repo(tmp_path)
    sidecar = tmp_path / "ctx.md"
    result = _bpc(repo, "--sidecar", str(sidecar), "a.py",
                  env={"DYNOS_PROMPT_CONTEXT_MAX_BYTES": "5000"})
    assert result.returncode == 0, result.stderr
    text = sidecar.read_text()
    assert "Prompt-context budget: max_bytes=5000" in text
