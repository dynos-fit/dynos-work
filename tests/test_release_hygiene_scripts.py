from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import bump_version  # noqa: E402
import check_release_hygiene  # noqa: E402


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def test_bump_release_updates_all_manifests_and_changelog(
    tmp_path: Path, monkeypatch
) -> None:
    package_json = tmp_path / "package.json"
    claude_plugin = tmp_path / ".claude-plugin" / "plugin.json"
    claude_marketplace = tmp_path / ".claude-plugin" / "marketplace.json"
    codex_plugin = tmp_path / ".codex-plugin" / "plugin.json"
    changelog = tmp_path / "CHANGELOG.md"

    _write_json(package_json, {"version": "1.2.3"})
    _write_json(claude_plugin, {"version": "1.2.3"})
    _write_json(claude_marketplace, {"plugins": [{"version": "1.2.3"}]})
    _write_json(codex_plugin, {"version": "1.2.3"})
    changelog.write_text(
        "# Changelog\n\n## [Unreleased]\n\n### Changed\n- Existing unreleased change.\n\n---\n\n"
        "## [1.2.3] - 2026-01-01\n"
    )

    monkeypatch.setattr(bump_version, "ROOT", tmp_path)
    monkeypatch.setattr(bump_version, "PACKAGE_JSON", package_json)
    monkeypatch.setattr(bump_version, "CLAUDE_PLUGIN_JSON", claude_plugin)
    monkeypatch.setattr(bump_version, "CLAUDE_MARKETPLACE_JSON", claude_marketplace)
    monkeypatch.setattr(bump_version, "CODEX_PLUGIN_JSON", codex_plugin)
    monkeypatch.setattr(bump_version, "CHANGELOG", changelog)
    monkeypatch.setattr(
        bump_version,
        "VERSION_FILES",
        (package_json, claude_plugin, claude_marketplace, codex_plugin),
    )

    version = bump_version.bump_release(
        bump="patch",
        base_ref=None,
        set_version=None,
        message="Test release automation.",
    )

    assert version == "1.2.4"
    assert json.loads(package_json.read_text())["version"] == "1.2.4"
    assert json.loads(claude_plugin.read_text())["version"] == "1.2.4"
    assert json.loads(codex_plugin.read_text())["version"] == "1.2.4"
    assert json.loads(claude_marketplace.read_text())["plugins"][0]["version"] == "1.2.4"
    changelog_text = changelog.read_text()
    assert "## [1.2.4]" in changelog_text
    assert changelog_text.index("Existing unreleased change") < changelog_text.index("## [1.2.4]")
    assert changelog_text.index("## [1.2.4]") < changelog_text.index("## [1.2.3]")


def test_check_release_hygiene_reports_mismatched_versions(
    tmp_path: Path, monkeypatch
) -> None:
    _write_json(tmp_path / "package.json", {"version": "1.2.4"})
    _write_json(tmp_path / ".claude-plugin" / "plugin.json", {"version": "1.2.4"})
    _write_json(tmp_path / ".claude-plugin" / "marketplace.json", {"plugins": [{"version": "1.2.3"}]})
    _write_json(tmp_path / ".codex-plugin" / "plugin.json", {"version": "1.2.4"})
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\n## [1.2.4]\n")

    monkeypatch.setattr(check_release_hygiene, "ROOT", tmp_path)

    errors = check_release_hygiene.check_release_hygiene(base_ref=None)

    assert errors
    assert "version files disagree" in errors[0]


def test_check_release_hygiene_accepts_consistent_release(
    tmp_path: Path, monkeypatch
) -> None:
    _write_json(tmp_path / "package.json", {"version": "1.2.4"})
    _write_json(tmp_path / ".claude-plugin" / "plugin.json", {"version": "1.2.4"})
    _write_json(tmp_path / ".claude-plugin" / "marketplace.json", {"plugins": [{"version": "1.2.4"}]})
    _write_json(tmp_path / ".codex-plugin" / "plugin.json", {"version": "1.2.4"})
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\n## [1.2.4]\n")

    monkeypatch.setattr(check_release_hygiene, "ROOT", tmp_path)

    assert check_release_hygiene.check_release_hygiene(base_ref=None) == []


def test_start_skill_added_command_warns_without_budget_test(monkeypatch) -> None:
    monkeypatch.setattr(check_release_hygiene, "ROOT", Path.cwd())
    monkeypatch.setattr(check_release_hygiene, "_changed_paths", lambda _base: {"skills/start/SKILL.md"})
    monkeypatch.setattr(
        check_release_hygiene.subprocess,
        "check_output",
        lambda *_a, **_k: '+  "$DYNOS" ctl timeline .dynos/task-X\n',
    )
    warning = check_release_hygiene.start_skill_budget_warning(base_ref="origin/main")
    assert warning and "tests/test_start_skill_budget.py" in warning


@pytest.mark.parametrize(
    ("version", "bump", "expected"),
    [
        ("7.5.24", "patch", "7.5.25"),
        ("7.5.24", "minor", "7.6.0"),
        ("7.5.24", "major", "8.0.0"),
        ("7.5.24", "none", "7.5.24"),
    ],
)
def test_bumped(version: str, bump: str, expected: str) -> None:
    assert bump_version._bumped(version, bump) == expected


def test_bumped_rejects_unknown_type() -> None:
    with pytest.raises(ValueError, match="unknown bump type"):
        bump_version._bumped("7.5.24", "huge")


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("feat: pre-execution latency round two", "minor"),
        ("feat(start): combined path by default", "minor"),
        ("fix: honor planned greenfield schema rows", "patch"),
        ("docs: file Unreleased changelog notes under 7.5.23 and 7.5.24", "patch"),
        ("chore: update release metadata", "patch"),
        ("ci: derive the release bump from the PR title prefix", "patch"),
        ("refactor(hooks): split lib_core", "patch"),
        ("feat!: drop staged planning path", "major"),
        ("fix(api)!: rename receipt fields", "major"),
        ("feat: new receipt layout BREAKING CHANGE", "major"),
        ("  feat: leading whitespace is fine", "minor"),
    ],
)
def test_bump_from_title(title: str, expected: str) -> None:
    assert bump_version.bump_from_title(title) == expected


@pytest.mark.parametrize(
    "title",
    [
        "Re-chain artifacts after amendment",
        "Feat: capitalized type",
        "feat:no space after colon",
        "feature: unknown type",
        "feat",
        "",
    ],
)
def test_bump_from_title_rejects_unprefixed_titles(title: str) -> None:
    with pytest.raises(ValueError, match="no conventional-commit prefix"):
        bump_version.bump_from_title(title)


def test_bump_release_auto_uses_title(tmp_path: Path, monkeypatch) -> None:
    package_json = tmp_path / "package.json"
    changelog = tmp_path / "CHANGELOG.md"
    _write_json(package_json, {"version": "7.5.24"})
    changelog.write_text("# Changelog\n\n## [Unreleased]\n\n---\n\n## [7.5.24] - 2026-10-19\n")
    monkeypatch.setattr(bump_version, "ROOT", tmp_path)
    monkeypatch.setattr(bump_version, "PACKAGE_JSON", package_json)
    monkeypatch.setattr(bump_version, "CHANGELOG", changelog)
    monkeypatch.setattr(bump_version, "VERSION_FILES", (package_json,))

    assert bump_version.bump_release(
        bump="auto", base_ref=None, set_version=None, message="feat: something new"
    ) == "7.6.0"
    assert bump_version.bump_release(
        bump="auto", base_ref=None, set_version=None, message="fix: something broken"
    ) == "7.6.1"
