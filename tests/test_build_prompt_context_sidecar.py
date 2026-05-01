"""TDD-first tests for build_prompt_context --sidecar mode (task-20260430-012).

The 2026-04-24 commit CG-013 added build_prompt_context to pre-load file
contents into auditor base prompts. Per the latency investigation
(/tmp/bug_report.json recommendation #3), this commit is mislabeled
"perf:" — it replaced cheap, parallel, model-side Read calls with a
150K-char stdout blob that gets injected into EVERY auditor's base
prompt. With three mandatory auditors and a haiku→sonnet→opus cascade,
the same context is billed as input tokens up to 9 times per audit.

This test suite drives the --sidecar flag: instead of streaming the
context to stdout (where the orchestrator captures it into $DIFF_CONTEXT
and appends to every auditor prompt), --sidecar PATH writes the context
to a file. The orchestrator's auditor prompts then reference that path
("read .dynos/task-{id}/audit-context.md once at the start") and use
the model's Read tool. Read calls are cheap and parallel; prompt input
tokens are billed per call.

Cost shape change:
  Before: N_auditors × M_cascade_models × len(context) input tokens
  After:  1 sidecar write + per-auditor Read of the same content
          (Read costs are amortized; sidecar reference in prompt is small)

This test does NOT remove or change stdout mode — that remains the
default for backward compatibility. --sidecar is opt-in; the audit
skill prose chooses to use it.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "hooks" / "build_prompt_context.py"


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=30,
    )


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "foo.py").write_text("def hello():\n    return 'hi'\n")
    (repo / "bar.md").write_text("# Bar\n\nSome doc.\n")
    return repo


def test_sidecar_flag_writes_file_and_emits_minimal_stdout(tmp_path: Path):
    repo = _make_repo(tmp_path)
    sidecar = tmp_path / "ctx.md"
    proc = _run(["--root", str(repo), "--sidecar", str(sidecar), "foo.py", "bar.md"], cwd=repo)
    assert proc.returncode == 0, f"non-zero exit: rc={proc.returncode} stderr={proc.stderr}"
    assert sidecar.is_file(), "sidecar file must be created"
    content = sidecar.read_text()
    assert "foo.py" in content and "def hello" in content
    assert "bar.md" in content and "# Bar" in content
    # Stdout must NOT contain the full content (that would defeat the
    # whole point of the sidecar — orchestrator should reference the
    # path, not pipe content). A short pointer line is acceptable.
    assert len(proc.stdout) < 1000, (
        f"stdout should be minimal in sidecar mode; got {len(proc.stdout)} chars"
    )
    # The pointer should reference the sidecar path so the caller can
    # find it programmatically.
    assert str(sidecar) in proc.stdout or sidecar.name in proc.stdout


def test_default_mode_unchanged_streams_to_stdout(tmp_path: Path):
    repo = _make_repo(tmp_path)
    proc = _run(["--root", str(repo), "foo.py", "bar.md"], cwd=repo)
    assert proc.returncode == 0
    assert "foo.py" in proc.stdout
    assert "def hello" in proc.stdout


def test_sidecar_content_matches_stdout_content(tmp_path: Path):
    """The sidecar file content must equal what the default mode emits
    to stdout — same content, different transport. This protects against
    the sidecar mode silently dropping characters or formatting."""
    repo = _make_repo(tmp_path)
    sidecar = tmp_path / "ctx2.md"

    # Sidecar mode
    _run(["--root", str(repo), "--sidecar", str(sidecar), "foo.py", "bar.md"], cwd=repo)
    sidecar_text = sidecar.read_text()

    # Default mode
    default_proc = _run(["--root", str(repo), "foo.py", "bar.md"], cwd=repo)
    default_text = default_proc.stdout

    assert sidecar_text == default_text, (
        "sidecar content must equal stdout-mode content; "
        f"sidecar={len(sidecar_text)} chars, stdout={len(default_text)} chars"
    )


def test_sidecar_creates_parent_directory_if_missing(tmp_path: Path):
    repo = _make_repo(tmp_path)
    sidecar = tmp_path / "nested" / "deeper" / "ctx.md"
    proc = _run(["--root", str(repo), "--sidecar", str(sidecar), "foo.py"], cwd=repo)
    assert proc.returncode == 0
    assert sidecar.is_file()
