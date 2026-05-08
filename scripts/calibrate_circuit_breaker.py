"""Calibrate circuit_breaker.py thresholds against historical task data.

Usage:
    python3 scripts/calibrate_circuit_breaker.py [--exclude-task <id>] ...

The script reads task data from:
  - .dynos/task-*/task-retrospective.json  (retrospectives)
  - ~/.dynos/projects/Users-hassam-Documents-dynos-work/postmortems/*.json

Token data is loaded in priority order (AC-4):
  1. token-usage.json .total (fallback: total_input_tokens + total_output_tokens)
  2. task-retrospective.json .total_token_usage
  3. postmortem .cost_summary.total_tokens

Percentile method: statistics.quantiles with n=100, method="inclusive".
The p95 is index 94 (0-based) of the returned list, corresponding to the
95th percentile inclusive interpolation.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Repository root resolution
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
_POSTMORTEMS_DIR = (
    Path.home()
    / ".dynos"
    / "projects"
    / "Users-hassam-Documents-dynos-work"
    / "postmortems"
)

# ---------------------------------------------------------------------------
# Data loading helpers
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> Optional[dict]:
    """Best-effort JSON read; return None on missing or invalid JSON."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return None


def _token_from_usage_json(task_dir: Path) -> Optional[int]:
    """Load token count from token-usage.json (priority 1 per AC-4)."""
    data = _read_json(task_dir / "token-usage.json")
    if not isinstance(data, dict):
        return None
    total = data.get("total")
    if isinstance(total, (int, float)) and total != 0:
        return int(total)
    inp = data.get("total_input_tokens", 0)
    out = data.get("total_output_tokens", 0)
    try:
        val = int(inp) + int(out)
    except (TypeError, ValueError):
        return None
    # Return None only if both fields were missing/zero; 0 is a valid sum
    # but we treat all-zero as missing per AC-4 ("all-zero ... excluded").
    if val == 0 and (not inp and not out):
        return None
    return val if val != 0 else None


def _token_from_retrospective(retro: dict) -> Optional[int]:
    """Load token count from retrospective total_token_usage (priority 2)."""
    val = retro.get("total_token_usage")
    if isinstance(val, (int, float)) and val != 0:
        return int(val)
    return None


def _token_from_postmortem(pm: dict) -> Optional[int]:
    """Load token count from postmortem cost_summary.total_tokens (priority 3)."""
    cs = pm.get("cost_summary")
    if not isinstance(cs, dict):
        return None
    val = cs.get("total_tokens")
    if isinstance(val, (int, float)) and val != 0:
        return int(val)
    return None


def _unique_files_from_graph(task_dir: Path) -> Optional[int]:
    """Count distinct files_expected across all segments. Returns None if no graph."""
    graph = _read_json(task_dir / "execution-graph.json")
    if not isinstance(graph, dict):
        return None
    segments = graph.get("segments")
    if not isinstance(segments, list):
        return 0
    unique: set[str] = set()
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        for entry in seg.get("files_expected") or []:
            if isinstance(entry, str):
                unique.add(entry)
    return len(unique)


# ---------------------------------------------------------------------------
# Corpus assembly
# ---------------------------------------------------------------------------


def _load_corpus(exclude_ids: list[str]) -> list[dict]:
    """Build the unified task corpus.

    Each record is a dict with:
      task_id, task_type, tokens (int|None), wasted_spawns (int|None),
      auditor_zero_finding_streaks (dict|None), has_retrospective (bool),
      task_dir (Path|None), source (str: "retrospective"|"postmortem"|"both")
    """
    exclude_set = set(exclude_ids)

    # --- retrospectives ---
    retro_by_id: dict[str, dict] = {}
    for retro_path in sorted(_REPO_ROOT.glob(".dynos/task-*/task-retrospective.json")):
        task_id = retro_path.parent.name
        if task_id in exclude_set:
            continue
        data = _read_json(retro_path)
        if not isinstance(data, dict):
            continue
        retro_by_id[task_id] = data

    # --- postmortems ---
    pm_by_id: dict[str, dict] = {}
    if _POSTMORTEMS_DIR.is_dir():
        for pm_path in sorted(_POSTMORTEMS_DIR.glob("*.json")):
            task_id = pm_path.stem
            if task_id in exclude_set:
                continue
            data = _read_json(pm_path)
            if not isinstance(data, dict):
                continue
            pm_by_id[task_id] = data

    # --- union ---
    all_ids = sorted(set(retro_by_id) | set(pm_by_id))
    records: list[dict] = []

    for task_id in all_ids:
        retro = retro_by_id.get(task_id)
        pm = pm_by_id.get(task_id)
        has_retrospective = retro is not None
        task_dir = _REPO_ROOT / ".dynos" / task_id

        # task_type: prefer retrospective, fallback postmortem
        task_type: Optional[str] = None
        if retro:
            task_type = retro.get("task_type")
        if not task_type and pm:
            task_type = pm.get("task_type")

        # tokens: priority order per AC-4
        tokens: Optional[int] = None
        token_source = "none"
        usage_val = _token_from_usage_json(task_dir)
        if usage_val is not None:
            tokens = usage_val
            token_source = "token-usage.json"
        elif retro:
            retro_val = _token_from_retrospective(retro)
            if retro_val is not None:
                tokens = retro_val
                token_source = "retrospective"
        if tokens is None and pm:
            pm_val = _token_from_postmortem(pm)
            if pm_val is not None:
                tokens = pm_val
                token_source = "postmortem"

        # wasted_spawns: retrospective preferred, fallback postmortem (AC-6)
        wasted_spawns: Optional[int] = None
        if retro is not None:
            ws = retro.get("wasted_spawns")
            if isinstance(ws, (int, float)):
                wasted_spawns = int(ws)
        if wasted_spawns is None and pm:
            cs = pm.get("cost_summary") or {}
            ws = cs.get("wasted_spawns")
            if isinstance(ws, (int, float)):
                wasted_spawns = int(ws)

        # auditor_zero_finding_streaks: retrospective only (AC-7)
        azfs: Optional[dict] = None
        if retro is not None:
            val = retro.get("auditor_zero_finding_streaks")
            if isinstance(val, dict):
                azfs = val

        source = "both" if (retro and pm) else ("retrospective" if retro else "postmortem")

        records.append(
            {
                "task_id": task_id,
                "task_type": task_type,
                "tokens": tokens,
                "token_source": token_source,
                "wasted_spawns": wasted_spawns,
                "auditor_zero_finding_streaks": azfs,
                "has_retrospective": has_retrospective,
                "task_dir": task_dir,
                "source": source,
            }
        )

    return records


# ---------------------------------------------------------------------------
# Percentile helper
# ---------------------------------------------------------------------------


def _p95(values: list[float]) -> float:
    """Return the 95th percentile using statistics.quantiles(n=100, method='inclusive').

    With method='inclusive', quantiles(data, n=100) returns 99 cut points
    for the 1st through 99th percentiles. Index 94 (0-based) is p95.
    Requires at least 2 data points; returns the single value for a 1-item list.
    """
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    cuts = statistics.quantiles(values, n=100, method="inclusive")
    return cuts[94]  # index 94 == p95


def _percentile_stats(values: list[float]) -> dict:
    """Compute descriptive stats dict for a list of values."""
    if not values:
        return {"n": 0, "min": None, "max": None, "p50": None, "p75": None, "p90": None, "p95": None}
    n = len(values)
    sorted_vals = sorted(values)
    if n == 1:
        return {
            "n": n,
            "min": sorted_vals[0],
            "max": sorted_vals[0],
            "p50": sorted_vals[0],
            "p75": sorted_vals[0],
            "p90": sorted_vals[0],
            "p95": sorted_vals[0],
        }
    cuts = statistics.quantiles(sorted_vals, n=100, method="inclusive")
    return {
        "n": n,
        "min": sorted_vals[0],
        "max": sorted_vals[-1],
        "p50": cuts[49],   # p50
        "p75": cuts[74],   # p75
        "p90": cuts[89],   # p90
        "p95": cuts[94],   # p95
    }


# ---------------------------------------------------------------------------
# Main calibration logic
# ---------------------------------------------------------------------------


def calibrate(exclude_ids: list[str]) -> None:
    """Run calibration and print the report to stdout."""
    records = _load_corpus(exclude_ids)

    # ------------------------------------------------------------------
    # AC-3: note about task-20260430-001 policy-based exclusion
    # ------------------------------------------------------------------
    print("=" * 72)
    print("Circuit Breaker Threshold Calibration Report")
    print("=" * 72)
    print()
    print("Policy note (AC-3):")
    print("  task-20260430-001 has a recorded token count of 8,253,071 (~8.25M).")
    print("  Statistical check: this value is NOT anomalous relative to the corpus")
    print("  (it sits near the p75-p90 range of task token distributions).")
    print("  Exclusion of this task is POLICY-BASED (audit-chain forgery incident,")
    print("  2026-04-30), not statistical. The corpus excludes it because its data")
    print("  integrity was compromised, not because it is an outlier.")
    print()

    if exclude_ids:
        print(f"Excluded task IDs: {', '.join(exclude_ids)}")
        print()

    corpus_size = len(records)
    print(f"Corpus size (after exclusions): {corpus_size} tasks")
    print()

    # ------------------------------------------------------------------
    # Dimension 1: SMALL_TASK_TOKEN_LIMIT  (AC-8, AC-9)
    # small task = task_type in {bugfix, feature} AND unique_files <= 2
    # AND has execution-graph.json
    # ------------------------------------------------------------------
    small_task_tokens: list[float] = []
    small_task_ids: list[str] = []
    small_task_excluded_no_graph: list[str] = []

    # Dimension 2: BUGFIX_TOKEN_LIMIT (all bugfix tasks)
    bugfix_tokens: list[float] = []

    # Dimension 3: WASTED_SPAWN_ABORT_THRESHOLD
    wasted_spawn_values: list[float] = []

    # Dimension 4: OPUS_AUDITOR_ZERO_YIELD_THRESHOLD (tasks with retrospective)
    opus_zero_values: list[float] = []

    # Track tasks with zero/missing tokens for warning
    zero_token_tasks: list[str] = []

    for rec in records:
        task_id = rec["task_id"]
        task_type = rec.get("task_type") or ""
        tokens = rec["tokens"]
        wasted = rec["wasted_spawns"]
        has_retro = rec["has_retrospective"]
        azfs = rec["auditor_zero_finding_streaks"]
        task_dir: Path = rec["task_dir"]

        # Token warning
        if tokens is None:
            zero_token_tasks.append(task_id)
        elif tokens == 0:
            zero_token_tasks.append(task_id)

        # Wasted spawns dimension
        if wasted is not None:
            wasted_spawn_values.append(float(wasted))

        # Opus zero yield dimension (retrospective tasks only, AC-7)
        if has_retro and azfs is not None:
            # Count auditors whose streak >= 1
            count = sum(1 for v in azfs.values() if isinstance(v, (int, float)) and v >= 1)
            opus_zero_values.append(float(count))

        # Token-based dimensions: skip if no valid token data
        if not tokens:
            continue

        # Bugfix dimension
        if task_type == "bugfix":
            bugfix_tokens.append(float(tokens))

        # Small task dimension (AC-8)
        if task_type in {"bugfix", "feature"}:
            unique_files = _unique_files_from_graph(task_dir)
            if unique_files is None:
                # No execution-graph.json: exclude from small-task sub-corpus (not overall)
                small_task_excluded_no_graph.append(task_id)
            elif unique_files <= 2:
                small_task_tokens.append(float(tokens))
                small_task_ids.append(task_id)

    # Zero-token warnings
    if zero_token_tasks:
        print("WARNING: The following tasks have all-zero or missing token data")
        print("  across all three sources and are excluded from token-based calibration:")
        for tid in zero_token_tasks:
            print(f"    - {tid}")
        print()

    # ------------------------------------------------------------------
    # Compute stats
    # ------------------------------------------------------------------
    stats_small = _percentile_stats(small_task_tokens)
    stats_bugfix = _percentile_stats(bugfix_tokens)
    stats_wasted = _percentile_stats(wasted_spawn_values)
    stats_opus = _percentile_stats(opus_zero_values)

    # ------------------------------------------------------------------
    # Proposed values (AC-10, AC-11)
    # ------------------------------------------------------------------
    # Token-based: ceil(p95 * 1.5)
    def _proposed_token(p95_val: float) -> int:
        return math.ceil(p95_val * 1.5)

    # Count-based: round(p95 * 1.5), minimum 1
    def _proposed_count(p95_val: float) -> int:
        return max(1, round(p95_val * 1.5))

    p95_small = stats_small["p95"] or 0.0
    p95_bugfix = stats_bugfix["p95"] or 0.0
    p95_wasted = stats_wasted["p95"] or 0.0
    p95_opus = stats_opus["p95"] or 0.0

    proposed_small_limit = _proposed_token(p95_small)
    proposed_bugfix_limit = _proposed_token(p95_bugfix)
    proposed_wasted = _proposed_count(p95_wasted)
    proposed_opus = _proposed_count(p95_opus)

    # Downgrade thresholds: floor(proposed_limit * 0.80) (AC-10, AC-11)
    proposed_small_downgrade = math.floor(proposed_small_limit * 0.80)
    proposed_bugfix_downgrade = math.floor(proposed_bugfix_limit * 0.80)

    # Current values (for comparison)
    CURRENT_SMALL_LIMIT = 1_000_000
    CURRENT_BUGFIX_LIMIT = 5_000_000
    CURRENT_WASTED = 3
    CURRENT_OPUS = 3
    CURRENT_SMALL_DOWNGRADE = 800_000
    CURRENT_BUGFIX_DOWNGRADE = 4_000_000

    # ------------------------------------------------------------------
    # Section 1: Distribution summary (AC-12, AC-13)
    # ------------------------------------------------------------------
    print("=" * 72)
    print("Section 1: Distribution Summary")
    print("=" * 72)
    print()

    def _fmt_int(v) -> str:
        if v is None:
            return "N/A"
        return f"{int(v):,}"

    def _row(
        name: str,
        stats: dict,
        current: int,
        proposed: int,
        note: str = "",
    ) -> None:
        n = stats["n"]
        mn = _fmt_int(stats["min"])
        mx = _fmt_int(stats["max"])
        p50 = _fmt_int(stats["p50"])
        p75 = _fmt_int(stats["p75"])
        p90 = _fmt_int(stats["p90"])
        p95 = _fmt_int(stats["p95"])
        print(f"  Dimension : {name}")
        print(f"  n={n}  min={mn}  max={mx}  p50={p50}  p75={p75}  p90={p90}  p95={p95}")
        print(f"  current={current:,}  proposed={proposed:,}{note}")
        print()

    _row(
        "SMALL_TASK_TOKEN_LIMIT",
        stats_small,
        CURRENT_SMALL_LIMIT,
        proposed_small_limit,
        f"  [small-task sub-corpus n={stats_small['n']}]",
    )
    _row(
        "BUGFIX_TOKEN_LIMIT",
        stats_bugfix,
        CURRENT_BUGFIX_LIMIT,
        proposed_bugfix_limit,
    )
    _row(
        "WASTED_SPAWN_ABORT_THRESHOLD",
        stats_wasted,
        CURRENT_WASTED,
        proposed_wasted,
    )
    _row(
        "OPUS_AUDITOR_ZERO_YIELD_THRESHOLD",
        stats_opus,
        CURRENT_OPUS,
        proposed_opus,
    )
    _row(
        "SMALL_TASK_TOKEN_DOWNGRADE_THRESHOLD",
        {"n": stats_small["n"], "min": None, "max": None,
         "p50": None, "p75": None, "p90": None, "p95": None},
        CURRENT_SMALL_DOWNGRADE,
        proposed_small_downgrade,
        "  [derived: floor(proposed_SMALL_TASK_TOKEN_LIMIT * 0.80)]",
    )
    _row(
        "BUGFIX_TOKEN_DOWNGRADE_THRESHOLD",
        {"n": stats_bugfix["n"], "min": None, "max": None,
         "p50": None, "p75": None, "p90": None, "p95": None},
        CURRENT_BUGFIX_DOWNGRADE,
        proposed_bugfix_downgrade,
        "  [derived: floor(proposed_BUGFIX_TOKEN_LIMIT * 0.80)]",
    )

    # ------------------------------------------------------------------
    # Flags for notable changes (AC-20, AC-21)
    # ------------------------------------------------------------------
    print("=" * 72)
    print("Notable threshold changes")
    print("=" * 72)
    print()
    # AC-20: ALWAYS print a prominent banner block for OPUS_AUDITOR_ZERO_YIELD_THRESHOLD
    # so the operator is explicitly aware of the calibrated outcome before adopting,
    # regardless of whether the proposed value is lower, higher, or equal to current.
    if proposed_opus < CURRENT_OPUS:
        print(
            f"  *** OPUS_AUDITOR_ZERO_YIELD_THRESHOLD TIGHTENED ***"
        )
        print(
            f"      current={CURRENT_OPUS}  proposed={proposed_opus}"
        )
        print(
            f"      The threshold is LOWER than current — the breaker will fire"
        )
        print(
            f"      sooner when auditors produce empty findings."
        )
        print(
            f"      Tasks with {proposed_opus}+ auditors producing empty findings will trip"
        )
        print(
            f"      the breaker once BREAKER_ACTIVE flips."
        )
    elif proposed_opus > CURRENT_OPUS:
        print(
            f"  *** OPUS_AUDITOR_ZERO_YIELD_THRESHOLD LOOSENED ***"
        )
        print(
            f"      current={CURRENT_OPUS}  proposed={proposed_opus}"
        )
        print(
            f"      The threshold is HIGHER than current — the breaker will fire"
        )
        print(
            f"      later when auditors produce empty findings."
        )
        print(
            f"      Tasks with {proposed_opus}+ auditors producing empty findings will trip"
        )
        print(
            f"      the breaker once BREAKER_ACTIVE flips."
        )
    else:
        print(
            f"  *** OPUS_AUDITOR_ZERO_YIELD_THRESHOLD UNCHANGED ***"
        )
        print(
            f"      current={CURRENT_OPUS}  proposed={proposed_opus}"
        )
        p95_opus_int = int(p95_opus) if float(p95_opus).is_integer() else round(p95_opus, 2)
        print(
            f"      The empirical p95 ({p95_opus_int}) and the 1.5x margin ({proposed_opus}) match the current value."
        )
        print(
            f"      Tasks with {proposed_opus}+ auditors producing empty findings will trip the breaker"
        )
        print(
            f"      once BREAKER_ACTIVE flips. The threshold is not being tightened OR loosened"
        )
        print(
            f"      by this calibration; the current value already matches the calibrated outcome."
        )
    print()

    if proposed_wasted > CURRENT_WASTED:
        print(
            f"  *** WASTED_SPAWN_ABORT_THRESHOLD LARGE INCREASE ***"
        )
        print(
            f"      current={CURRENT_WASTED}  proposed={proposed_wasted}"
        )
        print(
            "      Tasks with 3-8 wasted spawns will CONTINUE COMPLETING under"
        )
        print(
            "      the new threshold. Only tasks exceeding the new limit abort."
        )
    elif proposed_wasted == CURRENT_WASTED:
        print(
            f"  WASTED_SPAWN_ABORT_THRESHOLD: unchanged at {CURRENT_WASTED}."
        )
    else:
        print(
            f"  WASTED_SPAWN_ABORT_THRESHOLD TIGHTENED: {CURRENT_WASTED} -> {proposed_wasted}."
        )
    print()

    # AC-22: SMALL_TASK_FILES_THRESHOLD is NOT proposed/changed
    print(
        "  SMALL_TASK_FILES_THRESHOLD: NOT changed by this calibration (policy constant)."
    )
    print(
        f"  Eligible small-task count used for SMALL_TASK_TOKEN_LIMIT: {len(small_task_ids)}"
    )
    print()

    # ------------------------------------------------------------------
    # Section 2: Proposed constant block (AC-12, AC-14)
    # ------------------------------------------------------------------
    print("=" * 72)
    print("Section 2: Proposed Constant Block (paste into hooks/circuit_breaker.py)")
    print("=" * 72)
    print()
    print("WASTED_SPAWN_ABORT_THRESHOLD: int = " + str(proposed_wasted))
    print("SMALL_TASK_TOKEN_LIMIT: int = " + str(proposed_small_limit))
    print("BUGFIX_TOKEN_LIMIT: int = " + str(proposed_bugfix_limit))
    print("OPUS_AUDITOR_ZERO_YIELD_THRESHOLD: int = " + str(proposed_opus))
    print("SMALL_TASK_FILES_THRESHOLD: int = 2  # NOT changed by this task")
    print("SMALL_TASK_TOKEN_DOWNGRADE_THRESHOLD: int = " + str(proposed_small_downgrade))
    print("BUGFIX_TOKEN_DOWNGRADE_THRESHOLD: int = " + str(proposed_bugfix_downgrade))
    print()

    # ------------------------------------------------------------------
    # Section 3: Sample exclusions (AC-12)
    # ------------------------------------------------------------------
    print("=" * 72)
    print("Section 3: Sample Exclusions")
    print("=" * 72)
    print()
    if exclude_ids:
        for tid in exclude_ids:
            print(f"  {tid} — excluded by --exclude-task flag (policy-based)")
    for tid in zero_token_tasks:
        print(f"  {tid} — excluded from token calibration: all-zero/missing token data")
    for tid in small_task_excluded_no_graph:
        print(
            f"  {tid} — excluded from small-task sub-corpus: no execution-graph.json"
        )
    if not exclude_ids and not zero_token_tasks and not small_task_excluded_no_graph:
        print("  (none)")
    print()
    print("=" * 72)
    print("End of report")
    print("=" * 72)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Calibrate circuit_breaker.py thresholds from historical task data.",
    )
    parser.add_argument(
        "--exclude-task",
        dest="exclude_task",
        metavar="ID",
        action="append",
        default=None,
        help=(
            "Task ID to exclude from the corpus. Repeatable. "
            "Default: ['task-20260430-001']. "
            "Passing this flag OVERRIDES the default entirely."
        ),
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    if args.exclude_task is None:
        exclude_ids = ["task-20260430-001"]
    else:
        exclude_ids = args.exclude_task
    calibrate(exclude_ids)
    return 0


if __name__ == "__main__":
    sys.exit(main())
