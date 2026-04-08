#!/usr/bin/env python3
"""Core utilities and path helpers for the autofix package.

Subset of dynoslib_core — only functions needed by the autofix scanner.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def now_iso() -> str:
    """Return current UTC time as ISO-8601 string with Z suffix."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> dict:
    """Read and parse a JSON file."""
    return json.loads(path.read_text())


def write_json(path: Path, data: Any) -> None:
    """Atomic JSON write: write to temp file then rename to avoid partial writes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(json.dumps(data, indent=2) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Persistent project directory
# ---------------------------------------------------------------------------

def _persistent_project_dir(root: Path) -> Path:
    """Returns ~/.dynos/projects/{slug}/ for persistent project state.

    Pure path resolution — NO side effects. Does NOT create directories.
    """
    resolved = str(root.resolve())
    if os.environ.get("DYNOS_AUTOFIX_WORKTREE") == "1" and resolved.startswith("/tmp/"):
        return root.resolve() / ".dynos" / "ephemeral-project"

    dynos_home = Path(os.environ.get("DYNOS_HOME", str(Path.home() / ".dynos")))
    slug = resolved.strip("/").replace("/", "-")
    return dynos_home / "projects" / slug


# ---------------------------------------------------------------------------
# Global home (inlined from dynoglobal)
# ---------------------------------------------------------------------------

def global_home() -> Path:
    """Returns DYNOS_HOME env var or ~/.dynos/."""
    env = os.environ.get("DYNOS_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / ".dynos"


def project_slug(root: Path) -> str:
    """Convert a project root path to a safe directory name."""
    return str(root.resolve()).strip("/").replace("/", "-")


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

def project_policy(root: Path) -> dict:
    """Read project policy from persistent dir."""
    path = _persistent_project_dir(root) / "policy.json"
    default: dict[str, Any] = {
        "freshness_task_window": 5,
        "active_rebenchmark_task_window": 3,
        "shadow_rebenchmark_task_window": 2,
        "token_budget_multiplier": 1.0,
        "fast_track_skip_plan_audit": False,
    }
    if not path.exists() or not path.read_text().strip():
        write_json(path, default)
        return default
    try:
        data = load_json(path)
    except (json.JSONDecodeError, FileNotFoundError, OSError):
        write_json(path, default)
        return default
    merged = {**default, **data}
    return merged


# ---------------------------------------------------------------------------
# Retrospective helpers
# ---------------------------------------------------------------------------

def collect_retrospectives(root: Path) -> list[dict]:
    """Collect all task retrospective JSON files from .dynos/."""
    retrospectives: list[dict] = []
    for path in sorted((root / ".dynos").glob("task-*/task-retrospective.json")):
        try:
            data = load_json(path)
        except (json.JSONDecodeError, FileNotFoundError, OSError):
            continue
        data["_path"] = str(path)
        retrospectives.append(data)
    return retrospectives


# ---------------------------------------------------------------------------
# Patterns path (inlined from dynopatterns)
# ---------------------------------------------------------------------------

def local_patterns_path(root: Path) -> Path:
    """Return the path to dynos_patterns.md for the given project."""
    return _persistent_project_dir(root) / "dynos_patterns.md"
