#!/usr/bin/env python3
"""Validate and auto-fix audit report schemas after auditor subagents complete.

Called by the SubagentStop hook after every subagent completes.
If the subagent was an auditor, scans the active task's audit-reports/
directory for recently-modified JSON files and fixes common schema issues:
  - Missing or malformed auditor_name
  - scope that is an object instead of a string
  - Missing scope
  - Missing or non-list findings

Usage:
    python3 dynoslib_schema_hook.py \
        --root /path/to/project \
        --agent-type "dynos-work:code-quality-auditor" \
        --agent-desc "Audit code quality"
"""

from __future__ import annotations

import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import argparse
import json
import re
import sys
import time
from pathlib import Path

from dynoslib_core import find_active_tasks


_RECENT_WINDOW_SECONDS = 120


def _is_auditor(agent_type: str, agent_desc: str) -> bool:
    """Return True if the subagent was an auditor."""
    lower_type = (agent_type or "").lower()
    lower_desc = (agent_desc or "").lower()
    return "auditor" in lower_type or "auditor" in lower_desc


def _derive_auditor_name(filename: str) -> str:
    """Derive auditor_name from a report filename.

    Examples:
        code-quality-auditor-checkpoint.json -> code-quality-auditor
        db-schema-auditor-vote-1.json -> db-schema-auditor
        ui-auditor.json -> ui-auditor
    """
    stem = Path(filename).stem
    # Strip -checkpoint suffix
    stem = re.sub(r"-checkpoint$", "", stem)
    # Strip -vote-N suffix
    stem = re.sub(r"-vote-\d+$", "", stem)
    return stem


def _stringify_scope(scope: object) -> str:
    """Convert a scope object to a string representation."""
    if isinstance(scope, str):
        return scope
    if isinstance(scope, dict):
        sha = scope.get("audit_start_sha")
        if sha:
            file_count = 0
            files = scope.get("files_audited") or scope.get("files") or scope.get("changed_files") or []
            if isinstance(files, list):
                file_count = len(files)
            elif isinstance(files, int):
                file_count = files
            return f"{sha} ({file_count} files)"
        return "changed-files"
    return "changed-files"


def _fix_report(report_path: Path) -> None:
    """Validate and fix a single audit report file."""
    try:
        raw = report_path.read_text(encoding="utf-8")
    except OSError:
        return

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return

    if not isinstance(data, dict):
        return

    fixed_fields: list[str] = []

    # Fix missing auditor_name
    if "auditor_name" not in data or not data["auditor_name"]:
        data["auditor_name"] = _derive_auditor_name(report_path.name)
        fixed_fields.append("auditor_name")

    # Fix scope: object -> string, or missing
    if "scope" not in data:
        data["scope"] = "changed-files"
        fixed_fields.append("scope")
    elif not isinstance(data["scope"], str):
        data["scope"] = _stringify_scope(data["scope"])
        fixed_fields.append("scope")

    # Fix findings: missing or not a list
    if "findings" not in data:
        data["findings"] = []
        fixed_fields.append("findings")
    elif not isinstance(data["findings"], list):
        data["findings"] = []
        fixed_fields.append("findings")

    if not fixed_fields:
        return

    # Write back
    try:
        report_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError:
        return

    filename = report_path.name
    for field in fixed_fields:
        print(f"[schema-fix] {filename}: fixed {field}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate audit report schemas")
    parser.add_argument("--root", required=True, help="Project root path")
    parser.add_argument("--agent-type", default="", help="Agent type")
    parser.add_argument("--agent-desc", default="", help="Agent description")
    args = parser.parse_args()

    if not _is_auditor(args.agent_type, args.agent_desc):
        return 0

    root = Path(args.root).resolve()

    try:
        active_tasks = find_active_tasks(root)
    except Exception:
        return 0

    if not active_tasks:
        return 0

    now = time.time()

    for task_dir in active_tasks:
        audit_dir = task_dir / "audit-reports"
        if not audit_dir.is_dir():
            continue

        try:
            report_files = list(audit_dir.glob("*.json"))
        except OSError:
            continue

        for report_path in report_files:
            try:
                mtime = report_path.stat().st_mtime
            except OSError:
                continue

            if (now - mtime) <= _RECENT_WINDOW_SECONDS:
                try:
                    _fix_report(report_path)
                except Exception:
                    continue

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
