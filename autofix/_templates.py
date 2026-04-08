#!/usr/bin/env python3
"""Fix template storage and retrieval for recurring audit findings."""

from __future__ import annotations

import json
from pathlib import Path

from autofix._core import global_home, load_json, now_iso, project_slug, write_json
from autofix._defaults import MAX_FIX_TEMPLATES, MAX_TEMPLATE_DIFF_LINES


def _templates_path(root: Path) -> Path:
    """Return the path to fix-templates.json for the given project."""
    slug = project_slug(root)
    return global_home() / "projects" / slug / "fix-templates.json"


def _extract_file_ext(finding: dict) -> str:
    """Extract the file extension from a finding's evidence file path."""
    evidence = finding.get("evidence", {})
    file_path = evidence.get("file", "")
    if not file_path:
        return ""
    return Path(file_path).suffix


def _load_templates(path: Path) -> list[dict]:
    """Load templates from disk, returning [] on any error."""
    if not path.exists():
        return []
    try:
        data = load_json(path)
    except (json.JSONDecodeError, OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return data


def _truncate_diff(diff: str) -> str:
    """Truncate a diff to at most MAX_TEMPLATE_DIFF_LINES lines."""
    lines = diff.split("\n")
    if len(lines) > MAX_TEMPLATE_DIFF_LINES:
        lines = lines[:MAX_TEMPLATE_DIFF_LINES]
    return "\n".join(lines)


def save_fix_template(root: Path, finding: dict, diff: str) -> None:
    """Save a fix template derived from a finding and its diff.

    Templates are stored at ~/.dynos/projects/{slug}/fix-templates.json.
    FIFO eviction keeps the list at most 50 entries.
    Never raises.
    """
    try:
        path = _templates_path(root)
        templates = _load_templates(path)

        entry = {
            "category": finding.get("category", ""),
            "file_ext": _extract_file_ext(finding),
            "diff": _truncate_diff(diff),
            "saved_at": now_iso(),
        }
        templates.append(entry)

        # FIFO eviction: remove oldest entries until within capacity
        while len(templates) > MAX_FIX_TEMPLATES:
            templates.pop(0)

        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, templates)
    except Exception:
        pass


def find_matching_template(root: Path, finding: dict) -> dict | None:
    """Find the most recently saved template matching (category, file_ext).

    Returns the matching template dict, or None if no match or on any error.
    """
    try:
        path = _templates_path(root)
        templates = _load_templates(path)
        if not templates:
            return None

        category = finding.get("category", "")
        file_ext = _extract_file_ext(finding)

        # Iterate in reverse to find the most recent match first
        for template in reversed(templates):
            if template.get("category") == category and template.get("file_ext") == file_ext:
                return template

        return None
    except Exception:
        return None
