"""Tests for the H1 self-modification guard (docs/permissions-on-design.md §D5-3).

Agent roles must never be able to write the installed plugin directory
(hooks.json, write_policy.py, ctl.py) or the host-level config locations that
control hook execution — that is the "agent disables its own guardrails"
primitive. The plugin source repo itself (developer mode) is exempt for the
plugin-root check only.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

import write_policy  # noqa: E402
from write_policy import WriteAttempt, decide_write  # noqa: E402


@pytest.fixture()
def fake_plugin_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An 'installed plugin' root: no .git, separate from the project."""
    plugin_root = tmp_path / "installed" / "dynos-work"
    (plugin_root / "hooks").mkdir(parents=True)
    (plugin_root / ".claude-plugin").mkdir()
    monkeypatch.setattr(write_policy, "_PLUGIN_ROOT", plugin_root.resolve())
    return plugin_root


def _project_task_dir(tmp_path: Path) -> Path:
    task_dir = tmp_path / "project" / ".dynos" / "task-20260611-001"
    task_dir.mkdir(parents=True)
    return task_dir


@pytest.mark.parametrize(
    "role",
    ["backend-executor", "execute-inline", "planning", "audit-security", "orchestrator"],
)
def test_agent_roles_cannot_write_installed_plugin_dir(
    fake_plugin_root: Path, tmp_path: Path, role: str
) -> None:
    task_dir = _project_task_dir(tmp_path)
    for target in (
        fake_plugin_root / "hooks" / "write_policy.py",
        fake_plugin_root / "hooks.json",
        fake_plugin_root / "hooks" / "ctl.py",
    ):
        decision = decide_write(
            WriteAttempt(
                role=role,
                task_dir=task_dir,
                path=target,
                operation="modify",
                source="agent",
            )
        )
        assert decision.allowed is False, f"{role} wrote {target}"
        assert "self-modification" in decision.reason


def test_executor_repo_writes_still_allowed_outside_plugin_dir(
    fake_plugin_root: Path, tmp_path: Path
) -> None:
    task_dir = _project_task_dir(tmp_path)
    decision = decide_write(
        WriteAttempt(
            role="backend-executor",
            task_dir=task_dir,
            path=tmp_path / "project" / "src" / "main.py",
            operation="modify",
            source="agent",
        )
    )
    assert decision.allowed is True


def test_developer_mode_allows_plugin_repo_self_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the project IS the plugin source repo, plugin files are repo files."""
    plugin_root = tmp_path / "dynos-work-src"
    (plugin_root / "hooks").mkdir(parents=True)
    (plugin_root / ".claude-plugin").mkdir()
    (plugin_root / ".git").mkdir()
    monkeypatch.setattr(write_policy, "_PLUGIN_ROOT", plugin_root.resolve())
    task_dir = plugin_root / ".dynos" / "task-20260611-002"
    task_dir.mkdir(parents=True)
    decision = decide_write(
        WriteAttempt(
            role="backend-executor",
            task_dir=task_dir,
            path=plugin_root / "hooks" / "lib_validate.py",
            operation="modify",
            source="agent",
        )
    )
    assert decision.allowed is True


def test_no_task_dev_mode_requires_source_checkout(
    fake_plugin_root: Path,
) -> None:
    """Installed (cache) plugin root without .git: denied even with no task."""
    decision = decide_write(
        WriteAttempt(
            role="execute-inline",
            task_dir=None,
            path=fake_plugin_root / "hooks" / "ctl.py",
            operation="modify",
            source="agent",
        )
    )
    assert decision.allowed is False
    assert "self-modification" in decision.reason


@pytest.mark.parametrize(
    "suffix",
    [
        Path(".claude") / "plugins" / "cache" / "dynos-work" / "hooks.json",
        Path(".claude") / "settings.json",
        Path(".claude") / "settings.local.json",
        Path(".dynos") / "registry.json",
    ],
)
def test_host_config_paths_denied_for_agent_roles(
    fake_plugin_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: Path,
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    task_dir = _project_task_dir(tmp_path)
    decision = decide_write(
        WriteAttempt(
            role="backend-executor",
            task_dir=task_dir,
            path=fake_home / suffix,
            operation="modify",
            source="agent",
        )
    )
    assert decision.allowed is False
    assert "self-modification" in decision.reason


def test_project_memory_under_dot_claude_still_allowed(
    fake_plugin_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only hook-controlling paths are protected, not all of ~/.claude."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    task_dir = _project_task_dir(tmp_path)
    decision = decide_write(
        WriteAttempt(
            role="docs-executor",
            task_dir=task_dir,
            path=fake_home / ".claude" / "projects" / "slug" / "memory" / "project_rules.md",
            operation="create",
            source="agent",
        )
    )
    assert decision.allowed is True


def test_framework_roles_exempt(fake_plugin_root: Path, tmp_path: Path) -> None:
    """The guard never fires for framework roles — they fall through to the
    pre-existing boundary rules (which still deny ctl out-of-task writes)."""
    task_dir = _project_task_dir(tmp_path)
    ctl_decision = decide_write(
        WriteAttempt(
            role="ctl",
            task_dir=task_dir,
            path=fake_plugin_root / "hooks" / "anything.json",
            operation="modify",
            source="ctl",
        )
    )
    assert "self-modification" not in ctl_decision.reason

    system_decision = decide_write(
        WriteAttempt(
            role="system",
            task_dir=task_dir,
            path=fake_plugin_root / "logs" / "events.jsonl",
            operation="modify",
            source="system",
        )
    )
    assert system_decision.allowed is True
