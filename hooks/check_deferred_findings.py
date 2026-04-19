"""Deferred-findings TTL check (task-20260419-002 G4).

Exposes BOTH an importable Python function AND a thin argparse CLI that
delegates to that function. Both paths return/emit the same data — the
module-level helper is the source of truth; the CLI is a shell wrapper
for CI and for operators running ``python3 hooks/check_deferred_findings.py``.

Failure mode is deliberately fail-open for MISSING signal:
  - registry file absent        → returns ``[]``, exits 0 (cold start).
  - registry file malformed     → returns ``[]``, exits 0 (operator will
                                   be told by ``append_deferred_findings``
                                   when they next try to write; we don't
                                   want a corrupt registry to wedge the
                                   DONE gate and block unrelated tasks).
  - registry present, healthy,
    but no intersecting entries → returns ``[]``, exits 0.
  - intersecting entry still
    within TTL                  → returns ``[]``, exits 0.
  - intersecting entry PAST TTL → returned in the list with an ``elapsed``
                                   field; CLI exits 1 and prints one line
                                   per expired entry.

``transition_task`` imports the Python function directly (no subprocess
spawn) so the gate behaves deterministically in tests and does not
inherit shell / PYTHONPATH surprises from the caller's environment.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Support both ``python3 hooks/check_deferred_findings.py`` (script mode,
# hooks/ is on sys.path because the file lives there) and ``from hooks
# import check_deferred_findings`` (package-ish import from tests). The
# ``_persistent_project_dir`` import is the only thing we need from
# lib_core; we fail open if it is missing so the gate never wedges on a
# half-installed framework.
try:
    from lib_core import _persistent_project_dir  # type: ignore
except ImportError:
    try:
        from hooks.lib_core import _persistent_project_dir  # type: ignore
    except ImportError:
        _persistent_project_dir = None  # type: ignore


def _current_retrospective_count(root: Path) -> int:
    """Return the count of ``retrospectives/*.json`` files under the
    persistent project dir for ``root``. Returns 0 on any error — the
    TTL baseline is simply "no tasks yet", matching cold-start behavior.
    """
    if _persistent_project_dir is None:
        return 0
    try:
        retro_dir = _persistent_project_dir(root) / "retrospectives"
    except Exception:
        return 0
    if not retro_dir.exists():
        return 0
    try:
        return len(list(retro_dir.glob("*.json")))
    except OSError:
        return 0


def _load_registry(root: Path) -> dict[str, Any] | None:
    """Load ``root/.dynos/deferred-findings.json``. Returns None on any
    failure (missing file, unreadable, malformed JSON, wrong shape).
    The caller treats None as "no signal → fail open → exit 0"."""
    registry_path = root / ".dynos" / "deferred-findings.json"
    if not registry_path.exists():
        return None
    try:
        text = registry_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def check_deferred_findings(
    root: Path,
    changed_files: list[str],
) -> list[dict]:
    """Return TTL-expired deferred findings whose ``files`` intersect
    ``changed_files``.

    Each returned dict is the original registry entry augmented with an
    ``elapsed`` field (int) so callers can surface the overshoot.

    Never raises. Missing or malformed registry → empty list (fail open
    for missing signal). A malformed entry within an otherwise-valid
    registry is skipped individually without poisoning the other entries.
    """
    root = Path(root)
    if not isinstance(changed_files, list):
        # Defensive: the CLI guarantees a list, but an in-process caller
        # might hand us something else. Fail open.
        return []
    changed_set = {f for f in changed_files if isinstance(f, str) and f}
    if not changed_set:
        return []

    registry = _load_registry(root)
    if registry is None:
        return []
    entries = registry.get("findings")
    if not isinstance(entries, list):
        return []

    current_count = _current_retrospective_count(root)

    expired: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_files = entry.get("files")
        if not isinstance(entry_files, list):
            continue
        # Intersection: any exact-string match between entry.files and
        # changed_files. We do NOT normalize paths here — callers pass
        # project-relative paths on both sides.
        if not any(
            isinstance(f, str) and f in changed_set for f in entry_files
        ):
            continue
        first_seen = entry.get("first_seen_at_task_count")
        ttl = entry.get("acknowledged_until_task_count")
        if not isinstance(first_seen, int) or not isinstance(ttl, int):
            # Malformed registry entry — skip rather than fail the gate.
            continue
        elapsed = current_count - first_seen
        if elapsed >= ttl:
            expired.append({**entry, "elapsed": elapsed})

    return expired


def _format_expired_line(entry: dict) -> str:
    """Format one expired-entry line for the CLI. Keys are sorted in a
    stable, human-readable order; ``files`` is rendered as a JSON array
    so the output is unambiguous for downstream parsers."""
    return (
        f"DEFERRED FINDING EXPIRED: "
        f"id={entry.get('id', '')} "
        f"category={entry.get('category', '')} "
        f"task_id={entry.get('task_id', '')} "
        f"files={json.dumps(entry.get('files', []))}"
    )


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_deferred_findings",
        description=(
            "Check the project deferred-findings registry against a list "
            "of changed files. Exits 1 if any TTL-expired finding intersects."
        ),
    )
    parser.add_argument(
        "--root",
        default=".",
        help="Project root (default: current directory).",
    )
    parser.add_argument(
        "--changed-files",
        nargs="*",
        default=[],
        help="One or more file paths, project-relative, to check.",
    )
    args = parser.parse_args(argv)

    root = Path(args.root)
    changed_files = list(args.changed_files or [])

    expired = check_deferred_findings(root, changed_files)

    for entry in expired:
        print(_format_expired_line(entry))

    return 1 if expired else 0


if __name__ == "__main__":
    sys.exit(_main())
