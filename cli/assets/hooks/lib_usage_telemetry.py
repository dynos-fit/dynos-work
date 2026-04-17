#!/usr/bin/env python3
"""Lightweight usage telemetry for dormancy detection.

Records a timestamp + caller whenever a monitored module is invoked.
Data written to ~/.dynos/usage-telemetry.jsonl (append-only, one JSON
object per line). After the monitoring period, modules with zero entries
are safe to remove.

Usage (in monitored module, at module load time):
    from lib_usage_telemetry import record_usage
    record_usage("dream")

Or for function-level tracking:
    record_usage("dream", function="simulate_option")
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path


def _telemetry_path() -> Path:
    dynos_home = Path(os.environ.get("DYNOS_HOME", str(Path.home() / ".dynos")))
    return dynos_home / "usage-telemetry.jsonl"


def record_usage(module: str, function: str | None = None) -> None:
    """Append a usage record. Never raises — telemetry must not break callers."""
    try:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "module": module,
            "pid": os.getpid(),
        }
        if function:
            entry["function"] = function
        # Capture caller info
        stack = traceback.extract_stack(limit=3)
        if len(stack) >= 2:
            caller = stack[-2]
            entry["caller_file"] = caller.filename
            entry["caller_line"] = caller.lineno

        path = _telemetry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # Never fail


def read_telemetry() -> list[dict]:
    """Read all telemetry entries. Returns empty list if no data."""
    path = _telemetry_path()
    if not path.exists():
        return []
    entries = []
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return entries


def summarize_telemetry() -> dict[str, int]:
    """Return invocation counts per module."""
    entries = read_telemetry()
    counts: dict[str, int] = {}
    for e in entries:
        mod = e.get("module", "unknown")
        counts[mod] = counts.get(mod, 0) + 1
    return counts
