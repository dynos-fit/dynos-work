#!/usr/bin/env python3
"""Verify dynos-work release metadata stays in lockstep."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION_PATHS = (
    Path("package.json"),
    Path(".claude-plugin/plugin.json"),
    Path(".claude-plugin/marketplace.json"),
    Path(".codex-plugin/plugin.json"),
)
IGNORED_CHANGE_PATHS = {
    "CHANGELOG.md",
    "CLAUDE.md",
    "AGENTS.md",
}


def _json(path: Path) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def _version_for(path: Path) -> str:
    data = _json(path)
    if path.as_posix() == ".claude-plugin/marketplace.json":
        return str(data["plugins"][0]["version"])
    return str(data["version"])


def _parse_version(version: str) -> tuple[int, int, int]:
    parts = version.split(".")
    if len(parts) != 3:
        raise ValueError(f"expected MAJOR.MINOR.PATCH version, got {version!r}")
    return tuple(int(part) for part in parts)  # type: ignore[return-value]


def _base_file(base_ref: str, path: Path) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "show", f"{base_ref}:{path.as_posix()}"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return None


def _base_version(base_ref: str | None) -> str | None:
    if not base_ref:
        return None
    raw = _base_file(base_ref, Path("package.json"))
    if raw is None:
        return None
    return str(json.loads(raw)["version"])


def _changed_paths(base_ref: str | None) -> set[str]:
    if not base_ref:
        return set()
    try:
        raw = subprocess.check_output(
            ["git", "diff", "--name-only", f"{base_ref}...HEAD"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return set()
    return {line.strip() for line in raw.splitlines() if line.strip()}


def _release_relevant_changes(paths: set[str]) -> set[str]:
    return {
        path for path in paths
        if path not in IGNORED_CHANGE_PATHS and path not in {p.as_posix() for p in VERSION_PATHS}
    }


def check_release_hygiene(*, base_ref: str | None) -> list[str]:
    errors: list[str] = []
    versions = {path.as_posix(): _version_for(path) for path in VERSION_PATHS}
    unique_versions = set(versions.values())
    if len(unique_versions) != 1:
        errors.append(f"version files disagree: {versions}")
        return errors

    version = next(iter(unique_versions))
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    if f"## [{version}]" not in changelog:
        errors.append(f"CHANGELOG.md is missing an entry for {version}")

    base_version = _base_version(base_ref)
    changed = _release_relevant_changes(_changed_paths(base_ref))
    if changed and base_version is not None:
        try:
            if _parse_version(version) <= _parse_version(base_version):
                errors.append(
                    f"release-relevant changes require version > {base_version}; got {version}"
                )
        except ValueError as exc:
            errors.append(str(exc))
        if "CHANGELOG.md" not in _changed_paths(base_ref):
            errors.append("release-relevant changes require CHANGELOG.md to be updated")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-ref", default=None)
    args = parser.parse_args()
    errors = check_release_hygiene(base_ref=args.base_ref)
    if errors:
        for error in errors:
            print(f"release-hygiene: {error}", file=sys.stderr)
        return 1
    print("release-hygiene: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
