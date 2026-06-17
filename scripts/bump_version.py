#!/usr/bin/env python3
"""Bump dynos-work release metadata idempotently.

Default behavior is PR-friendly: read the version from the base ref, compute
the requested bump, set every package/plugin manifest to that target, and add
a small changelog section if it is missing. Re-running the script on the same
PR does not keep incrementing the version.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_JSON = ROOT / "package.json"
CLAUDE_PLUGIN_JSON = ROOT / ".claude-plugin" / "plugin.json"
CLAUDE_MARKETPLACE_JSON = ROOT / ".claude-plugin" / "marketplace.json"
CODEX_PLUGIN_JSON = ROOT / ".codex-plugin" / "plugin.json"
CHANGELOG = ROOT / "CHANGELOG.md"
VERSION_FILES = (
    PACKAGE_JSON,
    CLAUDE_PLUGIN_JSON,
    CLAUDE_MARKETPLACE_JSON,
    CODEX_PLUGIN_JSON,
)


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _current_version() -> str:
    return str(_load_json(PACKAGE_JSON)["version"])


def _base_version(base_ref: str | None) -> str:
    if not base_ref:
        return _current_version()
    try:
        raw = subprocess.check_output(
            ["git", "show", f"{base_ref}:package.json"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return str(json.loads(raw)["version"])
    except Exception:
        return _current_version()


def _parse_version(version: str) -> tuple[int, int, int]:
    parts = version.split(".")
    if len(parts) != 3:
        raise ValueError(f"expected MAJOR.MINOR.PATCH version, got {version!r}")
    return tuple(int(part) for part in parts)  # type: ignore[return-value]


def _bumped(version: str, bump: str) -> str:
    major, minor, patch = _parse_version(version)
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    if bump == "patch":
        return f"{major}.{minor}.{patch + 1}"
    if bump == "none":
        return version
    raise ValueError(f"unknown bump type: {bump}")


def _set_manifest_version(path: Path, version: str) -> None:
    data = _load_json(path)
    if path == CLAUDE_MARKETPLACE_JSON:
        plugins = data.get("plugins")
        if not isinstance(plugins, list) or not plugins:
            raise ValueError(".claude-plugin/marketplace.json must contain a non-empty plugins list")
        plugins[0]["version"] = version
    else:
        data["version"] = version
    _write_json(path, data)


def _changelog_has_version(text: str, version: str) -> bool:
    return f"## [{version}]" in text


def _insert_changelog_entry(version: str, message: str) -> None:
    text = CHANGELOG.read_text(encoding="utf-8")
    if _changelog_has_version(text, version):
        return
    today = dt.date.today().isoformat()
    entry = (
        f"## [{version}] - {today}\n"
        "### Changed\n"
        f"- {message.strip() or 'Automated release metadata update.'}\n\n"
        "### Plugin / Distribution\n"
        f"- Bump package and plugin metadata to `{version}`.\n\n"
        "---\n\n"
    )
    marker = "## [Unreleased]\n\n---\n\n"
    if marker not in text:
        raise ValueError("CHANGELOG.md must contain the standard [Unreleased] section")
    CHANGELOG.write_text(text.replace(marker, f"## [Unreleased]\n\n---\n\n{entry}", 1), encoding="utf-8")


def bump_release(*, bump: str, base_ref: str | None, set_version: str | None, message: str) -> str:
    base = _base_version(base_ref)
    version = set_version or _bumped(base, bump)
    for path in VERSION_FILES:
        _set_manifest_version(path, version)
    _insert_changelog_entry(version, message)
    return version


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bump", choices=("patch", "minor", "major", "none"), default="patch")
    parser.add_argument("--base-ref", default=None, help="Git ref to derive the previous version from")
    parser.add_argument("--set-version", default=None, help="Set an exact version instead of deriving one")
    parser.add_argument(
        "--message",
        default="Automated release metadata update.",
        help="One-line changelog bullet for the generated release entry",
    )
    args = parser.parse_args()
    try:
        version = bump_release(
            bump=args.bump,
            base_ref=args.base_ref,
            set_version=args.set_version,
            message=args.message,
        )
    except Exception as exc:
        print(f"bump-version: {exc}", file=sys.stderr)
        return 1
    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
