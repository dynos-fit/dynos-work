"""Tests for wiring guarantee — RED state for lib_project_id imports.

These tests verify:
- All consumer call sites observe the same UUID slug component (AC 40)
- lib_project_id import is at module scope in lib_core.py (AC 11)
- lib_project_id does not import lib_core (AC 12)
- _persistent_project_dir returns UUID path for git repos (AC 13)
- _persistent_project_dir returns path- path for non-git dirs (AC 14)
- _persistent_project_dir propagates ProjectIdSecurityError (AC 15)
- _resolve_git_toplevel ignores GIT_DIR env (AC 16)
- DYNOS_HOME env var still works (AC 17)
- daemon loop logs and continues on ProjectIdSecurityError (daemon AC)
"""

from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path

import pytest

# lib_core already exists.
import lib_core

# lib_project_id is new — RED import.
from lib_project_id import (
    ProjectIdSecurityError,
    is_uuid_id,
    resolve_project_id,
    _safe_git_env,
)

# ---------------------------------------------------------------------------
# UUID4 regex for assertions.
# ---------------------------------------------------------------------------

import re
UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "t@t.com"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "T"],
        check=True, capture_output=True,
    )
    return path


# ---------------------------------------------------------------------------
# AC 40 — every consumer call site observes the same project_id
# ---------------------------------------------------------------------------


def test_all_call_sites_observe_same_project_id(tmp_path: Path, monkeypatch):
    """AC 40 — all sampled consumers of _persistent_project_dir return the same
    UUID slug component (parts[-1]) for the same git repo root.

    Sampled consumers:
      1. lib_core._persistent_project_dir (direct)
      2. policy_engine.local_patterns_path
      3. _persistent_project_dir path used by agent_generator (via registry_path or fallback)
      4. lib_log event-log path (via lib_log module call)
      5. rules_engine prevention-rules path
    """
    dynos_home = tmp_path / "dynos-home"
    dynos_home.mkdir()
    monkeypatch.setenv("DYNOS_HOME", str(dynos_home))

    repo = _make_git_repo(tmp_path / "repo")

    slugs: list[str] = []

    def _extract_uuid_slug(p):
        """Find the UUID4 / path-fallback slug in a path's parts (it sits between 'projects/' and any file/subdir)."""
        parts = p.parts
        try:
            idx = parts.index("projects")
            return parts[idx + 1]
        except (ValueError, IndexError):
            return parts[-1]

    # Consumer 1: _persistent_project_dir directly.
    p1 = lib_core._persistent_project_dir(repo)
    slugs.append(_extract_uuid_slug(p1))

    # Consumer 2: policy_engine.local_patterns_path.
    import policy_engine
    p2 = policy_engine.local_patterns_path(repo)
    slugs.append(_extract_uuid_slug(p2))

    # Consumer 3: agent_generator registry path.
    try:
        import agent_generator
        p3 = agent_generator.registry_path(repo)
    except (ImportError, AttributeError):
        # Fallback: construct the path the same way the module would.
        p3 = lib_core._persistent_project_dir(repo) / "learned-agents" / "registry.json"
    slugs.append(_extract_uuid_slug(p3))

    # Consumer 4: lib_log event-log path.
    try:
        import lib_log
        p4 = lib_log.event_log_path(repo)
        slugs.append(_extract_uuid_slug(p4))
    except (ImportError, AttributeError):
        # If lib_log doesn't expose the path directly, use _persistent_project_dir.
        p4 = lib_core._persistent_project_dir(repo) / "events.jsonl"
        slugs.append(_extract_uuid_slug(p4))

    # Consumer 5: rules_engine prevention-rules path.
    try:
        import rules_engine
        p5 = rules_engine.prevention_rules_path(repo)
        slugs.append(_extract_uuid_slug(p5))
    except (ImportError, AttributeError):
        p5 = lib_core._persistent_project_dir(repo) / "prevention-rules.json"
        slugs.append(_extract_uuid_slug(p5))

    # All slug components must be equal.
    assert len(set(slugs)) == 1, (
        f"Call sites observed different project IDs: {slugs}"
    )
    # The common slug must be a UUID4.
    slug = slugs[0]
    assert UUID4_RE.match(slug), (
        f"Observed slug {slug!r} is not a UUID4"
    )


# ---------------------------------------------------------------------------
# AC 11 — lib_project_id import is at module scope in lib_core.py
# ---------------------------------------------------------------------------


def test_lib_project_id_import_is_module_level():
    """AC 11 — lib_core.py must import resolve_project_id from lib_project_id at
    module scope, not inside a function body.
    """
    src_file = Path(__file__).resolve().parents[1] / "hooks" / "lib_core.py"
    assert src_file.exists()
    tree = ast.parse(src_file.read_text(encoding="utf-8"))

    # Walk only top-level nodes (direct children of Module).
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "lib_project_id":
            names = [alias.name for alias in node.names]
            if "resolve_project_id" in names:
                return  # Found at module level.

    pytest.fail(
        "lib_core.py does not import resolve_project_id from lib_project_id at module scope"
    )


# ---------------------------------------------------------------------------
# AC 12 — lib_project_id does not import from lib_core
# ---------------------------------------------------------------------------


def test_lib_project_id_does_not_import_from_lib_core():
    """AC 12 — lib_project_id.py must not contain any import from lib_core."""
    src_file = Path(__file__).resolve().parents[1] / "hooks" / "lib_project_id.py"
    assert src_file.exists(), "hooks/lib_project_id.py not found"
    tree = ast.parse(src_file.read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and (
                node.module == "lib_core" or node.module.startswith("lib_core.")
            ):
                pytest.fail(
                    f"lib_project_id.py imports from lib_core (line {node.lineno})"
                )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "lib_core" or alias.name.startswith("lib_core."):
                    pytest.fail(
                        f"lib_project_id.py imports lib_core (line {node.lineno})"
                    )


# ---------------------------------------------------------------------------
# AC 13 — _persistent_project_dir uses UUID for git repos
# ---------------------------------------------------------------------------


def test_persistent_project_dir_uses_uuid_when_git_repo(tmp_path: Path, monkeypatch):
    """AC 13 — _persistent_project_dir on a git repo returns a Path whose parts[-1]
    is a UUID4.
    """
    dynos_home = tmp_path / "dynos-home"
    dynos_home.mkdir()
    monkeypatch.setenv("DYNOS_HOME", str(dynos_home))

    repo = _make_git_repo(tmp_path / "uuidrepo")
    result = lib_core._persistent_project_dir(repo)
    slug = result.parts[-1]
    assert UUID4_RE.match(slug), (
        f"Expected UUID4 slug in parts[-1], got {slug!r}"
    )


# ---------------------------------------------------------------------------
# AC 14 — _persistent_project_dir returns path- fallback for non-git dirs
# ---------------------------------------------------------------------------


def test_persistent_project_dir_returns_path_fallback_for_non_git_dir(
    tmp_path: Path, monkeypatch
):
    """AC 14 — _persistent_project_dir on a non-git directory returns a Path
    whose parts[-1] starts with 'path-'.
    """
    dynos_home = tmp_path / "dynos-home"
    dynos_home.mkdir()
    monkeypatch.setenv("DYNOS_HOME", str(dynos_home))

    non_git = tmp_path / "non_git"
    non_git.mkdir()
    result = lib_core._persistent_project_dir(non_git)
    slug = result.parts[-1]
    assert slug.startswith("path-"), (
        f"Expected path- prefix in parts[-1], got {slug!r}"
    )


# ---------------------------------------------------------------------------
# AC 15 — _persistent_project_dir propagates ProjectIdSecurityError
# ---------------------------------------------------------------------------


def test_persistent_project_dir_propagates_security_error(
    tmp_path: Path, monkeypatch
):
    """AC 15 — _persistent_project_dir must NOT catch ProjectIdSecurityError;
    it must propagate unchanged to the caller.
    """
    def _raise(_root):
        raise ProjectIdSecurityError("test security trip")

    monkeypatch.setattr(lib_core, "resolve_project_id", _raise)

    with pytest.raises(ProjectIdSecurityError, match="test security trip"):
        lib_core._persistent_project_dir(tmp_path)


# ---------------------------------------------------------------------------
# AC 16 — _resolve_git_toplevel ignores GIT_DIR env
# ---------------------------------------------------------------------------


def test_resolve_git_toplevel_ignores_GIT_DIR_env(tmp_path: Path, monkeypatch):
    """AC 16 — _resolve_git_toplevel uses scrubbed env; setting GIT_DIR=/nonexistent
    does not redirect the result away from the real repo root.
    """
    repo = _make_git_repo(tmp_path / "repo")
    monkeypatch.setenv("GIT_DIR", "/nonexistent/path/that/does/not/exist")

    result = lib_core._resolve_git_toplevel(str(repo))
    assert result is not None, "_resolve_git_toplevel returned None for a real git repo"
    assert Path(result).resolve() == repo.resolve(), (
        f"Expected {repo.resolve()}, got {result!r}"
    )


# ---------------------------------------------------------------------------
# AC 17 — DYNOS_HOME env var still works
# ---------------------------------------------------------------------------


def test_dynos_home_env_var_still_works(tmp_path: Path, monkeypatch):
    """AC 17 — setting DYNOS_HOME causes _persistent_project_dir to return a path
    under that custom home rather than the default ~/.dynos.
    """
    custom_home = tmp_path / "custom-dynos"
    custom_home.mkdir()
    monkeypatch.setenv("DYNOS_HOME", str(custom_home))

    repo = _make_git_repo(tmp_path / "repo")
    result = lib_core._persistent_project_dir(repo)

    assert str(result).startswith(str(custom_home)), (
        f"Path {result} does not start with DYNOS_HOME={custom_home}"
    )
    assert "projects" in result.parts, "Expected 'projects' in path parts"


# ---------------------------------------------------------------------------
# Daemon loop — logs and continues on ProjectIdSecurityError
# ---------------------------------------------------------------------------


def test_daemon_loop_logs_and_continues_on_security_error(
    tmp_path: Path, monkeypatch
):
    """AC 40 (daemon) — the daemon background loop must catch ProjectIdSecurityError
    from _persistent_project_dir, log it, and continue rather than crashing.
    """
    import daemon as daemon_mod  # hooks/daemon.py

    logged: list[str] = []

    def _raise_security(_root):
        raise ProjectIdSecurityError("simulated security trip in daemon")

    def _mock_log_event(root, event_name, **kwargs):
        logged.append(event_name)

    monkeypatch.setattr(lib_core, "resolve_project_id", _raise_security)
    monkeypatch.setattr(daemon_mod, "log_event", _mock_log_event, raising=False)

    # Attempt to call one of the daemon functions that uses _persistent_project_dir
    # in an error-tolerant loop context.  If the security error propagates,
    # the test fails because it will raise.
    try:
        # check_prevention_rules_bootstrap is in the daemon background loop.
        daemon_mod.check_prevention_rules_bootstrap(tmp_path)
    except ProjectIdSecurityError:
        pytest.fail(
            "ProjectIdSecurityError propagated out of daemon loop; expected it to be caught"
        )
    except Exception:
        # Other errors are acceptable; we only care that security errors are caught.
        pass

    # The security error event must have been logged.
    security_events = [e for e in logged if "security" in e.lower() or "project_id" in e.lower()]
    assert security_events, (
        f"Expected a security-related log event from daemon loop, got {logged}"
    )
