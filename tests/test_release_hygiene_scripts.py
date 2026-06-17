from __future__ import annotations

import json
import sys
from pathlib import Path


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
    changelog.write_text("# Changelog\n\n## [Unreleased]\n\n---\n\n")

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
    assert "## [1.2.4]" in changelog.read_text()


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
