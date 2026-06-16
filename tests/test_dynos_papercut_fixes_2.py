"""Regression tests for three more dynos-work pipeline papercuts:

  A. ``hooks/eventbus.py:run_register`` debounces the registry
     ``set-active`` call when the last_active_at timestamp is recent
     (default 60s). The /dynos-work:start skill calls
     ``registry.py register`` at task init (which itself updates
     last_active_at), so the task-completed handler firing seconds
     later was overwriting a fresh timestamp with another fresh
     timestamp — pure I/O waste. Mirrors the existing
     ``_DASHBOARD_DEBOUNCE_SECONDS`` pattern.

  B. ``memory/postmortem_analysis.py:_validate_rule_schema`` defaults
     a missing ``template`` field to ``"advisory"`` when the rule
     otherwise has the expected shape (non-empty `rule` text). Every
     postmortem during PRO-001/005/007 had 4-8 rules rejected as
     "missing template field" because the LLM output didn't include
     the field. The advisory template has no required params and is
     always permitted; this stops zero-rules-merged from being the
     default outcome.

  C. ``hooks/build_prompt_context.py`` gains a ``--task-dir`` flag
     that reads ``manifest.snapshot.head_sha`` and uses it as the
     diff base. Eliminates the abbreviated-SHA failure mode from
     ``--diff`` since the SHA comes from the state-machine's
     authoritative record, not the operator's keyboard.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / "hooks"
sys.path.insert(0, str(HOOKS_DIR))


# ---------------------------------------------------------------------------
# Fix A — eventbus run_register debounces recent set-active calls
# ---------------------------------------------------------------------------


def test_run_register_skips_when_last_active_at_is_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """When the registry already has the project marked active within
    the debounce window, run_register must short-circuit without
    spawning the subprocess."""
    from datetime import datetime, timezone
    import eventbus  # type: ignore[import-not-found]

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    # Pin DYNOS_HOME so run_register's canonical registry path resolves to
    # fake_home/.dynos deterministically, regardless of any DYNOS_HOME leaked
    # by an earlier test (eventbus now matches registry._registry_path()).
    monkeypatch.setenv("DYNOS_HOME", str(fake_home / ".dynos"))

    project_root = tmp_path / "project"
    project_root.mkdir()

    # Pre-populate registry with a fresh last_active_at (right now).
    reg_dir = fake_home / ".dynos"
    reg_dir.mkdir()
    fresh_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (reg_dir / "registry.json").write_text(json.dumps({
        "projects": [{
            "path": str(project_root.resolve()),
            "registered_at": fresh_ts,
            "last_active_at": fresh_ts,
            "status": "active",
        }],
    }))

    spawn_calls: list[list[str]] = []

    def fake_run(cmd, *_args, **_kwargs):
        spawn_calls.append(list(cmd))
        return True

    monkeypatch.setattr(eventbus, "_run", fake_run)

    rc = eventbus.run_register(project_root, {})
    assert rc is True, "skip path returns True (success)"
    assert spawn_calls == [], (
        "fresh last_active_at must short-circuit the subprocess spawn; "
        f"got calls: {spawn_calls!r}"
    )


def test_run_register_fires_when_last_active_at_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """When the registry's last_active_at is older than the debounce
    window, run_register must call set-active."""
    import eventbus  # type: ignore[import-not-found]

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    project_root = tmp_path / "project"
    project_root.mkdir()

    # Last_active_at is 10 minutes ago — past the debounce window.
    reg_dir = fake_home / ".dynos"
    reg_dir.mkdir()
    (reg_dir / "registry.json").write_text(json.dumps({
        "projects": [{
            "path": str(project_root.resolve()),
            "registered_at": "2024-01-01T00:00:00Z",
            "last_active_at": "2024-01-01T00:00:00Z",
            "status": "active",
        }],
    }))

    spawn_calls: list[list[str]] = []

    def fake_run(cmd, *_args, **_kwargs):
        spawn_calls.append(list(cmd))
        return True

    monkeypatch.setattr(eventbus, "_run", fake_run)

    rc = eventbus.run_register(project_root, {})
    assert rc is True
    assert len(spawn_calls) == 1, "stale timestamp must trigger one spawn"
    assert "set-active" in spawn_calls[0]


def test_run_register_fires_when_no_registry_file_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """When the registry file does not exist yet, run_register must
    fall through to the subprocess (which creates the registry)."""
    import eventbus  # type: ignore[import-not-found]

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    project_root = tmp_path / "project"
    project_root.mkdir()

    # No registry.json on disk.

    spawn_calls: list[list[str]] = []

    def fake_run(cmd, *_args, **_kwargs):
        spawn_calls.append(list(cmd))
        return True

    monkeypatch.setattr(eventbus, "_run", fake_run)

    rc = eventbus.run_register(project_root, {})
    assert rc is True
    assert len(spawn_calls) == 1, "missing registry must fall through"


def test_run_register_fires_when_project_not_in_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """When the registry exists but doesn't list this project, the
    handler must fall through and let the subprocess decide."""
    import eventbus  # type: ignore[import-not-found]

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    project_root = tmp_path / "project"
    project_root.mkdir()

    reg_dir = fake_home / ".dynos"
    reg_dir.mkdir()
    (reg_dir / "registry.json").write_text(json.dumps({
        "projects": [{
            "path": "/some/other/path",
            "registered_at": "2099-01-01T00:00:00Z",
            "last_active_at": "2099-01-01T00:00:00Z",
            "status": "active",
        }],
    }))

    spawn_calls: list[list[str]] = []

    def fake_run(cmd, *_args, **_kwargs):
        spawn_calls.append(list(cmd))
        return True

    monkeypatch.setattr(eventbus, "_run", fake_run)

    rc = eventbus.run_register(project_root, {})
    assert rc is True
    assert len(spawn_calls) == 1, "unregistered project must trigger spawn"


# ---------------------------------------------------------------------------
# Fix B — postmortem _validate_rule_schema defaults to advisory
# ---------------------------------------------------------------------------


def test_validate_rule_schema_defaults_template_to_advisory():
    """A rule without a `template` field but with a non-empty `rule`
    text must validate (defaulted to advisory) and gain
    `template: "advisory"` on the in-place dict so the persisted form
    carries the explicit template."""
    sys.path.insert(0, str(REPO_ROOT / "memory"))
    from postmortem_analysis import _validate_rule_schema  # type: ignore[import-not-found]

    rule = {
        "executor": "all",
        "category": "process",
        "rule": "Run residual-freshness probe before draining.",
        "rationale": "PRO-001 underlying code was already remediated.",
    }
    ok, reason = _validate_rule_schema(rule)
    assert ok is True, f"expected default-template acceptance; got reason={reason!r}"
    assert rule["template"] == "advisory", (
        "the rule dict must be mutated in place to carry the explicit "
        "default template so downstream readers see it"
    )


def test_validate_rule_schema_rejects_missing_rule_text():
    """A rule with no `template` AND no `rule` text is still rejected
    — the default-to-advisory fallback only applies when the rule
    looks like a real rule otherwise."""
    sys.path.insert(0, str(REPO_ROOT / "memory"))
    from postmortem_analysis import _validate_rule_schema  # type: ignore[import-not-found]

    rule = {"executor": "all", "category": "process"}  # no `rule` key
    ok, reason = _validate_rule_schema(rule)
    assert ok is False
    assert reason == "missing template field"


def test_validate_rule_schema_rejects_blank_rule_text():
    """An all-whitespace `rule` is treated as no rule text — rejected."""
    sys.path.insert(0, str(REPO_ROOT / "memory"))
    from postmortem_analysis import _validate_rule_schema  # type: ignore[import-not-found]

    rule = {"executor": "all", "rule": "   "}
    ok, _reason = _validate_rule_schema(rule)
    assert ok is False


def test_validate_rule_schema_explicit_template_unchanged():
    """When the LLM does include `template`, it is honored verbatim
    (no overwriting). Regression net for the default-fallback."""
    sys.path.insert(0, str(REPO_ROOT / "memory"))
    from postmortem_analysis import _validate_rule_schema  # type: ignore[import-not-found]

    rule = {"template": "advisory", "rule": "explicit advisory rule"}
    ok, _reason = _validate_rule_schema(rule)
    assert ok is True
    assert rule["template"] == "advisory"


# ---------------------------------------------------------------------------
# Fix C — build_prompt_context --task-dir reads snapshot from manifest
# ---------------------------------------------------------------------------


def _git_init_with_one_commit(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.run(["git", "init", "--initial-branch=main", "-q"], cwd=str(repo), check=True, env=env)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=str(repo), check=True, env=env)
    f = repo / "f.txt"
    f.write_text("baseline\n")
    subprocess.run(["git", "add", str(f)], cwd=str(repo), check=True, env=env)
    subprocess.run(["git", "commit", "-m", "feat: baseline", "-q"], cwd=str(repo), check=True, env=env)
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return repo, sha


def test_build_prompt_context_task_dir_reads_snapshot_sha(tmp_path: Path):
    """--task-dir must read manifest.snapshot.head_sha and use it as
    the diff base. Operator never types the SHA."""
    repo, sha = _git_init_with_one_commit(tmp_path)
    task_dir = repo / ".dynos" / "task-fixc"
    task_dir.mkdir(parents=True)
    (task_dir / "manifest.json").write_text(json.dumps({
        "task_id": "task-fixc",
        "stage": "EXECUTION",
        "created_at": "2026-05-06T00:00:00Z",
        "raw_input": "fixture",
        "snapshot": {"head_sha": sha, "branch": "main", "recorded_at": "2026-05-06T00:00:00Z"},
    }))

    result = subprocess.run(
        [sys.executable, str(HOOKS_DIR / "build_prompt_context.py"),
         "--task-dir", str(task_dir), "--root", str(repo)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"stderr: {result.stderr!r}"
    # Real-SHA path must NOT trigger the fix-#4 validation error
    assert "does not resolve" not in result.stderr


def test_build_prompt_context_task_dir_missing_manifest(tmp_path: Path):
    """Missing manifest must produce a clear error (not silent empty)."""
    task_dir = tmp_path / ".dynos" / "task-x"
    task_dir.mkdir(parents=True)
    # No manifest.json

    result = subprocess.run(
        [sys.executable, str(HOOKS_DIR / "build_prompt_context.py"),
         "--task-dir", str(task_dir), "--root", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "manifest.json not found" in result.stderr


def test_build_prompt_context_task_dir_missing_snapshot(tmp_path: Path):
    """Manifest without `snapshot` must produce a clear error naming
    the missing field and the recovery action."""
    task_dir = tmp_path / ".dynos" / "task-y"
    task_dir.mkdir(parents=True)
    (task_dir / "manifest.json").write_text(json.dumps({
        "task_id": "task-y",
        "stage": "PLANNING",
        "created_at": "2026-05-06T00:00:00Z",
        "raw_input": "fixture",
    }))

    result = subprocess.run(
        [sys.executable, str(HOOKS_DIR / "build_prompt_context.py"),
         "--task-dir", str(task_dir), "--root", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "no `snapshot` field" in result.stderr
    assert "ctl record-snapshot" in result.stderr


def test_build_prompt_context_task_dir_empty_head_sha(tmp_path: Path):
    """Manifest with empty/missing head_sha must produce a clear error."""
    task_dir = tmp_path / ".dynos" / "task-z"
    task_dir.mkdir(parents=True)
    (task_dir / "manifest.json").write_text(json.dumps({
        "task_id": "task-z",
        "stage": "EXECUTION",
        "created_at": "2026-05-06T00:00:00Z",
        "raw_input": "fixture",
        "snapshot": {"head_sha": "", "branch": "main", "recorded_at": "2026-05-06T00:00:00Z"},
    }))

    result = subprocess.run(
        [sys.executable, str(HOOKS_DIR / "build_prompt_context.py"),
         "--task-dir", str(task_dir), "--root", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "head_sha is empty or missing" in result.stderr
