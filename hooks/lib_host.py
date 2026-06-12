"""lib_host — leaf module: host detection and persistence helpers.

Zero imports from any hooks/ or memory/ module.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Host detection
# ---------------------------------------------------------------------------


def detect_host() -> str:
    """Detect the current AI host from environment variables.

    Priority:
      1. CODEX_PLUGIN_ROOT non-empty  → "codex"
      2. CLAUDE_PLUGIN_ROOT non-empty → "claude"
      3. fallback                     → "claude"
    """
    if os.environ.get("CODEX_PLUGIN_ROOT", ""):
        return "codex"
    # CLAUDE_PLUGIN_ROOT or absent — safe default is "claude"
    return "claude"


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def persist_host(path: Path, host: str) -> None:
    """Write *host* into *path* (control-plane.json) under the key ``host``.

    Reads existing JSON if present and merges, so other keys are preserved.
    Always overwrites the ``host`` field.
    """
    data: dict = {}
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    data["host"] = host
    path.write_text(json.dumps(data), encoding="utf-8")


def get_persisted_host(path: Path) -> Optional[str]:
    """Return the ``host`` field from *path* (control-plane.json), or None.

    Returns None if the file is absent, unreadable, not valid JSON, or
    lacks the ``host`` key.
    """
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        return data.get("host")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
