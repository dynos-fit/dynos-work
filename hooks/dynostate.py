#!/usr/bin/env python3
"""Deterministic state encoder for dynos-work."""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import argparse
import json
import re
from pathlib import Path

from dynoslib import collect_retrospectives


TEXT_EXTENSIONS = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".md",
    ".json",
    ".yml",
    ".yaml",
    ".toml",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".rb",
    ".php",
    ".sh",
    ".css",
    ".scss",
    ".html",
}


def iter_code_files(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    files: list[Path] = []
    for path in target.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        if ".git" in path.parts or ".dynos" in path.parts or "node_modules" in path.parts:
            continue
        if path.suffix.lower() in TEXT_EXTENSIONS:
            files.append(path)
    return files


def read_text(path: Path) -> str:
    try:
        return path.read_text()
    except UnicodeDecodeError:
        return path.read_text(errors="ignore")


def encode_state(root: Path, target: Path | None = None) -> dict:
    target = (target or root).resolve()
    files = iter_code_files(target)
    total_lines = 0
    import_count = 0
    control_flow = 0
    languages: dict[str, int] = {}
    symbols = 0
    for path in files:
        text = read_text(path)
        total_lines += len(text.splitlines())
        import_count += len(re.findall(r"^\s*(import|from|require\()", text, flags=re.MULTILINE))
        control_flow += len(re.findall(r"\b(if|for|while|switch|case|catch|try)\b", text))
        symbols += len(re.findall(r"\b(class|def|function|const|let|var)\b", text))
        languages[path.suffix.lower() or "<none>"] = languages.get(path.suffix.lower() or "<none>", 0) + 1

    retrospectives = collect_retrospectives(root)
    findings_by_category: dict[str, int] = {}
    for item in retrospectives[-5:]:
        for key, value in item.get("findings_by_category", {}).items():
            if isinstance(value, int):
                findings_by_category[key] = findings_by_category.get(key, 0) + value

    architecture_complexity = round((control_flow + symbols) / max(1, len(files)), 4)
    dependency_flux = round(import_count / max(1, len(files)), 4)
    total_findings = sum(findings_by_category.values())
    finding_entropy = round(total_findings / max(1, len(retrospectives[-5:])), 4)
    dominant_languages = [lang for lang, _ in sorted(languages.items(), key=lambda item: (-item[1], item[0]))[:5]]

    return {
        "version": 1,
        "target": str(target),
        "architecture_complexity_score": architecture_complexity,
        "dependency_flux": dependency_flux,
        "finding_entropy": finding_entropy,
        "file_count": len(files),
        "line_count": total_lines,
        "import_count": import_count,
        "control_flow_count": control_flow,
        "dominant_languages": dominant_languages,
        "recent_findings_by_category": findings_by_category,
    }


def cmd_state(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    target = Path(args.target).resolve() if args.target else None
    print(json.dumps(encode_state(root, target), indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--target")
    parser.set_defaults(func=cmd_state)
    return parser


if __name__ == "__main__":
    from dyno_cli_base import cli_main
    raise SystemExit(cli_main(build_parser))
