"""Per-cycle repair-log history.

``repair-log.json`` holds only the current repair cycle: every
``run-repair-log-build`` / ``write-repair-log`` overwrites it. The postmortem
needs every cycle (a finding repaired in cycle 1 that came back and was
repaired again in cycle 2 is the signal worth learning from), so each write
also records the cycle in ``repair-log-history.json``.

``repair-log-history.json`` is ctl-owned: ``write_policy`` lists it in
``_WRAPPER_REQUIRED``, so only the ctl role (these writers) may write it.

Shape::

    {"task_id": "...", "cycles": [
        {"repair_cycle": 1, "recorded_at": "...", "repair_log": {...}}, ...]}

One entry per cycle, sorted by cycle; rebuilding a cycle replaces its entry.
"""
from __future__ import annotations

import json
from pathlib import Path

from lib_core import load_json, now_iso, write_ctl_json

HISTORY_FILENAME = "repair-log-history.json"


def _cycle_of(repair_log: object) -> int | None:
    if not isinstance(repair_log, dict):
        return None
    try:
        cycle = int(repair_log.get("repair_cycle") or 0)
    except (TypeError, ValueError):
        return None
    return cycle if cycle > 0 else None


def _load_history(task_dir: Path) -> list[dict]:
    try:
        data = load_json(task_dir / HISTORY_FILENAME)
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    cycles = data.get("cycles") if isinstance(data, dict) else None
    if not isinstance(cycles, list):
        return []
    return [c for c in cycles if isinstance(c, dict) and _cycle_of(c.get("repair_log")) is not None]


def record_repair_log(task_dir: Path, repair_log: dict, *, previous: object = None) -> Path | None:
    """Record ``repair_log`` (and the log it replaces) in the history.

    ``previous`` is the repair-log.json content being overwritten. It is
    archived too, so a task whose earlier cycles predate the history file
    still keeps the cycle that was on disk. Returns the history path, or
    None when neither log carries a cycle number.
    """
    task_dir = Path(task_dir)
    entries = {(_cycle_of(e["repair_log"])): e for e in _load_history(task_dir)}
    changed = False
    for log in (previous, repair_log):
        cycle = _cycle_of(log)
        if cycle is None:
            continue
        if log is previous and cycle in entries:
            # The history already holds this cycle; the incoming log (same or
            # later cycle) is recorded below.
            continue
        entries[cycle] = {"repair_cycle": cycle, "recorded_at": now_iso(), "repair_log": log}
        changed = True
    if not changed:
        return None
    path = task_dir / HISTORY_FILENAME
    write_ctl_json(task_dir, path, {
        "task_id": task_dir.name,
        "cycles": [entries[c] for c in sorted(entries)],
    })
    return path


def load_repair_cycles(task_dir: Path) -> list[dict]:
    """Every repair cycle of the task, oldest first.

    Each entry: ``{"repair_cycle": int, "source": str, "batches": list}``.
    Sources, in precedence order for a cycle:

    - ``repair-log``: the current ``repair-log.json``;
    - ``history``: ``repair-log-history.json``;
    - ``repair-cycle-plan``: a cycle older than the archive (task predates
      it) that ``repair-cycle-plan.json`` says ran. Its batches are unknown,
      so the entry carries ``reconstructed: True`` and the ``auditors`` whose
      findings drove it.
    """
    task_dir = Path(task_dir)
    cycles: dict[int, dict] = {}
    for entry in _load_history(task_dir):
        log = entry["repair_log"]
        cycles[_cycle_of(log)] = {
            "repair_cycle": _cycle_of(log), "source": "history",
            "batches": [b for b in log.get("batches") or [] if isinstance(b, dict)],
        }
    try:
        current = load_json(task_dir / "repair-log.json")
    except (OSError, ValueError, json.JSONDecodeError):
        current = None
    current_cycle = _cycle_of(current)
    if current_cycle is not None:
        cycles[current_cycle] = {
            "repair_cycle": current_cycle, "source": "repair-log",
            "batches": [b for b in current.get("batches") or [] if isinstance(b, dict)],
        }

    try:
        plan = load_json(task_dir / "repair-cycle-plan.json")
    except (OSError, ValueError, json.JSONDecodeError):
        plan = None
    raw = plan.get("auditor_repair_cycles") if isinstance(plan, dict) else None
    if isinstance(raw, dict):
        by_cycle: dict[int, list[str]] = {}
        for auditor, nums in raw.items():
            if not isinstance(nums, list):
                continue
            for n in nums:
                if isinstance(n, int) and not isinstance(n, bool) and n > 0:
                    by_cycle.setdefault(n, []).append(str(auditor))
        for n, auditors in by_cycle.items():
            if n not in cycles:
                cycles[n] = {
                    "repair_cycle": n, "source": "repair-cycle-plan", "reconstructed": True,
                    "batches": [], "auditors": sorted(auditors),
                }
    return [cycles[n] for n in sorted(cycles)]
